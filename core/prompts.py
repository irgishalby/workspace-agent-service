from datetime import datetime
from zoneinfo import ZoneInfo

def build_system_prompt(books: list[str]) -> str:
    now = datetime.now(ZoneInfo("Asia/Jakarta"))
    books_list = ", ".join(books) if books else "none"
    return f"""You are G0BL-D-GK, an executive task and note manager for Notion.

Current time: {now:%A, %Y-%m-%d %H:%M} (Asia/Jakarta).
Existing notebooks: {books_list}

Behavior:
- Reply in English. Keep answers to one concise line.
- Default to TASK unless explicitly asked to record a NOTE ("note", "record", "save note").

Tasks:
- Requires task_name and deadline.
- Missing deadline: ask "When is the deadline?". Never assume or fabricate dates.
- Book linkage: automatically handled by system backend.
- Pass book_name only if user explicitly names a new/unlisted notebook, or book_name="none" if skipped.

Notes:
- Requires book_name and content.
- Call search_book first. If missing, report unavailable and list active notebooks."""

TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "search_book",
            "description": "Check if notebook page exists in directory.",
            "parameters": {
                "type": "object",
                "properties": {"book_name": {"type": "string"}},
                "required": ["book_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_book",
            "description": "Create new notebook page in directory.",
            "parameters": {
                "type": "object",
                "properties": {"book_name": {"type": "string"}},
                "required": ["book_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_task_in_db",
            "description": "Create task entry in database.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_name": {"type": "string", "description": "Title of task"},
                    "deadline": {"type": "string", "description": "Raw string ('monday', 'tomorrow') or YYYY-MM-DD"},
                    "book_name": {"type": ["string", "null"], "description": "Related notebook name or null"},
                    "complete": {"type": "boolean", "default": False},
                },
                "required": ["task_name", "deadline"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_note_to_book",
            "description": "Append note content to notebook page.",
            "parameters": {
                "type": "object",
                "properties": {
                    "page_id": {"type": "string", "description": "Notion page ID"},
                    "content": {"type": "string", "description": "Text content to append"},
                },
                "required": ["page_id", "content"],
            },
        },
    },
]