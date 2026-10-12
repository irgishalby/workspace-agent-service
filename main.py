# main.py
import asyncio
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.error import BadRequest
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    TypeHandler, ApplicationHandlerStop, PicklePersistence, filters, ContextTypes
)
from config.settings import TELEGRAM_TOKEN, ALLOWED_USER_ID
from core.agent import process_user_intent
from core.dates import format_due_full
from core.assistant import agent_text, on_agent
from core.books import books_cmd, books_text, on_books
from core.reminders import check_reminders, reminder_kb
from core.edit import edit_cmd, remind_cmd, edit_text, on_edit
from core.summary import summary_cmd, summary_text, on_summary, check_summary
from tools.notion_tool import list_tasks, list_books, book_counts, mark_done, migrate_legacy

from datetime import datetime
from zoneinfo import ZoneInfo
from tools.notion_tool import mark_undone

WIB = ZoneInfo("Asia/Jakarta")

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", level=logging.INFO)
for noisy in ("httpx", "httpcore", "notion_client"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

CHAT_ID = ALLOWED_USER_ID
PAGE = 8          # book buttons per page
MAX_ROWS = 40     # task lines per message
FLOW_KEYS = ("pending", "st", "bk", "sm", "ed", "act", "chat")   # user_data keys that /cancel clears. Add new flows here.

DATE_TITLES = {"today": "🗓 Today", "week": "🗓 This week", "month": "🗓 This month", "all": "🗓 All tasks"}


# ---------- security ----------
async def guard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.id != ALLOWED_USER_ID:
        logging.warning("Unauthorized access blocked: user_id=%s", user.id if user else None)
        raise ApplicationHandlerStop


# ---------- helpers ----------
def _btn(text, data):
    return InlineKeyboardButton(text, callback_data=data)


async def _edit(q, text, kb=None):
    try:
        await q.edit_message_text(text, reply_markup=kb)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


async def _strip_menu(context, st):
    """Remove buttons from an old flow message."""
    if st and st.get("msg"):
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=st["msg"][0], message_id=st["msg"][1], reply_markup=None)
        except Exception:
            pass


def _render(st):
    tasks = st["view"]
    back = InlineKeyboardMarkup([[_btn("⬅ Back", st["back"])]])
    if not tasks:
        return f"{st['title']}\nNo open tasks, sir. 🎉", back

    lines = [f"{st['title']} ({len(tasks)})"]
    rows = []
    for i, t in enumerate(tasks[:MAX_ROWS]):
        done = t["id"] in st["done"]
        when = format_due_full(t["deadline"], t["time"])
        line = f"{'✅ ' if done else ''}{i + 1}. {t['name']} — {when}"
        if st["show_book"] and t["book"]:
            line += f" [{t['book']}]"
        lines.append(line)
        label = f"{'✖' if done else '☑'} {i + 1}. {t['name'][:30]}"
        rows.append([_btn(label, f"st:{'ud' if done else 'dn'}:{i}")])
    if len(tasks) > MAX_ROWS:
        lines.append(f"…and {len(tasks) - MAX_ROWS} more")
    rows.append([_btn("⬅ Back", st["back"])])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def _toggle(q, st, i, done):
    t = st["view"][i]
    fn = mark_done if done else mark_undone
    res = await asyncio.to_thread(fn, t["id"])
    if res["status"] != "success":
        await q.answer(f"Failed: {res['message'][:150]}", show_alert=True)
        return
    if done:
        st["done"].add(t["id"])
    else:
        st["done"].discard(t["id"])
    text, kb = _render(st)
    await _edit(q, text, kb)


# ---------- basic commands ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("G0BL-D-GK online, sir. Talk to me or use:\nCommands: /showtask, /edit, /books, /remind, /summary, /cancel")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud = context.user_data
    for k in FLOW_KEYS:
        flow = ud.pop(k, None)
        if isinstance(flow, dict):
            await _strip_menu(context, flow)
    await update.message.reply_text("Cancelled.")


