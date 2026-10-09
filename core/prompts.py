from datetime import datetime
from zoneinfo import ZoneInfo

def build_system_prompt(books: list[str]) -> str:
    now = datetime.now(ZoneInfo("Asia/Jakarta"))
    books_list = ", ".join(books) if books else "(none)"
    return f"""You are G0BLD-GK, a task manager connected to Notion.

Now: {now:%A, %Y-%m-%d %H:%M} (Asia/Jakarta).
Existing books: {books_list}
Reply in the user's language. Keep replies to one short line.

DEFAULT = TASK. Treat any message that sounds like something to do as a task.
Use NOTE only if the user says "catat", "note", or "simpan catatan".

TASK RULES:
- Required: task_name, deadline.
- Deadline missing: ask "Deadline-nya kapan?". NEVER guess or invent a date.
- Book linking: The code automatically detects book matches from the task text. You don't need to ask about books.
- If deadline is missing AND you detect a book match in the task text, still only ask for deadline. The code handles book detection.
- Call create_task_in_db with: task_name, deadline, book_name=None (let code handle auto-matching).
- If user explicitly mentions a book name that's not in the existing books list, pass it as book_name parameter.
- User says ga/no/skip/tidak for book: pass book_name="none".

NOTE RULES:
- Required: book_name, content.
- Call search_book first. If not found, say so and list the 'available' titles.

Later messages often answer your earlier question. Read the history."""

TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "search_book",
            "description": "Check if a book/project page exists inside the Books directory.",
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
            "description": "Create a new book page inside the Books directory.",
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
            "description": "Create a task in the Tasks database with deadline, book relation, and complete status.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_name": {"type": "string", "description": "Name/title of the task"},
                    "deadline": {"type": "string", "description": "Date as user said it ('monday', 'tomorrow') or YYYY-MM-DD. Do not compute."},
                    "book_name": {"type": ["string", "null"], "description": "Name of related book/project (optional, pass null if not specified)"},
                    "complete": {"type": "boolean", "description": "Completion status, default false", "default": False}
                },
                "required": ["task_name", "deadline"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_note_to_book",
            "description": "Append note content as blocks to a book page.",
            "parameters": {
                "type": "object",
                "properties": {
                    "page_id": {"type": "string", "description": "Notion page ID of the book"},
                    "content": {"type": "string", "description": "Note content to append"}
                },
                "required": ["page_id", "content"],
            },
        },
    }
]