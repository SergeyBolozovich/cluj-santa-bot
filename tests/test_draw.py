import random
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

from draw import make_pairs
from storage import Storage, OPEN, COMPLETED, DrawError, ParticipantsChanged


class PairingTests(unittest.TestCase):
    def test_exactly_two(self):
        self.assertEqual(make_pairs([10, 20]), [(10, 20), (20, 10)])

    def test_no_self_and_one_to_one_for_many_sizes(self):
        rng = random.getstate()
        self.addCleanup(random.setstate, rng)
        random.seed(17)
        for size in range(2, 101):
            ids = list(range(size))
            original = ids[:]
            pairs = make_pairs(ids)
            with self.subTest(size=size):
                self.assertEqual(len(pairs), size)
                self.assertEqual({giver for giver, _ in pairs}, set(ids))
                self.assertEqual({receiver for _, receiver in pairs}, set(ids))
                self.assertTrue(all(giver != receiver for giver, receiver in pairs))
                self.assertEqual(ids, original)

    def test_too_few_or_duplicate_participants(self):
        for ids in ([], [10], [10, 10]):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                make_pairs(ids)


class DrawStorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "santa.sqlite3"
        self.store = Storage(self.path)
        self.event = self.store.create_event(-100, 10, "Санта", 10050, "draw", 123)
        self.ids = [20, 30, 40]
        for uid in self.ids:
            self.store.join_event(self.event["id"], uid, f"Участник {uid}")
            self.store.activate_private(uid)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def complete(self, **kwargs):
        self.store.complete_draw(self.event["id"], 10, self.ids, **kwargs)

    def pair_ids(self):
        return [(r["giver_user_id"], r["receiver_user_id"]) for r in self.store.assignments(self.event["id"])]

    def test_commit_assignments_and_completed_status(self):
        self.complete()
        self.assertEqual(self.store.get_event(self.event["id"])["status"], COMPLETED)
        self.assertEqual(len(self.pair_ids()), 3)
        self.assertEqual(self.store.notification_counts(self.event["id"]), {"pending": 3, "sent": 0, "failed": 0})
        # A second connection sees the committed whole result, not just this connection's writes.
        other = Storage(self.path)
        try:
            self.assertEqual(other.get_event(self.event["id"])["status"], COMPLETED)
            self.assertEqual(len(other.assignments(self.event["id"])), 3)
        finally:
            other.close()

    def test_repeated_draw_never_calls_generator(self):
        self.complete()
        pairs = self.pair_ids()
        generator = Mock(side_effect=AssertionError("Must not regenerate"))
        with self.assertRaises(DrawError):
            self.complete(generator=generator)
        generator.assert_not_called()
        self.assertEqual(self.pair_ids(), pairs)

    def test_changed_membership_is_rejected(self):
        self.store.join_event(self.event["id"], 50, "Новый участник")
        with self.assertRaises(ParticipantsChanged):
            self.complete()
        self.assertEqual(self.pair_ids(), [])
        self.assertEqual(self.store.get_event(self.event["id"])["status"], OPEN)

    def test_cancelled_missing_or_wrong_owner_rejected(self):
        with self.assertRaises(DrawError):
            self.store.complete_draw(self.event["id"], 99, self.ids)
        with self.assertRaises(DrawError):
            self.store.complete_draw(999, 10, self.ids)
        self.store.cancel_event(self.event["id"], 10)
        with self.assertRaises(DrawError):
            self.complete()
        self.assertEqual(self.pair_ids(), [])

    def test_private_activation_rechecked_inside_transaction(self):
        with self.store.connection:
            self.store.connection.execute("DELETE FROM users WHERE telegram_user_id = 20")
        with self.assertRaises(DrawError):
            self.complete()
        self.assertEqual(self.pair_ids(), [])

    def test_too_few_participants_rejected_by_storage(self):
        event = self.store.create_event(-100, 10, "Мало участников", 100, "small")
        self.store.join_event(event["id"], 20, "Один")
        with self.assertRaises(DrawError):
            self.store.complete_draw(event["id"], 10, [20])

    def test_invalid_generator_output_rolls_back(self):
        outputs = [None, [(20, 20), (30, 40), (40, 30)], [(20, 30)],
                   [(20, 30), (30, 20), (40, 30)], [(20, 30), (30, 40), (40, 999)]]
        for output in outputs:
            with self.subTest(output=output), self.assertRaises(DrawError):
                self.complete(generator=lambda ids: output)
            self.assertEqual(self.pair_ids(), [])
            self.assertEqual(self.store.get_event(self.event["id"])["status"], OPEN)

    def test_partial_assignment_insert_rolls_back(self):
        self.store.connection.executescript("""
            CREATE TRIGGER fail_second_assignment BEFORE INSERT ON assignments
            WHEN (SELECT count(*) FROM assignments) = 1
            BEGIN SELECT RAISE(ABORT, 'injected failure'); END;
        """)
        with self.assertRaises(sqlite3.IntegrityError):
            self.complete()
        self.assertEqual(self.pair_ids(), [])
        self.assertEqual(self.store.get_event(self.event["id"])["status"], OPEN)
        self.assertFalse(self.store.connection.in_transaction)

    def test_status_update_failure_rolls_back_all_pairs(self):
        self.store.connection.executescript("""
            CREATE TRIGGER fail_completed BEFORE UPDATE OF status ON events
            WHEN NEW.status = 'draw_completed'
            BEGIN SELECT RAISE(ABORT, 'injected status failure'); END;
        """)
        with self.assertRaises(sqlite3.IntegrityError):
            self.complete()
        self.assertEqual(self.pair_ids(), [])
        self.assertEqual(self.store.get_event(self.event["id"])["status"], OPEN)

    def test_assignments_and_delivery_status_survive_restart(self):
        self.complete()
        pairs = self.pair_ids()
        self.store.set_notification_status(self.event["id"], 20, "failed")
        self.store.set_notification_status(self.event["id"], 30, "sent")
        self.store.close()
        self.store = Storage(self.path)
        self.assertEqual(self.pair_ids(), pairs)
        self.assertEqual(self.store.get_event(self.event["id"])["status"], COMPLETED)
        self.assertEqual(self.store.notification_counts(self.event["id"]), {"pending": 1, "sent": 1, "failed": 1})
        self.assertEqual({r["giver_user_id"] for r in self.store.assignments(self.event["id"], True)}, {20, 40})

    def test_two_connections_concurrently_draw_only_once(self):
        barrier = threading.Barrier(2)

        def attempt():
            store = Storage(self.path)
            try:
                barrier.wait(timeout=5)
                try:
                    store.complete_draw(self.event["id"], 10, self.ids)
                except DrawError:
                    return "rejected"
                return "committed"
            finally:
                store.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: attempt(), range(2)))
        self.assertCountEqual(results, ["committed", "rejected"])
        self.assertEqual(len(self.pair_ids()), 3)

    def test_events_have_independent_assignments(self):
        other = self.store.create_event(-100, 10, "Другая", 100, "other")
        for uid in self.ids:
            self.store.join_event(other["id"], uid, "Участник")
        self.complete()
        self.assertEqual(self.store.get_event(other["id"])["status"], OPEN)
        self.assertEqual(self.store.assignments(other["id"]), [])
        self.store.complete_draw(other["id"], 10, self.ids)
        self.assertEqual(len(self.store.assignments(other["id"])), 3)


if __name__ == "__main__":
    unittest.main()
