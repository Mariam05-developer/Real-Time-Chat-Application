# All of the SQL for the chat app lives in this file.

import hashlib
import secrets
import sqlite3
from datetime import datetime, timezone

DB_FILE = "chat.db"


def connect():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row  # lets us use row["column_name"]
    return conn


def query(sql, params=()):
    """Run a SELECT and return the rows as a list of dictionaries."""
    conn = connect()
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def execute(sql, params=()):
    """Run an INSERT, UPDATE or DELETE. Returns (new row id, rows changed)."""
    conn = connect()
    cursor = conn.execute(sql, params)
    conn.commit()
    conn.close()
    return cursor.lastrowid, cursor.rowcount


def create_tables():
    conn = connect()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            salt TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            is_group INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS members (
            conversation_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            PRIMARY KEY (conversation_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL,
            sender_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            sent_at TEXT NOT NULL
        );

        -- one row for each person who should receive a message
        CREATE TABLE IF NOT EXISTS receipts (
            message_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            delivered_at TEXT,
            read_at TEXT,
            PRIMARY KEY (message_id, user_id)
        );
    """)
    conn.close()


def now():
    return datetime.now(timezone.utc).isoformat()


# ---------- users ----------

def hash_password(password, salt):
    # PBKDF2 is deliberately slow, which makes passwords harder to guess
    result = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600_000)
    return result.hex()


def create_user(username, password):
    """Returns the new user's id, or None if the username is taken."""
    salt = secrets.token_hex(16)
    try:
        user_id, _ = execute(
            "INSERT INTO users (username, password_hash, salt) VALUES (?, ?, ?)",
            (username, hash_password(password, salt), salt),
        )
    except sqlite3.IntegrityError:
        return None
    return user_id


def check_login(username, password):
    """Returns the user's id if the password is correct, otherwise None."""
    rows = query("SELECT * FROM users WHERE username = ?", (username,))
    if not rows:
        return None
    user = rows[0]
    if not secrets.compare_digest(hash_password(password, user["salt"]), user["password_hash"]):
        return None
    return user["id"]


def get_user(user_id):
    rows = query("SELECT id, username FROM users WHERE id = ?", (user_id,))
    return rows[0] if rows else None


def get_all_users():
    return query("SELECT id, username FROM users ORDER BY username")


# ---------- conversations ----------

def create_conversation(name, is_group, user_ids):
    conversation_id, _ = execute(
        "INSERT INTO conversations (name, is_group) VALUES (?, ?)", (name, is_group)
    )
    for user_id in user_ids:
        add_member(conversation_id, user_id)
    return conversation_id


def find_private_chat(user1, user2):
    rows = query(
        """SELECT c.id FROM conversations c
           JOIN members a ON a.conversation_id = c.id AND a.user_id = ?
           JOIN members b ON b.conversation_id = c.id AND b.user_id = ?
           WHERE c.is_group = 0""",
        (user1, user2),
    )
    return rows[0]["id"] if rows else None


def get_conversation(conversation_id):
    rows = query("SELECT * FROM conversations WHERE id = ?", (conversation_id,))
    return rows[0] if rows else None


def add_member(conversation_id, user_id):
    # OR IGNORE means adding someone twice does nothing
    execute(
        "INSERT OR IGNORE INTO members (conversation_id, user_id) VALUES (?, ?)",
        (conversation_id, user_id),
    )


def remove_member(conversation_id, user_id):
    execute(
        "DELETE FROM members WHERE conversation_id = ? AND user_id = ?",
        (conversation_id, user_id),
    )
    # don't leave senders waiting for someone who has left to read their messages
    execute(
        """DELETE FROM receipts WHERE user_id = ? AND read_at IS NULL
           AND message_id IN (SELECT id FROM messages WHERE conversation_id = ?)""",
        (user_id, conversation_id),
    )


def get_members(conversation_id):
    return query(
        """SELECT users.id, users.username FROM members
           JOIN users ON users.id = members.user_id
           WHERE members.conversation_id = ?
           ORDER BY users.username""",
        (conversation_id,),
    )


def is_member(conversation_id, user_id):
    rows = query(
        "SELECT 1 FROM members WHERE conversation_id = ? AND user_id = ?",
        (conversation_id, user_id),
    )
    return len(rows) > 0


def get_conversations(user_id):
    """All of a user's conversations, with members and number of unread messages."""
    conversations = query(
        """SELECT c.id, c.name, c.is_group,
                  (SELECT COUNT(*) FROM receipts r
                   JOIN messages m ON m.id = r.message_id
                   WHERE m.conversation_id = c.id AND r.user_id = members.user_id
                   AND r.read_at IS NULL) AS unread
           FROM conversations c
           JOIN members ON members.conversation_id = c.id
           WHERE members.user_id = ?
           ORDER BY (SELECT MAX(id) FROM messages WHERE conversation_id = c.id) DESC""",
        (user_id,),
    )
    for conversation in conversations:
        conversation["members"] = get_members(conversation["id"])
    return conversations


# ---------- messages and read receipts ----------

def save_message(conversation_id, sender_id, text):
    message_id, _ = execute(
        "INSERT INTO messages (conversation_id, sender_id, text, sent_at) VALUES (?, ?, ?, ?)",
        (conversation_id, sender_id, text, now()),
    )
    for member in get_members(conversation_id):
        if member["id"] != sender_id:
            execute(
                "INSERT INTO receipts (message_id, user_id) VALUES (?, ?)",
                (message_id, member["id"]),
            )
    return message_id


def get_messages(conversation_id):
    messages = query(
        """SELECT messages.*, users.username AS sender FROM messages
           JOIN users ON users.id = messages.sender_id
           WHERE conversation_id = ?
           ORDER BY messages.id""",
        (conversation_id,),
    )
    receipts = query(
        """SELECT receipts.*, users.username FROM receipts
           JOIN users ON users.id = receipts.user_id
           JOIN messages ON messages.id = receipts.message_id
           WHERE messages.conversation_id = ?""",
        (conversation_id,),
    )
    for message in messages:
        message["receipts"] = [r for r in receipts if r["message_id"] == message["id"]]
    return messages


def mark_delivered(message_id, user_id):
    execute(
        """UPDATE receipts SET delivered_at = ?
           WHERE message_id = ? AND user_id = ? AND delivered_at IS NULL""",
        (now(), message_id, user_id),
    )


def mark_all_delivered(user_id):
    """Called when a user connects. Returns the conversations that changed."""
    changed = query(
        """SELECT DISTINCT messages.conversation_id FROM receipts
           JOIN messages ON messages.id = receipts.message_id
           WHERE receipts.user_id = ? AND receipts.delivered_at IS NULL""",
        (user_id,),
    )
    execute(
        "UPDATE receipts SET delivered_at = ? WHERE user_id = ? AND delivered_at IS NULL",
        (now(), user_id),
    )
    return [row["conversation_id"] for row in changed]


def mark_read(conversation_id, user_id):
    """Marks everything in a conversation as read. Returns True if anything changed."""
    time = now()
    _, rows_changed = execute(
        """UPDATE receipts SET read_at = ?, delivered_at = COALESCE(delivered_at, ?)
           WHERE user_id = ? AND read_at IS NULL
           AND message_id IN (SELECT id FROM messages WHERE conversation_id = ?)""",
        (time, time, user_id, conversation_id),
    )
    return rows_changed > 0
