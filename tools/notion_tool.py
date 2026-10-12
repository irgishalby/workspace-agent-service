"""Notion layer: Books, Tasks, Calendar. Single copy of every function."""
import re
import uuid
import time as _time
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from notion_client import Client
from config.settings import (
    NOTION_TOKEN, BOOKS_PAGE_ID, TASKS_DATABASE_ID, CALENDAR_DATABASE_ID,
)

notion = Client(auth=NOTION_TOKEN)
TZ = ZoneInfo("Asia/Jakarta")
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
EMPTY = {"", "none", "no", "skip", "ga", "gak", "tidak", "nggak", "-"}
NOT_DONE = {"property": "Complete", "checkbox": {"equals": False}}


# ---------- date / time parsing ----------
DAYS = {
    "monday": 0, "senin": 0, "tuesday": 1, "selasa": 1,
    "wednesday": 2, "rabu": 2, "thursday": 3, "kamis": 3,
    "friday": 4, "jumat": 4, "jum'at": 4, "saturday": 5, "sabtu": 5,
    "sunday": 6, "minggu": 6, "ahad": 6,
}


MONTHS = {
    "jan": 1, "january": 1, "januari": 1, "feb": 2, "february": 2, "februari": 2,
    "mar": 3, "march": 3, "maret": 3, "apr": 4, "april": 4, "may": 5, "mei": 5,
    "jun": 6, "june": 6, "juni": 6, "jul": 7, "july": 7, "juli": 7,
    "aug": 8, "august": 8, "agu": 8, "agustus": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "okt": 10, "oktober": 10, "nov": 11, "november": 11,
    "nop": 11, "nopember": 11, "dec": 12, "december": 12, "des": 12, "desember": 12,
}


def _month_date(t: str, today):
    """'20 dec', 'dec 20', '20 desember 2026' -> ISO. Past dates without year roll to next year."""
    pats = (r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]+)(?:,?\s+(\d{4}))?\b",
            r"\b([a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?\b")
    for i, pat in enumerate(pats):
        for m in re.finditer(pat, t):
            day, mon = (m.group(1), m.group(2)) if i == 0 else (m.group(2), m.group(1))
            if mon not in MONTHS:
                continue
            try:
                yr = int(m.group(3)) if m.group(3) else today.year
                d = date(yr, MONTHS[mon], int(day))
                if not m.group(3) and d < today:
                    d = date(yr + 1, MONTHS[mon], int(day))
                return d.isoformat()
            except ValueError:
                continue
    return None


def parse_natural_date(date_text: str):
    """Natural date (EN/ID) -> 'YYYY-MM-DD'. None if unknown."""
    t = (date_text or "").lower().strip()
    today = datetime.now(TZ).date()

    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", t)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            return None

    fixed = {"today": 0, "now": 0, "hari ini": 0,
             "tomorrow": 1, "besok": 1, "esok": 1,
             "day after tomorrow": 2, "lusa": 2}
    for word in sorted(fixed, key=len, reverse=True):
        if re.search(rf"\b{re.escape(word)}\b", t):
            return (today + timedelta(days=fixed[word])).isoformat()
    if any(k in t for k in ("next week", "minggu depan", "pekan depan")):
        return (today + timedelta(days=7)).isoformat()
    if any(k in t for k in ("next month", "bulan depan")):
        return (today + timedelta(days=30)).isoformat()

    d = _month_date(t, today)
    if d:
        return d

    for word, idx in DAYS.items():
        if re.search(rf"\b{re.escape(word)}\b", t):
            ahead = (idx - today.weekday()) % 7 or 7
            return (today + timedelta(days=ahead)).isoformat()

    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(t, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def parse_time(text: str):
    """'jam 10', 'pukul 9.30', '14:00', '5pm' -> 'HH:MM'. None if no time."""
    t = (text or "").lower()
    m = re.search(r"(?:jam|pukul)\s*(\d{1,2})(?:[:.](\d{2}))?", t) \
        or re.search(r"\b(\d{1,2}):(\d{2})\b", t) \
        or re.search(r"\b(\d{1,2})()\s*(?:am|pm)\b", t)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    if re.search(r"\d\s*pm\b|\bpm\b|sore|malam", t) and h < 12:
        h += 12
    elif re.search(r"\d\s*am\b", t) and h == 12:
        h = 0
    if h > 23 or mi > 59:
        return None
    return f"{h:02d}:{mi:02d}"


def _time_input(value: str):
    """Typed time in edit flows. Accepts bare hour ('8') too."""
    v = (value or "").strip()
    if re.fullmatch(r"\d{1,2}", v) and int(v) < 24:
        return f"{int(v):02d}:00"
    return parse_time(v)


def _iso(day: str, hhmm: str) -> str:
    return datetime.fromisoformat(f"{day}T{hhmm}:00").replace(tzinfo=TZ).isoformat()


def _hhmm(iso: str) -> str:
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ).strftime("%H:%M")


