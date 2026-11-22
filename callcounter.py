"""Counts this process's Airtable REST calls (what Airtable bills as "API calls").

Wraps requests.Session.request, so EVERY call to api.airtable.com / content.airtable.com is
counted - including retries and 429s - without touching the call sites. Counts are kept per
"METHOD table" label, reset at local midnight, and reported to the log every hour and to
Telegram once a day. Counters live in memory: a redeploy/restart starts them from zero
(the report says since when).

Usage (once, at process start):
    import callcounter
    callcounter.install("gmail_sync")
    callcounter.start_reporter(default_chat="")        # optional; log + Telegram report

Env:
  USAGE_TELEGRAM          "false" -> log only, no Telegram report            default true
  USAGE_REPORT_AT         local HH:MM of the daily Telegram report           default 18:05
  USAGE_LOG_SEC           seconds between log summaries                      default 3600
  USAGE_TELEGRAM_CHAT_ID  chat for the report (else TELEGRAM_CHAT_ID)
  TELEGRAM_BOT_TOKEN      bot used for the report
  WORK_TZ_OFFSET_HOURS    offset from UTC for "today"                        default 3
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests

log = logging.getLogger("callcounter")

TZ = timezone(timedelta(hours=float(os.getenv("WORK_TZ_OFFSET_HOURS", "3"))))
AIRTABLE_HOSTS = {"api.airtable.com", "content.airtable.com"}
TABLE_NAMES = {  # ROSMA Product Catalog base, for readable reports
    "tbl0F4KKFXXaObAHm": "Inquiries", "tblchEJTeS55IoHNv": "Inquired Items",
    "tblAGP1eIx6Oyomq1": "Updates", "tblRRW1btCVX9Yp8F": "Clients",
    "tblMbmrz7NpUl55E0": "Contacts", "tblBt8oxps8dt0YPm": "Звонки",
    "tble38JtpsOBIg9uc": "Employees", "tbl3ltCT3jqHQPtlW": "Goods",
    "tblXy9ryuMDrhAsPJ": "Pellet mill", "tblvBaDIdSi9YY6q8": "Packing List",
    "tblUUtTNA52sf07Dm": "Cases", "tbl7Kzrna7Nz58fb6": "Suppliers",
    "tblO6wVaUwLV9Jljx": "Our company", "tblfU0LK1wAa7XQjW": "Contracts",
}

_lock = threading.Lock()
_service = "service"
_installed = False
_orig = None
_counts: Counter = Counter()      # label -> calls today
_last_hour: Counter = Counter()   # label -> calls since the last log line
_throttled = 0                    # 429 responses today
_day = datetime.now(TZ).date()
_since = datetime.now(TZ)


def _label(method: str, url: str) -> str | None:
    p = urlparse(url)
    if p.hostname not in AIRTABLE_HOSTS:
        return None
    parts = [x for x in p.path.split("/") if x]          # ['v0', base, table, ...]
    verb = method.upper()
    if p.hostname == "content.airtable.com":
        return f"{verb} upload"
    if len(parts) >= 2 and parts[1] == "meta":           # /v0/meta/bases/... (schema calls)
        return f"{verb} meta"
    if len(parts) < 3:
        return f"{verb} other"
    table = parts[2]
    if len(parts) >= 4 and parts[3] == "listRecords":
        verb = "LIST"
    elif verb == "GET" and len(parts) == 3:
        verb = "GET(list)"
    return f"{verb} {TABLE_NAMES.get(table, table)}"


def _roll_day() -> None:
    global _day, _since, _throttled
    today = datetime.now(TZ).date()
    if today != _day:
        _counts.clear()
        _last_hour.clear()
        _throttled = 0
        _day, _since = today, datetime.now(TZ)


def record(method: str, url: str, status: int | None = None) -> None:
    global _throttled
    label = _label(method, url)
    if label is None:
        return
    with _lock:
        _roll_day()
        _counts[label] += 1
        _last_hour[label] += 1
        if status == 429:
            _throttled += 1


def install(service: str) -> None:
    """Start counting every Airtable request made via `requests` in this process."""
    global _service, _installed, _orig
    _service = service
    if _installed:
        return
    _orig = requests.sessions.Session.request

    def wrapped(self, method, url, *a, **kw):
        resp = _orig(self, method, url, *a, **kw)
        try:
            record(method, str(url), resp.status_code)
        except Exception:  # counting must never break a request
            pass
        return resp

    requests.sessions.Session.request = wrapped
    _installed = True
    log.info("Airtable call counter installed for %s", service)


def snapshot() -> dict:
    with _lock:
        _roll_day()
        return {"service": _service, "since": _since, "total": sum(_counts.values()),
                "throttled": _throttled, "by_label": dict(_counts.most_common())}


def summary(top: int = 8) -> str:
    s = snapshot()
    lines = [f"{lbl}: {n}" for lbl, n in list(s["by_label"].items())[:top]]
    head = (f"Airtable API · {s['service']} · {s['since']:%d.%m %H:%M} → сейчас\n"
            f"Всего: {s['total']}" + (f" (из них 429: {s['throttled']})" if s["throttled"] else ""))
    return head + ("\n" + "\n".join(lines) if lines else "")


def _send_telegram(text: str, default_chat: str | None) -> None:
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()   # Railway vars often carry newlines
    chat = (os.getenv("USAGE_TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID") or default_chat or "").strip()
    if not (token and chat):
        return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": "📊 " + text}, timeout=15)
    except Exception as exc:
        log.warning("usage report not sent: %s", exc)


def start_reporter(default_chat: str | None = None, report_at: str | None = None) -> None:
    """Background thread: hourly log line + one Telegram report per day.

    report_at: local "HH:MM" default for this service (env USAGE_REPORT_AT overrides it)."""
    log_every = int(os.getenv("USAGE_LOG_SEC", "3600"))
    telegram = os.getenv("USAGE_TELEGRAM", "true").strip().lower() not in ("0", "false", "no")
    hh, mm = (int(x) for x in (os.getenv("USAGE_REPORT_AT") or report_at or "18:05").strip().split(":"))

    def loop() -> None:
        global _last_hour
        last_log = time.time()
        now = datetime.now(TZ)
        # started after today's report time -> wait for tomorrow's
        reported_on = now.date() if (now.hour, now.minute) >= (hh, mm) else None
        while True:
            time.sleep(30)
            now = datetime.now(TZ)
            if time.time() - last_log >= log_every:
                with _lock:
                    hour_total, _last_hour = sum(_last_hour.values()), Counter()
                s = snapshot()
                log.info("Airtable calls: %d in the last hour, %d today | top: %s", hour_total,
                         s["total"], ", ".join(f"{k}={v}" for k, v in list(s["by_label"].items())[:5]))
                last_log = time.time()
            if telegram and reported_on != now.date() and (now.hour, now.minute) >= (hh, mm):
                _send_telegram(summary(), default_chat)
                reported_on = now.date()

    threading.Thread(target=loop, daemon=True, name="callcounter").start()
