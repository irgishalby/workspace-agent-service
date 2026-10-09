import json, re, asyncio
from groq import Groq
from config.settings import GROQ_API_KEY
from tools.notion_tool import (_list_books, create_task_in_db, create_book,
                               parse_natural_date, parse_time)

groq_client = Groq(api_key=GROQ_API_KEY)
MODEL = "openai/gpt-oss-120b"

EXTRACT_PROMPT = """Extract tasks from the user's message.
Return ONLY JSON, no other text:
{"tasks":[{"name":str,"deadline":str|null,"time":str|null,"book":str|null}],"reply":str|null}

Rules:
- Split lists into separate tasks. Keep the user's wording and language.
- deadline: the exact words the user used for the date ("senin", "besok"). null if none. Never invent one.
- time: only if the user gave a clock time ("jam 10", "14:00"). Copy the user's words. null otherwise.
- book: only if the user explicitly names a book or project. Otherwise null.
- If the message is not a task (chat, question), return tasks=[] and put a short answer in reply, in the user's language."""

def text(content):
    return {"type": "text", "content": content}

def buttons(content, btns):
    return {"type": "buttons", "content": content, "buttons": btns}


def _extract(user_text: str) -> dict:
    r = groq_client.chat.completions.create(
        model=MODEL, temperature=0,
        messages=[{"role": "system", "content": EXTRACT_PROMPT},
                  {"role": "user", "content": user_text}],
    )
    raw = r.choices[0].message.content or ""
    m = re.search(r"\{.*\}", raw, re.S)
    try:
        return json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        return {}


def _match_book(name: str, hint, titles: list):
    """Return an existing book title found in the hint or the task name, else None."""
    for src in (hint, name):
        if not src:
            continue
        s = src.lower()
        for t in titles:
            tl = t.lower().strip()
            if tl and (tl in s or (src is hint and s in tl)):
                return t
    return None


async def process_user_intent(user_text: str, context) -> dict:
    ud = context.user_data
    pending = ud.get("pending")

    # Button press
    if user_text.startswith("book:"):
        if not pending:
            return text("Sesi habis. Kirim ulang task-nya.")
        return await _on_book_choice(user_text[5:], ud)

    # Answer to "Nama buku barunya?"
    if pending and pending["stage"] == "newbook":
        return await _on_new_book(user_text.strip(), ud)

    # Answer to "Deadline-nya kapan?"
    if pending and pending["stage"] == "deadline":
        if parse_natural_date(user_text):
            tm = parse_time(user_text)
            for t in pending["tasks"]:
                if not t["deadline"]:
                    t["deadline"] = user_text
                    if tm:
                        t["time"] = tm
            return await _advance(ud)
        ud.pop("pending", None)

    # New message
    data = await asyncio.to_thread(_extract, user_text)
    raw_tasks = data.get("tasks") or []
    if not raw_tasks:
        return text(data.get("reply") or "Task apa yang mau dicatat?")

    books = await asyncio.to_thread(_list_books)
    titles = [b["title"] for b in books]

    tasks = []
    for t in raw_tasks:
        tasks.append({
            "name": t.get("name", "").strip(),
            "deadline": (t.get("deadline") or "").strip() or None,
            "time": parse_time(t.get("time") or "") or parse_time(t.get("deadline") or ""),
            "book": _match_book(t.get("name", ""), t.get("book"), titles),
        })
    ud["pending"] = {"tasks": tasks, "titles": titles, "stage": None}
    return await _advance(ud)


async def _advance(ud) -> dict:
    p = ud["pending"]

    if any(not t["deadline"] for t in p["tasks"]):
        p["stage"] = "deadline"
        if len(p["tasks"]) == 1:
            return text("Deadline-nya kapan?")
        return text("Deadline-nya kapan untuk semua task?")

    if any(t["book"] is None for t in p["tasks"]):
        p["stage"] = "book"
        btns = [{"text": title, "callback": f"book:{i}"}
                for i, title in enumerate(p["titles"])]
        btns.append({"text": "➕ Buku baru", "callback": "book:new"})
        btns.append({"text": "Tanpa buku", "callback": "book:none"})
        return buttons("Masuk buku mana?", btns)

    return await _finish(ud)


async def _on_book_choice(choice: str, ud) -> dict:
    p = ud["pending"]
    if choice == "new":
        p["stage"] = "newbook"
        return text("Nama buku barunya? (/cancel untuk batal)")
    if choice == "none":
        picked = "none"
    else:
        try:
            picked = p["titles"][int(choice)]
        except (ValueError, IndexError):
            return text("Pilihan tidak valid. Kirim ulang task-nya.")
    for t in p["tasks"]:
        if t["book"] is None:
            t["book"] = picked
    return await _finish(ud)


async def _on_new_book(name: str, ud) -> dict:
    """Handle user input for new book name."""
    p = ud["pending"]
    if not name:
        return text("Nama buku kosong. Ketik nama bukunya.")

    # Reuse if it already exists (exact, case-insensitive)
    books = await asyncio.to_thread(_list_books)
    hit = next((b for b in books if b["title"].lower().strip() == name.lower()), None)

    if hit:
        book_id, title = hit["page_id"], hit["title"]
    else:
        r = await asyncio.to_thread(create_book, name)
        if r.get("status") != "success":
            ud.pop("pending", None)
            return text(f"❌ Gagal bikin buku: {r.get('message')}")
        book_id, title = r["page_id"], r["title"]

    for t in p["tasks"]:
        if t["book"] is None:
            t["book"] = title
            t["book_id"] = book_id
    return await _finish(ud)


async def _finish(ud) -> dict:
    p = ud.pop("pending")
    lines = []
    for t in p["tasks"]:
        r = await asyncio.to_thread(
            create_task_in_db,
            task_name=t["name"], deadline=t["deadline"],
            book_name=t["book"] or "none",
            book_id=t.get("book_id"),
            time=t.get("time"),
        )
        if r.get("status") == "success":
            when = r["deadline"] + (f" {t['time']}" if t.get("time") else "")
            line = f"✅ {r['task_name']} — {when}"
            if r.get("book"):
                line += f" ({r['book']})"
            if r.get("warning"):
                line += f"\n⚠️ {r['warning']}"
        else:
            line = f"❌ {t['name']}: {r.get('message')}"
        lines.append(line)
    return text("\n".join(lines))