def _date_prop(day: str, st=None, en=None) -> dict:
    if not day:
        return {"date": None}
    if not st:
        return {"date": {"start": day}}
    d = {"start": _iso(day, st)}
    if en:
        d["end"] = _iso(day, en)
    return {"date": d}


# ---------- generic Notion helpers ----------
_ds_cache, _title_keys = {}, {}


def _ds_id(db_id):
    """New Notion API queries data sources. None on old API."""
    if db_id not in _ds_cache:
        try:
            src = notion.databases.retrieve(database_id=db_id).get("data_sources") or []
            _ds_cache[db_id] = src[0]["id"] if src else None
        except Exception:
            _ds_cache[db_id] = None
    return _ds_cache[db_id]


def _query(db_id, flt=None, sorts=None) -> list:
    ds, rows, cursor = _ds_id(db_id), [], None
    while True:
        kw = {"page_size": 100}
        if flt:
            kw["filter"] = flt
        if sorts:
            kw["sorts"] = sorts
        if cursor:
            kw["start_cursor"] = cursor
        if ds:
            res = notion.data_sources.query(data_source_id=ds, **kw)
        else:
            res = notion.databases.query(database_id=db_id, **kw)
        rows += res["results"]
        if not res.get("has_more"):
            return rows
        cursor = res["next_cursor"]


def _title_key(db_id) -> str:
    if db_id not in _title_keys:
        key = "Name"
        try:
            ds = _ds_id(db_id)
            schema = (notion.data_sources.retrieve(data_source_id=ds) if ds
                      else notion.databases.retrieve(database_id=db_id))
            for k, v in schema.get("properties", {}).items():
                if v.get("type") == "title":
                    key = k
                    break
        except Exception:
            pass
        _title_keys[db_id] = key
    return _title_keys[db_id]


def _title(props: dict, key="Name") -> str:
    return "".join(t["plain_text"] for t in (props.get(key) or {}).get("title", []))


def _rel_id(props: dict):
    rel = (props.get("Books") or {}).get("relation") or []
    return rel[0]["id"] if rel else None


def _trash(page_id: str):
    try:
        notion.pages.update(page_id=page_id, in_trash=True)
    except Exception:
        notion.pages.update(page_id=page_id, archived=True)


# ---------- books ----------
_books = {"t": 0.0, "v": []}


def list_books(fresh: bool = False) -> list:
    """[{'page_id','title'}] sorted by title. 30s cache."""
    if not fresh and _time.time() - _books["t"] < 30:
        return _books["v"]
    key = _title_key(BOOKS_PAGE_ID)
    out = [{"page_id": r["id"], "title": _title(r["properties"], key) or "Untitled"}
           for r in _query(BOOKS_PAGE_ID)]
    out.sort(key=lambda b: b["title"].lower())
    _books.update(t=_time.time(), v=out)
    return out


