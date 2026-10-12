# core/assistant.py  -  the AI agent for free text.
# Reads freely (schedule, tasks, books). Every change is STAGED, shown on a confirm card, and only
# runs after you tap Confirm. Plain "add this" messages are handed to the capture workflow (agent.py).
import asyncio
import json
import logging
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import ContextTypes
from core.agent import groq_client, MODEL, process_user_intent
from tools.notion_tool import (
    notion, _parse_event, list_tasks, list_books, book_counts, events_between, search_book,
    create_task_in_db, create_event, update_task, update_event, update_series, add_exception,
    delete_event, mark_done, set_remind, roll_until, parse_natural_date, parse_time, _iso, EMPTY, TZ,
)

MAX_STEPS = 6          # tool-call rounds per message
HISTORY = 8            # chat turns remembered
MAX_RANGE_DAYS = 62

SYSTEM = """Your name is G0BL-D-GK or Gobbledygook, a personal schedule assistant for one user, Aby. Timezone Asia/Jakarta.
Now: <<NOW>>.
Reply in English. Short, concrete, no filler. Call the user "sir" sparingly.

Data: Tasks (title, book, optional due date+time). Events (title, date, start time, optional end,
optional book). A repeating class is stored as separate sessions tied by a series id. Books.

Rules:
- Look before you act. Use list_* tools to find ids and check clashes. Never invent ids.
- Write tools only STAGE changes. The user must confirm. After staging, say in one line what you
  proposed. Never say it is already done.
- Every task needs a book. If the book is not given, ask which one (name the books) before staging.
  "none" is allowed.
- Events need a date and start time. Ask if missing. End time and book are optional: do not ask end time.
- New repeating class: create_event with repeat, days, until. Whole class: edit_series / delete_series.
  One session: update_event / cancel_session.
- Default event reminders (1 day and 1h30 before) are automatic. Use set_reminder only on request.
- Work out relative dates yourself from Now. Dates are YYYY-MM-DD. Times are HH:MM, 24h.
- If the message is ONLY new tasks/events to add (nothing to look up, edit or answer), call capture.
- For schedule questions answer from the data with times and names.
- If a request is ambiguous, ask one short question.
- If you cannot answer, say "I don't know, sir." Do not make up answers.
- Dont use ** for markdown. Emojis are allowed. Use ✅ for done, ✏ for edit, 🚫 for cancel, 🗑 for delete, ⏰ for reminder, "🔁" for repeat.
- If the user asks for a summary, call summary_cmd or summary_text. If the user asks for a migration, call migrate_legacy.
"""

# ---------- tool schemas ----------
def _fn(name, desc, props=None, required=None):
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props or {}, "required": required or []}}}

S = lambda d: {"type": "string", "description": d}
LABEL = S("short name of the item, shown on the confirm card")
DATE = "YYYY-MM-DD"

