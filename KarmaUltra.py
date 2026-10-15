"""
⚡ Karma Ultra Bot — VETRO UI EDITION
All v7.1 Logic + VETRO-Style GUI (Dark, Sidebar, Task Table)
"""

import asyncio
import concurrent.futures
import imaplib
import json
import random
import re
import string
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, asdict, field
from email import message_from_bytes
from email.utils import getaddresses
from pathlib import Path

import httpx
import requests

# ═════════════════════════════════════════════
#  PATHS / CONFIG
# ═════════════════════════════════════════════

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
SESSIONS_DIR = DATA_DIR / "sessions"; SESSIONS_DIR.mkdir(exist_ok=True)
SHOTS_DIR = DATA_DIR / "screenshots"; SHOTS_DIR.mkdir(exist_ok=True)

CONFIG_FILE = DATA_DIR / "config.json"
ACCOUNTS_FILE = DATA_DIR / "accounts.json"
GROUPS_FILE = DATA_DIR / "task_groups.json"
PROXIES_FILE = DATA_DIR / "proxies.txt"
PROXY_GROUPS_FILE = DATA_DIR / "proxy_groups.json"
LEARNED_FILE = DATA_DIR / "learned_popups.json"

DEFAULT_CONFIG = {
    "discord": {
        "webhook_url": "https://discord.com/api/webhooks/YOUR_WEBHOOK_HERE",
        "mention_user_id": "",
        "include_cookies": True, "include_screenshot": True,
        "include_cart_link": True,
    },
    "captcha": {"provider": "capsolver", "api_key": "YOUR_CAPSOLVER_KEY"},
    "general": {"headless": True, "hybrid_mode": True, "keep_browser_open_min": 15},
    "restock": {
        "events": [],
        "poll_interval_sec": 180, "jitter_sec": 60,
        "auto_cart": True, "default_qty": 2,
        "min_price": 0.0, "max_price": 250.0,
        "default_sections": [], "default_contiguous": True,
        "default_presale_codes": [], "min_qty_available": 1,
    },
    "ai": {"provider": "openai", "api_key": "", "model": "gpt-4o-mini",
           "temperature": 0.2},
    "imap": {"email": "", "password": "", "host": "", "port": 993, "ssl": True},
}


@dataclass
class TaskGroup:
    id: str
    name: str
    event_url: str
    quantity: int = 2
    min_price: float = 0.0
    max_price: float = 250.0
    contiguous: bool = True
    section_mode: str = "include"
    sections: list = field(default_factory=list)
    account_group: str = "default"
    proxy_group: str = "any"
    max_tasks: int = 2
    enabled: bool = True
    presale_codes: list = field(default_factory=list)
    presale_played: list = field(default_factory=list)


def jload(path, default):
    if Path(path).exists():
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            return default
    return default