# ---------- one-off: old repeat rules -> one row per session ----------
async def migrate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Converting repeating events, sir. This can take a minute.")
    try:
        r = await asyncio.to_thread(migrate_legacy)
    except Exception as e:
        logging.exception("migrate failed")
        await update.message.reply_text(f"❌ Failed: {e}")
        return
    text = f"✅ Converted {r['converted']} routines into {r['rows']} sessions."
    if r["errors"]:
        text += "\n⚠️ " + "\n⚠️ ".join(r["errors"][:10])
    await update.message.reply_text(text)


# ---------- /showtask ----------
def _root_kb():
    return InlineKeyboardMarkup([[_btn("📚 Books", "st:b"), _btn("🗓 Dates", "st:d")]])


def _dates_kb():
    return InlineKeyboardMarkup([
        [_btn("Today", "st:dt:today")],
        [_btn("This week", "st:dt:week"), _btn("This month", "st:dt:month"), _btn("All", "st:dt:all")],
        [_btn("⬅ Back", "st:root")],
    ])


def _books_view(st):
    books = st["books"]
    pages = max(1, -(-len(books) // PAGE))
    page = max(0, min(st["page"], pages - 1))
    st["page"] = page

    rows = [[_btn(f"All books ({st['total']})", "st:bk:all")]]
    for i in range(page * PAGE, min(len(books), (page + 1) * PAGE)):
        b = books[i]
        rows.append([_btn(f"{b['title'][:40]} ({b['n']})", f"st:bk:{i}")])
    if page == 0 and st["none"]:
        rows.append([_btn(f"No book ({st['none']})", "st:bk:none")])
    nav = []
    if page > 0:
        nav.append(_btn("◀ Prev", f"st:bp:{page - 1}"))
    if page < pages - 1:
        nav.append(_btn("Next ▶", f"st:bp:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([_btn("⬅ Back", "st:root")])

    text = "From which book, sir?"
    if pages > 1:
        text += f" ({page + 1}/{pages})"
    return text, InlineKeyboardMarkup(rows)


async def showtask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _strip_menu(context, context.user_data.get("st"))
    msg = await update.message.reply_text("Sort by book or date, sir?", reply_markup=_root_kb())
    context.user_data["st"] = {"books": [], "page": 0, "total": 0, "none": 0,
                               "msg": (msg.chat_id, msg.message_id)}


async def on_showtask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    st = context.user_data.get("st")
    if st is None:
        await q.answer("Expired. Send /showtask again.", show_alert=True)
        return
    await q.answer()

    try:
        parts = q.data.split(":")
        act, arg = parts[1], (parts[2] if len(parts) > 2 else None)

        if act == "root":
            await _edit(q, "Sort by book or date, sir?", _root_kb())

        elif act == "d":
            await _edit(q, "Which period, sir?", _dates_kb())

        elif act == "b":
            books = await asyncio.to_thread(list_books, True)
            counts = await asyncio.to_thread(book_counts)
            st["books"] = [{"id": b["page_id"], "title": b["title"],
                            "n": counts["by_book"].get(b["page_id"], 0)} for b in books]
            st["total"], st["none"], st["page"] = counts["total"], counts["no_book"], 0
            text, kb = _books_view(st)
            await _edit(q, text, kb)

        elif act == "bp":
            st["page"] = int(arg)
            text, kb = _books_view(st)
            await _edit(q, text, kb)

        elif act == "bk":
            if arg == "all":
                tasks, title, show_book, back = await asyncio.to_thread(list_tasks, "all"), "📚 All books", True, "st:bp:" + str(st["page"])
            elif arg == "none":
                tasks = [t for t in await asyncio.to_thread(list_tasks, "all") if not t["book_id"]]
                title, show_book, back = "📚 No book", False, "st:bp:" + str(st["page"])
            else:
                b = st["books"][int(arg)]
                tasks = await asyncio.to_thread(list_tasks, "all", b["id"])
                title, show_book, back = f"📚 {b['title']}", False, "st:bp:" + str(st["page"])
            st.update(view=tasks, title=title, show_book=show_book, back=back, done=set())
            text, kb = _render(st)
            await _edit(q, text, kb)

        elif act == "dt":
            if arg == "today":
                today = datetime.now(WIB).date().isoformat()
                tasks = [t for t in await asyncio.to_thread(list_tasks, "all") if t["deadline"] == today]
            else:
                tasks = await asyncio.to_thread(list_tasks, arg)
            st.update(view=tasks, title=DATE_TITLES[arg], show_book=True, back="st:d", done=set())
            text, kb = _render(st)
            await _edit(q, text, kb)

        elif act == "dn":
            await _toggle(q, st, int(arg), True)

        elif act == "ud":
            await _toggle(q, st, int(arg), False)

    except Exception:
        logging.exception("on_showtask failed")
        await q.message.reply_text("❌ An error occurred, sir. Please try again.")


# ---------- free-text (task / event extraction) ----------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if (await books_text(update, context) or await summary_text(update, context)
                or await edit_text(update, context)):
            return
        await agent_text(update, context)
    except Exception:
        logging.exception("handle_message failed")
        await update.message.reply_text("❌ An error occurred, sir. Please try again.")


async def handle_book(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        res = await process_user_intent(query.data, context)
        if res["type"] == "buttons":
            kb = [[InlineKeyboardButton(b["text"], callback_data=b["callback"])] for b in res["buttons"]]
            await query.edit_message_text(text=res["content"], reply_markup=InlineKeyboardMarkup(kb))
        else:
            await query.edit_message_text(text=res["content"])
    except Exception:
        logging.exception("handle_book failed")
        await query.message.reply_text("❌ An error occurred, sir. Please try again.")


# ---------- reminder buttons ----------
async def handle_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer("Completed ✅")
        _, page_id, _code = query.data.split(":")
        res = await asyncio.to_thread(mark_done, page_id)
        if res["status"] != "success":
            await query.answer(f"Failed: {res['message'][:150]}", show_alert=True)
            return
        await query.edit_message_text("⚡ Task completed, sir.")
    except Exception:
        logging.exception("handle_done failed")
        await query.answer("Failed to mark task as done.", show_alert=True)


async def handle_snooze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split(":")
    page_id, mins = parts[1], parts[2]
    kind = parts[3] if len(parts) > 3 else "t"
    context.job_queue.run_once(
        snooze_job, int(mins) * 60,
        data={"id": page_id, "text": query.message.text.split("\n(Snoozed")[0], "kind": kind}
    )
    await query.answer("Snoozed")
    await query.edit_message_text(f"{query.message.text}\n(Snoozed for {mins} mins)")


async def snooze_job(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data
    await context.bot.send_message(CHAT_ID, d["text"], reply_markup=reminder_kb(d["id"], d.get("kind", "t")))


# ---------- errors / setup ----------
async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    logging.error("Unhandled exception caught", exc_info=context.error)


async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("showtask", "View tasks by book or date"),
        BotCommand("edit", "Edit a task or event"),
        BotCommand("remind", "Set a reminder for a task or event"),
        BotCommand("books", "List, add, rename, delete books"),
        BotCommand("summary", "Today's events, tasks, overdue"),
        BotCommand("cancel", "Cancel current operation"),
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
    app.add_handler(CommandHandler("showtask", showtask))
    app.add_handler(CommandHandler("migrate", migrate))
    app.add_handler(CommandHandler("edit", edit_cmd))
    app.add_handler(CommandHandler("remind", remind_cmd))
    app.add_handler(CommandHandler("books", books_cmd))
    app.add_handler(CommandHandler("summary", summary_cmd))

    app.add_handler(CallbackQueryHandler(on_showtask, pattern=r"^st:"))
    app.add_handler(CallbackQueryHandler(on_agent, pattern=r"^ag:"))
    app.add_handler(CallbackQueryHandler(on_edit, pattern=r"^ed:"))
    app.add_handler(CallbackQueryHandler(on_books, pattern=r"^bk:"))
    app.add_handler(CallbackQueryHandler(on_summary, pattern=r"^sm:"))
    app.add_handler(CallbackQueryHandler(handle_done, pattern=r"^done:"))
    app.add_handler(CallbackQueryHandler(handle_snooze, pattern=r"^snz:"))
    app.add_handler(CallbackQueryHandler(handle_book, pattern=r"^(book|kind):"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.add_error_handler(on_error)

    app.job_queue.run_repeating(check_reminders, interval=60, first=15, name="reminders")
    app.job_queue.run_repeating(check_summary, interval=60, first=20, name="summary")

    print("G0BL-D-GK active and operational.")
    app.run_polling()


if __name__ == "__main__":
    main()