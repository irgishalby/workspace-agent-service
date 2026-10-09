import os
import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from notion_client import Client
from config.settings import NOTION_TOKEN, BOOKS_PAGE_ID, TASKS_DATABASE_ID

notion = Client(auth=NOTION_TOKEN)
TZ = ZoneInfo("Asia/Jakarta")

# State file for persistent conversation state
STATE_FILE = "conversation_state.json"

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f)

DAYS = {
    "monday": 0, "senin": 0, "tuesday": 1, "selasa": 1,
    "wednesday": 2, "rabu": 2, "thursday": 3, "kamis": 3,
    "friday": 4, "jumat": 4, "jum'at": 4, "saturday": 5, "sabtu": 5,
    "sunday": 6, "minggu": 6, "ahad": 6,
}

def parse_natural_date(date_text: str):
    """Parse natural language date to YYYY-MM-DD format, supports English and Indonesian."""
    t = date_text.lower().strip()
    today = datetime.now(TZ).date()

    fixed = {"today": 0, "now": 0, "hari ini": 0,
             "tomorrow": 1, "besok": 1, "esok": 1,
             "day after tomorrow": 2, "lusa": 2}
    if t in fixed:
        return (today + timedelta(days=fixed[t])).isoformat()

    if any(k in t for k in ("next week", "minggu depan", "pekan depan")):
        return (today + timedelta(days=7)).isoformat()
    if any(k in t for k in ("next month", "bulan depan")):
        return (today + timedelta(days=30)).isoformat()

    for word, idx in DAYS.items():
        if re.search(rf"\b{re.escape(word)}\b", t):
            ahead = (idx - today.weekday()) % 7 or 7
            return (today + timedelta(days=ahead)).isoformat()

    for fmt in ["%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"]:
        try:
            return datetime.strptime(t, fmt).date().isoformat()
        except ValueError:
            pass
    return None

def parse_time(text: str):
    """'jam 10', 'pukul 9.30', '14:00', '5pm' -> 'HH:MM'. None if no time."""
    t = text.lower()
    m = re.search(r"(?:jam|pukul)\s*(\d{1,2})(?:[:.](\d{2}))?", t) \
        or re.search(r"\b(\d{1,2}):(\d{2})\b", t) \
        or re.search(r"\b(\d{1,2})()\s*(?:am|pm)\b", t)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    if re.search(r"\bpm\b|sore|malam", t) and h < 12:
        h += 12
    if h > 23 or mi > 59:
        return None
    return f"{h:02d}:{mi:02d}"

def _list_books():
    """List all books from database with pagination support."""
    books = []
    cursor = None

    # Normalize BOOKS_PAGE_ID to have no dashes for comparison
    books_id_normalized = BOOKS_PAGE_ID.replace("-", "")

    while True:
        # Use search API to query all pages in the Books database
        kwargs = {
            "query": "",
            "filter": {"property": "object", "value": "page"},
            "page_size": 100
        }
        if cursor:
            kwargs["start_cursor"] = cursor

        result = notion.search(**kwargs)

        # Filter for pages in the Books database
        for page in result.get("results", []):
            parent = page.get("parent", {})
            # Handle both old (database_id) and new (data_source_id) parent types
            parent_db_id = None
            if parent.get("type") == "database_id":
                parent_db_id = parent.get("database_id")
            elif parent.get("type") == "data_source_id":
                parent_db_id = parent.get("database_id")  # New API structure has both

            # Normalize parent_db_id for comparison (remove dashes)
            if parent_db_id and parent_db_id.replace("-", "") == books_id_normalized:
                props = page.get("properties", {})
                # Try common property names for title
                title_prop = props.get("Name") or props.get("Title") or props.get("title")
                if title_prop and title_prop.get("title"):
                    title_text = title_prop["title"][0]["plain_text"] if title_prop["title"] else "Untitled"
                    books.append({"page_id": page["id"], "title": title_text})

        if not result.get("has_more"):
            break
        cursor = result.get("next_cursor")

    return books

def search_book(book_name: str) -> dict:
    """Search for a book by name with exact and fuzzy matching."""
    q = book_name.lower().strip()
    books = _list_books()
    for b in books:  # exact match
        if b["title"].lower().strip() == q:
            return {"found": True, **b}
    for b in books:  # fuzzy match
        t = b["title"].lower()
        if q in t or t in q:
            return {"found": True, **b}
    return {"found": False, "available": [b["title"] for b in books]}

def create_book(book_name: str) -> dict:
    """Create a new book entry in the Books database."""
    try:
        # Find the title column name (Name, Title, ...)
        title_key = "Name"
        try:
            db = notion.databases.retrieve(database_id=BOOKS_PAGE_ID)
            src = (db.get("data_sources") or [None])[0]
            schema = (notion.data_sources.retrieve(data_source_id=src["id"])
                      if src else db)
            for k, v in schema.get("properties", {}).items():
                if v.get("type") == "title":
                    title_key = k
                    break
        except Exception:
            pass
        page = notion.pages.create(
            parent={"database_id": BOOKS_PAGE_ID},
            properties={title_key: {"title": [{"text": {"content": book_name}}]}},
        )
        return {"status": "success", "page_id": page["id"], "title": book_name}
    except Exception as e:
        return {"status": "error", "message": str(e)}