TOOLS = [
    _fn("list_tasks", "List OPEN tasks. Returns id, name, due, time, book.",
        {"view": {"type": "string", "enum": ["all", "today", "tomorrow", "overdue", "week", "month", "day"]},
         "book": S("optional book name filter")}, ["view"]),
    _fn("list_events", f"List event sessions between two dates inclusive (max {MAX_RANGE_DAYS} days). "
        "Repeating classes are expanded. Returns id, name, date, start, end, book, series.",
        {"from_date": S(DATE), "to_date": S(DATE)}, ["from_date", "to_date"]),
    _fn("list_books", "List books with their open-task counts."),
    _fn("capture", "Hand over a message that ONLY adds new tasks/events to the capture workflow."),
    _fn("create_task", "STAGE a new task.",
        {"name": S("title"), "due_date": S(DATE), "due_time": S("HH:MM"), "book": S("book name or 'none'")},
        ["name"]),
    _fn("create_event", "STAGE a new event, or a repeating class (repeat Weekly/Daily).",
        {"name": S("title"), "date": S(f"{DATE}, first session"), "start_time": S("HH:MM"),
         "end_time": S("HH:MM"), "book": S("book name or 'none'"),
         "repeat": {"type": "string", "enum": ["None", "Weekly", "Daily"]},
         "days": {"type": "array", "items": {"type": "string"}, "description": "Weekly: Mon,Tue,Wed,Thu,Fri,Sat,Sun"},
         "until": S(f"{DATE}, last session")}, ["name", "date"]),
    _fn("update_task", "STAGE a change to one task.",
        {"task_id": S("id from list_tasks"), "label": LABEL,
         "field": {"type": "string", "enum": ["name", "deadline", "book"]},
         "value": S("name text | 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM' or 'none' | book name or 'none'")},
        ["task_id", "label", "field", "value"]),
    _fn("complete_task", "STAGE marking a task done.",
        {"task_id": S("id from list_tasks"), "label": LABEL}, ["task_id", "label"]),
    _fn("update_event", "STAGE a change to ONE event or ONE session (not the whole class).",
        {"event_id": S("id from list_events"), "label": LABEL,
         "field": {"type": "string", "enum": ["name", "date", "start", "end", "book"]},
         "value": S("name | YYYY-MM-DD | HH:MM | 'none' (end/book) | book name")},
        ["event_id", "label", "field", "value"]),
    _fn("cancel_session", "STAGE removing one session of a class, or deleting a one-off event.",
        {"event_id": S("id from list_events"), "label": LABEL, "date": S(DATE)},
        ["event_id", "label", "date"]),
    _fn("edit_series", "STAGE a change to ALL upcoming sessions of a repeating class.",
        {"series": S("series id from list_events"), "label": LABEL,
         "field": {"type": "string", "enum": ["name", "start", "end", "book", "days", "until"]},
         "value": S("name | HH:MM | 'none' (end/book) | 'Mon, Wed' | YYYY-MM-DD | book name")},
        ["series", "label", "field", "value"]),
    _fn("delete_series", "STAGE deleting ALL upcoming sessions of a repeating class.",
        {"series": S("series id from list_events"), "label": LABEL}, ["series", "label"]),
    _fn("set_reminder", "STAGE an extra reminder for a task or event, at a date and time.",
        {"item_id": S("task or event id"), "label": LABEL, "date": S(DATE), "time": S("HH:MM")},
        ["item_id", "label", "date", "time"]),
]


# ---------- helpers ----------
def _book(name):
    """-> (ok, book_id, title). 'none'/empty = no book."""
    if not name or str(name).strip().lower() in EMPTY:
        return True, None, None
    hit = search_book(str(name))
    if hit.get("found"):
        return True, hit["page_id"], hit["title"]
    return False, None, f"book '{name}' not found (have: {', '.join(hit.get('available', []))})"


def _row(page_id):
    return _parse_event(notion.pages.retrieve(page_id=page_id), {})


def _valid_date(d):
    return bool(d) and parse_natural_date(str(d)) == str(d)[:10] and len(str(d)) == 10


def _valid_time(t):
    return t is None or parse_time(str(t)) == str(t)


def _describe(tool, a):
    book = lambda: f" [{a['book']}]" if a.get("book") and str(a["book"]).lower() not in EMPTY else ""
    if tool == "create_task":
        due = f" — {a['due_date']}" + (f" {a['due_time']}" if a.get("due_time") else "") if a.get("due_date") else ""
        return f"✅ Add task: {a['name']}{due}{book()}"
    if tool == "create_event":
        when = a["date"] + (f" {a['start_time']}" + (f"–{a['end_time']}" if a.get("end_time") else "")
                            if a.get("start_time") else " (all day)")
        rep = ""
        if a.get("repeat") not in (None, "None"):
            rep = f"\n    🔁 {a['repeat']} {', '.join(a.get('days') or [])}" + (f" until {a['until']}" if a.get("until") else " (12 weeks)")
        return f"📅 Add event: {a['name']} — {when}{book()}{rep}"
    if tool == "update_task":
        return f"✏ Task '{a['label']}': {a['field']} → {a['value']}"
    if tool == "complete_task":
        return f"✅ Complete task '{a['label']}'"
    if tool == "update_event":
        return f"✏ Event '{a['label']}': {a['field']} → {a['value']}"
    if tool == "cancel_session":
        return f"🚫 Cancel '{a['label']}' on {a['date']}"
    if tool == "edit_series":
        return f"✏ Class '{a['label']}' (all upcoming): {a['field']} → {a['value']}"
    if tool == "delete_series":
        return f"🗑 Delete class '{a['label']}' (all upcoming sessions)"
    if tool == "set_reminder":
        return f"⏰ Remind '{a['label']}' at {a['date']} {a['time']}"
    return tool


