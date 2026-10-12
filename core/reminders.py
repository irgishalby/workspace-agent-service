# core/reminders.py  -  reminder engine. One job, runs every minute.
import asyncio
import logging
from datetime import datetime, timedelta
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from config.settings import ALLOWED_USER_ID
from core.dates import format_due, format_due_full
from tools.notion_tool import events_between, due_custom_reminders, TZ

CHAT_ID = ALLOWED_USER_ID
ONE_DAY = timedelta(days=1)
NINETY = timedelta(minutes=90)
GRACE_1D = timedelta(hours=2)      # late-send window after the 1-day point (bot was off)
GRACE_90 = timedelta(minutes=30)   # late-send window after the 1h30 point
KEEP = 500                         # sent keys kept in pickle


def _now():
    return datetime.now(TZ)


def reminder_kb(page_id: str, kind: str):
    """kind: 't' task (Complete + Snooze) | 'e' event (Snooze only)."""
    snooze = InlineKeyboardButton("⏰ Snooze 1 hr", callback_data=f"snz:{page_id}:60:{kind}")
    if kind == "t":
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Complete", callback_data=f"done:{page_id}:r"), snooze]])
    return InlineKeyboardMarkup([[snooze]])


def _start(e: dict) -> datetime:
    return datetime.fromisoformat(f"{e['date']}T{e['start']}:00").replace(tzinfo=TZ)


def _span(e: dict) -> str:
    return e["start"] + (f"–{e['end']}" if e["end"] else "")


def _tag(e: dict) -> str:
    return f" [{e['book']}]" if e.get("book") else ""


def _in(td: timedelta) -> str:
    m = max(1, int(td.total_seconds() // 60))
    h, mm = divmod(m, 60)
    return f"{h}h{mm:02d}" if h else f"{m}m"


async def _send(context, text, kb=None):
    await context.bot.send_message(CHAT_ID, text, reply_markup=kb)


async def check_reminders(context: ContextTypes.DEFAULT_TYPE):
    now = _now()
    sent = context.bot_data.setdefault("reminded", [])
    seen = set(sent)

    def mark(key):
        sent.append(key)
        seen.add(key)

    # --- events + routines: 1 day before, 1h30 before ---
    try:
        today = now.date()
        for e in await asyncio.to_thread(events_between, today, today + ONE_DAY):
            if not e["start"]:
                continue                      # all-day: no timed reminder
            start = _start(e)
            if start <= now:
                continue
            k1, k2 = f"{e['id']}:{e['date']}:1d", f"{e['id']}:{e['date']}:90m"

            due = start - NINETY
            if now >= due and now - due <= GRACE_90 and k2 not in seen:
                await _send(context, f"⏰ In {_in(start - now)} — {e['name']}\n"
                                     f"{format_due(e['date'])} {_span(e)}{_tag(e)}", reminder_kb(e["id"], "e"))
                mark(k2)

            due = start - ONE_DAY
            if now >= due and now - due <= GRACE_1D and k1 not in seen and start - now > NINETY:
                day = format_due(e["date"])
                await _send(context, f"📅 {day} {_span(e)} — {e['name']}{_tag(e)}",
                            reminder_kb(e["id"], "e"))
                mark(k1)
    except Exception:
        logging.exception("event reminders failed")

    # --- custom 'Remind At' on tasks and events ---
    try:
        for it in await asyncio.to_thread(due_custom_reminders):
            key = f"{it['id']}:c:{it['remind_at']}"
            if key in seen:
                continue
            if it["kind"] == "task":
                due = format_due_full(it["deadline"], it["time"]) if it["deadline"] else "no date"
                text = f"⏰ Reminder — {it['name']}\nDue: {due}{_tag(it)}"
                kb = reminder_kb(it["id"], "t")
            else:
                when = format_due(it["date"]) + (f" {_span(it)}" if it["start"] else "")
                text = f"⏰ Reminder — {it['name']}\n{when}{_tag(it)}"
                kb = reminder_kb(it["id"], "e")
            await _send(context, text, kb)
            mark(key)
    except Exception:
        logging.exception("custom reminders failed")

    del sent[:-KEEP]