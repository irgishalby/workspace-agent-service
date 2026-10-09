import asyncio
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler,CallbackQueryHandler, TypeHandler,ApplicationHandlerStop, PicklePersistence,filters, ContextTypes
from config.settings import TELEGRAM_TOKEN, ALLOWED_USER_ID
from core.agent import process_user_intent
from tools.notion_tool import list_tasks, mark_done
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo


logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                    level=logging.INFO)
for noisy in ("httpx", "httpcore", "notion_client"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

TITLES = {"today": "📅 Hari ini", "tomorrow": "🌙 Besok", "overdue": "⚠️ Terlambat", "week": "🗓 7 hari ke depan"}
VIEW_CODE = {"today": "t", "overdue": "o", "week": "w", "tomorrow": "m"}
CODE_VIEW = {v: k for k, v in VIEW_CODE.items()}
MAX_ROWS = 20
TZ = ZoneInfo("Asia/Jakarta")


# ---------- security ----------
async def guard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.id != ALLOWED_USER_ID:
        logging.warning("Blocked user id=%s", user.id if user else None)
        raise ApplicationHandlerStop


# ---------- views ----------
async def render(view: str):
    tasks = await asyncio.to_thread(list_tasks, view)
    if not tasks:
        return f"{TITLES[view]}\nKosong. 🎉", None
    tasks = tasks[:MAX_ROWS]
    lines, kb = [TITLES[view]], []
    for i, t in enumerate(tasks, 1):
        when = t["deadline"] + (f" {t['time']}" if t["time"] else "")
        line = f"{i}. {t['name']} — {when}"
        if t["book"]:
            line += f" ({t['book']})"
        lines.append(line)
        kb.append([InlineKeyboardButton(
            f"✅ {i}. {t['name'][:30]}",
            callback_data=f"done:{t['id']}:{VIEW_CODE[view]}")])
    return "\n".join(lines), InlineKeyboardMarkup(kb)


def view_command(view: str):
    async def cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        text, kb = await render(view)
        await update.message.reply_text(text, reply_markup=kb)
    return cmd


# ---------- basic commands ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "G0BLD-GK aktif.\n/today /week /overdue /cancel")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("pending", None)
    await update.message.reply_text("Dibatalkan.")


# ---------- free text ----------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        response = await process_user_intent(update.message.text, context)
        if response["type"] == "buttons":
            kb = [[InlineKeyboardButton(b["text"], callback_data=b["callback"])]
                  for b in response["buttons"]]
            await update.message.reply_text(
                response["content"], reply_markup=InlineKeyboardMarkup(kb))
        else:
            await update.message.reply_text(response["content"])
    except Exception:
        logging.exception("handle_message failed")
        await update.message.reply_text("❌ Maaf, terjadi kesalahan. Coba lagi.")


# ---------- buttons ----------
async def handle_book(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        response = await process_user_intent(query.data, context)
        await query.edit_message_text(text=response["content"])
    except Exception:
        logging.exception("handle_book failed")
        await query.message.reply_text("❌ Maaf, terjadi kesalahan. Coba lagi.")


async def handle_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer("Selesai ✅")
        _, page_id, code = query.data.split(":")
        result = await asyncio.to_thread(mark_done, page_id)
        if result["status"] != "success":
            await query.answer(f"Gagal: {result['message'][:150]}", show_alert=True)
            return
        if code == "r":
            await query.edit_message_text("✅ Selesai.")
            return
        text, kb = await render(CODE_VIEW.get(code, "today"))
        await query.edit_message_text(text, reply_markup=kb)
    except Exception:
        logging.exception("handle_done failed")
        await query.answer("Gagal. Coba lagi.", show_alert=True)


# ---------- reminders ----------
CHAT = ALLOWED_USER_ID  # private chat id equals user id

async def morning_digest(context: ContextTypes.DEFAULT_TYPE):
    for view in ("overdue", "today"):
        text, kb = await render(view)
        if kb:  # skip empty views
            await context.bot.send_message(CHAT, text, reply_markup=kb)

async def evening_digest(context: ContextTypes.DEFAULT_TYPE):
    text, kb = await render("tomorrow")
    if kb:
        await context.bot.send_message(CHAT, text, reply_markup=kb)

def _reminder_kb(task_id: str):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Selesai", callback_data=f"done:{task_id}:r"),
        InlineKeyboardButton("⏰ Tunda 1 jam", callback_data=f"snz:{task_id}:60"),
    ]])

async def check_timed(context: ContextTypes.DEFAULT_TYPE):
    """Every 10 min: ping tasks starting within 2 hours."""
    now = datetime.now(TZ)
    sent = context.bot_data.setdefault("reminded", [])
    tasks = (await asyncio.to_thread(list_tasks, "today")
             + await asyncio.to_thread(list_tasks, "tomorrow"))
    for t in tasks:
        if not t["time"]:
            continue
        start = datetime.fromisoformat(f"{t['deadline']}T{t['time']}:00").replace(tzinfo=TZ)
        key = f"{t['id']}:{t['deadline']}T{t['time']}"
        if timedelta(0) < start - now <= timedelta(hours=2) and key not in sent:
            sent.append(key)
            await context.bot.send_message(
                CHAT, f"⏰ {t['time']} — {t['name']}", reply_markup=_reminder_kb(t["id"]))
    del sent[:-300]

async def handle_snooze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, page_id, mins = query.data.split(":")
    context.job_queue.run_once(
        snooze_job, int(mins) * 60,
        data={"id": page_id, "text": query.message.text})
    await query.answer("Ditunda")
    await query.edit_message_text(f"{query.message.text}\n(ditunda {mins} menit)")

async def snooze_job(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data
    await context.bot.send_message(CHAT, d["text"], reply_markup=_reminder_kb(d["id"]))


# ---------- errors / setup ----------
async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    logging.error("Unhandled error", exc_info=context.error)


async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("today", "Task hari ini"),
        BotCommand("tomorrow", "Task besok"),
        BotCommand("week", "Task 7 hari ke depan"),
        BotCommand("overdue", "Task terlambat"),
        BotCommand("cancel", "Batalkan proses"),
    ])


def main():
    persistence = PicklePersistence(filepath="bot_data.pkl")
    app = (Application.builder()
           .token(TELEGRAM_TOKEN)
           .persistence(persistence)
           .post_init(post_init)
           .build())

    app.add_handler(TypeHandler(Update, guard), group=-1)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("today", view_command("today")))
    app.add_handler(CommandHandler("tomorrow", view_command("tomorrow")))
    app.add_handler(CommandHandler("week", view_command("week")))
    app.add_handler(CommandHandler("overdue", view_command("overdue")))

    app.add_handler(CallbackQueryHandler(handle_done, pattern=r"^done:"))
    app.add_handler(CallbackQueryHandler(handle_snooze, pattern=r"^snz:"))
    app.add_handler(CallbackQueryHandler(handle_book, pattern=r"^book:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.add_error_handler(on_error)

    jq = app.job_queue
    jq.run_daily(morning_digest, time=dtime(7, 0, tzinfo=TZ), name="morning")
    jq.run_daily(evening_digest, time=dtime(19, 0, tzinfo=TZ), name="evening")
    jq.run_repeating(check_timed, interval=600, first=30, name="timed")

    print("G0BLD-GK Agent running...")
    app.run_polling()


if __name__ == "__main__":
    main()