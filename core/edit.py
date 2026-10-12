# core/edit.py  -  /edit and /remind (tasks and events)
import asyncio
import logging
import re
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import ContextTypes
from core.dates import format_due, format_due_full
from tools.notion_tool import (
    list_tasks, list_events, list_books, update_task, update_event, update_series, add_exception,
    set_remind, parse_natural_date, parse_time, _time_input, _iso, DAY_NAMES, EMPTY, TZ,
)

PAGE = 8
TASK_FIELDS = [("name", "Title"), ("deadline", "Due date"), ("book", "Book")]
EVENT_FIELDS = [("name", "Title"), ("date", "Date"), ("start", "Start time"),
                ("end", "End time"), ("book", "Book")]
ROUTINE_FIELDS = [("days", "Days"), ("until", "Repeat until"), ("skip", "Skip one date")]
SERIES_FIELDS = [("name", "Title"), ("start", "Start time"), ("end", "End time"), ("book", "Book"),
                 ("days", "Days"), ("until", "Repeat until"), ("skip", "Skip one date"),
                 ("delete", "Delete all upcoming")]
LABEL = {k: v for k, v in TASK_FIELDS + EVENT_FIELDS + ROUTINE_FIELDS + SERIES_FIELDS}
PROMPT = {
    "name": "New title?",
    "deadline": "New due date? e.g. friday 14:00 or 20 dec. 'none' removes it.",
    "date": "New date? e.g. tomorrow or 20 dec.",
    "start": "New start time? e.g. 08:00 or 2pm.",
    "end": "New end time? e.g. 10:00. 'none' removes it.",
    "days": "Which days? e.g. Mon, Wed",
    "until": "Repeat until? e.g. 20 dec. 'none' removes it.",
    "skip": "Which date to skip? e.g. 14 oct",
}


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


# ---------- labels ----------
def _label(kind, it):
    if kind == "t":
        return f"{it['name']} · {format_due_full(it['deadline'], it['time'])}"
    if it["repeat"] != "None":
        return f"🔁 {it['name']} · {','.join(it['days']) or it['repeat']}"
    when = format_due(it["date"])
    return f"{it['name']} · {when}" + (f" {it['start']}" if it["start"] else "")


def _old(kind, it, field):
    if field == "deadline":
        return format_due_full(it["deadline"], it["time"]) if it["deadline"] else "none"
    if field == "book":
        return it["book"] or "none"
    if field == "days":
        return ", ".join(it["days"]) or "none"
    if field == "skip":
        return "—"
    return str(it.get(field) or "none")


def _fields(kind, it):
    if kind == "t":
        return TASK_FIELDS
    if it.get("series"):
        return SERIES_FIELDS
    return EVENT_FIELDS + (ROUTINE_FIELDS if it["repeat"] != "None" else [])


def _is_routine(st):
    return st["kind"] == "e" and st["items"][st["idx"]]["repeat"] != "None"


# ---------- validation of typed values ----------
def _check(field, txt, series=False):
    """-> (ok, normalized_value_for_apply, shown) or (False, None, error)."""
    low = txt.strip().lower()
    if field == "name":
        return (True, txt, txt) if txt else (False, None, "Title required.")
    if series and field == "until" and low in EMPTY:
        return False, None, "A repeating class needs an end date."
    if field in ("deadline", "end", "until") and low in EMPTY:
        return True, txt, "none"
    if field == "deadline" or field in ("date", "until", "skip"):
        d = parse_natural_date(txt)
        if not d:
            return False, None, "Didn't get that date. Try 'friday', 'tomorrow' or 20 dec."
        tm = parse_time(txt) if field == "deadline" else None
        val = d if field == "skip" else txt
        return True, val, d + (f" {tm}" if tm else "")
    if field in ("start", "end"):
        tm = _time_input(txt)
        return (True, txt, tm) if tm else (False, None, "Didn't get that time. Try 08:00 or 2pm.")
    if field == "days":
        names = [n.strip()[:3].title() for n in re.split(r"[,\s]+", txt) if n.strip()]
        if not names or any(n not in DAY_NAMES for n in names):
            return False, None, "Didn't get that. Try: Mon, Wed"
        return True, txt, ", ".join(names)
    return False, None, "Unknown field."


# ---------- screens ----------
def _type_screen(st):
    verb = "Edit" if st["mode"] == "edit" else "Remind about"
    return (f"{verb} which type, sir?",
            InlineKeyboardMarkup([[_btn("✅ Task", "ed:t:t"), _btn("📅 Event", "ed:t:e")]]))


