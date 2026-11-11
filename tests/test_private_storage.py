import tempfile
import unittest
from pathlib import Path

from storage import Storage, OPEN, COMPLETED, CANCELLED


class PrivateStorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "santa.sqlite3"
        self.store = Storage(self.path)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def event(self, key="first", chat=-100, owner=10):
        return self.store.create_event(chat, owner, "Санта", 10050, key)

    def completed_event(self):
        event = self.event()
        for uid in (20, 21, 22):
            self.store.join_event(event["id"], uid, f"Имя {uid}")
            self.store.activate_private(uid)
        self.store.complete_draw(event["id"], 10, [20, 21, 22], generator=lambda ids: [(20, 21), (21, 22), (22, 20)])
        return event

    def test_list_requires_participation_even_for_organizer(self):
        first = self.event()
        second = self.event("second", -200, 20)
        unrelated = self.event("unrelated")
        self.store.join_event(first["id"], 20, "Анна")
        self.store.join_event(first["id"], 21, "Иван")
        self.assertEqual([e["id"] for e in self.store.participant_games(20)], [first["id"]])
        self.assertEqual(self.store.participant_games(10), [])
        self.store.join_event(second["id"], 20, "Анна")
        games = self.store.participant_games(20)
        self.assertEqual([e["id"] for e in games], [second["id"], first["id"]])
        self.assertEqual(games[1]["participant_count"], 2)
        self.assertEqual(games[1]["budget"], 10050)
        self.assertNotIn(unrelated["id"], {e["id"] for e in games})

    def test_detail_access_and_cancelled_history(self):
        event = self.event()
        self.store.join_event(event["id"], 20, "Анна")
        self.assertIsNone(self.store.participant_event(event["id"], 10))
        self.assertIsNone(self.store.participant_event(event["id"], 999))
        self.store.cancel_event(event["id"], 10)
        self.assertEqual(self.store.participant_event(event["id"], 20)["status"], CANCELLED)
        self.assertEqual(self.store.participant_games(20)[0]["status"], CANCELLED)

    def test_saved_recipient_returns_only_callers_pair(self):
        event = self.completed_event()
        self.assertEqual(self.store.saved_recipient(event["id"], 20)["receiver_user_id"], 21)
        self.assertEqual(self.store.saved_recipient(event["id"], 21)["receiver_user_id"], 22)
        self.assertIsNone(self.store.saved_recipient(event["id"], 10))
        self.assertIsNone(self.store.saved_recipient(event["id"], 999))
        self.assertIsNone(self.store.saved_recipient(999, 20))

    def test_recipient_unavailable_for_incomplete_draw_or_missing_assignment(self):
        event = self.event()
        self.store.join_event(event["id"], 20, "Анна")
        self.assertEqual(self.store.get_event(event["id"])["status"], OPEN)
        self.assertIsNone(self.store.saved_recipient(event["id"], 20))
        # Old SS-1 completed events may have no assignments.
        with self.store.connection:
            self.store.connection.execute("UPDATE events SET status = ? WHERE id = ?", (COMPLETED, event["id"]))
        self.assertIsNone(self.store.saved_recipient(event["id"], 20))

    def test_reads_survive_restart_and_leave_draw_and_delivery_unchanged(self):
        event = self.completed_event()
        self.store.set_notification_status(event["id"], 20, "failed")
        before = [dict(row) for row in self.store.assignments(event["id"])]
        self.store.close()
        self.store = Storage(self.path)
        self.assertEqual(self.store.participant_games(20)[0]["id"], event["id"])
        for _ in range(2):
            self.assertEqual(self.store.saved_recipient(event["id"], 20)["receiver_name"], "Имя 21")
        self.assertEqual([dict(row) for row in self.store.assignments(event["id"])], before)

    def test_pagination_is_scoped_and_stable(self):
        ids = []
        for index in range(8):
            event = self.event(str(index))
            self.store.join_event(event["id"], 20, "Анна")
            ids.append(event["id"])
        self.assertEqual([e["id"] for e in self.store.participant_games(20, 0, 5)], list(reversed(ids))[:5])
        self.assertEqual([e["id"] for e in self.store.participant_games(20, 5, 5)], list(reversed(ids))[5:])
        self.assertEqual(self.store.participant_games(999, 0, 5), [])


if __name__ == "__main__":
    unittest.main()