_list_books = list_books  # agent.py compat


def _titles_map() -> dict:
    return {b["page_id"]: b["title"] for b in list_books()}


def search_book(book_name: str) -> dict:
    q = book_name.lower().strip()
    books = list_books()
    for b in books:
        if b["title"].lower().strip() == q:
            return {"found": True, **b}
    for b in books:
        t = b["title"].lower()
        if q in t or t in q:
            return {"found": True, **b}
    return {"found": False, "available": [b["title"] for b in books]}


def create_book(book_name: str) -> dict:
    try:
        page = notion.pages.create(
            parent={"database_id": BOOKS_PAGE_ID},
            properties={_title_key(BOOKS_PAGE_ID):
                        {"title": [{"text": {"content": book_name}}]}},
        )
        _books["t"] = 0
        return {"status": "success", "page_id": page["id"], "title": book_name}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def rename_book(book_id: str, new_name: str) -> dict:
    try:
        notion.pages.update(
            page_id=book_id,
            properties={_title_key(BOOKS_PAGE_ID):
                        {"title": [{"text": {"content": new_name}}]}},
        )
        _books["t"] = 0
        return {"status": "success", "title": new_name}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def book_counts() -> dict:
    """Open-task counts: {'by_book': {id: n}, 'no_book': n, 'total': n}."""
    rows = _query(TASKS_DATABASE_ID, NOT_DONE)
    by_book, none = {}, 0
    for r in rows:
        rel = (r["properties"].get("Books") or {}).get("relation") or []
        if not rel:
            none += 1
        for x in rel:
            by_book[x["id"]] = by_book.get(x["id"], 0) + 1
    return {"by_book": by_book, "no_book": none, "total": len(rows)}


def delete_book(book_id: str, mode: str, target_id: str = None) -> dict:
    """mode: 'unassign' (tasks keep, no book) | 'move' (to target_id) | 'delete' (tasks trashed)."""
    try:
        rows = _query(TASKS_DATABASE_ID,
                      {"property": "Books", "relation": {"contains": book_id}})
        for r in rows:
            if mode == "delete":
                _trash(r["id"])
            else:
                rel = [{"id": target_id}] if mode == "move" and target_id else []
                notion.pages.update(page_id=r["id"], properties={"Books": {"relation": rel}})
        _trash(book_id)
        _books["t"] = 0
        return {"status": "success", "tasks_affected": len(rows)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ---------- tasks ----------
def create_task_in_db(task_name: str, deadline: str = None, book_name: str = None,
                      complete: bool = False, book_id: str = None, time: str = None) -> dict:
    try:
        props = {
            "Name": {"title": [{"text": {"content": task_name}}]},
            "Complete": {"checkbox": complete},
        }
        parsed = None
        if deadline:
            parsed = parse_natural_date(deadline)
            if not parsed:
                return {"status": "error", "message": f"Bad deadline: {deadline}"}
            props["Deadline"] = _date_prop(parsed, time)

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


def _parse_task(r: dict, titles: dict) -> dict:
    p = r["properties"]
    start = ((p.get("Deadline") or {}).get("date") or {}).get("start") or ""
    day, tm = start[:10], None
    if len(start) > 10:
        try:
            dt = datetime.fromisoformat(start)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=TZ)
            dt = dt.astimezone(TZ)
            day, tm = dt.date().isoformat(), dt.strftime("%H:%M")
        except ValueError:
            pass
    bid = _rel_id(p)
    return {"id": r["id"], "name": _title(p) or "(no name)", "deadline": day,
            "time": tm, "book": titles.get(bid) if bid else None, "book_id": bid}