def jsave(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def merge_config(cfg, defaults):
    for k, v in defaults.items():
        if k not in cfg:
            cfg[k] = v
        elif isinstance(v, dict):
            for kk, vv in v.items():
                cfg[k].setdefault(kk, vv)
    return cfg


CONFIG = merge_config(jload(CONFIG_FILE, {}), DEFAULT_CONFIG)
jsave(CONFIG_FILE, CONFIG)


def load_groups():
    return [TaskGroup(**g) for g in jload(GROUPS_FILE, [])]


def save_groups(groups):
    jsave(GROUPS_FILE, [asdict(g) for g in groups])


def load_accounts():
    return jload(ACCOUNTS_FILE, [])


def save_accounts(a):
    jsave(ACCOUNTS_FILE, a)


# ═════════════════════════════════════════════
#  🩺 ACCOUNT HEALTH
# ═════════════════════════════════════════════

MAX_OTP_FAILS = 2


def get_account(email):
    for a in load_accounts():
        if a.get("email") == email:
            return a
    return None


def update_account(email, **kw):
    accs = load_accounts()
    for a in accs:
        if a.get("email") == email:
            a.update(kw)
            save_accounts(accs)
            return True
    return False


def record_otp_result(email, success, reason=""):
    acc = get_account(email)
    if not acc:
        return False
    if success:
        if acc.get("otp_fails", 0) > 0 or acc.get("status") == "otp_warn":
            update_account(email, otp_fails=0, status="ok")
        return True
    fails = acc.get("otp_fails", 0) + 1
    st = "dead_otp" if fails >= MAX_OTP_FAILS else "otp_warn"
    update_account(email, otp_fails=fails, status=st,
                   last_otp_fail=reason[:120],
                   last_fail_at=datetime.now().isoformat())
    print(f"🩺 {email}: OTP fail {fails}/{MAX_OTP_FAILS} → {st}")
    return st != "dead_otp"


def retire_account(email, reason):
    update_account(email, status="dead", fail_reason=reason[:120],
                   last_fail_at=datetime.now().isoformat())


def revive_account(email):
    update_account(email, status="ok", otp_fails=0, used=False,
                   fail_reason="", last_otp_fail="")


def accounts_in_group(group_name, healthy_only=True):
    good = ("ok", "otp_warn")
    return [a for a in load_accounts()
            if a.get("group", "default") == group_name
            and not a.get("used", False)
            and (not healthy_only or a.get("status", "ok") in good)]


# ═════════════════════════════════════════════
#  🧠 ERRORS + AI BRAIN
# ═════════════════════════════════════════════

ERROR_CODES = {
    "TM-QUEUE-TIMEOUT":  "Stuck in queue past the 15 min limit — task retries.",
    "TM-NO-MATCH":       "No offer passed your filters (sections/price/qty).",
    "TM-429":            "TM rate-limited us — bot backed off automatically.",
    "TM-403":            "Access denied — proxy rotates on retry.",
    "TM-BAP":            "Browser Activity Paused — .tmpt removed automatically.",
    "TM-CAPTCHA-FAIL":   "FunCaptcha unsolved — check CapSolver key/balance.",
    "TM-CART-FAIL":      "Add-to-cart didn't confirm — tickets may have vanished mid-click.",
    "TM-QTY-FORCE":      "Couldn't set exact quantity — dropdown missing/locked.",
    "TM-PRESALE-LOCKED": "All presale codes rejected — check spelling.",
    "TM-OTP":            "OTP email never arrived — check IMAP settings/catch-all.",
    "TM-NAV":            "Page failed to load — check URL and proxies.",
    "TM-SEL":            "Ticket list never appeared — layout change or redirect.",
    "TM-SESSION":        "Session rejected (OTP would be needed) — fresh login next run.",
    "TM-GEN":            "Unexpected error — see log.",
}

ENDPOINTS = {
    "openai":     "https://api.openai.com/v1/chat/completions",
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
    "groq":       "https://api.groq.com/openai/v1/chat/completions",
    "ollama":     "http://localhost:11434/v1/chat/completions",
}


def explain(exc=None, code="TM-GEN"):
    msg = str(exc) if exc else ""
    low = msg.lower()
    if exc is not None:
        if "timeout" in low and "captcha" in low:
            code = "TM-CAPTCHA-FAIL"
        elif "timeout" in low and "goto" in low:
            code = "TM-NAV"
        elif "timeout" in low:
            code = "TM-SEL"
        elif re.search(r"\b429\b", low):
            code = "TM-429"
        elif re.search(r"\b403\b", low):
            code = "TM-403"
    return code, ERROR_CODES.get(code, ERROR_CODES["TM-GEN"])


class AIBrain:
    def __init__(self):
        cfg = CONFIG.get("ai", {})
        self.key = cfg.get("api_key", "").strip()
        self.model = cfg.get("model", "gpt-4o-mini")
        self.temp = cfg.get("temperature", 0.2)
        self.endpoint = ENDPOINTS.get(cfg.get("provider", "openai"),
                                      ENDPOINTS["openai"])
        self.enabled = bool(self.key)
        self.calls = 0

    def _ask(self, system, user, max_tokens=200):
        if not self.enabled:
            return None
        try:
            r = requests.post(self.endpoint, timeout=20, json={
                "model": self.model, "temperature": self.temp,
                "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}]},
                headers={"Authorization": f"Bearer {self.key}",
                         "Content-Type": "application/json"})
            if r.status_code == 200:
                self.calls += 1
                return r.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            print(f"🧠 AI call failed: {e}")
        return None

    def diagnose(self, code, exception, context=""):
        fallback = ERROR_CODES.get(code, ERROR_CODES["TM-GEN"])
        return self._ask(
            "You are a Ticketmaster bot engineer. Given an error code, raw "
            "exception and page context, explain in max 2 short sentences: "
            "(1) what failed, (2) the fix. Plain English.",
            f"Error code: {code}\nException: {exception}\n"
            f"Context: {str(context)[:800]}", max_tokens=120) or fallback

    def section_matches(self, filter_text, section_name, mode):
        a = self._ask(
            "Does this venue section match the fan's filter intent? Reply ONLY "
            "'YES' or 'NO'. Synonyms: Floor≈GA≈Pit, Lower≈100 level, "
            "Upper≈300 level. Ranges 101-110 inclusive.",
            f"Filter ({mode}): {filter_text}\nSection: {section_name}",
            max_tokens=5)
        if a and "YES" in a.upper():
            return True
        if a and "NO" in a.upper():
            return False
        return None

    def summarize_run(self, results):
        return self._ask(
            "Summarize a ticket bot run in 2-3 punchy Discord sentences. Wins "
            "first, then the most useful fix. Emojis.",
            json.dumps(results), max_tokens=150) or \
            f"{results.get('carts', 0)} cart(s), {len(results.get('fails', []))} fail(s)."

    def describe_page(self, text_snippet):
        return self._ask(
            "Identify this Ticketmaster page text: queue page, captcha, "
            "'browser paused' page, ticket list, or other. One short answer.",
            str(text_snippet)[:1500], max_tokens=80) or "unidentified"

    def popup_button(self, caption, texts):
        a = self._ask(
            "You dismiss website popups. Given popup caption and its buttons, "
            "reply ONLY with the exact button text that dismisses/accepts it "
            "(prefer accepting consent). If none, reply NONE.",
            f"Popup caption: {caption}\nButtons: {json.dumps(texts)}",
            max_tokens=30)
        if a and a.upper().strip() != "NONE":
            return a
        return None


AI = AIBrain()


def explain_deep(exc=None, code="TM-GEN", context=""):
    code, base = explain(exc, code)
    if AI.enabled:
        return code, AI.diagnose(code, exc or Exception(code), context)
    return code, base


# ═════════════════════════════════════════════
#  💱 CURRENCY-NATIVE PRICE ENGINE
# ═════════════════════════════════════════════

CURRENCY_MARKERS = [
    "MX$", "AR$", "CL$", "COP$", "AU$", "NZ$", "CA$", "HK$", "S$", "NT$",
    "R$", "US$", "kr", "zł", "¥", "£", "€", "₩", "₹", "$",
]


class PriceEngine:
    @staticmethod
    def detect_symbol(text):
        text = str(text)
        for m in CURRENCY_MARKERS:
            if m in text:
                return m
        return "$"

    @staticmethod
    def _num(s):
        s = str(s).strip()
        try:
            if "," in s and "." in s:
                if s.rfind(",") > s.rfind("."):
                    s = s.replace(".", "").replace(",", ".")
                else:
                    s = s.replace(",", "")
            elif "," in s:
                s = s.replace(",", "")
            return float(s)
        except ValueError:
            return None

    @staticmethod
    def parse_prices(text):
        out = []
        for m in re.finditer(
                r"(MX\$|AR\$|CL\$|COP\$|AU\$|NZ\$|CA\$|HK\$|S\$|NT\$|R\$|US\$|£|€|¥|₩|₹|\$)\s?"
                r"(\d{1,6}(?:[.,]\d{3})*(?:[.,]\d{1,2})?)", text):
            v = PriceEngine._num(m.group(2))
            if v is not None:
                out.append(v)
        return out

    @staticmethod
    def within(unit_price, tg):
        if unit_price is None:
            return False
        return tg.min_price <= unit_price <= tg.max_price


# ═════════════════════════════════════════════
#  💾 SESSION VAULT
# ═════════════════════════════════════════════

class SessionVault:
    FLAG_COOKIES = ("tmpt",)

    @staticmethod
    def _path(email):
        return SESSIONS_DIR / f"{str(email).replace('@', '_at_')}.json"

    @staticmethod
    def _scrub(state):
        state["cookies"] = [c for c in state.get("cookies", [])
                           if str(c.get("name", "")).lower().strip(".")
                           not in SessionVault.FLAG_COOKIES]
        return state

    @staticmethod
    def save(context, email):
        state = SessionVault._scrub(context.storage_state())
        state["_saved_at"] = datetime.now().isoformat()
        state["_email"] = email
        jsave(SessionVault._path(email), state)

    @staticmethod
    def load(email):
        p = SessionVault._path(email)
        if p.exists():
            try:
                with open(p) as f:
                    return SessionVault._scrub(json.load(f))
            except Exception:
                return None
        return None

    @staticmethod
    def has(email):
        return SessionVault._path(str(email)).exists()

    @staticmethod
    def import_cookies(text, email=None):
        cookies = []
        for m in re.finditer(r"`([^`]+)`\s*=\s*`([^`]+)`", text):
            cookies.append({"name": m.group(1), "value": m.group(2),
                            "domain": ".ticketmaster.com", "path": "/"})
        if not cookies:
            for pair in text.split(";"):
                if "=" in pair:
                    n, _, v = pair.strip().partition("=")
                    if n and v:
                        cookies.append({"name": n.strip(), "value": v.strip(),
                                        "domain": ".ticketmaster.com", "path": "/"})
        if not cookies:
            return False
        state = {"cookies": cookies, "origins": [],
                 "_saved_at": datetime.now().isoformat(),
                 "_email": email or "imported_manual", "_imported": True}
        jsave(SessionVault._path(email or "imported_manual"),
              SessionVault._scrub(state))
        return True

    @staticmethod
    def list_sessions():
        out = []
        for p in SESSIONS_DIR.glob("*.json"):
            try:
                m = SessionVault._scrub(jload(p, {}))
                out.append({"email": m.get("_email", p.stem),
                            "saved": str(m.get("_saved_at", "?"))[:16],
                            "cookies": len(m.get("cookies", [])),
                            "imported": m.get("_imported", False)})
            except Exception:
                out.append({"email": p.stem, "saved": "?", "cookies": 0,
                            "imported": False})
        return out


# ═════════════════════════════════════════════
#  📡 STATUS BOARD
# ═════════════════════════════════════════════

class StatusBoard:
    def __init__(self):
        self.lock = threading.Lock()
        self.tasks = {}
        self._listeners = []

    def on_change(self, cb):
        self._listeners.append(cb)

    def _emit(self):
        with self.lock:
            snap = {k: dict(v) for k, v in self.tasks.items()}
        for cb in self._listeners:
            try:
                cb(snap)
            except Exception:
                pass

    def create(self, task_id, group, account):
        with self.lock:
            self.tasks[task_id] = {
                "group": group, "account": account, "state": "starting",
                "queue_pos": None, "queue_pct": None, "started": time.time(),
                "errors": [], "bap_heals": 0, "popups": 0,
                "takeover_available": False}
        self._emit()

    def update(self, task_id, **kw):
        with self.lock:
            if task_id in self.tasks:
                self.tasks[task_id].update(kw)
        self._emit()

    def error(self, task_id, code, message):
        with self.lock:
            t = self.tasks.get(task_id)
            if t:
                t["errors"].append((time.time(), code, message))
                t["errors"] = t["errors"][-10:]
        self._emit()

    def finish(self, task_id, done=True):
        self.update(task_id, state=("done" if done else "failed"),
                    takeover_available=False)

    def clear(self):
        with self.lock:
            self.tasks = {}
        self._emit()


STATUS = StatusBoard()


# ═════════════════════════════════════════════
#  🛡️ BAP GUARD
# ═════════════════════════════════════════════

class BaPGuard:
    BAP_MARKERS = [
        "browser activity has been paused", "browser activity is paused",
        "we've detected unusual activity", "verify you are a human",
        "unusual browsing activity",
    ]

    def __init__(self):
        self.hits, self.heals = 0, 0

    @staticmethod
    async def is_bapped(page):
        try:
            body = (await page.content()).lower()
            return any(m in body for m in BaPGuard.BAP_MARKERS)
        except Exception:
            return False

    @staticmethod
    def _is_flag(c):
        return str(c.get("name", "")).lower().strip(".") == "tmpt"

    async def heal(self, context, page, log):
        self.hits += 1
        log(f"🛡️ BAP #{self.hits} — removing .tmpt + refresh…")
        try:
            cookies = await context.cookies()
            good = [c for c in cookies if not self._is_flag(c)]
            if good and len(good) != len(cookies):
                try:
                    await context.clear_cookies()
                except Exception:
                    pass
                await context.add_cookies(good)
        except Exception:
            pass
        try:
            await page.reload(wait_until="domcontentloaded", timeout=20000)
            await asyncio.sleep(1.5)
            if not await self.is_bapped(page):
                self.heals += 1
                log(f"✅ BAP healed ({self.heals}/{self.hits})")
                return True
        except Exception:
            pass
        try:
            await page.evaluate(
                "document.cookie='.tmpt=; expires=Thu, 01 Jan 1970 00:00:01 GMT;"
                "domain=.ticketmaster.com; path=/'")
            await page.reload(wait_until="domcontentloaded", timeout=20000)
            await asyncio.sleep(1.5)
            if not await self.is_bapped(page):
                self.heals += 1
                return True
        except Exception:
            pass
        return False

    async def guard(self, context, page, log):
        if await self.is_bapped(page):
            return await self.heal(context, page, log)
        return False


# ═════════════════════════════════════════════
#  🎯 POPUP GUARD v2 — static + AI + 🎓 teach
# ═════════════════════════════════════════════

class PopupGuard:
    STATIC_ACCEPT = [
        "i understand", "understood", "accept all", "accept", "i agree",
        "agree", "got it", "ok, got it", "continue", "allow all", "allow",
        "yes, accept", "sounds good", "dismiss", "close",
    ]
    STATIC_SELECTORS = [
        "button#onetrust-accept-btn-handler",
        "button#onetrust-close-btn-container",
        "[aria-label='close']", "[aria-label='Close']",
        "button.close", ".modal-close", "#modal-close",
        "[data-testid='cookie-banner'] button",
    ]

    def __init__(self):
        self.static_hits, self.ai_hits, self.learned_hits = 0, 0, 0
        self.learned = jload(LEARNED_FILE, {})

    def _save_learned(self):
        jsave(LEARNED_FILE, self.learned)

    @staticmethod
    def _fingerprint(overlay):
        caption = re.sub(r"\d+", "#", overlay.get("caption", "").lower())
        return " ".join(re.findall(r"[a-z#]{4,}", caption)[:10])

    async def blocked_by(self, page):
        try:
            return await page.evaluate("""() => {
                const sels = ["[role='dialog']", "[class*='modal']",
                              "#onetrust-banner-sdk", "[class*='overlay']",
                              "[class*='consent']", "[class*='popup']"];
                for (const sel of sels) {
                    for (const el of document.querySelectorAll(sel)) {
                        const r = el.getBoundingClientRect();
                        if (r.width > 150 && r.height > 80 &&
                            getComputedStyle(el).display !== 'none') {
                            const btns = [...el.querySelectorAll(
                                "button, a, [role='button']")]
                                .map(b => (b.textContent||"").trim())
                                .filter(t => t);
                            return {caption: (el.textContent||"").trim()
                                    .slice(0,200), texts: btns.slice(0,12)};
                        }
                    }
                }
                return null;
            }""")
        except Exception:
            return None

    async def _static_click(self, page):
        for sel in self.STATIC_SELECTORS:
            try:
                el = page.locator(sel).first
                if await el.is_visible():
                    await el.click(timeout=2000)
                    await asyncio.sleep(0.4)
                    self.static_hits += 1
                    return True
            except Exception:
                continue
        known = list(self.STATIC_ACCEPT) + \
            [e["button"].lower() for e in self.learned.values() if e.get("button")]
        for text in known:
            try:
                el = page.locator(
                    f"button:has-text(\"{text}\"), a:has-text(\"{text}\"), "
                    f"[role='button']:has-text(\"{text}\")").first
                if await el.is_visible():
                    await el.click(timeout=2000)
                    await asyncio.sleep(0.4)
                    self.static_hits += 1
                    return True
            except Exception:
                continue
        return False

    async def _learned_click(self, page, overlay):
        fp = self._fingerprint(overlay)
        entry = self.learned.get(fp)
        if not entry or not entry.get("button"):
            return None
        if entry["button"] not in overlay.get("texts", []):
            return None
        try:
            el = page.locator(f"button:has-text(\"{entry['button']}\"), "
                              f"a:has-text(\"{entry['button']}\")").first
            await el.click(timeout=2000)
            await asyncio.sleep(0.4)
            self.learned_hits += 1
            entry["hits"] = entry.get("hits", 0) + 1
            self._save_learned()
            return True
        except Exception:
            return None

    async def _ai_click(self, page, overlay, log):
        if not AI.enabled:
            return False
        answer = await asyncio.to_thread(AI.popup_button,
                                          overlay.get("caption", ""),
                                          overlay.get("texts", []))
        if not answer:
            return False
        for txt in overlay.get("texts", []):
            if txt.lower() in answer.lower() or answer.lower() in txt.lower():
                try:
                    await page.locator(f"button:has-text(\"{txt}\")").first \
                        .click(timeout=3000)
                    await asyncio.sleep(0.4)
                    self.ai_hits += 1
                    fp = self._fingerprint(overlay)
                    self.learned[fp] = {"button": txt, "hits": 1,
                                        "learned_at": datetime.now().isoformat(),
                                        "caption": overlay["caption"][:120]}
                    self._save_learned()
                    log(f"🎯 AI dismissed '{txt}' + 🎓 learned it")
                    return True
                except Exception:
                    continue
        return False

    async def sweep(self, page, log):
        if await self._static_click(page):
            return True
        overlay = await self.blocked_by(page)
        if not overlay or not overlay.get("texts"):
            return False
        learned = await self._learned_click(page, overlay)
        if learned:
            log("🎯 popup dismissed (🎓 learned — instant)")
            return True
        if await self._ai_click(page, overlay, log):
            return True
        return False


# ═════════════════════════════════════════════
#  📧 IMAP OTP FISHER (catch-all)
# ═════════════════════════════════════════════

class IMAPFisher:
    TM_SENDERS = ("ticketmaster", "tm-bot", "noreply", "tix")

    def __init__(self, cfg=None):
        c = cfg or CONFIG.get("imap", {})
        self.email = c.get("email", "")
        self.password = c.get("password", "")
        self.host = c.get("host", "")
        self.port = int(c.get("port", 993))
        self.ssl = c.get("ssl", True)
        self.enabled = bool(self.email and self.password and self.host)

    def _connect(self):
        mb = (imaplib.IMAP4_SSL(self.host, self.port) if self.ssl
              else imaplib.IMAP4(self.host, self.port))
        mb.login(self.email, self.password)
        mb.select("INBOX")
        return mb

    @staticmethod
    def _body(msg):
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() in ("text/plain", "text/html"):
                    try:
                        return part.get_payload(decode=True).decode(errors="ignore")
                    except Exception:
                        continue
            return ""
        try:
            return msg.get_payload(decode=True).decode(errors="ignore")
        except Exception:
            return ""

    @staticmethod
    def _extract_code(text):
        m = re.search(r"(?:code|verify|verification|passcode|pin)"
                      r"[^0-9]{0,40}\b(\d{6})\b", text, re.I)
        if m:
            return m.group(1)
        weak = re.findall(r"(?<![\d.])(\d{6})(?![\d.])", text)
        return weak[0] if weak else None

    def scan_once(self, account_email, since):
        try:
            mb = self._connect()
            date_str = since.strftime("%d-%b-%Y")
            _, ids = mb.search(None, f'(UNSEEN SINCE "{date_str}")')
            if not ids or not ids[0]:
                return None
            for mid in ids[0].split()[-25:]:
                _, data = mb.fetch(mid, "(RFC822)")
                if not data or not data[0]:
                    continue
                msg = message_from_bytes(data[0][1])
                to_list = [a[1].lower() for a in
                           getaddresses(msg.get_all("To", []) or [])]
                if account_email.lower() not in to_list:
                    continue
                frm = " ".join(a[1].lower() for a in
                              getaddresses(msg.get_all("From", []) or []))
                if frm and not any(s in frm for s in self.TM_SENDERS):
                    continue
                body = self._body(msg)
                code = self._extract_code(body) or \
                    self._extract_code(str(msg.get("Subject", "")))
                if code:
                    mb.store(mid, "+FLAGS", "\\Seen")
                    return code
            return None
        except Exception as e:
            print(f"📧 IMAP error: {e}")
            return None

    def wait_for_otp(self, account_email, since, timeout=120):
        if not self.enabled:
            return None
        t0 = time.time()
        print(f"📧 fishing OTP for {account_email}…")
        while time.time() - t0 < timeout:
            code = self.scan_once(account_email, since)
            if code:
                print(f"📧✅ OTP {code} in {time.time()-t0:.0f}s")
                return code
            time.sleep(4)
        return None

    def test_connection(self):
        try:
            mb = self._connect()
            _, cnt = mb.search(None, "ALL")
            n = len(cnt[0].split()) if cnt and cnt[0] else 0
            return True, f"✅ connected to {self.host} — {n} mail(s)"
        except Exception as e:
            return False, f"❌ {e}"


# ═════════════════════════════════════════════
#  📤 DISCORD (ONE channel, async-safe)
# ═════════════════════════════════════════════

EMOJI = {"queue": (0x5865F2, "🚦"), "carted": (0x57F287, "🛒"),
         "fail": (0xED4245, "❌"), "info": (0x99AAB5, "ℹ️")}


class DiscordNotifier:
    _POOL = concurrent.futures.ThreadPoolExecutor(
        max_workers=2, thread_name_prefix="discord")

    def __init__(self, url):
        self.webhook_url = url
        self.fail_count = 0

    def test(self):
        if not self.webhook_url or "YOUR_WEBHOOK" in self.webhook_url:
            return False, "webhook URL not set (still the placeholder!)"
        try:
            r = requests.get(self.webhook_url, timeout=10)
            if r.status_code == 200:
                return True, f"webhook OK → #{r.json().get('name', '?')}"
            return False, f"webhook returned HTTP {r.status_code} — check URL"
        except Exception as e:
            return False, f"webhook unreachable: {e}"

    def _post(self, payload, file_path=None):
        for _ in range(2):
            try:
                if file_path and Path(file_path).exists():
                    with open(file_path, "rb") as f:
                        r = requests.post(
                            self.webhook_url,
                            data={"payload_json": json.dumps(payload)},
                            files={"files[0]": (Path(file_path).name, f,
                                                "image/png")}, timeout=15)
                else:
                    r = requests.post(self.webhook_url, json=payload, timeout=15)
                if r.status_code in (200, 204):
                    self.fail_count = 0
                    return True
                print(f"⚠️ webhook HTTP {r.status_code}")
            except Exception as e:
                print(f"⚠️ webhook: {e}")
            time.sleep(2)
        self.fail_count += 1
        return False

    def send(self, kind, title, description="", fields=None, content="",
             file_path=None):
        color, icon = EMOJI.get(kind, EMOJI["info"])
        embed = {"title": f"{icon} {title}",
                 "description": description or None, "color": color,
                 "timestamp": datetime.now(timezone.utc).isoformat(),
                 "footer": {"text": f"Karma Ultra v1.0 • "
                                    f"{datetime.now().strftime('%H:%M:%S')}"}}
        if fields:
            embed["fields"] = [{"name": n, "value": str(v) or "—", "inline": i}
                               for n, v, i in fields]
        payload = {k: v for k, v in {"content": content,
                                     "username": "Karma Ultra",
                                     "embeds": [embed]}.items() if v}
        DiscordNotifier._POOL.submit(self._post, payload, file_path)

    def notify_queue_passed(self, task_id, gn, cookies, url):
        text = "\n".join(f"`{k}` = `{v}`" for k, v in cookies.items())[:900]
        self.send("queue", f"QUEUE PASSED — [{gn}]",
                  description="Cookies — your browser or the 🍪 tab:",
                  fields=[("🪪 Task", task_id, True),
                          ("🎫 Event", f"[Open]({url})", True),
                          ("🍪 Cookies", text if CONFIG["discord"].get(
                              "include_cookies") else "*(off)*", False)])

    def notify_carted(self, task_id, gn, section, row, seats, qty, unit,
                      total, expires, checkout_url, contiguous, bap_heals,
                      currency="$", tg_min=0, tg_max=0, screenshot=None):
        cfg = CONFIG["discord"]
        mention = f"<@{cfg['mention_user_id']}>" if cfg.get("mention_user_id") else ""
        rem = max(0, (expires - datetime.now()).total_seconds())
        desc = (f"### ⏳ Expires <t:{int(expires.timestamp())}:R> "
                f"({int(rem//60)}m {int(rem%60):02d}s)\n")
        if cfg.get("include_cart_link") and checkout_url:
            desc += f"## ➡️ [**CLICK HERE TO PAY NOW**]({checkout_url})\n"
        fields = [("📍 Section", f"**{section}**", True),
                  ("🔤 Row", f"**{row}**", True),
                  ("💺 Seats", f"**{', '.join(seats) if seats else '—'}**", True),
                  ("🎟️ Qty", f"**{qty}**", True),
                  ("🔗 Contiguous", "✅" if contiguous else "❌", True),
                  ("💵 Each", f"**{currency}{unit:,.2f}**/ticket", True),
                  ("💰 Total", f"**{currency}{total:,.2f}**", True),
                  ("🎚️ Bounds", f"{currency}[{tg_min:,.0f}–{tg_max:,.0f}]/tk ✅", True)]
        if bap_heals:
            fields.append(("🛡️ BAP heals", f"{bap_heals}×", True))
        self.send("carted", f"🛒 CARTED — [{gn}]", description=desc,
                  content=mention, fields=fields,
                  file_path=screenshot if cfg.get("include_screenshot") else None)

    def notify_bap(self, task_id, gn, hits, heals):
        self.send("fail", f"🛡️ BAP Auto-Healed — [{gn}]",
                  description=f"Paused **{hits}×** — `.tmpt` removed & refreshed "
                              f"automatically ({heals} heals). No action needed.")

    def fail(self, task_id, gn, reason):
        self.send("fail", f"Failed — [{gn}]",
                  fields=[("🪪 Task", str(task_id), True),
                          ("📝 Reason", str(reason)[:400], False)])

    def info(self, title, msg):
        self.send("info", title, description=msg)


# ═════════════════════════════════════════════
#  ACCOUNTS / PROXIES (groups) / CAPTCHA
# ═════════════════════════════════════════════

class AccountGenerator:
    FIRST = ["James", "Emma", "Liam", "Olivia", "Noah", "Ava", "Ethan", "Sophia"]
    LAST = ["Smith", "Johnson", "Brown", "Wilson", "Taylor", "Clark", "Lewis",
            "Walker"]

    @staticmethod
    def _rand(n=10):
        return "".join(random.choice(string.ascii_letters + string.digits)
                      for _ in range(n))

    @staticmethod
    def random_email():
        dom = CONFIG.get("imap", {}).get("domain_hint", "")
        if dom:
            return f"tm.bot.{AccountGenerator._rand(8).lower()}@{dom}"
        try:
            d = requests.get("https://api.mail.tm/domains", timeout=10) \
                     .json()["hydra:member"][0]["domain"]
            return f"tm.bot.{AccountGenerator._rand(8).lower()}@{d}"
        except Exception:
            return f"tm.bot.{AccountGenerator._rand(8).lower()}@gmail.com"

    def create(self, group="default"):
        return {"email": self.random_email(),
                "password": self._rand(14),
                "first_name": random.choice(self.FIRST),
                "last_name": random.choice(self.LAST),
                "phone": f"+1{random.randint(2000000000, 9999999999)}",
                "created_at": datetime.now().isoformat(),
                "group": group, "verified": False, "used": False,
                "status": "ok", "otp_fails": 0}


class ProxyGroupManager:
    @staticmethod
    def load():
        return jload(PROXY_GROUPS_FILE, {})

    @staticmethod
    def save(groups):
        jsave(PROXY_GROUPS_FILE, groups)

    @staticmethod
    def get(group="any"):
        g = ProxyGroupManager.load()
        if group in ("any", "", None):
            flat = [p for lst in g.values() for p in lst]
            random.shuffle(flat)
            return flat
        return g.get(group, [])

    @staticmethod
    def to_playwright(line):
        if "@" in line:
            creds, _, host = line.partition("@")
            u, _, pw = creds.partition(":")
            return {"server": f"http://{host}", "username": u, "password": pw}
        return {"server": f"http://{line}"}


class ProxyManager:
    def __init__(self):
        self.proxies, self.i, self.lock = [], 0, threading.Lock()
        if PROXIES_FILE.exists():
            with open(PROXIES_FILE) as f:
                self.proxies = [l.strip() for l in f if l.strip()]

    def next(self):
        with self.lock:
            if not self.proxies:
                return None
            p = self.proxies[self.i % len(self.proxies)]
            self.i += 1
            return {"server": f"http://{p}"}


class CaptchaSolver:
    def __init__(self, key=None):
        self.key = key or CONFIG["captcha"]["api_key"]

    async def solve(self, public_key, site_url):
        async with httpx.AsyncClient(timeout=30) as c:
            r = (await c.post("https://api.capsolver.com/createTask", json={
                "clientKey": self.key,
                "task": {"type": "FunCaptchaTaskProxyLess",
                         "websitePublicKey": public_key,
                         "websiteURL": site_url}})).json()
            t0 = time.time()
            while time.time() - t0 < 180:
                res = (await c.post("https://api.capsolver.com/getTaskResult", json={
                    "clientKey": self.key, "taskId": r.get("taskId")})).json()
                if res.get("status") == "ready":
                    return res["solution"]["token"]
                await asyncio.sleep(2)
        raise TimeoutError("captcha solve timeout")


# ═════════════════════════════════════════════
#  BROWSER ENGINE (hybrid + proxy groups + proxy continuity)
# ═════════════════════════════════════════════

class BrowserEngine:
    STEALTH_JS = ("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
                 "Object.defineProperty(navigator,'languages',"
                 "{get:()=>['en-US','en']});window.chrome={runtime:{}};")
    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

    def __init__(self, pm, headless=True, hybrid=True):
        from playwright.async_api import async_playwright
        self.pw = async_playwright()
        self.pm, self.headless, self.hybrid = pm, headless, hybrid
        self._plock = threading.Lock()
        self._pi = {}
        self._started = False
        self._last_proxy = None

    async def start(self):
        if not self._started:
            self.pw_ = await self.pw.start()
            self._started = True

    async def stop(self):
        if self._started:
            await self.pw_.stop()
            self._started = False

    def _next_proxy(self, group):
        with self._plock:
            pool = ProxyGroupManager.get(group)
            if not pool:
                return self.pm.next() if self.pm.proxies else None
            line = pool[self._pi.get(group, 0) % len(pool)]
            self._pi[group] = self._pi.get(group, 0) + 1
            return ProxyGroupManager.to_playwright(line)

    async def new_context(self, headless=None, email=None, proxy_group="any",
                          force_visible=False):
        headless = False if force_visible else \
            (self.headless if headless is None else headless)
        proxy = self._next_proxy(proxy_group)
        self._last_proxy = proxy
        browser = await self.pw_.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled",
                  "--no-sandbox", "--disable-dev-shm-usage"])
        ctx = await browser.new_context(
            proxy=proxy, user_agent=self.UA,
            viewport={"width": 1920, "height": 1080}, locale="en-US",
            storage_state=SessionVault.load(email))
        await ctx.add_init_script(self.STEALTH_JS)
        return browser, ctx

    async def go_visible(self, ctx):
        state = SessionVault._scrub(ctx.storage_state())
        browser = await self.pw_.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled",
                  "--window-position=100,50"])
        c = await browser.new_context(user_agent=self.UA, locale="en-US",
                                      storage_state=state,
                                      proxy=self._last_proxy)
        await c.add_init_script(self.STEALTH_JS)
        return browser, c


