"""Minimal SQLite repository; importing it does not create files."""
import sqlite3
from pathlib import Path
from draw import make_pairs
from validation import validate_title

OPEN = "registration_open"
COMPLETED = "draw_completed"
CANCELLED = "cancelled"


class DrawError(ValueError):
    """Safe message that may be shown to an organizer; never contains pairs."""


class ParticipantsChanged(DrawError):
    pass


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
            CREATE TABLE IF NOT EXISTS assignments (
                event_id INTEGER NOT NULL REFERENCES events(id),
                giver_user_id INTEGER NOT NULL,
                receiver_user_id INTEGER NOT NULL,
                notification_status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(notification_status IN ('pending', 'sent', 'failed')),
                PRIMARY KEY (event_id, giver_user_id),
                UNIQUE (event_id, receiver_user_id),
                CHECK(giver_user_id != receiver_user_id),
                FOREIGN KEY (event_id, giver_user_id)
                    REFERENCES participants(event_id, telegram_user_id),
                FOREIGN KEY (event_id, receiver_user_id)
                    REFERENCES participants(event_id, telegram_user_id)
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

    def participant_games(self, user_id, offset=0, limit=6):
        """The specification excludes organizer-only games from My Games."""
        return self.connection.execute(
            """SELECT e.*, (SELECT count(*) FROM participants all_p
                   WHERE all_p.event_id = e.id) AS participant_count
               FROM events e JOIN participants p ON p.event_id = e.id
               WHERE p.telegram_user_id = ? ORDER BY e.id DESC LIMIT ? OFFSET ?""",
            (user_id, limit, offset),
        ).fetchall()

    def participant_event(self, event_id, user_id):
        return self.connection.execute(
            """SELECT e.*, (SELECT count(*) FROM participants all_p
                   WHERE all_p.event_id = e.id) AS participant_count
               FROM events e JOIN participants p ON p.event_id = e.id
               WHERE e.id = ? AND p.telegram_user_id = ?""",
            (event_id, user_id),
        ).fetchone()

    def completed_assignment_games(self, user_id, offset=0, limit=6):
        return self.connection.execute(
            """SELECT e.*, (SELECT count(*) FROM participants p WHERE p.event_id=e.id) AS participant_count
               FROM events e JOIN assignments a ON a.event_id=e.id
               JOIN participants giver ON giver.event_id=e.id AND giver.telegram_user_id=a.giver_user_id
               WHERE a.giver_user_id=? AND e.status=? ORDER BY e.id DESC LIMIT ? OFFSET ?""",
            (user_id, COMPLETED, limit, offset),
        ).fetchall()

    def saved_recipient(self, event_id, user_id):
        """Read only this participant's assignment, and only for a completed draw."""
        return self.connection.execute(
            """SELECT a.receiver_user_id, receiver.display_name AS receiver_name
               FROM assignments a JOIN events e ON e.id = a.event_id
               JOIN participants giver ON giver.event_id = a.event_id
                   AND giver.telegram_user_id = a.giver_user_id
               JOIN participants receiver ON receiver.event_id = a.event_id
                   AND receiver.telegram_user_id = a.receiver_user_id
               WHERE a.event_id = ? AND a.giver_user_id = ? AND e.status = ?""",
            (event_id, user_id, COMPLETED),
        ).fetchone()

    def set_message_id(self, event_id, message_id):
        with self.connection:
            self.connection.execute("UPDATE events SET message_id = ? WHERE id = ?", (message_id, event_id))

    def participants(self, event_id):
        return self.connection.execute(
            "SELECT * FROM participants WHERE event_id = ? ORDER BY joined_at, telegram_user_id",
            (event_id,),
        ).fetchall()

    def group_participants(self, event_id):
        """Activation is derived from SS-4 users; no second state is stored."""
        return self.connection.execute(
            """SELECT p.*, u.telegram_user_id IS NOT NULL AS private_active
               FROM participants p LEFT JOIN users u ON u.telegram_user_id=p.telegram_user_id
               WHERE p.event_id=? ORDER BY p.joined_at, p.telegram_user_id""",
            (event_id,),
        ).fetchall()

    def participant_announcements(self, user_id):
        """All editable card references, including completed/cancelled games, without menu pagination."""
        return self.connection.execute(
            """SELECT e.* FROM events e JOIN participants p ON p.event_id=e.id
               WHERE p.telegram_user_id=? AND e.message_id IS NOT NULL ORDER BY e.id""",
            (user_id,),
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

    def complete_draw(self, event_id, organizer_id, expected_ids, generator=None):
        """Lock, revalidate, generate and commit all pairs + status, or nothing.

        BEGIN IMMEDIATE serializes writers even across separate connections.
        This method is synchronous: never await Telegram inside its transaction.
        """
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            event = self.get_event(event_id)
            if not event or event["organizer_id"] != organizer_id:
                raise DrawError("Жеребьёвка доступна только организатору игры.")
            if event["status"] != OPEN:
                raise DrawError("Игра отменена или жеребьёвка уже завершена.")
            ids = [p["telegram_user_id"] for p in self.participants(event_id)]
            if len(ids) < 2:
                raise DrawError("Нужно минимум 2 участника.")
            if set(ids) != set(expected_ids):
                raise ParticipantsChanged("Состав участников изменился. Требуется новое подтверждение.")
            pairs = (generator or make_pairs)(ids)
            if (not pairs or len(pairs) != len(ids)
                    or {giver for giver, _ in pairs} != set(ids)
                    or {receiver for _, receiver in pairs} != set(ids)
                    or any(giver == receiver for giver, receiver in pairs)):
                raise DrawError("Не удалось создать корректные назначения. Подтверди заново.")
            self.connection.executemany(
                "INSERT INTO assignments(event_id, giver_user_id, receiver_user_id) VALUES (?, ?, ?)",
                [(event_id, giver, receiver) for giver, receiver in pairs],
            )
            self.connection.execute("UPDATE events SET status = ? WHERE id = ?", (COMPLETED, event_id))

    def assignments(self, event_id, unsent_only=False):
        condition = " AND a.notification_status IN ('pending', 'failed')" if unsent_only else ""
        return self.connection.execute(
            """SELECT a.*, p.display_name AS receiver_name FROM assignments a
               JOIN participants p ON p.event_id = a.event_id
                   AND p.telegram_user_id = a.receiver_user_id
               WHERE a.event_id = ?""" + condition + " ORDER BY a.giver_user_id",
            (event_id,),
        ).fetchall()

    def set_notification_status(self, event_id, giver_id, status):
        if status not in {"sent", "failed"}:
            raise ValueError("Некорректный статус уведомления.")
        with self.connection:
            self.connection.execute(
                """UPDATE assignments SET notification_status = ?
                   WHERE event_id = ? AND giver_user_id = ?""",
                (status, event_id, giver_id),
            )

    def notification_counts(self, event_id):
        counts = {"pending": 0, "sent": 0, "failed": 0}
        for row in self.connection.execute(
            "SELECT notification_status, count(*) AS total FROM assignments WHERE event_id = ? GROUP BY notification_status",
            (event_id,),
        ):
            counts[row["notification_status"]] = row["total"]
        return counts

    def activate_private(self, user_id):
        with self.connection:
            self.connection.execute(
                "INSERT INTO users(telegram_user_id) VALUES (?) ON CONFLICT DO NOTHING", (user_id,)
            )

    def private_active(self, user_id):
        return self.connection.execute(
            "SELECT 1 FROM users WHERE telegram_user_id = ?", (user_id,)
        ).fetchone() is not None