def list_tasks(view: str = "all", book_id: str = None) -> list:
    """Open tasks. view: all | today | tomorrow | overdue | week (to Sunday) | month (to month end)."""
    today = datetime.now(TZ).date()
    d = lambda n: (today + timedelta(days=n)).isoformat()
    parts = [NOT_DONE]
    if book_id:
        parts.append({"property": "Books", "relation": {"contains": book_id}})

    if view == "overdue":
        parts.append({"property": "Deadline", "date": {"before": d(0)}})
    elif view != "all":
        if view == "today":
            a, b = d(0), d(1)
        elif view == "tomorrow":
            a, b = d(1), d(2)
        elif view == "week":
            a, b = d(0), d(7 - today.weekday())
        elif view == "month":
            nxt = (today.replace(day=28) + timedelta(days=4)).replace(day=1)
            a, b = d(0), nxt.isoformat()
        else:
            raise ValueError(f"bad view: {view}")
        parts += [{"property": "Deadline", "date": {"on_or_after": a}},
                  {"property": "Deadline", "date": {"before": b}}]

    rows = _query(TASKS_DATABASE_ID, {"and": parts},
                  [{"property": "Deadline", "direction": "ascending"}])
    titles = _titles_map() if any(_rel_id(r["properties"]) for r in rows) else {}
    return [_parse_task(r, titles) for r in rows]


def mark_done(page_id: str) -> dict:
    try:
        notion.pages.update(page_id=page_id, properties={"Complete": {"checkbox": True}})
        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def update_task(page_id: str, field: str, value) -> dict:
    """field: name | deadline | book. For book, value = book_id or None."""
    try:
        if field == "name":
            props = {"Name": {"title": [{"text": {"content": value}}]}}
        elif field == "deadline":
            if str(value).strip().lower() in EMPTY:
                props = {"Deadline": {"date": None}}
            else:
                day = parse_natural_date(value)
                if not day:
                    return {"status": "error", "message": f"Bad date: {value}"}
                props = {"Deadline": _date_prop(day, parse_time(value))}
        elif field == "book":
            props = {"Books": {"relation": [{"id": value}] if value else []}}
        else:
            return {"status": "error", "message": f"Unknown field: {field}"}
        notion.pages.update(page_id=page_id, properties=props)
        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ---------- calendar (events + routines) ----------
def _parse_event(r: dict, titles: dict) -> dict:
    p = r["properties"]
    d = (p.get("Date") or {}).get("date") or {}
    start, end = d.get("start") or "", d.get("end") or ""
    st = _hhmm(start) if len(start) > 10 else None
    en = _hhmm(end) if len(end) > 10 else None
    day = start[:10]
    if st:  # normalise to Jakarta date
        dt = datetime.fromisoformat(start)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        day = dt.astimezone(TZ).date().isoformat()

    repeat = ((p.get("Repeat") or {}).get("select") or {}).get("name") or "None"
    days = [o["name"] for o in (p.get("Days") or {}).get("multi_select", [])]
    until = ((p.get("Until") or {}).get("date") or {}).get("start")
    exc_raw = "".join(x["plain_text"] for x in (p.get("Exceptions") or {}).get("rich_text", []))
    exc = {s for s in re.split(r"[,\s;]+", exc_raw) if s}
    series = "".join(x["plain_text"] for x in (p.get("Series") or {}).get("rich_text", []))
    bid = _rel_id(p)
    return {"id": r["id"], "name": _title(p) or "(no name)", "date": day, "series": series,
            "start": st, "end": en, "repeat": repeat, "days": days,
            "until": until[:10] if until else None, "exceptions": exc,
            "book": titles.get(bid) if bid else None, "book_id": bid}


def occurs_on(ev: dict, day: date) -> bool:
    if not ev["date"]:
        return False
    first = date.fromisoformat(ev["date"])
    if ev["repeat"] in ("None", None, ""):
        return day == first
    if day < first:
        return False
    if ev["until"] and day > date.fromisoformat(ev["until"]):
        return False
    if day.isoformat() in ev["exceptions"]:
        return False
    if ev["repeat"] == "Daily":
        return True
    if ev["repeat"] == "Weekly":
        days = ev["days"] or [DAY_NAMES[first.weekday()]]
        return DAY_NAMES[day.weekday()] in days
    return False