EMPTY = {"", "none", "no", "skip", "ga", "gak", "tidak", "nggak", "-"}

def create_task_in_db(task_name: str, deadline: str, book_name: str = None, complete: bool = False, book_id: str = None, time: str = None) -> dict:
    """Create a task in the Tasks database with deadline, book relation, and complete status."""
    try:
        parsed = parse_natural_date(deadline)
        if not parsed:
            return {"status": "error", "message": f"Bad deadline: {deadline}"}

        start = f"{parsed}T{time}:00+07:00" if time else parsed
        props = {
            "Name": {"title": [{"text": {"content": task_name}}]},
            "Deadline": {"date": {"start": start}},
            "Complete": {"checkbox": complete},
        }

        linked, warning = None, None
        if book_id:
            props["Books"] = {"relation": [{"id": book_id}]}
            linked = book_name
        elif book_name and book_name.strip().lower() not in EMPTY:
            hit = search_book(book_name)
            if hit.get("found"):
                props["Books"] = {"relation": [{"id": hit["page_id"]}]}
                linked = hit["title"]
            else:
                warning = f"Book '{book_name}' not found. Task saved without book."

        page = notion.pages.create(parent={"database_id": TASKS_DATABASE_ID}, properties=props)
        return {"status": "success", "task_id": page["id"], "task_name": task_name,
                "deadline": parsed, "book": linked, "warning": warning}
    except Exception as e:
        return {"status": "error", "message": str(e)}

def _query_tasks(filter_obj: dict) -> list:
    """Query tasks database with filter."""
    results = []
    cursor = None
    while True:
        kwargs = {"database_id": TASKS_DATABASE_ID, "filter": filter_obj, "page_size": 100}
        if cursor:
            kwargs["start_cursor"] = cursor
        resp = notion.databases.query(**kwargs)
        results.extend(resp.get("results", []))
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")
    return results

def list_tasks(view: str) -> list:
    """view: today | tomorrow | overdue | week. Open tasks only."""
    today = datetime.now(TZ).date()
    d = lambda n: (today + timedelta(days=n)).isoformat()
    not_done = {"property": "Complete", "checkbox": {"equals": False}}

    def rng(a, b):  # a <= deadline < b
        return {"and": [
            {"property": "Deadline", "date": {"on_or_after": d(a)}},
            {"property": "Deadline", "date": {"before": d(b)}},
        ]}

    date_f = {"today": rng(0, 1), "tomorrow": rng(1, 2), "week": rng(0, 8),
              "overdue": {"property": "Deadline", "date": {"before": d(0)}}}[view]

    rows = _query_tasks({"and": [date_f, not_done]})

    titles = {}
    if any(r["properties"].get("Books", {}).get("relation") for r in rows):
        titles = {b["page_id"]: b["title"] for b in _list_books()}

    out = []
    for r in rows:
        p = r["properties"]
        name = "".join(t["plain_text"] for t in p["Name"]["title"]) or "(tanpa nama)"
        start = (p["Deadline"].get("date") or {}).get("start", "") or ""
        day, tm = start[:10], None
        if len(start) > 10:
            try:
                dt = datetime.fromisoformat(start).astimezone(TZ)
                day, tm = dt.date().isoformat(), dt.strftime("%H:%M")
            except ValueError:
                pass
        rel = p.get("Books", {}).get("relation") or []
        book = titles.get(rel[0]["id"]) if rel else None
        out.append({"id": r["id"], "name": name, "deadline": day,
                    "time": tm, "book": book})
    return out

def mark_done(page_id: str) -> dict:
    """Mark a task as complete."""
    try:
        notion.pages.update(page_id=page_id, properties={"Complete": {"checkbox": True}})
        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

def add_note_to_book(page_id: str, content: str) -> dict:
    """Append note content as blocks to a book page."""
    try:
        # Split content into paragraphs and create blocks
        paragraphs = [p.strip() for p in content.split('\n\n') if p.strip()]
        if not paragraphs:
            paragraphs = [content]

        blocks = []
        for para in paragraphs:
            # Check if it looks like a bullet list
            lines = para.split('\n')
            if all(line.strip().startswith(('- ', '* ', '• ')) for line in lines if line.strip()):
                # Create bulleted list items
                for line in lines:
                    line = line.strip()
                    if line:
                        text = line[2:].strip() if line.startswith(('- ', '* ')) else line[1:].strip()
                        blocks.append({
                            "object": "block",
                            "type": "bulleted_list_item",
                            "bulleted_list_item": {
                                "rich_text": [{"type": "text", "text": {"content": text}}]
                            }
                        })
            else:
                blocks.append({
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [{"type": "text", "text": {"content": para}}]
                    }
                })

        notion.blocks.children.append(block_id=page_id, children=blocks)
        return {"status": "success", "blocks_added": len(blocks)}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# For backward compatibility