# ---------- read tools ----------
def _read(name, a):
    if name == "list_tasks":
        view = {"day": "today"}.get(a.get("view", "all"), a.get("view", "all"))
        book_id = None
        if a.get("book"):
            ok, book_id, title = _book(a["book"])
            if not ok:
                return {"error": title}
        rows = list_tasks(view, book_id)
        return {"count": len(rows), "tasks": [
            {"id": t["id"], "name": t["name"], "due": t["deadline"] or None, "time": t["time"],
             "book": t["book"]} for t in rows[:60]]}
    if name == "list_events":
        if not (_valid_date(a.get("from_date")) and _valid_date(a.get("to_date"))):
            return {"error": "dates must be YYYY-MM-DD"}
        d1 = datetime.fromisoformat(a["from_date"]).date()
        d2 = datetime.fromisoformat(a["to_date"]).date()
        if d2 < d1 or (d2 - d1).days > MAX_RANGE_DAYS:
            return {"error": f"range must be 0-{MAX_RANGE_DAYS} days"}
        rows = events_between(d1, d2)
        return {"count": len(rows), "events": [
            {"id": e["id"], "name": e["name"], "date": e["date"], "start": e["start"], "end": e["end"],
             "book": e["book"], "series": e["series"] or None,
             "legacy_rule": e["repeat"] != "None" and not e["series"]} for e in rows[:80]]}
    if name == "list_books":
        c = book_counts()
        return {"books": [{"name": b["title"], "open_tasks": c["by_book"].get(b["page_id"], 0)}
                          for b in list_books(True)], "open_tasks_without_book": c["no_book"]}
    return {"error": f"unknown tool {name}"}


# ---------- stage write tools ----------
def _stage(name, a, staged):
    """Validate, then stage. Returns what the model sees."""
    if name == "create_task":
        if not (a.get("name") or "").strip():
            return {"error": "name required"}
        if a.get("due_date") and not _valid_date(a["due_date"]):
            return {"error": "due_date must be YYYY-MM-DD"}
        if not _valid_time(a.get("due_time")):
            return {"error": "due_time must be HH:MM"}
        if a.get("due_time") and not a.get("due_date"):
            return {"error": "due_time needs due_date"}
        ok, _, t = _book(a.get("book"))
        if not ok:
            return {"error": t}
    elif name == "create_event":
        if not (a.get("name") or "").strip() or not _valid_date(a.get("date")):
            return {"error": "name and date (YYYY-MM-DD) required"}
        if not (_valid_time(a.get("start_time")) and _valid_time(a.get("end_time"))):
            return {"error": "times must be HH:MM"}
        if a.get("until") and not _valid_date(a["until"]):
            return {"error": "until must be YYYY-MM-DD"}
        if a.get("repeat") not in (None, "None", "Weekly", "Daily"):
            return {"error": "repeat must be None, Weekly or Daily"}
        ok, _, t = _book(a.get("book"))
        if not ok:
            return {"error": t}
    elif name == "set_reminder":
        if not (_valid_date(a.get("date")) and _valid_time(a.get("time")) and a.get("time")):
            return {"error": "date YYYY-MM-DD and time HH:MM required"}
        if datetime.fromisoformat(_iso(a["date"], a["time"])) <= datetime.now(TZ):
            return {"error": "that time is already past"}
    elif name in ("update_task", "update_event", "edit_series") and a.get("field") in ("book",):
        ok, _, t = _book(a.get("value"))
        if not ok:
            return {"error": t}
    elif name == "cancel_session" and not _valid_date(a.get("date")):
        return {"error": "date must be YYYY-MM-DD"}
    staged.append({"tool": name, "args": a, "desc": _describe(name, a)})
    return {"status": "staged", "waiting_for_user_confirmation": True, "staged_so_far": len(staged)}


