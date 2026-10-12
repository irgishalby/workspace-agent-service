# core/agent.py  -  free-text input: extract tasks + events, ask for what is missing, save
import json, re, asyncio
from datetime import datetime, timedelta
from groq import Groq
from config.settings import GROQ_API_KEY
from core.dates import format_due, format_due_full
from tools.notion_tool import (
    list_books, create_task_in_db, create_book, create_event,
    parse_natural_date, parse_time, _time_input, DAY_NAMES, TZ,
    roll_until as _roll_until,
)

groq_client = Groq(api_key=GROQ_API_KEY)
MODEL = "openai/gpt-oss-120b"

EXTRACT_PROMPT = """You extract tasks and calendar events from a user message.
Today is <<TODAY>>. Timezone Asia/Jakarta.
Return JSON only:
{"items":[{"kind":"task"|"event"|"unclear","name":str,"book":str|null,
"deadline":str|null,"time":str|null,
"date":str|null,"start":str|null,"end":str|null,
"repeat":"None"|"Daily"|"Weekly","days":[str],"until":str|null}],"reply":str|null}

Rules:
- Split lists into separate items. Keep the user's language for names.
- task = something to do, maybe by a deadline (homework, assignment, buy, submit).
- event = happens at a set time or occasion (class, lecture, meeting, exam, appointment, trip).
- unclear = you cannot tell which. Never guess.
- Task: fill deadline/time. Event: fill date/start/end. Leave the others null.
- Relative dates ("today","tomorrow","monday","besok","lusa","next week") stay as typed.
  Explicit calendar dates become YYYY-MM-DD (use today's year).
- Times are 24h HH:MM ("10am" -> "10:00"). "8-10" -> start "08:00", end "10:00".
- Never invent dates or times. Null when absent.
- Routine ("every Monday", "setiap senin", "daily"): repeat Weekly or Daily,
  days like ["Mon","Wed"], until if stated (a date already past this year means next year). Otherwise repeat "None", days [].
- book: only if the user explicitly names a book/subject/project. Else null.
- Not a task or event (chat, question): items [] and a short English answer in reply.
- If the user asks for a note, call search_book first. If missing, report unavailable and list active notebooks.
- If the book exists, call create_book if needed, then create_task_in_db with book_name and content.
- If the user asks for a task, call create_task_in_db with task_name, deadline and book_name.
- If the user asks for an event, call create_event with name, date, start_time, end_time, book_name.
- If the user asks for a summary, call summary_cmd or summary_text. If the user asks for a migration, call migrate_legacy.
- Don't use ** for markdown. Emojis are allowed. Use "✅" for done, "✏" for edit, "🚫" for cancel, "🗑" for delete, "⏰" for reminder, "🔁" for repeat.
"""

SKIP = {"skip", "none", "no", "nope", "-", "ga", "gak", "tidak", "nggak", "lewati"}
TEXT_STAGES = {"deadline", "time", "date", "start", "until", "newbook"}


def text(content):
    return {"type": "text", "content": content}

def buttons(content, btns):
    return {"type": "buttons", "content": content, "buttons": btns}


# ---------- extraction ----------
def _extract(user_text: str) -> dict:
    now = datetime.now(TZ)
    prompt = EXTRACT_PROMPT.replace("<<TODAY>>", now.strftime("%Y-%m-%d (%A)"))
    r = groq_client.chat.completions.create(
        model=MODEL, temperature=0,
        messages=[{"role": "system", "content": prompt},
                  {"role": "user", "content": user_text}],
    )
    raw = r.choices[0].message.content or ""
    m = re.search(r"\{.*\}", raw, re.S)
    try:
        return json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        return {}

def _match_book(name: str, hint, titles: list):
    for src in filter(None, (hint, name)):
        s = src.lower()
        for t in titles:
            tl = t.lower().strip()
            if tl and (tl in s or (src is hint and s in tl)):
                return t
    return None

def _set_kind(it: dict, kind: str):
    it["kind"] = kind
    if kind == "task":
        it["deadline"] = it["deadline"] or it["date"]
        it["time"] = it["time"] or it["start"]
    elif kind == "event":
        it["date"] = it["date"] or it["deadline"]
        it["start"] = it["start"] or it["time"]

def _first_occurrence(days: list, repeat: str):
    today = datetime.now(TZ).date()
    if repeat == "Daily" or not days:
        return today.isoformat() if repeat == "Daily" else None
    for n in range(7):
        d = today + timedelta(days=n)
        if DAY_NAMES[d.weekday()] in days:
            return d.isoformat()
    return None