# ═════════════════════════════════════════════
#  🧱 SECTION FILTER (+AI intent)
# ═════════════════════════════════════════════

class SectionFilter:
    @staticmethod
    def _norm(s):
        return re.sub(r"\s+", " ", s.lower()).strip()

    @staticmethod
    def _filter_matches(f_, s_):
        f_, s_ = SectionFilter._norm(f_), SectionFilter._norm(s_)
        m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", f_)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            return any(lo <= int(n) <= hi for n in re.findall(r"\d+", s_))
        m = re.fullmatch(r"(.+?)\s+(\d+)\s*-\s*(\d+)", f_)
        if m:
            w, lo, hi = m.group(1), int(m.group(2)), int(m.group(3))
            return (w in s_ and
                    any(lo <= int(n) <= hi for n in re.findall(r"\d+", s_)))
        return f_ in s_

    @staticmethod
    def allowed(section_name, tg):
        if not tg.sections:
            return True
        matches = any(SectionFilter._filter_matches(f, section_name)
                      for f in tg.sections)
        verdict = matches if tg.section_mode == "include" else not matches
        if not verdict and AI.enabled:
            for f in tg.sections:
                if AI.section_matches(f, section_name, tg.section_mode) is True:
                    return True if tg.section_mode == "include" else False
        return verdict

    @staticmethod
    def describe(tg):
        if not tg.sections:
            return "any section"
        mode = "ONLY" if tg.section_mode == "include" else "NOT"
        return f"{mode}: {', '.join(tg.sections)}"


def seats_contiguous(nums):
    if len(nums) < 2:
        return True
    s = sorted(int(x) for x in nums)
    return all(b - a == 1 for a, b in zip(s, s[1:]))


def extract_seat_numbers(text):
    nums = []
    for m in re.finditer(r"seats?\s*:?\s*(\d+)\s*[-–]\s*(\d+)", text, re.I):
        nums.extend(range(int(m.group(1)), int(m.group(2)) + 1))
    for m in re.finditer(r"seats?\s*:?\s*((?:\d+[,\s]+)+\d+)", text, re.I):
        for x in re.findall(r"\d+", m.group(1)):
            if int(x) not in nums:
                nums.append(int(x))
    for m in re.finditer(r"seat\s+(\d+)\b", text, re.I):
        if int(m.group(1)) not in nums:
            nums.append(int(m.group(1)))
    return nums


# ═════════════════════════════════════════════
#  💳 PURCHASE BOT
# ═════════════════════════════════════════════

CAPTCHA_HOSTS = ("arkoselabs", "funcaptcha", "arkose")