# ---------- execute (after Confirm) ----------
def _exec(action):
    """-> (ok, text)"""
    t, a, desc = action["tool"], action["args"], action["desc"]
    try:
        if t == "create_task":
            ok, bid, title = _book(a.get("book"))
            if not ok:
                return False, f"{desc}\n   ❌ {title}"
            r = create_task_in_db(task_name=a["name"], deadline=a.get("due_date"), book_name=title,
                                  book_id=bid, time=a.get("due_time"))
        elif t == "create_event":
            ok, bid, title = _book(a.get("book"))
            if not ok:
                return False, f"{desc}\n   ❌ {title}"
            rep = a.get("repeat") or "None"
            until = roll_until(a.get("until"), a["date"]) if a.get("until") else None
            r = create_event(a["name"], a["date"], a.get("start_time"), a.get("end_time"), bid,
                             rep, a.get("days") or None, until)
        elif t == "update_task":
            v = a["value"]
            if a["field"] == "book":
                _, v, _ = _book(v)
            r = update_task(a["task_id"], a["field"], v)
        elif t == "complete_task":
            r = mark_done(a["task_id"])
        elif t == "update_event":
            row = _row(a["event_id"])
            if row["repeat"] != "None" and not row["series"]:
                return False, f"{desc}\n   ❌ old repeating row. Send /migrate first."
            v = a["value"]
            if a["field"] == "book":
                _, v, _ = _book(v)
            r = update_event(a["event_id"], a["field"], v)
        elif t == "cancel_session":
            row = _row(a["event_id"])
            if row["repeat"] != "None" and not row["series"]:
                r = add_exception(a["event_id"], a["date"])
            else:
                r = delete_event(a["event_id"])
        elif t == "edit_series":
            v = a["value"]
            if a["field"] == "book":
                _, v, _ = _book(v)
            r = update_series(a["series"], a["field"], v)
        elif t == "delete_series":
            r = update_series(a["series"], "delete", True)
        elif t == "set_reminder":
            r = set_remind(a["item_id"], _iso(a["date"], a["time"]))
        else:
            return False, f"{desc}\n   ❌ unknown action"
        if r.get("status") == "success":
            return True, desc
        return False, f"{desc}\n   ❌ {r.get('message')}"
    except Exception as e:
        logging.exception("agent action failed")
        return False, f"{desc}\n   ❌ {e}"


from groq import BadRequestError

def _llm(messages):
    def call():
        return groq_client.chat.completions.create(
            model=MODEL, temperature=0, messages=messages, tools=TOOLS, tool_choice="auto")
    try:
        return call()
    except BadRequestError as e:
        if "tool_use_failed" not in str(e):
            raise
        logging.warning("bad tool call, retrying")
        messages.append({"role": "user", "content":
            "Your last tool call was invalid. Use only the listed enum values. Omit optional fields instead of null."})
        return call()

async def _dispatch(name, args, staged):
    if name in ("list_tasks", "list_events", "list_books"):
        return await asyncio.to_thread(_read, name, args)
    return await asyncio.to_thread(_stage, name, args, staged)