def _norm(raw: dict, titles: list):
    name = (raw.get("name") or "").strip()
    if not name:
        return None
    days = []
    for d in raw.get("days") or []:
        d = str(d)[:3].title()
        if d in DAY_NAMES and d not in days:
            days.append(d)
    rep = str(raw.get("repeat") or "None").title()
    if rep not in ("None", "Daily", "Weekly"):
        rep = "None"
    kind = raw.get("kind") if raw.get("kind") in ("task", "event") else "unclear"
    if rep != "None":
        kind = "event"

    it = {
        "kind": "unclear", "name": name,
        "book": _match_book(name, raw.get("book"), titles),
        "deadline": parse_natural_date(raw.get("deadline") or ""),
        "time": parse_time(raw.get("time") or "") or parse_time(raw.get("deadline") or ""),
        "date": parse_natural_date(raw.get("date") or ""),
        "start": parse_time(raw.get("start") or "") or parse_time(raw.get("date") or ""),
        "end": parse_time(raw.get("end") or ""),
        "repeat": rep, "days": days,
        "until": parse_natural_date(raw.get("until") or ""),
    }
    if kind != "unclear":
        _set_kind(it, kind)
    if it["kind"] == "event" and rep != "None" and not it["date"]:
        it["date"] = _first_occurrence(days, rep)
    return it


# ---------- flow ----------
async def process_user_intent(user_text: str, context) -> dict:
    ud = context.user_data
    pending = ud.get("pending")

    if user_text.startswith("book:"):
        if not pending:
            return text("Session expired. Please resend.")
        return await _on_book_choice(user_text[5:], ud)

    if user_text.startswith("kind:"):
        if not pending or pending["stage"] != "kind":
            return text("Session expired. Please resend.")
        _set_kind(pending["items"][pending["cur"]], "task" if user_text == "kind:t" else "event")
        return await _advance(ud)

    if pending and pending["stage"] in TEXT_STAGES:
        res = await _on_text(user_text, ud)
        if res is not None:
            return res
        ud.pop("pending", None)   # long message = new request

    data = await asyncio.to_thread(_extract, user_text)
    raw_items = data.get("items") or []
    if not raw_items:
        return text(data.get("reply") or "No task or event detected, sir.")

    books = await asyncio.to_thread(list_books)
    titles = [b["title"] for b in books]
    items = [i for i in (_norm(r, titles) for r in raw_items) if i]
    if not items:
        return text(data.get("reply") or "No task or event detected, sir.")

    ud["pending"] = {"items": items, "books": [{"id": b["page_id"], "title": b["title"]} for b in books],
                     "stage": None, "cur": None}
    return await _advance(ud)


def _need(p: dict):
    items = p["items"]
    for i, t in enumerate(items):
        if t["kind"] == "unclear":
            return "kind", i
    tasks = [t for t in items if t["kind"] == "task"]
    if any(not t["deadline"] and not t.get("dl_done") for t in tasks):
        return "deadline", None
    if any(t["deadline"] and not t["time"] and t.get("asked_dl") and not t.get("time_done") for t in tasks):
        return "time", None
    for i, t in enumerate(items):
        if t["kind"] != "event":
            continue
        if not t["date"]:
            return "date", i
        if not t["start"] and not t.get("start_done"):
            return "start", i
        if t["repeat"] != "None" and not t["until"] and not t.get("until_done"):
            return "until", i
    if any(t["book"] is None for t in items):
        return "book", None
    return None, None

async def _advance(ud) -> dict:
    p = ud["pending"]
    stage, idx = _need(p)
    p["stage"], p["cur"] = stage, idx
    name = p["items"][idx]["name"] if idx is not None else ""
    many = sum(1 for t in p["items"] if t["kind"] == "task" and not t["deadline"]) > 1

    if stage == "kind":
        return buttons(f"'{name}': task or event, sir?",
                       [{"text": "✅ Task", "callback": "kind:t"}, {"text": "📅 Event", "callback": "kind:e"}])
    if stage == "deadline":
        return text(("When is the deadline for these tasks?" if many else "When is the deadline?")
                    + " ('skip' for none)")
    if stage == "time":
        return text("What time? ('skip' for none)")
    if stage == "date":
        return text(f"What date is '{name}'?")
    if stage == "start":
        return text(f"Start time for '{name}'? ('skip' for all-day)")
    if stage == "until":
        return text(f"'{name}' repeats. Until when? ('skip' for 12 weeks)")
    if stage == "book":
        btns = [{"text": b["title"], "callback": f"book:{i}"} for i, b in enumerate(p["books"])]
        btns += [{"text": "➕ New notebook", "callback": "book:new"},
                 {"text": "No notebook", "callback": "book:none"}]
        return buttons("Which notebook, sir?", btns)
    return await _finish(ud)