class PurchaseBot:
    def __init__(self, engine, solver, notifier, tg):
        self.engine, self.solver, self.discord = engine, solver, notifier
        self.tg = tg
        self.task_id = uuid.uuid4().hex[:8]
        self.bap = BaPGuard()
        self.popup = PopupGuard()
        self.imap = IMAPFisher()
        self.currency = "$"
        self._takeover = False

    async def apply_presale_code(self, page, log):
        tg = self.tg
        codes = [c.strip() for c in (tg.presale_codes or []) if c.strip()]
        if not codes:
            return True
        try:
            await page.wait_for_selector(
                "input[placeholder*='offer' i], input[placeholder*='code' i], "
                "button:has-text('Unlock'), text=/enter.*code/i", timeout=6000)
        except Exception:
            return True
        for code in codes:
            if code in tg.presale_played:
                continue
            try:
                box = page.locator("input[placeholder*='offer' i], "
                                    "input[placeholder*='code' i]").first
                await box.fill(code, timeout=5000)
                await page.locator(
                    "button:has-text('Unlock'), button:has-text('Submit'), "
                    "button:has-text('Apply')").first.click(timeout=5000)
                await asyncio.sleep(2)
                body = (await page.content()).lower()
                if not any(x in body for x in
                          ["invalid", "not valid", "expired", "incorrect code",
                           "code is not recognized", "try again"]):
                    tg.presale_played.append(code)
                    log(f"🔑✅ [{tg.name}] unlocked with {code}")
                    return True
            except Exception:
                continue
        return False

    async def _inject_captcha_token(self, page, token, log):
        try:
            found = await page.evaluate("""(token) => {
                let ok = false;
                for (const sel of ['#FunCaptcha-Token',
                                   'input[name="fc-token"]',
                                   'textarea[name="fc-token"]',
                                   'input[name="ARKOSE_TOKEN"]']) {
                    const el = document.querySelector(sel);
                    if (el) { el.value = token; ok = true; }
                }
                try {
                    window.postMessage(
                        JSON.stringify({eventId: 'challenge-complete',
                                        payload: {validation_token: token}}),
                        '*');
                    ok = true;
                } catch (e) {}
                return ok;
            }""", token)
            for sel in ["button:has-text('Verify You Are Human')",
                        "button:has-text('Continue')",
                        "[data-testid='submit-captcha']"]:
                try:
                    b = page.locator(sel).first
                    if await b.is_visible():
                        await b.click(timeout=2000)
                        break
                except Exception:
                    continue
            log(f"🧩 [{self.tg.name}] captcha token injected"
                f"{' (field+event)' if found else ' (event only)'}")
        except Exception as e:
            log(f"🧩⚠️ [{self.tg.name}] token inject: {e}")

    async def handle_otp(self, page, account, log):
        try:
            await page.wait_for_selector(
                "input[autocomplete='one-time-code'], input[name*='code' i], "
                "input[placeholder*='code' i], text=/enter.*code|verif/i",
                timeout=5000)
        except Exception:
            return True
        log(f"📧 [{self.tg.name}] OTP screen — fishing catch-all…")
        STATUS.update(self.task_id, state="otp")
        since = datetime.now() - timedelta(minutes=2)
        code = await asyncio.to_thread(self.imap.wait_for_otp,
                                       account["email"], since, 120)
        if not code:
            still_ok = record_otp_result(account["email"], False,
                                        "no OTP email in timeout")
            code_, text = await asyncio.to_thread(explain_deep, code="TM-OTP")
            STATUS.error(self.task_id, "TM-OTP",
                         text + (" 💀 account retired" if not still_ok else ""))
            self.discord.fail(self.task_id, self.tg.name,
                              f"TM-OTP — {text}")
            return False
        try:
            boxes = page.locator("input[autocomplete='one-time-code'], "
                                 "input[name*='code' i]")
            if await boxes.count() == 1:
                await boxes.first.fill(code, timeout=5000)
            else:
                for i, d in enumerate(code):
                    await boxes.nth(i).fill(d, timeout=3000)
            await page.locator("button:has-text('Verify'), "
                               "button:has-text('Submit'), "
                               "button:has-text('Continue')").first \
                .click(timeout=5000)
            await asyncio.sleep(2)
            record_otp_result(account["email"], True)
            log(f"📧✅ [{self.tg.name}] OTP {code} submitted")
            return True
        except Exception as e:
            log(f"📧⚠️ OTP fill error: {e}")
            return False

    async def offer_takeover(self, browser, context, page, reason, log):
        try:
            state = SessionVault._scrub(context.storage_state())
            vb = await self.engine.pw_.chromium.launch(
                headless=False,
                args=["--window-position=80,40", "--start-maximized"])
            vctx = await vb.new_context(user_agent=self.engine.UA,
                                        locale="en-US",
                                        storage_state=state,
                                        proxy=self.engine._last_proxy)
            await vctx.add_init_script(self.engine.STEALTH_JS)
            vpage = await vctx.new_page()
            await vpage.goto(self.tg.event_url, wait_until="domcontentloaded",
                             timeout=25000)
            STATUS.update(self.task_id, state="failed",
                          takeover_available=True)
            self._takeover = True
            self.discord.fail(self.task_id, self.tg.name,
                              f"👀 TAKEOVER OPEN ({reason}): visible browser "
                              f"with your session is on the event page — "
                              f"cart manually now.")
            log(f"👀 [{self.tg.name}] takeover window open")
            hold = CONFIG["general"].get("keep_browser_open_min", 15) * 60
            end = time.time() + hold
            while time.time() < end and not vpage.is_closed():
                await asyncio.sleep(5)
        except Exception as e:
            log(f"👀 takeover failed: {e}")

    async def run(self, account, log):
        tg = self.tg
        url, qty = tg.event_url, tg.quantity
        STATUS.create(self.task_id, tg.name, account["email"])
        log(f"▶ [{tg.name}] qty={qty} | {tg.min_price}-{tg.max_price}/tk | "
            f"{SectionFilter.describe(tg)} | {'🔗' if tg.contiguous else ''} | "
            f"pool:{tg.account_group} net:{tg.proxy_group}")

        had_session = SessionVault.has(account["email"])
        browser, context = await self.engine.new_context(
            headless=True, email=account["email"], proxy_group=tg.proxy_group)
        page = await context.new_page()

        async def route_guard(route):
            req = route.request
            if (req.resource_type in ("image", "media", "font")
                    and not any(h in req.url for h in CAPTCHA_HOSTS)):
                await route.abort()
            else:
                await route.continue_()
        await page.route("**/*", route_guard)

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            await self.popup.sweep(page, log)
            STATUS.update(self.task_id, popups=self.popup.static_hits +
                          self.popup.learned_hits + self.popup.ai_hits)

            self.currency = PriceEngine.detect_symbol(await page.content())
            log(f"💱 [{tg.name}] currency: {self.currency}")

            if await self.bap.guard(context, page, log):
                STATUS.update(self.task_id, bap_heals=self.bap.heals)
                self.discord.notify_bap(self.task_id, tg.name,
                                       self.bap.hits, self.bap.heals)

            log(f"🚦 [{tg.name}] waiting for queue…")
            STATUS.update(self.task_id, state="queue")
            passed, t0, start_pos = False, time.time(), None
            while not passed and time.time() - t0 < 900:
                html = await page.content()
                low = html.lower()
                if "queue" in low or "in line" in low:
                    qm = re.search(r"#([\d,]+)", html) or \
                        re.search(r"position[:\s]+([\d,]+)", html, re.I)
                    if qm:
                        pos = int(qm.group(1).replace(",", ""))
                        if start_pos is None and pos > 0:
                            start_pos = pos
                        pct = (100 * (start_pos - pos) / start_pos) \
                            if start_pos else 0
                        STATUS.update(self.task_id, queue_pos=pos,
                                      queue_pct=round(pct, 1))
                if any(x in low for x in ["it's your turn", "select tickets"]):
                    passed = True
                    STATUS.update(self.task_id, queue_pos=0, queue_pct=100.0)
                    break
                if await self.popup.sweep(page, log):
                    STATUS.update(self.task_id,
                                 popups=self.popup.static_hits +
                                 self.popup.learned_hits + self.popup.ai_hits)
                if await self.bap.guard(context, page, log):
                    STATUS.update(self.task_id, bap_heals=self.bap.heals)
                    self.discord.notify_bap(self.task_id, tg.name,
                                           self.bap.hits, self.bap.heals)
                await asyncio.sleep(1)
            if not passed:
                code, text = await asyncio.to_thread(explain_deep,
                                                     code="TM-QUEUE-TIMEOUT")
                STATUS.error(self.task_id, code, text)
                self.discord.fail(self.task_id, tg.name, f"{code} — {text}")
                await self.offer_takeover(browser, context, page, code, log)
                return False

            cookies = {c["name"]: c["value"] for c in await context.cookies()}
            SessionVault.save(context, account["email"])
            self.discord.notify_queue_passed(self.task_id, tg.name, cookies, url)
            log(f"🚦 [{tg.name}] queue passed "
                f"({'⚡ reused' if had_session else 'fresh'})")

            if self.engine.hybrid:
                vb, vctx = await self.engine.go_visible(context)
                await browser.close()
                browser, context = vb, vctx
                page = await context.new_page()
                await page.goto(url, wait_until="domcontentloaded",
                                timeout=25000)
                await self.popup.sweep(page, log)
                await self.bap.guard(context, page, log)

            if not await self.handle_otp(page, account, log):
                await self.offer_takeover(browser, context, page, "OTP", log)
                return False

            if not await self.apply_presale_code(page, log):
                code, text = await asyncio.to_thread(explain_deep,
                                                     code="TM-PRESALE-LOCKED")
                STATUS.error(self.task_id, code, text)
                self.discord.fail(self.task_id, tg.name, f"{code} — {text}")
                await self.offer_takeover(browser, context, page, code, log)
                return False
            await self.popup.sweep(page, log)

            STATUS.update(self.task_id, state="hunting")
            try:
                await page.wait_for_selector(
                    "[data-testid='ticket-list'], .ticket-list", timeout=45000)
            except Exception as e:
                ctx_txt = (await page.content())[:1500]
                ai_view = await asyncio.to_thread(AI.describe_page, ctx_txt) \
                    if AI.enabled else "—"
                code, text = await asyncio.to_thread(explain_deep, e, "TM-SEL",
                                                     ctx_txt)
                log(f"🧠 page: {ai_view}")
                STATUS.error(self.task_id, code, f"{text} ({ai_view})")
                self.discord.fail(self.task_id, tg.name,
                                  f"{code} — {text} (page: {ai_view})")
                await self.offer_takeover(browser, context, page, code, log)
                return False

            selected = False
            cart = {"section": "—", "row": "—", "unit": 0.0, "seats": []}
            offers = page.locator("div[aria-label]")
            total = min(await offers.count(), 60)
            for i in range(total):
                if selected:
                    break
                if i % 5 == 0 and await self.bap.guard(context, page, log):
                    STATUS.update(self.task_id, bap_heals=self.bap.heals)
                try:
                    el = offers.nth(i)
                    label = await el.get_attribute("aria-label") or ""
                    txt = label + "\n" + (await el.inner_text()
                                         if await el.is_visible() else "")
                except Exception:
                    continue
                section_name = label or txt.split("\n")[0]
                if not await asyncio.to_thread(SectionFilter.allowed,
                                               section_name, tg):
                    continue
                parsed = PriceEngine.parse_prices(txt)
                unit = parsed[0] if parsed else None
                em = re.search(r"(?:each|ea|per ticket)[^\d]{0,25}"
                               r"(?:MX\$|AR\$|R\$|£|€|\$|¥)\s?(\d[\d.,]*)",
                               txt, re.I)
                if em:
                    v = PriceEngine._num(em.group(1))
                    if v:
                        unit = v
                if unit is None or not PriceEngine.within(unit, tg):
                    continue
                avail = extract_seat_numbers(txt)
                m = re.search(r"(\d+)\s*(?:left|remaining|available)",
                              txt, re.I)
                n_avail = int(m.group(1)) if m else (len(avail) if avail else qty)
                if n_avail < qty:
                    continue
                if tg.contiguous and avail and len(avail) >= qty:
                    if not seats_contiguous(avail[:qty]):
                        continue
                try:
                    await el.click()
                except Exception:
                    continue
                rm = re.search(r"row\s+(\w+)", txt, re.I)
                cart.update(section=section_name[:40], unit=unit,
                           row=rm.group(1).upper() if rm else "—",
                           seats=[str(x) for x in avail[:qty]])
                selected = True
                log(f"✅ [{tg.name}] {section_name[:30]} "
                    f"{self.currency}{unit} ×{qty}")
            if not selected:
                code, text = await asyncio.to_thread(explain_deep,
                                                     code="TM-NO-MATCH")
                STATUS.error(self.task_id, code,
                             f"{text} ({SectionFilter.describe(tg)}, "
                             f"{self.currency}[{tg.min_price}-{tg.max_price}]/tk)")
                self.discord.fail(self.task_id, tg.name, f"{code} — {text}")
                await self.offer_takeover(browser, context, page, code, log)
                return False

            try:
                await page.locator("select[name='quantity']") \
                    .select_option(str(qty))
            except Exception:
                try:
                    await page.locator(
                        f"button[aria-label='quantity {qty}'], "
                        f"[data-qty='{qty}']").first.click(timeout=3000)
                except Exception:
                    code, text = await asyncio.to_thread(explain_deep,
                                                         code="TM-QTY-FORCE")
                    STATUS.error(self.task_id, code, text)

            try:
                await page.wait_for_selector("#FunCaptcha-Arkose", timeout=4000)
                STATUS.update(self.task_id, state="captcha")
                log(f"🧩 [{tg.name}] solving captcha…")
                token = await self.solver.solve(
                    "A2A14B1D-1AF3-C791-9BBC-EE33CC7A0A6F", url)
                await self._inject_captcha_token(page, token, log)
            except Exception as e:
                if "timeout" in str(e).lower() and "captcha" in str(e).lower():
                    code, text = await asyncio.to_thread(
                        explain_deep, e, "TM-CAPTCHA-FAIL")
                    STATUS.error(self.task_id, code, text)
                    self.discord.fail(self.task_id, tg.name, f"{code} — {text}")
                    await self.offer_takeover(browser, context, page, code, log)
                    return False

            await self.handle_otp(page, account, log)
            try:
                await page.click("[data-bd='funzone']", timeout=10000)
                await page.wait_for_load_state("domcontentloaded")
            except Exception as e:
                code, text = await asyncio.to_thread(
                    explain_deep, e, "TM-CART-FAIL",
                    (await page.content())[:800])
                STATUS.error(self.task_id, code, text)
                self.discord.fail(self.task_id, tg.name, f"{code} — {text}")
                await self.offer_takeover(browser, context, page, code, log)
                return False
            await self.bap.guard(context, page, log)

            body = await page.content()
            final_seats = extract_seat_numbers(body)
            if final_seats:
                cart["seats"] = [str(s) for s in final_seats[:qty]]
            contig = seats_contiguous(final_seats[:qty]) if final_seats else True
            checkout_url = page.url if ("/checkout" in page.url or
                                       "cartId" in page.url) else url
            expires = datetime.now() + timedelta(minutes=10)
            try:
                el = page.locator("[data-testid='reservation-timer'], "
                                  "text=/\\d{1,2}:\\d{2} remaining/i").first
                mm, ss = re.search(r"(\d{1,2}):(\d{2})",
                                   await el.inner_text()).groups()
                expires = datetime.now() + timedelta(minutes=int(mm),
                                                    seconds=int(ss))
            except Exception:
                pass

            SessionVault.save(context, account["email"])
            STATUS.update(self.task_id, state="carted")
            shot = str(SHOTS_DIR / f"cart_{self.task_id}.png")
            await page.screenshot(path=shot)
            self.discord.notify_carted(self.task_id, tg.name, cart["section"],
                                       cart["row"], cart["seats"], qty,
                                       cart["unit"],
                                       round(cart["unit"] * qty, 2), expires,
                                       checkout_url, contig, self.bap.heals,
                                       self.currency, tg.min_price,
                                       tg.max_price, shot)
            log(f"🛒 [{tg.name}] CARTED ×{qty} @ {self.currency}"
                f"{cart['unit']}/tk — link sent")

            STATUS.update(self.task_id, state="holding")
            hold = ((expires - datetime.now()).total_seconds() +
                    CONFIG["general"].get("keep_browser_open_min", 15) * 60)
            end, last = time.time() + max(hold, 0), time.time()
            while time.time() < end:
                await asyncio.sleep(5)
                if page.is_closed():
                    break
                await self.bap.guard(context, page, log)
                if time.time() - last > 120:
                    SessionVault.save(context, account["email"])
                    last = time.time()
            STATUS.finish(self.task_id, done=True)
            return True

        except Exception as e:
            code, _ = explain(e)
            try:
                ctx_txt = (await page.content())[:800]
            except Exception:
                ctx_txt = "page unavailable"
            code, text = await asyncio.to_thread(explain_deep, e, code, ctx_txt)
            log(f"❌ [{tg.name}] {code}: {text}")
            STATUS.error(self.task_id, code, text)
            self.discord.fail(self.task_id, tg.name, f"{code} — {text}")
            await self.offer_takeover(browser, context, page, code, log)
            return False
        finally:
            try:
                SessionVault.save(context, account["email"])
            except Exception:
                pass
            if not self._takeover:
                try:
                    await browser.close()
                except Exception:
                    pass


# ═════════════════════════════════════════════
#  ♻️ RESTOCK MONITOR (JSON backbone, non-blocking auto-cart)
# ═════════════════════════════════════════════

