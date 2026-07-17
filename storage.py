import sqlite3
import uuid
from contextlib import closing

from config import DB_PATH


def init_db():
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS seen_entries (
                entry_id TEXT PRIMARY KEY,
                feed_name TEXT,
                seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS drafts (
                draft_id TEXT PRIMARY KEY,
                feed_name TEXT,
                source_link TEXT,
                title TEXT,
                text TEXT,
                image_url TEXT,
                status TEXT DEFAULT 'pending',   -- pending / approved / rejected
                published_channel_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS channels (
                channel_id TEXT PRIMARY KEY,
                title TEXT,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()


def add_channel(channel_id: str, title: str):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO channels (channel_id, title) VALUES (?, ?)",
            (channel_id, title),
        )
        conn.commit()


def remove_channel(channel_id: str):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("DELETE FROM channels WHERE channel_id = ?", (channel_id,))
        conn.commit()


def list_channels():
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute("SELECT channel_id, title FROM channels ORDER BY added_at")
        return [{"channel_id": row[0], "title": row[1]} for row in cur.fetchall()]


def is_entry_seen(entry_id: str) -> bool:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute("SELECT 1 FROM seen_entries WHERE entry_id = ?", (entry_id,))
        return cur.fetchone() is not None


def mark_entry_seen(entry_id: str, feed_name: str):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO seen_entries (entry_id, feed_name) VALUES (?, ?)",
            (entry_id, feed_name),
        )
        conn.commit()


def create_draft(feed_name: str, source_link: str, title: str, text: str, image_url: str) -> str:
    draft_id = str(uuid.uuid4())
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            """INSERT INTO drafts (draft_id, feed_name, source_link, title, text, image_url)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (draft_id, feed_name, source_link, title, text, image_url),
        )
        conn.commit()
    return draft_id


def get_pending_draft_ids():
    """Все черновики со статусом pending, старые сначала — то, что накопил
    фоновый планировщик между твоими проверками, плюс всё найденное только что."""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(
            "SELECT draft_id FROM drafts WHERE status = 'pending' ORDER BY created_at"
        )
        return [row[0] for row in cur.fetchall()]


def get_draft(draft_id: str):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(
            """SELECT draft_id, feed_name, source_link, title, text, image_url, status, published_channel_id
               FROM drafts WHERE draft_id = ?""",
            (draft_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        keys = ["draft_id", "feed_name", "source_link", "title", "text", "image_url", "status", "published_channel_id"]
        return dict(zip(keys, row))


def update_draft_text(draft_id: str, new_text: str):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("UPDATE drafts SET text = ? WHERE draft_id = ?", (new_text, draft_id))
        conn.commit()


def update_draft_status(draft_id: str, status: str, published_channel_id: str = None):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "UPDATE drafts SET status = ?, published_channel_id = ? WHERE draft_id = ?",
            (status, published_channel_id, draft_id),
        )
        conn.commit()