def add_task(page_id: str, task_title: str) -> dict:
    notion.blocks.children.append(
        block_id=page_id,
        children=[{
            "object": "block",
            "type": "to_do",
            "to_do": {"rich_text": [{"type": "text", "text": {"content": task_title}}]}
        }]
    )
    return {"status": "success", "task": task_title}

_ds_cache = {}

def _tasks_ds_id():
    """New Notion API queries data sources. Falls back to None on old API."""
    if "tasks" not in _ds_cache:
        try:
            db = notion.databases.retrieve(database_id=TASKS_DATABASE_ID)
            src = db.get("data_sources") or []
            _ds_cache["tasks"] = src[0]["id"] if src else None
        except Exception:
            _ds_cache["tasks"] = None
    return _ds_cache["tasks"]

def _query_tasks(flt):
    ds = _tasks_ds_id()
    rows, cursor = [], None
    while True:
        kw = {"filter": flt, "page_size": 100,
              "sorts": [{"property": "Deadline", "direction": "ascending"}]}
        if cursor:
            kw["start_cursor"] = cursor
        if ds:
            res = notion.data_sources.query(data_source_id=ds, **kw)
        else:
            res = notion.databases.query(database_id=TASKS_DATABASE_ID, **kw)
        rows += res["results"]
        if not res.get("has_more"):
            return rows
        cursor = res["next_cursor"]

def list_tasks(view: str) -> list:
    """view: today | tomorrow | overdue | week. Open tasks only."""
    today = datetime.now(TZ).date()
    d = lambda n: (today + timedelta(days=n)).isoformat()
    not_done = {"property": "Complete", "checkbox": {"equals": False}}

    def rng(a, b):  # a <= deadline < b
        return {"and": [
            {"property": "Deadline", "date": {"on_or_after": d(a)}},
            {"property": "Deadline", "date": {"before": d(b)}},
        ]}

    date_f = {"today": rng(0, 1), "tomorrow": rng(1, 2), "week": rng(0, 8),
              "overdue": {"property": "Deadline", "date": {"before": d(0)}}}[view]

    rows = _query_tasks({"and": [date_f, not_done]})

    titles = {}
    if any(r["properties"].get("Books", {}).get("relation") for r in rows):
        titles = {b["page_id"]: b["title"] for b in _list_books()}

    out = []
    for r in rows:
        p = r["properties"]
        name = "".join(t["plain_text"] for t in p["Name"]["title"]) or "(tanpa nama)"
        start = (p["Deadline"].get("date") or {}).get("start", "") or ""
        day, tm = start[:10], None
        if len(start) > 10:
            try:
                dt = datetime.fromisoformat(start).astimezone(TZ)
                day, tm = dt.date().isoformat(), dt.strftime("%H:%M")
            except ValueError:
                pass
        rel = p.get("Books", {}).get("relation") or []
        book = titles.get(rel[0]["id"]) if rel else None
        out.append({"id": r["id"], "name": name, "deadline": day,
                    "time": tm, "book": book})
    return out

def mark_done(page_id: str) -> dict:
    try:
        notion.pages.update(page_id=page_id,
                            properties={"Complete": {"checkbox": True}})
        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

def create_book(book_name: str) -> dict:
    try:
        # find the title column name (Name, Title, ...)
        title_key = "Name"
        try:
            db = notion.databases.retrieve(database_id=BOOKS_PAGE_ID)
            src = (db.get("data_sources") or [None])[0]
            schema = (notion.data_sources.retrieve(data_source_id=src["id"])
                      if src else db)
            for k, v in schema.get("properties", {}).items():
                if v.get("type") == "title":
                    title_key = k
                    break
        except Exception:
            pass
        page = notion.pages.create(
            parent={"database_id": BOOKS_PAGE_ID},
            properties={title_key: {"title": [{"text": {"content": book_name}}]}},
        )
        return {"status": "success", "page_id": page["id"], "title": book_name}
    except Exception as e:
        return {"status": "error", "message": str(e)}

def parse_time(text: str):
    """'jam 10', 'pukul 9.30', '14:00', '5pm' -> 'HH:MM'. None if no time."""
    t = text.lower()
    m = re.search(r"(?:jam|pukul)\s*(\d{1,2})(?:[:.](\d{2}))?", t) \
        or re.search(r"\b(\d{1,2}):(\d{2})\b", t) \
        or re.search(r"\b(\d{1,2})()\s*(?:am|pm)\b", t)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    if re.search(r"\bpm\b|sore|malam", t) and h < 12:
        h += 12
    if h > 23 or mi > 59:
        return None
    return f"{h:02d}:{mi:02d}"