def _load_events(a: date, b: date) -> list:
    flt = {"or": [
        {"property": "Repeat", "select": {"is_not_empty": True}},
        {"and": [
            {"property": "Date", "date": {"on_or_after": a.isoformat()}},
            {"property": "Date", "date": {"before": (b + timedelta(days=1)).isoformat()}},
        ]},
    ]}
    rows = _query(CALENDAR_DATABASE_ID, flt)
    titles = _titles_map() if any(_rel_id(r["properties"]) for r in rows) else {}
    return [_parse_event(r, titles) for r in rows]


def events_between(a: date, b: date) -> list:
    """Expanded occurrences a..b inclusive. Routines computed, exceptions skipped."""
    evs, out, day = _load_events(a, b), [], a
    while day <= b:
        for ev in evs:
            if occurs_on(ev, day):
                out.append({**ev, "date": day.isoformat()})
        day += timedelta(days=1)
    out.sort(key=lambda e: (e["date"], e["start"] or "99:99"))
    return out


def events_on(day: date) -> list:
    return events_between(day, day)


DEFAULT_WEEKS = 12      # routine with no end date
MAX_SERIES = 200        # safety cap on rows per routine


def roll_until(until, start):
    """'Until' earlier than the first date means the year was guessed wrong. Move to next year."""
    if not until:
        return until
    d = date.fromisoformat(until[:10])
    ref = date.fromisoformat(start[:10]) if start else datetime.now(TZ).date()
    for _ in range(2):
        if d >= ref:
            break
        try:
            d = d.replace(year=d.year + 1)
        except ValueError:          # 29 Feb
            d = d.replace(year=d.year + 1, day=28)
    return d.isoformat()


def _call(fn, *a, **kw):
    """Retry on Notion rate limit (429)."""
    for i in range(5):
        try:
            return fn(*a, **kw)
        except Exception as e:
            if getattr(e, "status", 0) == 429 and i < 4:
                _time.sleep(1.5 * (i + 1))
                continue
            raise


def _pace():
    _time.sleep(0.35)       # Notion allows ~3 requests/second


def _make_row(name, day, st, en, book_id, series=None) -> str:
    props = {"Name": {"title": [{"text": {"content": name}}]},
             "Date": _date_prop(day, st, en)}
    if series:
        props["Series"] = {"rich_text": [{"text": {"content": series}}]}
    if book_id:
        props["Books"] = {"relation": [{"id": book_id}]}
    page = _call(notion.pages.create, parent={"database_id": CALENDAR_DATABASE_ID}, properties=props)
    _pace()
    return page["id"]


def _series_dates(first: date, repeat: str, days: list, last: date) -> list:
    out, d = [], first
    while d <= last:
        if repeat == "Daily" or DAY_NAMES[d.weekday()] in days:
            out.append(d)
        d += timedelta(days=1)
    return out


def _rollback(ids):
    for i in ids:
        try:
            _call(_trash, i)
            _pace()
        except Exception:
            pass


def list_events() -> list:
    """For /edit and /remind. One-offs, plus each routine collapsed into ONE item
    (next occurrence, with days, until and 'n' upcoming sessions)."""
    today = datetime.now(TZ).date().isoformat()
    flt = {"or": [
        {"property": "Repeat", "select": {"is_not_empty": True}},
        {"property": "Date", "date": {"on_or_after": today}},
    ]}
    rows = _query(CALENDAR_DATABASE_ID, flt, [{"property": "Date", "direction": "ascending"}])
    titles = _titles_map() if any(_rel_id(r["properties"]) for r in rows) else {}
    out, groups = [], {}
    for e in (_parse_event(r, titles) for r in rows):
        if e["series"]:
            if e["date"] >= today:
                groups.setdefault(e["series"], []).append(e)
        elif e["repeat"] == "None":
            if e["date"] >= today:
                out.append(e)
        elif not (e["until"] and e["until"] < today):    # legacy rule row
            out.append(e)
    for sid, g in groups.items():
        g.sort(key=lambda e: (e["date"], e["start"] or ""))
        wd = {DAY_NAMES[date.fromisoformat(e["date"]).weekday()] for e in g}
        days = [n for n in DAY_NAMES if n in wd]
        out.append({**g[0], "series": sid, "days": days, "until": g[-1]["date"], "n": len(g),
                    "repeat": "Daily" if len(days) == 7 else "Weekly"})
    out.sort(key=lambda e: (e["date"], e["start"] or ""))
    return out


