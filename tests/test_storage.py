import sqlite3
import tempfile
import unittest
from pathlib import Path

from storage import Storage, OPEN, CANCELLED, COMPLETED, DrawError


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "nested" / "santa.sqlite3"
        self.store = Storage(self.path)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def event(self, key="first", chat=-100):
        return self.store.create_event(chat, 10, "Новый год", 10050, key)

    def test_independent_events_same_group_and_different_groups(self):
        first = self.event()
        second = self.event("second")
        third = self.event("third", -200)
        self.assertEqual(len({first["id"], second["id"], third["id"]}), 3)
        self.assertTrue(self.store.join_event(first["id"], 20, "Анна"))
        self.assertTrue(self.store.join_event(second["id"], 20, "Анна"))
        self.assertTrue(self.store.join_event(second["id"], 30, "Иван"))
        self.assertEqual(len(self.store.participants(first["id"])), 1)
        self.assertEqual(len(self.store.participants(second["id"])), 2)
        self.assertEqual(self.store.participants(third["id"]), [])

    def test_duplicate_registration_preserves_first_entry(self):
        event = self.event()
        self.assertTrue(self.store.join_event(event["id"], 20, "Анна"))
        self.assertFalse(self.store.join_event(event["id"], 20, "Другое имя"))
        self.assertEqual(len(self.store.participants(event["id"])), 1)
        self.assertEqual(self.store.participants(event["id"])[0]["display_name"], "Анна")

    def test_confirmation_idempotent_and_organizer_not_registered(self):
        first = self.event()
        self.assertEqual(first["id"], self.event()["id"])
        self.assertEqual(self.store.participants(first["id"]), [])

    def test_persistence_after_reopening_and_duplicate_initialization(self):
        event = self.event()
        self.store.join_event(event["id"], 20, "Анна")
        self.store.activate_private(20)
        self.store.activate_private(20)
        self.store.set_message_id(event["id"], 123)
        self.store.close()
        self.store = Storage(self.path)
        saved = self.store.get_event(event["id"])
        self.assertEqual(saved["message_id"], 123)
        self.assertEqual(saved["budget"], 10050)
        self.assertEqual(saved["currency"], "RON")
        self.assertEqual(saved["status"], OPEN)
        self.assertEqual(len(self.store.participants(event["id"])), 1)
        self.assertTrue(self.store.private_active(20))
        self.assertFalse(self.store.private_active(21))
        self.assertFalse(self.store.join_event(event["id"], 20, "Анна"))

    def test_organizer_only_cancellation_keeps_history_and_closes_registration(self):
        event = self.event()
        other = self.event("second")
        self.store.join_event(event["id"], 20, "Анна")
        self.assertFalse(self.store.cancel_event(event["id"], 99))
        self.assertEqual(self.store.get_event(event["id"])["status"], OPEN)
        self.assertTrue(self.store.cancel_event(event["id"], 10))
        self.assertFalse(self.store.cancel_event(event["id"], 10))
        self.assertFalse(self.store.join_event(event["id"], 30, "Иван"))
        self.assertEqual(len(self.store.participants(event["id"])), 1)
        self.assertEqual(self.store.get_event(event["id"])["status"], CANCELLED)
        self.assertEqual(self.store.get_event(other["id"])["status"], OPEN)
        self.store.close()
        self.store = Storage(self.path)
        self.assertEqual(self.store.get_event(event["id"])["status"], CANCELLED)

    def test_completed_event_cannot_be_cancelled_or_joined(self):
        event = self.event()
        for uid in (20, 30):
            self.store.join_event(event["id"], uid, "Участник")
            self.store.activate_private(uid)
        with self.assertRaises(DrawError):
            self.store.complete_draw(event["id"], 99, [20, 30])
        self.store.complete_draw(event["id"], 10, [20, 30])
        with self.assertRaises(DrawError):
            self.store.complete_draw(event["id"], 10, [20, 30])
        self.assertFalse(self.store.cancel_event(event["id"], 10))
        self.assertFalse(self.store.join_event(event["id"], 20, "Анна"))
        self.assertEqual(self.store.get_event(event["id"])["status"], COMPLETED)

    def test_missing_event(self):
        self.assertIsNone(self.store.get_event(999))
        self.assertFalse(self.store.join_event(999, 20, "Анна"))
        self.assertFalse(self.store.cancel_event(999, 10))

    def test_database_constraints(self):
        event = self.event()
        with self.assertRaises(sqlite3.IntegrityError), self.store.connection:
            self.store.connection.execute(
                "INSERT INTO participants(event_id, telegram_user_id, display_name) VALUES (999, 1, 'X')"
            )
        with self.assertRaises(sqlite3.IntegrityError), self.store.connection:
            self.store.connection.execute("UPDATE events SET budget = 0 WHERE id = ?", (event["id"],))
        with self.assertRaises(sqlite3.IntegrityError), self.store.connection:
            self.store.connection.execute("UPDATE events SET currency = 'EUR' WHERE id = ?", (event["id"],))

    def test_storage_validates_event_fields(self):
        for title, budget in [("", 100), ("X" * 101, 100), ("X", 0), ("X", 10000001), ("X", 1.5)]:
            with self.subTest(title=title, budget=budget), self.assertRaises(ValueError):
                self.store.create_event(-1, 10, title, budget, "invalid")


if __name__ == "__main__":
    unittest.main()