def _pick_screen(st):
    items, kind = st["items"], st["kind"]
    if not items:
        return ("Nothing here, sir.", InlineKeyboardMarkup([[_btn("⬅ Back", "ed:root")]]))
    pages = max(1, -(-len(items) // PAGE))
    page = max(0, min(st["page"], pages - 1))
    st["page"] = page
    rows = [[_btn(_label(kind, items[i])[:55], f"ed:i:{i}")]
            for i in range(page * PAGE, min(len(items), (page + 1) * PAGE))]
    nav = []
    if page > 0:
        nav.append(_btn("◀ Prev", f"ed:p:{page - 1}"))
    if page < pages - 1:
        nav.append(_btn("Next ▶", f"ed:p:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([_btn("⬅ Back", "ed:root")])
    text = "Which one, sir?" + (f" ({page + 1}/{pages})" if pages > 1 else "")
    return text, InlineKeyboardMarkup(rows)


def _field_screen(st):
    it = st["items"][st["idx"]]
    rows = [[_btn(label, f"ed:f:{key}")] for key, label in _fields(st["kind"], it)]
    rows.append([_btn("⬅ Back", f"ed:p:{st['page']}")])
    return f"{_label(st['kind'], it)}\nWhich field, sir?", InlineKeyboardMarkup(rows)


def _book_screen(st):
    books = st["books"]
    pages = max(1, -(-len(books) // PAGE))
    page = max(0, min(st["bpage"], pages - 1))
    st["bpage"] = page
    rows = [[_btn(books[i]["title"][:40], f"ed:b:{i}")]
            for i in range(page * PAGE, min(len(books), (page + 1) * PAGE))]
    if page == 0:
        rows.append([_btn("No book", "ed:b:none")])
    nav = []
    if page > 0:
        nav.append(_btn("◀ Prev", f"ed:bp:{page - 1}"))
    if page < pages - 1:
        nav.append(_btn("Next ▶", f"ed:bp:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([_btn("⬅ Back", f"ed:i:{st['idx']}")])
    return "Which book, sir?" + (f" ({page + 1}/{pages})" if pages > 1 else ""), InlineKeyboardMarkup(rows)


def _prompt_screen(st):
    back = f"ed:i:{st['idx']}" if st["mode"] == "edit" else f"ed:p:{st['page']}"
    kb = InlineKeyboardMarkup([[_btn("⬅ Back", back)]])
    if st["mode"] == "remind":
        name = st["items"][st["idx"]]["name"]
        return (f"Remind about '{name}' when? Date and time, e.g. tomorrow 08:00 or 20 dec 7am. "
                "(/cancel to abort)"), kb
    return PROMPT[st["field"]] + " (/cancel to abort)", kb


def _confirm_screen(st):
    it = st["items"][st["idx"]]
    if st["mode"] == "remind":
        text = f"Remind about '{it['name']}' on {st['new']}?"
        if _is_routine(st):
            text += "\n(One time only, not every occurrence.)"
        ok = "✅ Set reminder"
    else:
        f = st["field"]
        if f == "delete":
            text = f"Delete '{it['name']}'?\nThis removes its {it.get('n', '?')} upcoming sessions. Past ones stay."
            ok = "🗑 Delete"
        else:
            text = (f"Change {LABEL[f]} of '{it['name']}'\nFrom: {_old(st['kind'], it, f)}\nTo: {st['new']}")
            if _is_routine(st) and f != "skip":
                text += "\n(Applies to all upcoming sessions.)"
            ok = "✅ Save"
    return text, InlineKeyboardMarkup([[_btn(ok, "ed:ok"), _btn("⬅ Back", f"ed:i:{st['idx']}")]])


# ---------- commands ----------
async def _start(update, context, mode):
    await _strip(context, context.user_data.get("ed"))
    st = {"mode": mode, "kind": None, "items": [], "page": 0, "idx": None, "field": None,
          "stage": None, "payload": None, "new": None, "books": [], "bpage": 0, "msg": None}
    text, kb = _type_screen(st)
    msg = await update.message.reply_text(text, reply_markup=kb)
    st["msg"] = (msg.chat_id, msg.message_id)
    context.user_data["ed"] = st


async def edit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _start(update, context, "edit")


async def remind_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _start(update, context, "remind")


# ---------- typed input ----------
async def edit_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    st = context.user_data.get("ed")
    if not st or st.get("stage") not in ("val", "when"):
        return False
    txt = update.message.text.strip()

    if st["stage"] == "val":
        ok, payload, shown = _check(st["field"], txt, bool(st["items"][st["idx"]].get("series")))
        if not ok:
            await update.message.reply_text(f"{shown} (/cancel to abort)")
            return True
        st["payload"], st["new"] = payload, shown
    else:
        d = parse_natural_date(txt) or datetime.now(TZ).date().isoformat()
        tm = parse_time(txt)
        if not tm:
            await update.message.reply_text("Need a time too. Try tomorrow 08:00 or friday 7am. (/cancel to abort)")
            return True
        iso = _iso(d, tm)
        if datetime.fromisoformat(iso) <= datetime.now(TZ):
            await update.message.reply_text("That time is already past. Another? (/cancel to abort)")
            return True
        st["payload"], st["new"] = iso, f"{d} {tm}"

    await _strip(context, st)
    st["stage"] = "confirm"
    text, kb = _confirm_screen(st)
    msg = await update.message.reply_text(text, reply_markup=kb)
    st["msg"] = (msg.chat_id, msg.message_id)
    return True


# ---------- callbacks ----------
async def _apply(st):
    it = st["items"][st["idx"]]
    if st["mode"] == "remind":
        return await asyncio.to_thread(set_remind, it["id"], st["payload"])
    f, v = st["field"], st["payload"]
    if st["kind"] == "t":
        return await asyncio.to_thread(update_task, it["id"], f, v)
    if it.get("series"):
        return await asyncio.to_thread(update_series, it["series"], f, v)
    if f == "skip":
        return await asyncio.to_thread(add_exception, it["id"], v)
    return await asyncio.to_thread(update_event, it["id"], f, v)


async def on_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    st = context.user_data.get("ed")
    if st is None:
        await q.answer("Expired. Send the command again.", show_alert=True)
        return
    await q.answer()

    try:
        parts = q.data.split(":")
        act, arg = parts[1], (parts[2] if len(parts) > 2 else None)

        if act == "root":
            st["stage"] = None
            text, kb = _type_screen(st)

        elif act == "t":
            st["kind"], st["page"], st["stage"] = arg, 0, None
            if arg == "t":
                st["items"] = await asyncio.to_thread(list_tasks, "all")
            else:
                st["items"] = await asyncio.to_thread(list_events)
            text, kb = _pick_screen(st)

        elif act == "p":
            st["page"], st["stage"] = int(arg), None
            text, kb = _pick_screen(st)

        elif act == "i":
            st["idx"], st["stage"] = int(arg), None
            if st["mode"] == "edit":
                text, kb = _field_screen(st)
            else:
                st["stage"] = "when"
                text, kb = _prompt_screen(st)

        elif act == "f":
            st["field"] = arg
            if arg == "delete":
                st["payload"], st["new"], st["stage"] = True, "delete", "confirm"
                text, kb = _confirm_screen(st)
            elif arg == "book":
                books = await asyncio.to_thread(list_books, True)
                st["books"] = [{"id": b["page_id"], "title": b["title"]} for b in books]
                st["bpage"] = 0
                text, kb = _book_screen(st)
            else:
                st["stage"] = "val"
                text, kb = _prompt_screen(st)

        elif act == "bp":
            st["bpage"] = int(arg)
            text, kb = _book_screen(st)

        elif act == "b":
            if arg == "none":
                st["payload"], st["new"] = None, "none"
            else:
                b = st["books"][int(arg)]
                st["payload"], st["new"] = b["id"], b["title"]
            st["stage"] = "confirm"
            text, kb = _confirm_screen(st)

        elif act == "ok":
            if st.get("stage") != "confirm":
                return
            it = st["items"][st["idx"]]
            r = await _apply(st)
            context.user_data.pop("ed", None)
            if r.get("status") == "success":
                what = ("Reminder set" if st["mode"] == "remind" else
                        "Deleted" if st["field"] == "delete" else f"{LABEL[st['field']]} updated")
                await _edit(q, f"✅ {what} for '{it['name']}'.")
            else:
                await _edit(q, f"❌ Failed: {r.get('message')}")
            return
        else:
            return

        await _edit(q, text, kb)

    except Exception:
        logging.exception("on_edit failed")
        await q.message.reply_text("❌ An error occurred, sir. Please try again.")