class RestockMonitor:
    UAS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ",
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    ]

    def __init__(self, notifier, log):
        self.discord, self.log = notifier, log
        cfg = CONFIG["restock"]
        self.poll_interval = cfg.get("poll_interval_sec", 180)
        self.jitter = cfg.get("jitter_sec", 60)
        self.auto_cart = cfg.get("auto_cart", True)
        self.min_qty = cfg.get("min_qty_available", 1)
        self.state, self.backoff, self.api_fails = {}, 1, {}

    @staticmethod
    def event_id_from_url(url):
        m = re.search(r"/event/([A-Za-z0-9]{8,20})", url)
        if m:
            return m.group(1)
        if re.fullmatch(r"[A-Za-z0-9]{8,20}", url.strip()):
            return url.strip()
        return None

    @staticmethod
    def parse_event_line(line):
        parts = [p.strip() for p in line.split("::")]
        rc = CONFIG["restock"]
        return {"url": parts[0],
                "qty": int(parts[1]) if len(parts) > 1 and parts[1].isdigit()
                      else rc.get("default_qty", 2),
                "min_price": float(parts[2]) if len(parts) > 2
                             else rc.get("min_price", 0.0),
                "max_price": float(parts[3]) if len(parts) > 3
                             else rc.get("max_price", 250.0),
                "sections": [s.strip() for s in parts[4].split(",")]
                            if len(parts) > 4 else rc.get("default_sections", []),
                "codes": [c.strip() for c in parts[5].split(",")]
                         if len(parts) > 5 else rc.get("default_presale_codes", []),
                "proxy_group": parts[6] if len(parts) > 6 else "any"}

    def _headers(self, api=False):
        h = {"User-Agent": random.choice(self.UAS),
             "Accept-Language": "en-US,en;q=0.9",
             "Accept": "application/json" if api else
                       "text/html,application/xhtml+xml,*/*;q=0.8"}
        if api:
            h["Origin"] = "https://www.ticketmaster.com"
            h["Referer"] = "https://www.ticketmaster.com/"
        return h

    async def check_api(self, client, event_id):
        url = (f"https://availability.ticketmaster.com/api/v2/"
               f"availability/{event_id}")
        try:
            r = await client.get(url, headers=self._headers(api=True),
                                 timeout=15)
            if r.status_code == 200:
                data = r.json()
                offers = []
                for offer in data.get("offers", []):
                    attrs = {str(a.get("name", "")).strip().lower():
                             a.get("value", "")
                             for a in offer.get("attributes", [])}
                    price = PriceEngine._num(
                        str(attrs.get("price", "")).replace("$", ""))
                    mq = re.search(r"\d+",
                                   str(attrs.get("quantity",
                                                attrs.get("quantities")) or ""))
                    offers.append({"price": price,
                                   "qty": int(mq.group()) if mq else None,
                                   "section": attrs.get("section")
                                              or attrs.get("name") or ""})
                for seg in (data.get("offersByPrice") or []) + \
                           (data.get("segments") or []):
                    if isinstance(seg, dict):
                        p = PriceEngine._num(str(seg.get("price",
                                                    seg.get("amount", ""))))
                        if p is not None:
                            offers.append({"price": p, "qty": None,
                                           "section": seg.get("name", "")})
                self.api_fails[event_id] = 0
                prices = [o["price"] for o in offers if o["price"] is not None]
                return {"in_stock": bool(prices) or data.get("ticketable",
                                                             False),
                        "cheapest": min(prices) if prices else None,
                        "offers": offers, "source": "api"}
            elif r.status_code == 404:
                self.api_fails[event_id] = self.api_fails.get(event_id, 0) + 1
                return {"in_stock": False, "cheapest": None, "offers": [],
                        "source": "api-404"}
            else:
                self.api_fails[event_id] = self.api_fails.get(event_id, 0) + 1
                return None
        except Exception:
            self.api_fails[event_id] = self.api_fails.get(event_id, 0) + 1
            return None

    async def check_html(self, client, url):
        try:
            r = await client.get(url, headers=self._headers(), timeout=20,
                                 follow_redirects=True)
            html = r.text.lower()
            isin = any(m in html for m in
                       ["see tickets", "find tickets", "get tickets",
                        "best available", "tickets from"])
            sold = any(m in html for m in
                       ["sold out", "currently unavailable", "no tickets",
                        "not on sale"])
            prices = [p for p in (PriceEngine._num(x) for x in
                       re.findall(r"\$(\d{2,6}(?:\.\d{2})?)", html)[:30]) if p]
            return {"in_stock": isin and not sold,
                    "cheapest": min(prices) if prices else None,
                    "offers": [], "source": "html"}
        except Exception:
            return None

    async def check(self, client, url):
        eid = self.event_id_from_url(url)
        if eid and self.api_fails.get(eid, 0) < 3:
            result = await self.check_api(client, eid)
            if result is not None:
                return result
        return await self.check_html(client, url)

    @staticmethod
    def offers_signature(offers):
        return "|".join(sorted(f"{o['section']}@{o['price']}x{o['qty'] or '?'}"
                              for o in offers))

    def alert(self, url, info, is_new, filtered):
        mention = CONFIG["discord"].get("mention_user_id")
        lines = []
        for o in sorted(filtered, key=lambda x: x["price"] or 0)[:8]:
            qty_txt = f" ×{o['qty']}" if o.get("qty") else ""
            lines.append(f"`{o['section'] or 'General'}` — "
                         f"**${o['price']:,.2f}**{qty_txt}")
        title = "♻️ RESTOCK! Tickets Available" if is_new else "♻️ Offer Changed"
        self.discord.send("carted", title,
                          description=f"## ➡️ [**BUY HERE NOW**]({url})\n"
                                      + ("\n".join(lines) or "*(not itemized)*"),
                          content=f"<@{mention}>" if mention else "",
                          fields=[("💵 From",
                                   f"${info['cheapest']:,.2f}"
                                   if info.get("cheapest") else "—", True),
                                  ("📡 Source", info["source"], True)])

    async def _trigger_cart(self, ev, cheapest):
        url, qty = ev["url"], ev["qty"]
        self.log(f"🛒 AUTO-CART: {self.event_id_from_url(url)} "
                 f"(qty={qty}, {ev['min_price']}-{ev['max_price']}/tk)")
        try:
            pm = ProxyManager()
            engine = BrowserEngine(pm, headless=CONFIG["general"]["headless"],
                                  hybrid=CONFIG["general"].get("hybrid_mode",
                                                                True))
            solver = CaptchaSolver()
            await engine.start()
            try:
                tg = TaskGroup(id=uuid.uuid4().hex[:8], name="♻️ Restock",
                               event_url=url, quantity=qty,
                               min_price=ev["min_price"],
                               max_price=ev["max_price"],
                               contiguous=CONFIG["restock"].get(
                                   "default_contiguous", True),
                               sections=ev["sections"],
                               presale_codes=ev.get("codes", []),
                               proxy_group=ev.get("proxy_group", "any"),
                               max_tasks=1)
                accounts = accounts_in_group("default")
                if not accounts:
                    raise RuntimeError("no healthy account in 'default' pool")
                accounts.sort(key=lambda a: not SessionVault.has(a["email"]))
                return await PurchaseBot(engine, solver, self.discord, tg) \
                    .run(accounts[0], self.log)
            finally:
                await engine.stop()
        except Exception as e:
            code, text = await asyncio.to_thread(explain_deep, e)
            self.log(f"🛒 auto-cart error: {text}")
            self.discord.fail("restock", "♻️ Auto-cart", f"{code} — {text}")
            return False

    async def run(self, stop_flag):
        raw = CONFIG["restock"].get("events", [])
        if not raw:
            self.log("♻️ no restock events configured")
            return
        events = [self.parse_event_line(l) for l in raw if l.strip()]
        events = [e for e in events if self.event_id_from_url(e["url"])]
        if not events:
            self.log("♻️ no valid event URLs")
            return
        self.log(f"♻️ monitor: {len(events)} event(s)")
        self.discord.info("♻️ Restock Monitor Online",
                          f"JSON backbone • {len(events)} event(s) 24/7 • "
                          f"{'AUTO-CART & HOLD 🛒' if self.auto_cart else 'notify-only'}")
        held = {}
        inflight = set()

        def _cart_done(url, task):
            inflight.discard(task)
            try:
                if not task.result():
                    held.pop(url, None)
            except Exception:
                held.pop(url, None)

        async with httpx.AsyncClient() as client:
            while not stop_flag[0]:
                hits = 0
                for ev in events:
                    url = ev["url"]
                    if stop_flag[0] or self.backoff > 1:
                        break
                    if url in held and time.time() - held[url] < 1200:
                        continue
                    await asyncio.sleep(random.uniform(2, 5))
                    info = await self.check(client, url)
                    if info is None:
                        self.backoff = min(self.backoff * 2, 10)
                        self.log(f"♻️ backing off ×{self.backoff}")
                        continue
                    self.backoff = max(1, self.backoff / 2)
                    prev = self.state.get(url, {"in_stock": False, "sig": ""})
                    filtered = [o for o in info.get("offers", [])
                                if o["price"] is not None
                                and ev["min_price"] <= o["price"] <= ev["max_price"]
                                and (o.get("qty") or self.min_qty) >= self.min_qty]
                    is_new = info["in_stock"] and not prev["in_stock"]
                    sig = self.offers_signature(filtered)
                    changed = info["in_stock"] and sig and sig != prev["sig"]
                    cheapest = info.get("cheapest")
                    price_ok = (cheapest is None or
                                ev["min_price"] <= cheapest <= ev["max_price"])
                    if info["in_stock"] and price_ok and (is_new or changed):
                        hits += 1
                        if self.auto_cart:
                            held[url] = time.time()
                            task = asyncio.create_task(
                                self._trigger_cart(ev, cheapest))
                            inflight.add(task)
                            task.add_done_callback(
                                lambda t, u=url: _cart_done(u, t))
                        else:
                            self.alert(url, info, is_new, filtered)
                    self.state[url] = {"in_stock": info["in_stock"], "sig": sig}
                total = self.poll_interval * self.backoff + \
                    random.uniform(0, self.jitter)
                self.log(f"♻️ sweep ({'🎯' if hits else '∅'}) → {int(total)}s")
                end = time.time() + total
                while time.time() < end and not stop_flag[0]:
                    await asyncio.sleep(2)
        if inflight:
            self.log(f"♻️ waiting on {len(inflight)} cart attempt(s)…")
            await asyncio.gather(*inflight, return_exceptions=True)


# ═════════════════════════════════════════════
#  RUNNERS
# ═════════════════════════════════════════════