def create_event(name: str, day: str, start: str = None, end: str = None,
                 book_id: str = None, repeat: str = "None",
                 days: list = None, until: str = None) -> dict:
    """One-off: one row. Routine (Daily/Weekly): ONE ROW PER OCCURRENCE, tied by a Series id,
    so every date shows in Notion's calendar view. No end date -> 12 weeks."""
    try:
        if repeat == "None":
            return {"status": "success", "event_id": _make_row(name, day, start, end, book_id), "count": 1}

        first = date.fromisoformat(day)
        wd = days or [DAY_NAMES[first.weekday()]]
        last = date.fromisoformat(roll_until(until, day)) if until else first + timedelta(weeks=DEFAULT_WEEKS)
        dates = _series_dates(first, repeat, wd, last)
        if not dates:
            return {"status": "error", "message": "No dates between start and end."}
        if len(dates) > MAX_SERIES:
            return {"status": "error", "message": f"{len(dates)} sessions is too many. Max {MAX_SERIES}. Shorten the end date."}

        sid, made = uuid.uuid4().hex[:12], []
        try:
            for d in dates:
                made.append(_make_row(name, d.isoformat(), start, end, book_id, sid))
        except Exception as e:
            _rollback(made)
            return {"status": "error", "message": f"{e} (rolled back {len(made)} rows)"}
        return {"status": "success", "event_id": made[0], "count": len(dates),
                "until": last.isoformat(), "series": sid}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def _series_rows(series_id: str, from_day: str = None) -> list:
    parts = [{"property": "Series", "rich_text": {"equals": series_id}}]
    if from_day:
        parts.append({"property": "Date", "date": {"on_or_after": from_day}})
    rows = _query(CALENDAR_DATABASE_ID, {"and": parts}, [{"property": "Date", "direction": "ascending"}])
    return [_parse_event(r, {}) for r in rows]