async def _on_text(txt: str, ud) -> dict | None:
    p, stage, low = ud["pending"], ud["pending"]["stage"], txt.strip().lower()
    items = p["items"]
    skip = low in SKIP

    if stage == "newbook":
        return await _on_new_book(txt.strip(), ud)

    if len(txt.split()) > 4:      # long message = new request, not an answer
        return None

    if stage == "deadline":
        targets = [t for t in items if t["kind"] == "task" and not t["deadline"] and not t.get("dl_done")]
        if skip:
            for t in targets:
                t["dl_done"] = True
            return await _advance(ud)
        d = parse_natural_date(txt)
        if not d:
            return text("Didn't get that date. Try 'friday', 'tomorrow' or 20 dec. Or 'skip'.")
        tm = parse_time(txt)
        for t in targets:
            t.update(deadline=d, time=tm or t["time"], dl_done=True, asked_dl=True)
        return await _advance(ud)

    if stage == "time":
        targets = [t for t in items if t["kind"] == "task" and t["deadline"] and not t["time"]
                   and t.get("asked_dl") and not t.get("time_done")]
        if skip:
            for t in targets:
                t["time_done"] = True
            return await _advance(ud)
        tm = _time_input(txt)
        if not tm:
            return text("Didn't get that time. Try 14:00 or 2pm. Or 'skip'.")
        for t in targets:
            t["time"] = tm
        return await _advance(ud)

    it = items[p["cur"]]
    if stage == "date":
        d = parse_natural_date(txt)
        if not d:
            return text("Didn't get that date. Try 'friday', 'tomorrow' or 20 dec.")
        it["date"] = d
        it["start"] = it["start"] or parse_time(txt)
    elif stage == "start":
        if skip:
            it["start_done"] = True
        else:
            tm = _time_input(txt)
            if not tm:
                return text("Didn't get that time. Try 14:00 or 2pm. Or 'skip'.")
            it["start"] = tm
    elif stage == "until":
        if skip:
            it["until_done"] = True
        else:
            d = parse_natural_date(txt)
            if not d:
                return text("Didn't get that date. Try 20 dec or 2026-12-20. Or 'skip'.")
            it["until"] = d
    return await _advance(ud)

async def _on_book_choice(choice: str, ud) -> dict:
    p = ud["pending"]
    if choice == "new":
        p["stage"] = "newbook"
        return text("Specify new notebook name. (/cancel to abort)")
    if choice == "none":
        picked = "none"
    else:
        try:
            picked = p["books"][int(choice)]["title"]
        except (ValueError, IndexError):
            return text("Invalid selection. Resend.")
    for t in p["items"]:
        if t["book"] is None:
            t["book"] = picked
    return await _advance(ud)

async def _on_new_book(name: str, ud) -> dict:
    p = ud["pending"]
    if not name:
        return text("Notebook name required.")
    books = await asyncio.to_thread(list_books, True)
    hit = next((b for b in books if b["title"].lower().strip() == name.lower()), None)
    if hit:
        book_id, title = hit["page_id"], hit["title"]
    else:
        r = await asyncio.to_thread(create_book, name)
        if r.get("status") != "success":
            ud.pop("pending", None)
            return text(f"❌ Failed creating notebook: {r.get('message')}")
        book_id, title = r["page_id"], r["title"]
    if not any(b["title"] == title for b in p["books"]):
        p["books"].append({"id": book_id, "title": title})
    for t in p["items"]:
        if t["book"] is None:
            t["book"] = title
    return await _advance(ud)


# ---------- save ----------
def _book_id(p: dict, title):
    return next((b["id"] for b in p["books"] if b["title"] == title), None)

def _event_line(e: dict, r: dict) -> str:
    when = format_due(e["date"])
    if e["start"]:
        when += f" {e['start']}" + (f"–{e['end']}" if e["end"] else "")
    else:
        when += " (all day)"
    line = f"📅 {e['name']} — {when}"
    if e["book"] and e["book"] != "none":
        line += f" [{e['book']}]"
    if e["repeat"] != "None":
        days = ", ".join(e["days"]) if e["repeat"] == "Weekly" and e["days"] else ""
        line += f"\n🔁 {e['repeat']} {days}".rstrip()
        until = r.get("until") or e["until"]
        if until:
            line += f" until {until}"
        if r.get("count"):
            line += f" ({r['count']} sessions)"
        if not e["until"]:
            line += " — no end given, 12 weeks"
    line += "\n⏰ 1 day before, 1h30 before"
    return line

async def _finish(ud) -> dict:
    p = ud.pop("pending")
    lines = []
    for t in p["items"]:
        title = None if t["book"] in (None, "none") else t["book"]
        bid = _book_id(p, title) if title else None

        if t["kind"] == "task":
            r = await asyncio.to_thread(
                create_task_in_db, task_name=t["name"], deadline=t["deadline"],
                book_name=title, book_id=bid, time=t["time"])
            if r.get("status") == "success":
                line = f"⚡ {r['task_name']}"
                if r["deadline"]:
                    line += f" — {format_due_full(r['deadline'], t['time'])}"
                if r.get("book"):
                    line += f" [{r['book']}]"
                if r.get("warning"):
                    line += f"\n⚠️ {r['warning']}"
            else:
                line = f"❌ {t['name']}: {r.get('message')}"
        else:
            t["until"] = _roll_until(t["until"], t["date"])
            r = await asyncio.to_thread(
                create_event, t["name"], t["date"], t["start"], t["end"], bid,
                t["repeat"], t["days"] or None, t["until"])
            line = (_event_line(t, r) if r.get("status") == "success"
                    else f"❌ {t['name']}: {r.get('message')}")
        lines.append(line)
    return text("\n".join(lines))