async def run_group(tg, engine, solver, notifier, log, stop_flag):
    accounts = accounts_in_group(tg.account_group)
    if not accounts:
        log(f"⚠️ [{tg.name}] pool '{tg.account_group}' empty/unhealthy")
        return
    vaulted = sum(1 for a in accounts if SessionVault.has(a["email"]))
    log(f"📂 [{tg.name}] {len(accounts)} healthy accounts, {vaulted} vaulted ⚡")
    started = time.time()

    async def runner(acc):
        while not stop_flag[0]:
            a_now = get_account(acc["email"]) or acc
            if a_now.get("status") in ("dead_otp", "dead"):
                log(f"💀 [{tg.name}] {acc['email']} retired — swapping")
                fresh = accounts_in_group(tg.account_group)
                if not fresh:
                    notifier.fail("pool", tg.name,
                                 f"Pool '{tg.account_group}' exhausted")
                    return False
                acc = random.choice(fresh)
                continue
            if await PurchaseBot(engine, solver, notifier, tg).run(acc, log):
                update_account(acc["email"], used=True,
                               last_win=datetime.now().isoformat())
                return True
            await asyncio.sleep(3)
        return False

    results = await asyncio.gather(
        *[runner(a) for a in accounts[:min(tg.max_tasks, len(accounts))]],
        return_exceptions=True)
    wins = sum(1 for r in results if r is True)
    summary = await asyncio.to_thread(AI.summarize_run, {
        "group": tg.name, "carts": wins,
        "fails": [str(r)[:60] for r in results if r is not True][:5],
        "runtime_min": int((time.time() - started) // 60)}) \
        if AI.enabled else f"{wins} cart(s) ×{tg.quantity} each."
    notifier.info(f"🏁 [{tg.name}] Run Summary", summary)


async def run_all_groups(groups, log, stop_flag):
    pm = ProxyManager()
    engine = BrowserEngine(pm, CONFIG["general"]["headless"],
                          CONFIG["general"].get("hybrid_mode", True))
    solver, notifier = CaptchaSolver(), DiscordNotifier(
        CONFIG["discord"]["webhook_url"])
    await engine.start()
    try:
        active = [g for g in groups if g.enabled]
        await asyncio.gather(*[run_group(g, engine, solver, notifier,
                                         log, stop_flag) for g in active])
    finally:
        await engine.stop()


async def run_restock(log, stop_flag):
    notifier = DiscordNotifier(CONFIG["discord"]["webhook_url"])
    await RestockMonitor(notifier, log).run(stop_flag)


# ═════════════════════════════════════════════
#  ✨ KARMA ULTRA — FUTURISTIC NEON GUI
# ═════════════════════════════════════════════

def run_gui():
    import customtkinter as ctk
    from tkinter import StringVar, BooleanVar, END as TKEND, messagebox
    import tkinter as tk

    # ── NEON FUTURISTIC PALETTE ──────────────────
    BG          = "#0A0A0F"      # near-black void
    SIDEBAR     = "#0D0D14"
    CARD        = "#12121A"
    CARD_HOVER  = "#1A1A28"
    BORDER      = "#1F1F2E"
    ACCENT      = "#00F0FF"      # cyan neon
    ACCENT2     = "#A855F7"      # purple neon
    ACCENT3     = "#F472B6"      # pink
    GREEN       = "#22FF88"
    RED         = "#FF4466"
    YELLOW      = "#FFD60A"
    ORANGE      = "#FF8C42"
    TEXT        = "#E8E8F0"
    MUTED       = "#6B6B80"
    DIM         = "#3A3A50"

    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("dark-blue")

    app = ctk.CTk()
    app.title("⚡ KARMA ULTRA  //  NEON CORE")
    app.geometry("1280x780")
    app.minsize(1100, 680)
    app.configure(fg_color=BG)

    # ── STATE ────────────────────────────────────
    state = {
        "groups": load_groups(),
        "running": False,
        "stopref": [False],
        "selected": None,
        "page": "dashboard",
        "log_lines": [],
    }
    if not state["groups"]:
        state["groups"].append(TaskGroup(
            id=uuid.uuid4().hex[:8],
            name="Example Group",
            event_url="https://www.ticketmaster.com/event/your-event-id"))
        save_groups(state["groups"])

    # ── HELPERS ──────────────────────────────────
    def neon_btn(parent, text, color, cmd=None, width=None, height=34, **kw):
        return ctk.CTkButton(
            parent, text=text, command=cmd,
            fg_color=color, hover_color=_darken(color),
            text_color="#0A0A0F" if color in (ACCENT, GREEN, YELLOW) else TEXT,
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            corner_radius=8, height=height, width=width,
            border_width=0, **kw)

    def _darken(hex_color, factor=0.75):
        h = hex_color.lstrip("#")
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return f"#{int(r*factor):02x}{int(g*factor):02x}{int(b*factor):02x}"

    def card(parent, **kw):
        return ctk.CTkFrame(parent, fg_color=CARD, corner_radius=12,
                            border_width=1, border_color=BORDER, **kw)

    def section_label(parent, text):
        return ctk.CTkLabel(parent, text=text,
                            font=ctk.CTkFont(size=11, weight="bold"),
                            text_color=MUTED, anchor="w")

    def field_label(parent, text):
        return ctk.CTkLabel(parent, text=text,
                            font=ctk.CTkFont(size=11), text_color=MUTED, anchor="w")

    # ════════════════════════════════════════════
    #  TOP BAR
    # ════════════════════════════════════════════
    top = ctk.CTkFrame(app, fg_color=SIDEBAR, height=56, corner_radius=0)
    top.pack(fill="x")
    top.pack_propagate(False)

    # Logo / title
    logo_frame = ctk.CTkFrame(top, fg_color="transparent")
    logo_frame.pack(side="left", padx=18, pady=10)
    ctk.CTkLabel(logo_frame, text="⚡", font=ctk.CTkFont(size=22),
                 text_color=ACCENT).pack(side="left")
    ctk.CTkLabel(logo_frame, text="KARMA ULTRA",
                 font=ctk.CTkFont(family="Segoe UI", size=18, weight="bold"),
                 text_color=TEXT).pack(side="left", padx=(6, 0))
    ctk.CTkLabel(logo_frame, text="  // NEON",
                 font=ctk.CTkFont(size=11), text_color=ACCENT2).pack(side="left", pady=(4, 0))

    # Status pill
    status_pill = ctk.CTkFrame(top, fg_color=CARD, corner_radius=20,
                               border_width=1, border_color=YELLOW)
    status_pill.pack(side="right", padx=18, pady=12)
    status_dot = ctk.CTkLabel(status_pill, text="●", font=ctk.CTkFont(size=10),
                              text_color=YELLOW)
    status_dot.pack(side="left", padx=(10, 4), pady=4)
    status_lbl = ctk.CTkLabel(status_pill, text="IDLE",
                              font=ctk.CTkFont(size=11, weight="bold"),
                              text_color=YELLOW)
    status_lbl.pack(side="left", padx=(0, 12), pady=4)

    # Clock
    clock_lbl = ctk.CTkLabel(top, text="", font=ctk.CTkFont(size=12),
                             text_color=MUTED)
    clock_lbl.pack(side="right", padx=8)

    def tick_clock():
        clock_lbl.configure(text=datetime.now().strftime("%H:%M:%S"))
        app.after(1000, tick_clock)
    tick_clock()

    # ════════════════════════════════════════════
    #  MAIN BODY
    # ════════════════════════════════════════════
    body = ctk.CTkFrame(app, fg_color=BG, corner_radius=0)
    body.pack(fill="both", expand=True)

    # ── LEFT SIDEBAR ─────────────────────────────
    sidebar = ctk.CTkFrame(body, fg_color=SIDEBAR, width=200, corner_radius=0)
    sidebar.pack(side="left", fill="y")
    sidebar.pack_propagate(False)

    nav_btns = {}
    pages = {}

    def switch_page(name):
        state["page"] = name
        for n, fr in pages.items():
            fr.pack_forget()
        pages[name].pack(fill="both", expand=True, padx=12, pady=12)
        for n, btn in nav_btns.items():
            if n == name:
                btn.configure(fg_color=CARD, text_color=ACCENT,
                              border_color=ACCENT, border_width=1)
            else:
                btn.configure(fg_color="transparent", text_color=MUTED,
                              border_width=0)

    nav_items = [
        ("dashboard", "◈  DASHBOARD"),
        ("tasks",     "▣  TASK GROUPS"),
        ("accounts",  "◉  ACCOUNTS"),
        ("proxies",   "◎  PROXIES"),
        ("restock",   "↻  RESTOCK"),
        ("settings",  "⚙  SETTINGS"),
        ("logs",      "☰  LIVE LOG"),
    ]

    ctk.CTkLabel(sidebar, text="NAVIGATION",
                 font=ctk.CTkFont(size=9, weight="bold"),
                 text_color=DIM).pack(anchor="w", padx=16, pady=(18, 6))

    for key, label in nav_items:
        b = ctk.CTkButton(
            sidebar, text=label, anchor="w",
            fg_color="transparent", text_color=MUTED,
            hover_color=CARD, height=38, corner_radius=8,
            font=ctk.CTkFont(size=13),
            command=lambda k=key: switch_page(k))
        b.pack(fill="x", padx=10, pady=2)
        nav_btns[key] = b

    # Sidebar footer stats
    side_stats = ctk.CTkFrame(sidebar, fg_color=CARD, corner_radius=10)
    side_stats.pack(side="bottom", fill="x", padx=12, pady=16)
    groups_stat = ctk.CTkLabel(side_stats, text="0 groups",
                               font=ctk.CTkFont(size=11), text_color=MUTED)
    groups_stat.pack(pady=(8, 2))
    acc_stat = ctk.CTkLabel(side_stats, text="0 accounts",
                            font=ctk.CTkFont(size=11), text_color=MUTED)
    acc_stat.pack(pady=(0, 8))

    def refresh_side_stats():
        groups_stat.configure(text=f"{len(state['groups'])} groups")
        accs = load_accounts()
        healthy = sum(1 for a in accs if a.get("status", "ok") in ("ok", "otp_warn"))
        acc_stat.configure(text=f"{healthy}/{len(accs)} healthy")

    # ── CONTENT AREA ─────────────────────────────
    content = ctk.CTkFrame(body, fg_color=BG, corner_radius=0)
    content.pack(side="left", fill="both", expand=True)

    # ════════════════════════════════════════════
    #  PAGE: DASHBOARD
    # ════════════════════════════════════════════
    page_dash = ctk.CTkFrame(content, fg_color=BG)
    pages["dashboard"] = page_dash

    # Hero row
    hero = ctk.CTkFrame(page_dash, fg_color="transparent")
    hero.pack(fill="x", pady=(0, 12))

    ctk.CTkLabel(hero, text="COMMAND CENTER",
                 font=ctk.CTkFont(size=20, weight="bold"),
                 text_color=TEXT).pack(side="left")

    # Quick action buttons
    qa = ctk.CTkFrame(hero, fg_color="transparent")
    qa.pack(side="right")

    def start_bot():
        if state["running"]:
            return
        state["running"] = True
        state["stopref"] = [False]
        set_status("RUNNING", GREEN)
        log("⚡ Engine starting — all enabled groups")

        def threader():
            try:
                async def main():
                    await run_all_groups(state["groups"], log, state["stopref"])
                asyncio.run(main())
            except Exception as e:
                log(f"❌ Engine error: {e}")
            finally:
                state["running"] = False
                set_status("IDLE", YELLOW)

        threading.Thread(target=threader, daemon=True).start()

    def stop_bot():
        if state.get("stopref"):
            state["stopref"][0] = True
        set_status("STOPPING…", ORANGE)
        log("⏹ Stop signal sent")

    def start_restock():
        if state["running"]:
            return
        state["running"] = True
        state["stopref"] = [False]
        set_status("RESTOCK", ACCENT2)
        log("♻️ Restock monitor launching…")

        def threader():
            try:
                async def main():
                    await run_restock(log, state["stopref"])
                asyncio.run(main())
            except Exception as e:
                log(f"❌ Restock error: {e}")
            finally:
                state["running"] = False
                set_status("IDLE", YELLOW)

        threading.Thread(target=threader, daemon=True).start()

    neon_btn(qa, "▶  RUN ALL", GREEN, start_bot, width=120).pack(side="left", padx=4)
    neon_btn(qa, "⏹  STOP", RED, stop_bot, width=100).pack(side="left", padx=4)
    neon_btn(qa, "↻  RESTOCK", ACCENT2, start_restock, width=120).pack(side="left", padx=4)

    # Metric cards row
    metrics = ctk.CTkFrame(page_dash, fg_color="transparent")
    metrics.pack(fill="x", pady=(0, 12))

    def metric_card(parent, title, value, color):
        f = card(parent)
        f.pack(side="left", fill="x", expand=True, padx=4)
        ctk.CTkLabel(f, text=title, font=ctk.CTkFont(size=10, weight="bold"),
                     text_color=MUTED).pack(anchor="w", padx=14, pady=(12, 0))
        lbl = ctk.CTkLabel(f, text=value, font=ctk.CTkFont(size=26, weight="bold"),
                           text_color=color)
        lbl.pack(anchor="w", padx=14, pady=(2, 12))
        return lbl

    m_groups = metric_card(metrics, "TASK GROUPS", "0", ACCENT)
    m_tasks  = metric_card(metrics, "ACTIVE TASKS", "0", GREEN)
    m_carts  = metric_card(metrics, "CARTED", "0", ACCENT3)
    m_fails  = metric_card(metrics, "FAILS", "0", RED)

    # Live task board
    board_card = card(page_dash)
    board_card.pack(fill="both", expand=True)

    board_header = ctk.CTkFrame(board_card, fg_color="transparent")
    board_header.pack(fill="x", padx=14, pady=(12, 4))
    ctk.CTkLabel(board_header, text="◈ LIVE TASK BOARD",
                 font=ctk.CTkFont(size=13, weight="bold"),
                 text_color=ACCENT).pack(side="left")

    # Column headers
    cols_frame = ctk.CTkFrame(board_card, fg_color="transparent")
    cols_frame.pack(fill="x", padx=14, pady=(4, 0))
    for col, w in [("TASK", 80), ("GROUP", 120), ("ACCOUNT", 160),
                   ("STATE", 100), ("QUEUE", 80), ("ERRORS", 60), ("TIME", 70)]:
        ctk.CTkLabel(cols_frame, text=col, width=w, anchor="w",
                     font=ctk.CTkFont(size=10, weight="bold"),
                     text_color=DIM).pack(side="left", padx=2)

    ctk.CTkFrame(board_card, height=1, fg_color=BORDER).pack(fill="x", padx=14, pady=4)

    board_scroll = ctk.CTkScrollableFrame(board_card, fg_color="transparent",
                                          height=280)
    board_scroll.pack(fill="both", expand=True, padx=8, pady=(0, 8))

    board_rows = {}

    def refresh_board(snap=None):
        snap = snap or {}
        # Clear old
        for w in board_scroll.winfo_children():
            w.destroy()
        board_rows.clear()

        if not snap:
            ctk.CTkLabel(board_scroll, text="No active tasks — hit RUN ALL to launch",
                         font=ctk.CTkFont(size=12), text_color=DIM).pack(pady=40)
            m_tasks.configure(text="0")
            return

        carts = fails = 0
        for tid, t in snap.items():
            row = ctk.CTkFrame(board_scroll, fg_color=CARD_HOVER, corner_radius=8,
                               height=36)
            row.pack(fill="x", pady=2, padx=2)
            row.pack_propagate(False)

            st = t.get("state", "?")
            st_color = {
                "starting": MUTED, "queue": ACCENT, "otp": YELLOW,
                "hunting": ACCENT2, "captcha": ORANGE, "carted": GREEN,
                "holding": GREEN, "done": GREEN, "failed": RED,
            }.get(st, TEXT)

            if st in ("carted", "holding", "done"):
                carts += 1
            if st == "failed":
                fails += 1

            elapsed = int(time.time() - t.get("started", time.time()))
            qpos = t.get("queue_pos")
            qtxt = f"#{qpos}" if qpos is not None else "—"
            if t.get("queue_pct") is not None:
                qtxt += f" {t['queue_pct']}%"

            vals = [
                (tid[:8], 80, MUTED),
                (t.get("group", "?")[:14], 120, TEXT),
                (t.get("account", "?")[:22], 160, MUTED),
                (st.upper(), 100, st_color),
                (qtxt, 80, ACCENT),
                (str(len(t.get("errors", []))), 60, RED if t.get("errors") else MUTED),
                (f"{elapsed}s", 70, MUTED),
            ]
            for txt, w, col in vals:
                ctk.CTkLabel(row, text=txt, width=w, anchor="w",
                             font=ctk.CTkFont(size=11), text_color=col).pack(
                    side="left", padx=2, pady=6)

        m_tasks.configure(text=str(len(snap)))
        m_carts.configure(text=str(carts))
        m_fails.configure(text=str(fails))

    # Hook STATUS board
    STATUS.on_change(lambda snap: app.after(0, lambda: refresh_board(snap)))

    # ════════════════════════════════════════════
    #  PAGE: TASK GROUPS
    # ════════════════════════════════════════════
    page_tasks = ctk.CTkFrame(content, fg_color=BG)
    pages["tasks"] = page_tasks

    tasks_top = ctk.CTkFrame(page_tasks, fg_color="transparent")
    tasks_top.pack(fill="x", pady=(0, 10))
    ctk.CTkLabel(tasks_top, text="TASK GROUPS",
                 font=ctk.CTkFont(size=20, weight="bold"),
                 text_color=TEXT).pack(side="left")
    neon_btn(tasks_top, "+  NEW GROUP", ACCENT, lambda: add_group(), width=130).pack(side="right", padx=4)
    neon_btn(tasks_top, "💾 SAVE ALL", ACCENT2, lambda: save_all_groups(), width=120).pack(side="right", padx=4)

    # Split: list + editor
    tasks_split = ctk.CTkFrame(page_tasks, fg_color="transparent")
    tasks_split.pack(fill="both", expand=True)

    # Left list
    list_card = card(tasks_split, width=280)
    list_card.pack(side="left", fill="y", padx=(0, 8))
    list_card.pack_propagate(False)

    ctk.CTkLabel(list_card, text="GROUPS",
                 font=ctk.CTkFont(size=11, weight="bold"),
                 text_color=MUTED).pack(anchor="w", padx=12, pady=(12, 4))

    group_list = ctk.CTkScrollableFrame(list_card, fg_color="transparent")
    group_list.pack(fill="both", expand=True, padx=6, pady=6)

    # Right editor
    edit_card = card(tasks_split)
    edit_card.pack(side="left", fill="both", expand=True)

    edit_title = ctk.CTkLabel(edit_card, text="Select a group to edit",
                              font=ctk.CTkFont(size=14, weight="bold"),
                              text_color=MUTED)
    edit_title.pack(anchor="w", padx=16, pady=(14, 8))

    edit_scroll = ctk.CTkScrollableFrame(edit_card, fg_color="transparent")
    edit_scroll.pack(fill="both", expand=True, padx=8, pady=(0, 8))

    # Form vars
    form = {}
    form_widgets = {}

    def clear_edit():
        for w in edit_scroll.winfo_children():
            w.destroy()
        form.clear()
        form_widgets.clear()
        edit_title.configure(text="Select a group to edit", text_color=MUTED)

    def make_entry(parent, key, label, value="", width=320):
        field_label(parent, label).pack(anchor="w", padx=8, pady=(8, 0))
        e = ctk.CTkEntry(parent, width=width, height=32,
                         fg_color=BG, border_color=BORDER, border_width=1,
                         text_color=TEXT, corner_radius=8)
        e.insert(0, str(value))
        e.pack(anchor="w", padx=8, pady=(2, 0))
        form[key] = e
        return e

    def make_switch(parent, key, label, value=True):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=8, pady=6)
        ctk.CTkLabel(row, text=label, font=ctk.CTkFont(size=12),
                     text_color=TEXT).pack(side="left")
        var = BooleanVar(value=value)
        sw = ctk.CTkSwitch(row, text="", variable=var,
                           progress_color=ACCENT, button_color=TEXT,
                           fg_color=DIM)
        sw.pack(side="right")
        form[key] = var
        return var

    def show_edit(g):
        clear_edit()
        state["selected"] = g
        edit_title.configure(text=f"Editing  ·  {g.name}", text_color=ACCENT)

        # Two-column layout helper
        grid = ctk.CTkFrame(edit_scroll, fg_color="transparent")
        grid.pack(fill="x")
        left = ctk.CTkFrame(grid, fg_color="transparent")
        left.pack(side="left", fill="both", expand=True)
        right = ctk.CTkFrame(grid, fg_color="transparent")
        right.pack(side="left", fill="both", expand=True)

        make_entry(left, "name", "GROUP NAME", g.name)
        make_entry(left, "event_url", "EVENT URL", g.event_url, width=360)
        make_entry(left, "quantity", "QUANTITY", g.quantity, width=100)
        make_entry(left, "min_price", "MIN PRICE", g.min_price, width=100)
        make_entry(left, "max_price", "MAX PRICE", g.max_price, width=100)
        make_entry(left, "max_tasks", "MAX PARALLEL TASKS", g.max_tasks, width=100)

        make_entry(right, "account_group", "ACCOUNT POOL", g.account_group)
        make_entry(right, "proxy_group", "PROXY GROUP", g.proxy_group)
        make_entry(right, "section_mode", "SECTION MODE (include/exclude)", g.section_mode)
        make_entry(right, "sections", "SECTIONS (comma-sep)",
                   ", ".join(g.sections) if g.sections else "")
        make_entry(right, "presale_codes", "PRESALE CODES (comma-sep)",
                   ", ".join(g.presale_codes) if g.presale_codes else "")

        make_switch(edit_scroll, "contiguous", "Require contiguous seats", g.contiguous)
        make_switch(edit_scroll, "enabled", "Group enabled", g.enabled)

        # Action row
        act = ctk.CTkFrame(edit_scroll, fg_color="transparent")
        act.pack(fill="x", padx=8, pady=16)
        neon_btn(act, "💾  SAVE GROUP", GREEN, lambda: save_group(g), width=140).pack(side="left", padx=4)
        neon_btn(act, "🗑  DELETE", RED, lambda: delete_group(g), width=110).pack(side="left", padx=4)
        neon_btn(act, "▶  RUN THIS", ACCENT, lambda: run_single(g), width=120).pack(side="left", padx=4)

    def save_group(g):
        try:
            g.name = form["name"].get().strip() or g.name
            g.event_url = form["event_url"].get().strip()
            g.quantity = int(form["quantity"].get() or 2)
            g.min_price = float(form["min_price"].get() or 0)
            g.max_price = float(form["max_price"].get() or 250)
            g.max_tasks = int(form["max_tasks"].get() or 2)
            g.account_group = form["account_group"].get().strip() or "default"
            g.proxy_group = form["proxy_group"].get().strip() or "any"
            g.section_mode = form["section_mode"].get().strip() or "include"
            secs = form["sections"].get().strip()
            g.sections = [s.strip() for s in secs.split(",") if s.strip()] if secs else []
            codes = form["presale_codes"].get().strip()
            g.presale_codes = [c.strip() for c in codes.split(",") if c.strip()] if codes else []
            g.contiguous = form["contiguous"].get()
            g.enabled = form["enabled"].get()
            save_groups(state["groups"])
            refresh_group_list()
            refresh_side_stats()
            m_groups.configure(text=str(len(state["groups"])))
            log(f"💾 Saved group [{g.name}]")
            edit_title.configure(text=f"Saved  ·  {g.name}", text_color=GREEN)
        except Exception as e:
            log(f"❌ Save error: {e}")

    def delete_group(g):
        state["groups"] = [x for x in state["groups"] if x.id != g.id]
        save_groups(state["groups"])
        clear_edit()
        refresh_group_list()
        refresh_side_stats()
        m_groups.configure(text=str(len(state["groups"])))
        log(f"🗑 Deleted group [{g.name}]")

    def save_all_groups():
        save_groups(state["groups"])
        log("💾 All groups persisted")

    def run_single(g):
        if state["running"]:
            return
        state["running"] = True
        state["stopref"] = [False]
        set_status("RUNNING", GREEN)
        log(f"▶ Launching single group [{g.name}]")

        def threader():
            try:
                async def main():
                    pm = ProxyManager()
                    engine = BrowserEngine(pm, CONFIG["general"]["headless"],
                                          CONFIG["general"].get("hybrid_mode", True))
                    solver = CaptchaSolver()
                    notifier = DiscordNotifier(CONFIG["discord"]["webhook_url"])
                    await engine.start()
                    try:
                        await run_group(g, engine, solver, notifier, log, state["stopref"])
                    finally:
                        await engine.stop()
                asyncio.run(main())
            except Exception as e:
                log(f"❌ {e}")
            finally:
                state["running"] = False
                set_status("IDLE", YELLOW)

        threading.Thread(target=threader, daemon=True).start()

    def add_group():
        g = TaskGroup(id=uuid.uuid4().hex[:8], name=f"Group {len(state['groups'])+1}",
                      event_url="https://www.ticketmaster.com/event/")
        state["groups"].append(g)
        save_groups(state["groups"])
        refresh_group_list()
        refresh_side_stats()
        m_groups.configure(text=str(len(state["groups"])))
        show_edit(g)
        log(f"+ Created [{g.name}]")

    def refresh_group_list():
        for w in group_list.winfo_children():
            w.destroy()
        for g in state["groups"]:
            row = ctk.CTkFrame(group_list, fg_color=CARD_HOVER if state.get("selected") and state["selected"].id == g.id else "transparent",
                               corner_radius=8, height=42)
            row.pack(fill="x", pady=2)
            row.pack_propagate(False)

            icon = "🟢" if g.enabled else "⚪"
            ctk.CTkLabel(row, text=f"{icon}  {g.name}",
                         font=ctk.CTkFont(size=12), text_color=TEXT,
                         anchor="w").pack(side="left", padx=10, pady=8)
            ctk.CTkLabel(row, text=f"×{g.quantity}",
                         font=ctk.CTkFont(size=10), text_color=MUTED).pack(side="right", padx=10)

            row.bind("<Button-1>", lambda e, gg=g: show_edit(gg))
            for child in row.winfo_children():
                child.bind("<Button-1>", lambda e, gg=g: show_edit(gg))

    # ════════════════════════════════════════════
    #  PAGE: ACCOUNTS
    # ════════════════════════════════════════════
    page_acc = ctk.CTkFrame(content, fg_color=BG)
    pages["accounts"] = page_acc

    acc_top = ctk.CTkFrame(page_acc, fg_color="transparent")
    acc_top.pack(fill="x", pady=(0, 10))
    ctk.CTkLabel(acc_top, text="ACCOUNTS",
                 font=ctk.CTkFont(size=20, weight="bold"),
                 text_color=TEXT).pack(side="left")
    neon_btn(acc_top, "+  GENERATE", ACCENT, lambda: gen_account(), width=130).pack(side="right", padx=4)
    neon_btn(acc_top, "↻  REFRESH", DIM, lambda: refresh_accounts(), width=110).pack(side="right", padx=4)

    acc_scroll = ctk.CTkScrollableFrame(page_acc, fg_color=CARD, corner_radius=12)
    acc_scroll.pack(fill="both", expand=True)

    def gen_account():
        gen = AccountGenerator()
        a = gen.create(group="default")
        accs = load_accounts()
        accs.append(a)
        save_accounts(accs)
        refresh_accounts()
        refresh_side_stats()
        log(f"✉ Generated {a['email']}")

    def revive(email):
        revive_account(email)
        refresh_accounts()
        log(f"💚 Revived {email}")

    def refresh_accounts():
        for w in acc_scroll.winfo_children():
            w.destroy()
        accs = load_accounts()
        if not accs:
            ctk.CTkLabel(acc_scroll, text="No accounts yet — hit GENERATE",
                         font=ctk.CTkFont(size=13), text_color=DIM).pack(pady=40)
            return

        # Header
        hdr = ctk.CTkFrame(acc_scroll, fg_color="transparent")
        hdr.pack(fill="x", padx=8, pady=(8, 2))
        for col, w in [("EMAIL", 220), ("GROUP", 80), ("STATUS", 90),
                       ("OTP FAILS", 70), ("SESSION", 70), ("ACTIONS", 100)]:
            ctk.CTkLabel(hdr, text=col, width=w, anchor="w",
                         font=ctk.CTkFont(size=10, weight="bold"),
                         text_color=DIM).pack(side="left", padx=2)
        ctk.CTkFrame(acc_scroll, height=1, fg_color=BORDER).pack(fill="x", padx=8, pady=4)

        for a in accs:
            row = ctk.CTkFrame(acc_scroll, fg_color=CARD_HOVER, corner_radius=8, height=38)
            row.pack(fill="x", padx=4, pady=2)
            row.pack_propagate(False)
            st = a.get("status", "ok")
            st_col = {"ok": GREEN, "otp_warn": YELLOW, "dead_otp": RED, "dead": RED}.get(st, MUTED)
            has_sess = "⚡" if SessionVault.has(a.get("email", "")) else "—"

            vals = [
                (a.get("email", "?")[:28], 220, TEXT),
                (a.get("group", "default"), 80, MUTED),
                (st.upper(), 90, st_col),
                (str(a.get("otp_fails", 0)), 70, MUTED),
                (has_sess, 70, ACCENT if has_sess == "⚡" else MUTED),
            ]
            for txt, w, col in vals:
                ctk.CTkLabel(row, text=txt, width=w, anchor="w",
                             font=ctk.CTkFont(size=11), text_color=col).pack(
                    side="left", padx=2, pady=6)
            if st in ("dead_otp", "dead", "otp_warn"):
                neon_btn(row, "REVIVE", GREEN,
                         lambda e=a.get("email"): revive(e),
                         width=70, height=26).pack(side="left", padx=4)

    # ════════════════════════════════════════════
    #  PAGE: PROXIES
    # ════════════════════════════════════════════
    page_proxy = ctk.CTkFrame(content, fg_color=BG)
    pages["proxies"] = page_proxy

    px_top = ctk.CTkFrame(page_proxy, fg_color="transparent")
    px_top.pack(fill="x", pady=(0, 10))
    ctk.CTkLabel(px_top, text="PROXIES",
                 font=ctk.CTkFont(size=20, weight="bold"),
                 text_color=TEXT).pack(side="left")
    neon_btn(px_top, "💾 SAVE", GREEN, lambda: save_proxies(), width=100).pack(side="right", padx=4)

    px_info = ctk.CTkLabel(page_proxy,
                           text="One proxy per line  ·  host:port  or  user:pass@host:port",
                           font=ctk.CTkFont(size=11), text_color=MUTED)
    px_info.pack(anchor="w", pady=(0, 6))

    proxy_box = ctk.CTkTextbox(page_proxy, fg_color=CARD, text_color=TEXT,
                               font=ctk.CTkFont(family="Consolas", size=12),
                               corner_radius=12, border_width=1, border_color=BORDER)
    proxy_box.pack(fill="both", expand=True)

    def load_proxies_ui():
        proxy_box.delete("1.0", TKEND)
        if PROXIES_FILE.exists():
            proxy_box.insert("1.0", PROXIES_FILE.read_text())

    def save_proxies():
        text = proxy_box.get("1.0", TKEND).strip()
        PROXIES_FILE.write_text(text + "\n" if text else "")
        n = len([l for l in text.splitlines() if l.strip()])
        log(f"◎ Saved {n} proxies")

    # ════════════════════════════════════════════
    #  PAGE: RESTOCK
    # ════════════════════════════════════════════
    page_rs = ctk.CTkFrame(content, fg_color=BG)
    pages["restock"] = page_rs

    rs_top = ctk.CTkFrame(page_rs, fg_color="transparent")
    rs_top.pack(fill="x", pady=(0, 10))
    ctk.CTkLabel(rs_top, text="RESTOCK MONITOR",
                 font=ctk.CTkFont(size=20, weight="bold"),
                 text_color=TEXT).pack(side="left")
    neon_btn(rs_top, "💾 SAVE", GREEN, lambda: save_restock(), width=100).pack(side="right", padx=4)
    neon_btn(rs_top, "▶ START", ACCENT2, start_restock, width=110).pack(side="right", padx=4)

    rs_info = ctk.CTkLabel(
        page_rs,
        text="Format per line:  URL :: qty :: min_price :: max_price :: sections :: codes :: proxy_group",
        font=ctk.CTkFont(size=11), text_color=MUTED)
    rs_info.pack(anchor="w", pady=(0, 6))

    rs_box = ctk.CTkTextbox(page_rs, fg_color=CARD, text_color=TEXT,
                            font=ctk.CTkFont(family="Consolas", size=12),
                            corner_radius=12, border_width=1, border_color=BORDER)
    rs_box.pack(fill="both", expand=True)

    # Restock options row
    rs_opts = ctk.CTkFrame(page_rs, fg_color=CARD, corner_radius=10)
    rs_opts.pack(fill="x", pady=(10, 0))

    rs_auto = BooleanVar(value=CONFIG["restock"].get("auto_cart", True))
    ctk.CTkSwitch(rs_opts, text="Auto-cart on restock", variable=rs_auto,
                  progress_color=ACCENT, button_color=TEXT, fg_color=DIM,
                  text_color=TEXT).pack(side="left", padx=14, pady=10)

    field_label(rs_opts, "Poll interval (s)").pack(side="left", padx=(20, 4))
    rs_poll = ctk.CTkEntry(rs_opts, width=70, height=28, fg_color=BG,
                           border_color=BORDER, text_color=TEXT)
    rs_poll.insert(0, str(CONFIG["restock"].get("poll_interval_sec", 180)))
    rs_poll.pack(side="left", pady=10)

    def load_restock_ui():
        rs_box.delete("1.0", TKEND)
        events = CONFIG["restock"].get("events", [])
        rs_box.insert("1.0", "\n".join(events))

    def save_restock():
        text = rs_box.get("1.0", TKEND).strip()
        CONFIG["restock"]["events"] = [l.strip() for l in text.splitlines() if l.strip()]
        CONFIG["restock"]["auto_cart"] = rs_auto.get()
        try:
            CONFIG["restock"]["poll_interval_sec"] = int(rs_poll.get())
        except ValueError:
            pass
        jsave(CONFIG_FILE, CONFIG)
        log(f"↻ Saved {len(CONFIG['restock']['events'])} restock event(s)")

    # ════════════════════════════════════════════
    #  PAGE: SETTINGS
    # ════════════════════════════════════════════
    page_set = ctk.CTkFrame(content, fg_color=BG)
    pages["settings"] = page_set

    ctk.CTkLabel(page_set, text="SETTINGS",
                 font=ctk.CTkFont(size=20, weight="bold"),
                 text_color=TEXT).pack(anchor="w", pady=(0, 12))

    set_scroll = ctk.CTkScrollableFrame(page_set, fg_color="transparent")
    set_scroll.pack(fill="both", expand=True)

    set_vars = {}

    def settings_section(title):
        ctk.CTkLabel(set_scroll, text=title,
                     font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=ACCENT).pack(anchor="w", pady=(14, 4), padx=4)
        f = card(set_scroll)
        f.pack(fill="x", pady=2)
        return f

    # Discord
    disc = settings_section("DISCORD")
    field_label(disc, "Webhook URL").pack(anchor="w", padx=12, pady=(10, 0))
    set_vars["webhook"] = ctk.CTkEntry(disc, height=32, fg_color=BG,
                                       border_color=BORDER, text_color=TEXT)
    set_vars["webhook"].insert(0, CONFIG["discord"].get("webhook_url", ""))
    set_vars["webhook"].pack(fill="x", padx=12, pady=4)
    field_label(disc, "Mention User ID").pack(anchor="w", padx=12, pady=(6, 0))
    set_vars["mention"] = ctk.CTkEntry(disc, height=32, fg_color=BG,
                                       border_color=BORDER, text_color=TEXT, width=200)
    set_vars["mention"].insert(0, CONFIG["discord"].get("mention_user_id", ""))
    set_vars["mention"].pack(anchor="w", padx=12, pady=(2, 10))

    # Captcha
    cap = settings_section("CAPTCHA  ·  CAPSOLVER")
    field_label(cap, "API Key").pack(anchor="w", padx=12, pady=(10, 0))
    set_vars["capkey"] = ctk.CTkEntry(cap, height=32, fg_color=BG,
                                      border_color=BORDER, text_color=TEXT, show="•")
    set_vars["capkey"].insert(0, CONFIG["captcha"].get("api_key", ""))
    set_vars["capkey"].pack(fill="x", padx=12, pady=(2, 10))

    # AI
    ai_f = settings_section("AI BRAIN")
    field_label(ai_f, "Provider (openai / openrouter / groq / ollama)").pack(anchor="w", padx=12, pady=(10, 0))
    set_vars["aiprov"] = ctk.CTkEntry(ai_f, height=32, fg_color=BG,
                                      border_color=BORDER, text_color=TEXT, width=160)
    set_vars["aiprov"].insert(0, CONFIG["ai"].get("provider", "openai"))
    set_vars["aiprov"].pack(anchor="w", padx=12, pady=2)
    field_label(ai_f, "API Key").pack(anchor="w", padx=12, pady=(6, 0))
    set_vars["aikey"] = ctk.CTkEntry(ai_f, height=32, fg_color=BG,
                                     border_color=BORDER, text_color=TEXT, show="•")
    set_vars["aikey"].insert(0, CONFIG["ai"].get("api_key", ""))
    set_vars["aikey"].pack(fill="x", padx=12, pady=2)
    field_label(ai_f, "Model").pack(anchor="w", padx=12, pady=(6, 0))
    set_vars["aimodel"] = ctk.CTkEntry(ai_f, height=32, fg_color=BG,
                                       border_color=BORDER, text_color=TEXT, width=200)
    set_vars["aimodel"].insert(0, CONFIG["ai"].get("model", "gpt-4o-mini"))
    set_vars["aimodel"].pack(anchor="w", padx=12, pady=(2, 10))

    # IMAP
    imap_f = settings_section("IMAP  ·  OTP CATCH-ALL")
    field_label(imap_f, "Email").pack(anchor="w", padx=12, pady=(10, 0))
    set_vars["imap_email"] = ctk.CTkEntry(imap_f, height=32, fg_color=BG,
                                          border_color=BORDER, text_color=TEXT)
    set_vars["imap_email"].insert(0, CONFIG["imap"].get("email", ""))
    set_vars["imap_email"].pack(fill="x", padx=12, pady=2)
    field_label(imap_f, "Password / App Password").pack(anchor="w", padx=12, pady=(6, 0))
    set_vars["imap_pass"] = ctk.CTkEntry(imap_f, height=32, fg_color=BG,
                                         border_color=BORDER, text_color=TEXT, show="•")
    set_vars["imap_pass"].insert(0, CONFIG["imap"].get("password", ""))
    set_vars["imap_pass"].pack(fill="x", padx=12, pady=2)
    field_label(imap_f, "Host").pack(anchor="w", padx=12, pady=(6, 0))
    set_vars["imap_host"] = ctk.CTkEntry(imap_f, height=32, fg_color=BG,
                                         border_color=BORDER, text_color=TEXT, width=200)
    set_vars["imap_host"].insert(0, CONFIG["imap"].get("host", ""))
    set_vars["imap_host"].pack(anchor="w", padx=12, pady=2)
    field_label(imap_f, "Port").pack(anchor="w", padx=12, pady=(6, 0))
    set_vars["imap_port"] = ctk.CTkEntry(imap_f, height=32, fg_color=BG,
                                         border_color=BORDER, text_color=TEXT, width=80)
    set_vars["imap_port"].insert(0, str(CONFIG["imap"].get("port", 993)))
    set_vars["imap_port"].pack(anchor="w", padx=12, pady=(2, 10))

    # General
    gen_f = settings_section("GENERAL")
    set_vars["headless"] = BooleanVar(value=CONFIG["general"].get("headless", True))
    set_vars["hybrid"] = BooleanVar(value=CONFIG["general"].get("hybrid_mode", True))
    row_g = ctk.CTkFrame(gen_f, fg_color="transparent")
    row_g.pack(fill="x", padx=12, pady=10)
    ctk.CTkSwitch(row_g, text="Headless", variable=set_vars["headless"],
                  progress_color=ACCENT, button_color=TEXT, fg_color=DIM,
                  text_color=TEXT).pack(side="left", padx=(0, 20))
    ctk.CTkSwitch(row_g, text="Hybrid Mode (visible after queue)",
                  variable=set_vars["hybrid"],
                  progress_color=ACCENT, button_color=TEXT, fg_color=DIM,
                  text_color=TEXT).pack(side="left")

    field_label(gen_f, "Keep browser open (minutes)").pack(anchor="w", padx=12, pady=(4, 0))
    set_vars["keep_min"] = ctk.CTkEntry(gen_f, height=32, fg_color=BG,
                                        border_color=BORDER, text_color=TEXT, width=80)
    set_vars["keep_min"].insert(0, str(CONFIG["general"].get("keep_browser_open_min", 15)))
    set_vars["keep_min"].pack(anchor="w", padx=12, pady=(2, 12))

    def save_settings():
        CONFIG["discord"]["webhook_url"] = set_vars["webhook"].get().strip()
        CONFIG["discord"]["mention_user_id"] = set_vars["mention"].get().strip()
        CONFIG["captcha"]["api_key"] = set_vars["capkey"].get().strip()
        CONFIG["ai"]["provider"] = set_vars["aiprov"].get().strip()
        CONFIG["ai"]["api_key"] = set_vars["aikey"].get().strip()
        CONFIG["ai"]["model"] = set_vars["aimodel"].get().strip()
        CONFIG["imap"]["email"] = set_vars["imap_email"].get().strip()
        CONFIG["imap"]["password"] = set_vars["imap_pass"].get().strip()
        CONFIG["imap"]["host"] = set_vars["imap_host"].get().strip()
        try:
            CONFIG["imap"]["port"] = int(set_vars["imap_port"].get())
        except ValueError:
            pass
        CONFIG["general"]["headless"] = set_vars["headless"].get()
        CONFIG["general"]["hybrid_mode"] = set_vars["hybrid"].get()
        try:
            CONFIG["general"]["keep_browser_open_min"] = int(set_vars["keep_min"].get())
        except ValueError:
            pass
        jsave(CONFIG_FILE, CONFIG)
        # Re-init AI brain with new key
        global AI
        AI = AIBrain()
        log("⚙ Settings saved")

    def test_discord():
        n = DiscordNotifier(set_vars["webhook"].get().strip())
        ok, msg = n.test()
        log(f"{'✅' if ok else '❌'} Discord: {msg}")

    def test_imap():
        cfg = {
            "email": set_vars["imap_email"].get().strip(),
            "password": set_vars["imap_pass"].get().strip(),
            "host": set_vars["imap_host"].get().strip(),
            "port": int(set_vars["imap_port"].get() or 993),
            "ssl": True,
        }
        fisher = IMAPFisher(cfg)
        ok, msg = fisher.test_connection()
        log(f"{'✅' if ok else '❌'} IMAP: {msg}")

    set_act = ctk.CTkFrame(set_scroll, fg_color="transparent")
    set_act.pack(fill="x", pady=16)
    neon_btn(set_act, "💾  SAVE SETTINGS", GREEN, save_settings, width=160).pack(side="left", padx=4)
    neon_btn(set_act, "🔔  TEST DISCORD", ACCENT, test_discord, width=150).pack(side="left", padx=4)
    neon_btn(set_act, "✉  TEST IMAP", ACCENT2, test_imap, width=130).pack(side="left", padx=4)

    # ════════════════════════════════════════════
    #  PAGE: LIVE LOG
    # ════════════════════════════════════════════
    page_log = ctk.CTkFrame(content, fg_color=BG)
    pages["logs"] = page_log

    log_top = ctk.CTkFrame(page_log, fg_color="transparent")
    log_top.pack(fill="x", pady=(0, 8))
    ctk.CTkLabel(log_top, text="LIVE LOG",
                 font=ctk.CTkFont(size=20, weight="bold"),
                 text_color=TEXT).pack(side="left")
    neon_btn(log_top, "CLR", DIM, lambda: clear_log(), width=60, height=28).pack(side="right")

    log_box = ctk.CTkTextbox(page_log, fg_color=CARD, text_color=TEXT,
                             font=ctk.CTkFont(family="Consolas", size=12),
                             corner_radius=12, border_width=1, border_color=BORDER,
                             state="disabled")
    log_box.pack(fill="both", expand=True)

    def log(msg):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}]  {msg}"
        print(line)
        state["log_lines"].append(line)
        if len(state["log_lines"]) > 500:
            state["log_lines"] = state["log_lines"][-400:]
        try:
            log_box.configure(state="normal")
            log_box.insert(TKEND, line + "\n")
            log_box.see(TKEND)
            log_box.configure(state="disabled")
        except Exception:
            pass

    def clear_log():
        state["log_lines"].clear()
        log_box.configure(state="normal")
        log_box.delete("1.0", TKEND)
        log_box.configure(state="disabled")

    # ── STATUS HELPER ────────────────────────────
    def set_status(text, color):
        status_lbl.configure(text=text, text_color=color)
        status_dot.configure(text_color=color)
        status_pill.configure(border_color=color)

    # ── INIT ─────────────────────────────────────
    refresh_group_list()
    refresh_accounts()
    load_proxies_ui()
    load_restock_ui()
    refresh_side_stats()
    m_groups.configure(text=str(len(state["groups"])))
    refresh_board()
    switch_page("dashboard")
    log("⚡ Karma Ultra online — Neon Core ready")

    app.mainloop()


if __name__ == "__main__":
    print("⚡ Karma Ultra — Neon Core GUI")
    run_gui()