def update_series(series_id: str, field: str, value) -> dict:
    """Edit a routine's UPCOMING sessions (today onward). Past sessions stay as they were.
    field: name | start | end | book | days | until | skip (value = YYYY-MM-DD) | delete."""
    try:
        today = datetime.now(TZ).date()
        rows = _series_rows(series_id, today.isoformat())
        if not rows:
            return {"status": "error", "message": "No upcoming sessions found."}
        head, last = rows[0], date.fromisoformat(rows[-1]["date"])
        have = {r["date"]: r for r in rows}
        cur_days = [n for n in DAY_NAMES
                    if n in {DAY_NAMES[date.fromisoformat(d).weekday()] for d in have}]

        def patch(props):
            for r in rows:
                _call(notion.pages.update, page_id=r["id"], properties=props)
                _pace()

        def trash(rs):
            for r in rs:
                _call(_trash, r["id"])
                _pace()

        def add(dates):
            made = []
            try:
                for d in dates:
                    made.append(_make_row(head["name"], d.isoformat(), head["start"], head["end"],
                                          head["book_id"], series_id))
            except Exception:
                _rollback(made)
                raise
            return len(made)

        if field == "delete":
            trash(rows)
            return {"status": "success", "count": len(rows)}

        if field == "skip":
            if value not in have:
                return {"status": "error", "message": f"No session on {value}."}
            trash([have[value]])
            return {"status": "success", "count": 1}

        if field == "name":
            patch({"Name": {"title": [{"text": {"content": value}}]}})
        elif field == "book":
            patch({"Books": {"relation": [{"id": value}] if value else []}})
        elif field in ("start", "end"):
            if field == "start":
                new = _time_input(value)
                if not new:
                    return {"status": "error", "message": f"Bad time: {value}"}
            else:
                new = None if str(value).strip().lower() in EMPTY else _time_input(value)
            for r in rows:
                st, en = (new, r["end"]) if field == "start" else (r["start"], new)
                if en and not st:
                    return {"status": "error", "message": "Set a start time first."}
                _call(notion.pages.update, page_id=r["id"], properties={"Date": _date_prop(r["date"], st, en)})
                _pace()
        elif field == "until":
            d = parse_natural_date(value)
            if not d:
                return {"status": "error", "message": f"Bad date: {value}"}
            nd = date.fromisoformat(d)
            if nd < date.fromisoformat(head["date"]):
                return {"status": "error", "message": "End date is before the next session."}
            if nd > last:
                dates = _series_dates(last + timedelta(days=1), "Weekly", cur_days, nd)
                if len(have) + len(dates) > MAX_SERIES:
                    return {"status": "error", "message": f"Too many sessions. Max {MAX_SERIES}."}
                add(dates)
            else:
                trash([r for r in rows if date.fromisoformat(r["date"]) > nd])
        elif field == "days":
            names = [n.strip()[:3].title() for n in re.split(r"[,\s]+", value) if n.strip()]
            if not names or any(n not in DAY_NAMES for n in names):
                return {"status": "error", "message": f"Bad days: {value}"}
            trash([r for r in rows if DAY_NAMES[date.fromisoformat(r["date"]).weekday()] not in names])
            add([d for d in _series_dates(date.fromisoformat(head["date"]), "Weekly", names, last)
                 if d.isoformat() not in have])
        else:
            return {"status": "error", "message": f"Unknown field: {field}"}
        return {"status": "success", "count": len(rows)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def migrate_legacy() -> dict:
    """One-off: turn old rule rows (Repeat=Weekly/Daily, no Series) into one row per
    occurrence from today on, then trash the rule row. Safe to run again."""
    rows = _query(CALENDAR_DATABASE_ID, {"property": "Repeat", "select": {"is_not_empty": True}})
    titles = _titles_map() if any(_rel_id(r["properties"]) for r in rows) else {}
    today = datetime.now(TZ).date()
    converted, made, errors = 0, 0, []
    for r in rows:
        e = _parse_event(r, titles)
        if e["repeat"] == "None" or e["series"] or not e["date"]:
            continue
        try:
            orig = date.fromisoformat(e["date"])
            first = max(orig, today)
            last = (date.fromisoformat(roll_until(e["until"], e["date"])) if e["until"]
                    else first + timedelta(weeks=DEFAULT_WEEKS))
            wd = e["days"] or [DAY_NAMES[orig.weekday()]]
            dates = [d for d in _series_dates(first, e["repeat"], wd, last)
                     if d.isoformat() not in e["exceptions"]]
            if not dates or len(dates) > MAX_SERIES:
                errors.append(f"{e['name']}: {len(dates)} dates, skipped")
                continue
            sid, ids = uuid.uuid4().hex[:12], []
            try:
                for d in dates:
                    ids.append(_make_row(e["name"], d.isoformat(), e["start"], e["end"], e["book_id"], sid))
            except Exception:
                _rollback(ids)
                raise
            _call(_trash, e["id"])
            _pace()
            converted += 1
            made += len(dates)
        except Exception as ex:
            errors.append(f"{e['name']}: {ex}")
    return {"converted": converted, "rows": made, "errors": errors}


def add_exception(event_id: str, day: str) -> dict:
    """Skip one occurrence of a routine."""
    try:
        r = notion.pages.retrieve(page_id=event_id)
        ev = _parse_event(r, {})
        ev["exceptions"].add(day)
        txt = ", ".join(sorted(ev["exceptions"]))
        notion.pages.update(page_id=event_id, properties={
            "Exceptions": {"rich_text": [{"text": {"content": txt}}]}})
        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def update_event(page_id: str, field: str, value) -> dict:
    """field: name | date | start | end | until | repeat | days | book.
    book: value = book_id or None. days: 'Mon, Wed'."""
    try:
        if field == "name":
            props = {"Name": {"title": [{"text": {"content": value}}]}}
        elif field in ("date", "start", "end"):
            ev = _parse_event(notion.pages.retrieve(page_id=page_id), {})
            day, st, en = ev["date"], ev["start"], ev["end"]
            if field == "date":
                day = parse_natural_date(value)
                if not day:
                    return {"status": "error", "message": f"Bad date: {value}"}
            elif field == "start":
                st = _time_input(value)
                if not st:
                    return {"status": "error", "message": f"Bad time: {value}"}
            else:
                en = None if str(value).strip().lower() in EMPTY else _time_input(value)
            props = {"Date": _date_prop(day, st, en)}
        elif field == "until":
            if str(value).strip().lower() in EMPTY:
                props = {"Until": {"date": None}}
            else:
                day = parse_natural_date(value)
                if not day:
                    return {"status": "error", "message": f"Bad date: {value}"}
                props = {"Until": {"date": {"start": day}}}
        elif field == "repeat":
            props = {"Repeat": {"select": {"name": value}}}
        elif field == "days":
            names = [n.strip()[:3].title() for n in re.split(r"[,\s]+", value) if n.strip()]
            bad = [n for n in names if n not in DAY_NAMES]
            if bad:
                return {"status": "error", "message": f"Bad days: {', '.join(bad)}"}
            props = {"Days": {"multi_select": [{"name": n} for n in names]}}
        elif field == "book":
            props = {"Books": {"relation": [{"id": value}] if value else []}}
        else:
            return {"status": "error", "message": f"Unknown field: {field}"}
        notion.pages.update(page_id=page_id, properties=props)
        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def delete_event(page_id: str) -> dict:
    try:
        _trash(page_id)
        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ---------- reminders ----------
def set_remind(page_id: str, remind_iso: str) -> dict:
    """Write 'Remind At' on a task or event page. remind_iso = full ISO datetime."""
    try:
        notion.pages.update(page_id=page_id,
                            properties={"Remind At": {"date": {"start": remind_iso}}})
        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def due_custom_reminders() -> list:
    """Tasks (open) and events with 'Remind At' within the last 24h up to now.
    Sent-tracking lives in pickle (main.py)."""
    now = datetime.now(TZ)
    flt = {"and": [
        {"property": "Remind At", "date": {"on_or_before": now.isoformat()}},
        {"property": "Remind At", "date": {"on_or_after": (now - timedelta(days=1)).isoformat()}},
    ]}
    out = []
    t_rows = _query(TASKS_DATABASE_ID, {"and": [NOT_DONE, *flt["and"]]})
    titles = _titles_map() if any(_rel_id(r["properties"]) for r in t_rows) else {}
    for r in t_rows:
        t = _parse_task(r, titles)
        t["kind"] = "task"
        t["remind_at"] = r["properties"]["Remind At"]["date"]["start"]
        out.append(t)
    e_rows = _query(CALENDAR_DATABASE_ID, flt)
    for r in e_rows:
        e = _parse_event(r, titles)
        e["kind"] = "event"
        e["remind_at"] = r["properties"]["Remind At"]["date"]["start"]
        out.append(e)
    return out

def mark_undone(page_id):
    try:
        notion.pages.update(page_id=page_id, properties={"Done": {"checkbox": False}})
        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "message": str(e)}