# core/summary.py  -  /summary: auto morning digest + on demand + editable times
import asyncio
import logging
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import ContextTypes
from config.settings import ALLOWED_USER_ID
from core.dates import format_due_full
from tools.notion_tool import events_on, list_tasks, _time_input, TZ

CHAT_ID = ALLOWED_USER_ID
DEFAULT_TIMES = {"a": "05:00", "b": "06:30"}       # a = Mon-Wed, b = Thu-Sun
GROUP_LABEL = {"a": "Mon–Wed", "b": "Thu–Sun"}
GRACE = timedelta(hours=3)    # late-send window if the bot was off at summary time
MAX_ROWS = 30


def _now():
    return datetime.now(TZ)


def _btn(text, data):
    return InlineKeyboardButton(text, callback_data=data)


async def _edit(q, text, kb=None):
    try:
        await q.edit_message_text(text, reply_markup=kb)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


async def _strip(context, st):
    if st and st.get("msg"):
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=st["msg"][0], message_id=st["msg"][1], reply_markup=None)
        except Exception:
            pass


def _times(bot_data) -> dict:
    return {**DEFAULT_TIMES, **bot_data.get("summary_times", {})}


# ---------- content ----------
def _section(title, lines):
    if not lines:
        return f"{title}\n• none"
    out = lines[:MAX_ROWS]
    if len(lines) > MAX_ROWS:
        out.append(f"…and {len(lines) - MAX_ROWS} more")
    return title + "\n" + "\n".join(out)


def _event_line(e):
    if e["start"]:
        when = e["start"] + (f"–{e['end']}" if e["end"] else "")
    else:
        when = "All day"
    line = f"• {when} — {e['name']}"
    if e["repeat"] != "None" or e.get("series"):
        line += " 🔁"
    if e["book"]:
        line += f" [{e['book']}]"
    return line


def _task_line(t, with_date):
    when = format_due_full(t["deadline"], t["time"]) if with_date else ""
    if not with_date and t["time"]:
        when = t["time"]
    line = f"• {t['name']}" + (f" — {when}" if when else "")
    if t["book"]:
        line += f" [{t['book']}]"
    return line


async def build_summary() -> str:
    now = _now()
    events, today_t, overdue = await asyncio.gather(
        asyncio.to_thread(events_on, now.date()),
        asyncio.to_thread(list_tasks, "today"),
        asyncio.to_thread(list_tasks, "overdue"),
    )
    events.sort(key=lambda e: (e["start"] is not None, e["start"] or ""))   # all-day first
    head = f"☀️ {now.strftime('%A, %d %b %Y')}"
    if not (events or today_t or overdue):
        return f"{head}\nClear day, sir. 🎉"
    return "\n\n".join([
        head,
        _section("📅 Events", [_event_line(e) for e in events]),
        _section("✅ Tasks today", [_task_line(t, False) for t in today_t]),
        _section("⚠️ Overdue", [_task_line(t, True) for t in overdue]),
    ])


# ---------- auto send ----------
async def check_summary(context: ContextTypes.DEFAULT_TYPE):
    """Runs every minute. Sends once per day, at the group's time (late up to 3h)."""
    now = _now()
    bd = context.bot_data
    today = now.date().isoformat()
    if bd.get("summary_last") == today:
        return
    h, m = map(int, _times(bd)["a" if now.weekday() <= 2 else "b"].split(":"))
    due = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if now < due or now - due > GRACE:
        return
    try:
        await context.bot.send_message(CHAT_ID, await build_summary())
        bd["summary_last"] = today
    except Exception:
        logging.exception("auto summary failed")


# ---------- /summary ----------
def _main_kb():
    return InlineKeyboardMarkup([[_btn("⏰ Edit times", "sm:edit")]])


async def summary_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _strip(context, context.user_data.get("sm"))
    try:
        text = await build_summary()
    except Exception:
        logging.exception("summary failed")
        await update.message.reply_text("❌ An error occurred, sir. Please try again.")
        return
    msg = await update.message.reply_text(text, reply_markup=_main_kb())
    context.user_data["sm"] = {"text": text, "stage": None, "group": None,
                               "msg": (msg.chat_id, msg.message_id)}


def _times_screen(bd):
    t = _times(bd)
    text = (f"Summary times (Jakarta)\n{GROUP_LABEL['a']}: {t['a']}\n{GROUP_LABEL['b']}: {t['b']}"
            "\nEdit which?")
    kb = InlineKeyboardMarkup([
        [_btn(f"{GROUP_LABEL['a']} ({t['a']})", "sm:g:a"), _btn(f"{GROUP_LABEL['b']} ({t['b']})", "sm:g:b")],
        [_btn("⬅ Back", "sm:back")],
    ])
    return text, kb


async def on_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    st = context.user_data.get("sm")
    if st is None:
        await q.answer("Expired. Send /summary again.", show_alert=True)
        return
    await q.answer()
    try:
        parts = q.data.split(":")
        act = parts[1]
        if act == "back":
            st["stage"] = None
            await _edit(q, st["text"], _main_kb())
        elif act == "edit":
            st["stage"] = None
            text, kb = _times_screen(context.bot_data)
            await _edit(q, text, kb)
        elif act == "g":
            st["stage"], st["group"] = "time", parts[2]
            await _edit(q, f"New time for {GROUP_LABEL[parts[2]]}? e.g. 05:30 (/cancel to abort)",
                        InlineKeyboardMarkup([[_btn("⬅ Back", "sm:edit")]]))
    except Exception:
        logging.exception("on_summary failed")
        await q.message.reply_text("❌ An error occurred, sir. Please try again.")


async def summary_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """True if the message was consumed (typing a new time)."""
    st = context.user_data.get("sm")
    if not st or st.get("stage") != "time":
        return False
    tm = _time_input(update.message.text)
    if not tm:
        await update.message.reply_text("Didn't get that time. Try 05:30 or 6am. (/cancel to abort)")
        return True
    context.bot_data.setdefault("summary_times", {})[st["group"]] = tm
    await _strip(context, st)
    st["stage"] = None
    text, kb = _times_screen(context.bot_data)
    msg = await update.message.reply_text(f"✅ {GROUP_LABEL[st['group']]} summary is now {tm}.\n\n{text}",
                                          reply_markup=kb)
    st["msg"] = (msg.chat_id, msg.message_id)
    return True