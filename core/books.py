# core/books.py  -  /books: list, add, rename, delete
import asyncio
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import ContextTypes
from tools.notion_tool import list_books, book_counts, create_book, rename_book, delete_book

PAGE = 8
KIND_LABEL = {"rn": "Rename which book, sir?", "dl": "Delete which book, sir?",
              "mv": "Move tasks to which book, sir?"}


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


async def _load(st):
    books = await asyncio.to_thread(list_books, True)
    counts = await asyncio.to_thread(book_counts)
    st["books"] = [{"id": b["page_id"], "title": b["title"],
                    "n": counts["by_book"].get(b["page_id"], 0)} for b in books]
    st["none"] = counts["no_book"]


def _menu_text(st):
    if not st["books"]:
        return "📚 Books\nNo books yet, sir."
    lines = ["📚 Books"]
    for i, b in enumerate(st["books"], 1):
        lines.append(f"{i}. {b['title']} — {b['n']} open")
    if st["none"]:
        lines.append(f"(No book — {st['none']} open)")
    return "\n".join(lines)


def _menu_kb():
    return InlineKeyboardMarkup([[_btn("➕ Add", "bk:add"), _btn("✏ Rename", "bk:pick:rn:0"),
                                  _btn("🗑 Delete", "bk:pick:dl:0")]])


def _pick_kb(st, kind, page):
    skip = st.get("target") if kind == "mv" else None
    idx = [i for i in range(len(st["books"])) if i != skip]
    pages = max(1, -(-len(idx) // PAGE))
    page = max(0, min(page, pages - 1))
    rows = [[_btn(f"{st['books'][i]['title'][:40]} ({st['books'][i]['n']})", f"bk:sel:{kind}:{i}")]
            for i in idx[page * PAGE:(page + 1) * PAGE]]
    nav = []
    if page > 0:
        nav.append(_btn("◀ Prev", f"bk:pick:{kind}:{page - 1}"))
    if page < pages - 1:
        nav.append(_btn("Next ▶", f"bk:pick:{kind}:{page + 1}"))
    if nav:
        rows.append(nav)
    back = f"bk:sel:dl:{skip}" if kind == "mv" else "bk:menu"
    rows.append([_btn("⬅ Back", back)])
    text = KIND_LABEL[kind] + (f" ({page + 1}/{pages})" if pages > 1 else "")
    return text, InlineKeyboardMarkup(rows)


def _delete_screen(st):
    b = st["books"][st["target"]]
    rows = [[_btn("Keep tasks, no book", "bk:mode:unassign")]]
    if len(st["books"]) > 1:
        rows.append([_btn("Move tasks to another book", "bk:mode:move")])
    rows.append([_btn("Delete tasks too", "bk:mode:delete")])
    rows.append([_btn("⬅ Back", "bk:pick:dl:0")])
    text = f"Delete '{b['title']}'?\nOpen tasks in it: {b['n']}\nWhat happens to its tasks?"
    return text, InlineKeyboardMarkup(rows)


def _confirm_screen(st):
    b = st["books"][st["target"]]
    mode = st["mode"]
    if mode == "unassign":
        what = f"Tasks stay, with no book."
    elif mode == "move":
        what = f"Tasks move to '{st['books'][st['dest']]['title']}'."
    else:
        what = "ALL its tasks (done and open) are deleted."
    text = f"Confirm delete '{b['title']}'?\n{what}"
    return text, InlineKeyboardMarkup([[_btn("✅ Confirm", "bk:go"),
                                        _btn("⬅ Back", f"bk:sel:dl:{st['target']}")]])


def _prompt_kb():
    return InlineKeyboardMarkup([[_btn("⬅ Back", "bk:menu")]])


# ---------- command ----------
async def books_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _strip(context, context.user_data.get("bk"))
    st = {"books": [], "none": 0, "stage": None, "msg": None}
    await _load(st)
    msg = await update.message.reply_text(_menu_text(st), reply_markup=_menu_kb())
    st["msg"] = (msg.chat_id, msg.message_id)
    context.user_data["bk"] = st


# ---------- text input (add / rename names) ----------
async def books_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """True if the message was consumed by the books flow."""
    st = context.user_data.get("bk")
    if not st or st.get("stage") not in ("add", "rename"):
        return False

    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("Name required, sir. (/cancel to abort)")
        return True

    taken = {b["title"].lower().strip(): i for i, b in enumerate(st["books"])}
    hit = taken.get(name.lower())
    if hit is not None and not (st["stage"] == "rename" and hit == st["target"]):
        await update.message.reply_text(f"'{name}' already exists. Another name? (/cancel to abort)")
        return True

    if st["stage"] == "add":
        r = await asyncio.to_thread(create_book, name)
        done = f"✅ Added '{name}'."
    else:
        old = st["books"][st["target"]]
        r = await asyncio.to_thread(rename_book, old["id"], name)
        done = f"✅ Renamed '{old['title']}' to '{name}'."

    await _strip(context, st)
    if r.get("status") != "success":
        done = f"❌ Failed: {r.get('message')}"
    st["stage"] = None
    await _load(st)
    msg = await update.message.reply_text(f"{done}\n\n{_menu_text(st)}", reply_markup=_menu_kb())
    st["msg"] = (msg.chat_id, msg.message_id)
    return True


# ---------- callbacks ----------
async def on_books(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    st = context.user_data.get("bk")
    if st is None:
        await q.answer("Expired. Send /books again.", show_alert=True)
        return
    await q.answer()

    try:
        parts = q.data.split(":")
        act = parts[1]

        if act == "menu":
            st["stage"] = None
            await _load(st)
            await _edit(q, _menu_text(st), _menu_kb())

        elif act == "add":
            st["stage"] = "add"
            await _edit(q, "Send the new book name. (/cancel to abort)", _prompt_kb())

        elif act == "pick":
            kind, page = parts[2], int(parts[3])
            text, kb = _pick_kb(st, kind, page)
            await _edit(q, text, kb)

        elif act == "sel":
            kind, i = parts[2], int(parts[3])
            if kind == "rn":
                st["target"], st["stage"] = i, "rename"
                await _edit(q, f"New name for '{st['books'][i]['title']}'? (/cancel to abort)",
                            _prompt_kb())
            elif kind == "dl":
                st["target"], st["stage"] = i, None
                text, kb = _delete_screen(st)
                await _edit(q, text, kb)
            elif kind == "mv":
                st["dest"], st["mode"] = i, "move"
                text, kb = _confirm_screen(st)
                await _edit(q, text, kb)

        elif act == "mode":
            mode = parts[2]
            if mode == "move":
                text, kb = _pick_kb(st, "mv", 0)
            else:
                st["mode"] = mode
                text, kb = _confirm_screen(st)
            await _edit(q, text, kb)

        elif act == "go":
            b = st["books"][st["target"]]
            dest = st["books"][st["dest"]]["id"] if st.get("mode") == "move" else None
            r = await asyncio.to_thread(delete_book, b["id"], st["mode"], dest)
            if r.get("status") == "success":
                done = f"✅ Deleted '{b['title']}'. Tasks affected: {r['tasks_affected']}."
            else:
                done = f"❌ Failed: {r.get('message')}"
            await _load(st)
            await _edit(q, f"{done}\n\n{_menu_text(st)}", _menu_kb())

    except Exception:
        logging.exception("on_books failed")
        await q.message.reply_text("❌ An error occurred, sir. Please try again.")