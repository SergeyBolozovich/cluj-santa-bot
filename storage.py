"""Minimal SQLite repository; importing it does not create files."""
import sqlite3
from pathlib import Path
from validation import validate_title

OPEN = "registration_open"
COMPLETED = "draw_completed"
CANCELLED = "cancelled"


class Storage:
    def __init__(self, path):
        if str(path) != ":memory:":
            path = str(Path(path).expanduser())
            Path(path).resolve().parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                organizer_id INTEGER NOT NULL,
                title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 100),
                budget INTEGER NOT NULL CHECK(budget BETWEEN 1 AND 10000000),
                currency TEXT NOT NULL DEFAULT 'RON' CHECK(currency = 'RON'),
                status TEXT NOT NULL DEFAULT 'registration_open'
                    CHECK(status IN ('registration_open', 'draw_completed', 'cancelled')),
                message_id INTEGER,
                creation_key TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS participants (
                event_id INTEGER NOT NULL REFERENCES events(id),
                telegram_user_id INTEGER NOT NULL,
                display_name TEXT NOT NULL,
                joined_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (event_id, telegram_user_id)
            );
            CREATE TABLE IF NOT EXISTS users (
                telegram_user_id INTEGER PRIMARY KEY,
                private_started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
        """)

    def close(self):
        self.connection.close()

    def create_event(self, chat_id, organizer_id, title, budget, creation_key, message_id=None):
        title = validate_title(title)
        if type(budget) is not int or not 1 <= budget <= 10_000_000:
            raise ValueError("Некорректный бюджет.")
        with self.connection:
            self.connection.execute(
                """INSERT INTO events(chat_id, organizer_id, title, budget, creation_key, message_id)
                   VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(creation_key) DO NOTHING""",
                (chat_id, organizer_id, title, budget, creation_key, message_id),
            )
        return self.connection.execute(
            "SELECT * FROM events WHERE creation_key = ?", (creation_key,)
        ).fetchone()

    def get_event(self, event_id):
        return self.connection.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()

    def get_event_by_creation_key(self, key):
        return self.connection.execute("SELECT * FROM events WHERE creation_key = ?", (key,)).fetchone()

    def set_message_id(self, event_id, message_id):
        with self.connection:
            self.connection.execute("UPDATE events SET message_id = ? WHERE id = ?", (message_id, event_id))

    def participants(self, event_id):
        return self.connection.execute(
            "SELECT * FROM participants WHERE event_id = ? ORDER BY joined_at, telegram_user_id",
            (event_id,),
        ).fetchall()

    def join_event(self, event_id, user_id, display_name):
        with self.connection:
            cursor = self.connection.execute(
                """INSERT INTO participants(event_id, telegram_user_id, display_name)
                   SELECT id, ?, ? FROM events WHERE id = ? AND status = ?
                   ON CONFLICT(event_id, telegram_user_id) DO NOTHING""",
                (user_id, display_name, event_id, OPEN),
            )
        return cursor.rowcount == 1

    def cancel_event(self, event_id, organizer_id):
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE events SET status = ? WHERE id = ? AND organizer_id = ? AND status = ?",
                (CANCELLED, event_id, organizer_id, OPEN),
            )
        return cursor.rowcount == 1

    def mark_completed(self, event_id, organizer_id):
        """Legacy draw only; SS-2 will persist assignments atomically."""
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE events SET status = ? WHERE id = ? AND organizer_id = ? AND status = ?",
                (COMPLETED, event_id, organizer_id, OPEN),
            )
        return cursor.rowcount == 1

    def activate_private(self, user_id):
        with self.connection:
            self.connection.execute(
                "INSERT INTO users(telegram_user_id) VALUES (?) ON CONFLICT DO NOTHING", (user_id,)
            )

    def private_active(self, user_id):
        return self.connection.execute(
            "SELECT 1 FROM users WHERE telegram_user_id = ?", (user_id,)
        ).fetchone() is not None
