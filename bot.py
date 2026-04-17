#!/usr/bin/env python3
"""
iMessage -> Claude Code CLI bridge.

Polls ~/Library/Messages/chat.db for new messages whose text contains
"claude" (case-insensitive), pipes them through `claude -p` (using your
Claude Code subscription — no API key needed), and replies via osascript.

Scope: 1v1 chats only. Group chats are skipped (osascript group send is
unreliable on macOS 14+; use BlueBubbles if you need them).

Loop protection: skips messages where is_from_me=1, so the bot's own
replies never retrigger it.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

HOME = Path.home()
DB_PATH = HOME / "Library" / "Messages" / "chat.db"
STATE_DIR = HOME / ".config" / "imessage-claude-bot"
STATE_FILE = STATE_DIR / "state.json"
LOG_FILE = STATE_DIR / "bot.log"

CLAUDE_BIN = HOME / ".local" / "bin" / "claude"

POLL_INTERVAL_SEC = 2.0
TRIGGER_RE = re.compile(r"\bclaude\b", re.IGNORECASE)
CONTEXT_MESSAGES = 3
CLAUDE_TIMEOUT_SEC = 90

SYSTEM_PROMPT = (
    "You are replying through iMessage. Keep replies under 100 sentences. "
    "Match the user's language. No markdown, no code blocks unless asked. "
    "You have no tools and cannot browse — answer from general knowledge."
)


def setup_logging() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE),
            logging.StreamHandler(sys.stdout),
        ],
    )


@dataclass
class Message:
    rowid: int
    chat_identifier: str
    sender: str
    text: str
    is_group: bool


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_rowid": 0}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def init_last_rowid(state: dict) -> None:
    """On first run, jump to the current max ROWID so we don't reply to
    every old message in your history."""
    if state["last_rowid"] != 0:
        return
    with connect_db() as db:
        row = db.execute("SELECT MAX(ROWID) FROM message").fetchone()
        state["last_rowid"] = row[0] or 0
    save_state(state)
    logging.info("first run — starting at ROWID %d", state["last_rowid"])


def connect_db() -> sqlite3.Connection:
    uri = f"file:{DB_PATH}?mode=ro"
    db = sqlite3.connect(uri, uri=True, timeout=5.0)
    db.row_factory = sqlite3.Row
    return db


def fetch_new_messages(last_rowid: int) -> list[Message]:
    sql = """
        SELECT m.ROWID,
               m.text,
               m.is_from_me,
               h.id AS sender,
               c.chat_identifier,
               c.room_name
        FROM message m
        LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
        LEFT JOIN chat c               ON c.ROWID         = cmj.chat_id
        LEFT JOIN handle h             ON h.ROWID         = m.handle_id
        WHERE m.ROWID > ?
          AND m.is_from_me = 0
        ORDER BY m.ROWID ASC
    """
    out: list[Message] = []
    with connect_db() as db:
        for row in db.execute(sql, (last_rowid,)):
            text = (row["text"] or "").strip()
            chat_id = row["chat_identifier"] or ""
            is_group = bool(row["room_name"]) or chat_id.startswith("chat")
            out.append(
                Message(
                    rowid=row["ROWID"],
                    chat_identifier=chat_id,
                    sender=row["sender"] or "",
                    text=text,
                    is_group=is_group,
                )
            )
    return out


def fetch_recent_context(chat_identifier: str, before_rowid: int, limit: int) -> list[str]:
    """Recent USER messages in the same 1v1 chat (excluding bot replies),
    oldest first."""
    sql = """
        SELECT m.text
        FROM message m
        LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
        LEFT JOIN chat c               ON c.ROWID         = cmj.chat_id
        WHERE c.chat_identifier = ?
          AND m.ROWID < ?
          AND m.is_from_me = 0
          AND m.text IS NOT NULL
          AND TRIM(m.text) != ''
        ORDER BY m.ROWID DESC
        LIMIT ?
    """
    with connect_db() as db:
        rows = db.execute(sql, (chat_identifier, before_rowid, limit)).fetchall()
    return [r["text"] for r in reversed(rows)]


def call_claude(prompt: str) -> str | None:
    cmd = [
        str(CLAUDE_BIN),
        "-p",
        "--no-session-persistence",
        "--effort", "xhigh",
        "--model", "opus",
        "--output-format", "text",
        "--tools", "",
        "--append-system-prompt", SYSTEM_PROMPT,
        prompt,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=CLAUDE_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        logging.error("claude -p timed out after %ds", CLAUDE_TIMEOUT_SEC)
        return None
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        logging.error("claude -p exit %d: %s", result.returncode, stderr[:500])
        return None
    reply = (result.stdout or "").strip()
    return reply or None


def send_imessage(buddy: str, body: str) -> bool:
    """Send via osascript using argv to avoid quoting hell."""
    script = (
        'on run argv\n'
        '  set msg to item 1 of argv\n'
        '  set who to item 2 of argv\n'
        '  tell application "Messages"\n'
        '    set targetService to first account whose service type is iMessage\n'
        '    set targetBuddy to buddy who of targetService\n'
        '    send msg to targetBuddy\n'
        '  end tell\n'
        'end run\n'
    )
    try:
        result = subprocess.run(
            ["osascript", "-", body, buddy],
            input=script,
            text=True,
            capture_output=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        logging.error("osascript timed out sending to %s", buddy)
        return False
    if result.returncode != 0:
        logging.error("osascript exit %d: %s", result.returncode, (result.stderr or "").strip()[:500])
        return False
    return True


def build_prompt(message: Message, context: list[str]) -> str:
    parts: list[str] = []
    if context:
        parts.append("Recent messages from the same person (oldest first):")
        for i, c in enumerate(context, 1):
            parts.append(f"{i}. {c}")
        parts.append("")
    parts.append("Current message to reply to:")
    parts.append(message.text)
    return "\n".join(parts)


def handle(message: Message, state: dict) -> None:
    if message.is_group:
        logging.info("skip group message rowid=%d chat=%s", message.rowid, message.chat_identifier)
        return
    if not message.text:
        return
    if not TRIGGER_RE.search(message.text):
        return
    if not message.chat_identifier:
        logging.warning("no chat_identifier for rowid=%d, skipping", message.rowid)
        return

    logging.info("triggered: rowid=%d from=%s text=%r", message.rowid, message.sender, message.text[:120])
    context = fetch_recent_context(message.chat_identifier, message.rowid, CONTEXT_MESSAGES)
    prompt = build_prompt(message, context)

    reply = call_claude(prompt)
    if not reply:
        logging.error("no reply generated for rowid=%d", message.rowid)
        return

    if send_imessage(message.chat_identifier, reply):
        logging.info("replied to %s: %r", message.chat_identifier, reply[:120])
    else:
        logging.error("send failed for rowid=%d", message.rowid)


def main() -> None:
    setup_logging()
    if not DB_PATH.exists():
        logging.error("chat.db not found at %s", DB_PATH)
        sys.exit(1)
    state = load_state()
    init_last_rowid(state)
    logging.info("bot started — polling every %.1fs", POLL_INTERVAL_SEC)

    while True:
        try:
            messages = fetch_new_messages(state["last_rowid"])
            for m in messages:
                handle(m, state)
                state["last_rowid"] = max(state["last_rowid"], m.rowid)
            if messages:
                save_state(state)
        except sqlite3.OperationalError as e:
            logging.warning("sqlite error (likely permission or busy): %s", e)
        except Exception:
            logging.exception("unexpected error in poll loop")
        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