async def run_agent(user_text: str, ud) -> dict:
    hist = ud.get("chat", [])
    now = datetime.now(TZ).strftime("%A %Y-%m-%d %H:%M")
    messages = ([{"role": "system", "content": SYSTEM.replace("<<NOW>>", now)}]
                + hist + [{"role": "user", "content": user_text}])
    staged, final = [], ""

    for _ in range(MAX_STEPS):
        resp = await asyncio.to_thread(_llm, messages)
        msg = resp.choices[0].message
        calls = msg.tool_calls or []
        if not calls:
            final = (msg.content or "").strip()
            break
        if any(c.function.name == "capture" for c in calls):
            return {"type": "handoff"}
        messages.append({"role": "assistant", "content": msg.content or "", "tool_calls": [
            {"id": c.id, "type": "function",
             "function": {"name": c.function.name, "arguments": c.function.arguments or "{}"}}
            for c in calls]})
        for c in calls:
            try:
                args = json.loads(c.function.arguments or "{}")
                out = await _dispatch(c.function.name, args, staged)
            except Exception as e:
                logging.exception("tool %s failed", c.function.name)
                out = {"error": str(e)}
            messages.append({"role": "tool", "tool_call_id": c.id,
                             "content": json.dumps(out, ensure_ascii=False, default=str)[:12000]})
    else:
        final = final or "That took too many steps, sir. Try a simpler request."

    note = final or ("Proposed: " + "; ".join(s["desc"] for s in staged))
    ud["chat"] = (hist + [{"role": "user", "content": user_text},
                          {"role": "assistant", "content": note}])[-HISTORY:]
    if staged:
        lines = [f"{i}. {s['desc']}" for i, s in enumerate(staged, 1)]
        head = (final + "\n\n") if final else ""
        return {"type": "confirm", "actions": staged,
                "content": (head + "Confirm these, sir?\n" + "\n".join(lines))[:3800]}
    return {"type": "text", "content": final or "No answer, sir. Try again."}


# ---------- Telegram glue ----------
async def _send(update, res):
    if res["type"] == "buttons":
        kb = [[InlineKeyboardButton(b["text"], callback_data=b["callback"])] for b in res["buttons"]]
        await update.message.reply_text(res["content"], reply_markup=InlineKeyboardMarkup(kb))
    else:
        await update.message.reply_text(res["content"])


async def _strip(context, st):
    if st and st.get("msg"):
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=st["msg"][0], message_id=st["msg"][1], reply_markup=None)
        except Exception:
            pass


async def agent_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud, text = context.user_data, update.message.text

    if ud.get("pending"):                       # capture workflow is mid-question
        await _send(update, await process_user_intent(text, context))
        return

    await _strip(context, ud.pop("act", None))  # a new message replaces an old proposal
    try:
        res = await run_agent(text, ud)
    except Exception:
        logging.exception("agent failed, using capture workflow")
        res = {"type": "handoff"}

    if res["type"] == "handoff":
        await _send(update, await process_user_intent(text, context))
    elif res["type"] == "confirm":
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Confirm", callback_data="ag:ok"),
                                    InlineKeyboardButton("✖ Cancel", callback_data="ag:no")]])
        msg = await update.message.reply_text(res["content"], reply_markup=kb)
        ud["act"] = {"actions": res["actions"], "msg": (msg.chat_id, msg.message_id)}
    else:
        await update.message.reply_text(res["content"])


async def on_agent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    st = context.user_data.get("act")
    if st is None:
        await q.answer("Expired. Ask again.", show_alert=True)
        return
    await q.answer()
    context.user_data.pop("act", None)
    try:
        if q.data == "ag:no":
            await q.edit_message_text("Cancelled.")
            return
        lines, fails = [], 0
        for action in st["actions"]:
            ok, line = await asyncio.to_thread(_exec, action)
            fails += not ok
            lines.append(("• " if ok else "❌ ") + line)
        head = "Done, sir." if not fails else f"Done with {fails} problem(s):"
        await q.edit_message_text((head + "\n" + "\n".join(lines))[:3800])
    except BadRequest:
        logging.exception("on_agent edit failed")
    except Exception:
        logging.exception("on_agent failed")
        await q.message.reply_text("❌ An error occurred, sir. Please try again.")