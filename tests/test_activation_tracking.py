"""SS-5.4: public activation indicators derived from existing SS-4 users."""
import unittest

try:
    import telegram
except ModuleNotFoundError:
    raise unittest.SkipTest("python-telegram-bot is not installed")

from telegram.error import BadRequest, Forbidden, NetworkError
import bot
import test_registration_activation as fixtures
from storage import Storage


class ActivationTrackingTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.RegistrationActivationTests.setUp
    tearDown = fixtures.RegistrationActivationTests.tearDown
    update = fixtures.RegistrationActivationTests.update

    def event(self, key="first", chat=-100, users=(20,), owner=10):
        event = self.store.create_event(chat, owner, "Санта", 10050, key)
        self.store.set_message_id(event["id"], event["id"] + 100)
        for uid in users:
            self.store.join_event(event["id"], uid, f"<Имя {uid}>")
        return self.store.get_event(event["id"])

    async def test_mixed_indicators_summary_legend_and_escaping(self):
        event = self.event(users=(20, 21))
        self.store.activate_private(20)
        await bot.refresh_event(self.context, event)
        text = self.context.bot.edit_message_text.call_args.kwargs["text"]
        self.assertIn("1. ✅ &lt;Имя 20&gt;", text)
        self.assertIn("2. ⏳ &lt;Имя 21&gt;", text)
        self.assertIn("📩 Связь с ботом: 1/2", text)
        self.assertIn("✅ — личный чат активирован\n⏳ — ожидает активации", text)
        self.assertNotIn("Ты даришь подарок", text)

    async def test_empty_game_shows_zero_summary_and_legend(self):
        event = self.event(users=())
        await bot.refresh_event(self.context, event)
        text = self.context.bot.edit_message_text.call_args.kwargs["text"]
        self.assertIn("📩 Связь с ботом: 0/0", text)
        self.assertIn("— пока никого", text)
        self.assertIn("⏳ — ожидает активации", text)

    async def test_join_then_start_updates_same_card_and_summary(self):
        event = self.event(users=(20,))
        self.store.activate_private(20)
        await bot.join_callback(self.update(user_id=21, data=f"join:{event['id']}"), self.context)
        self.assertIn("📩 Связь с ботом: 1/2", self.context.bot.edit_message_text.call_args.kwargs["text"])
        self.reply.reset_mock()
        self.context.args = [f"activate_{event['id']}"]
        await bot.start(self.update(user_id=21, private=True), self.context)
        text = self.context.bot.edit_message_text.call_args.kwargs["text"]
        self.assertIn("📩 Связь с ботом: 2/2", text)
        self.assertEqual(self.context.bot.edit_message_text.call_args.kwargs["message_id"], event["message_id"])
        self.assertEqual(self.reply.await_count, 2)  # Activation confirmation + menu, both private.
        self.context.bot.send_message.assert_not_awaited()

    async def test_start_updates_all_games_without_pagination_or_status_filter(self):
        events = [self.event(str(i), chat=-100 if i % 2 else -200, users=(20, 21)) for i in range(8)]
        self.store.cancel_event(events[6]["id"], 10)
        self.store.complete_draw(events[7]["id"], 10, [20, 21])
        self.event("unrelated", users=(21,))
        self.event("organizer-only", users=(21,), owner=20)
        await bot.start(self.update(private=True), self.context)
        edits = self.context.bot.edit_message_text.call_args_list
        self.assertEqual(len(edits), 8)
        self.assertEqual({call.kwargs["message_id"] for call in edits}, {e["message_id"] for e in events})
        for call in edits:
            self.assertIn("📩 Связь с ботом: 1/2", call.kwargs["text"])
            self.assertNotIn("Ты даришь подарок", call.kwargs["text"])
        self.assertEqual(self.reply.await_count, 1)  # Private menu; no group activation posts.

    async def test_restart_preserves_derived_activation_without_new_schema(self):
        event = self.event()
        self.store.activate_private(20)
        columns = [row[1] for row in self.store.connection.execute("PRAGMA table_info(participants)")]
        self.assertNotIn("private_active", columns)
        self.store.close()
        self.store = Storage(self.path)
        self.context.bot_data = {"storage": self.store}
        await bot.start(self.update(private=True), self.context)
        self.assertIn("📩 Связь с ботом: 1/1", self.context.bot.edit_message_text.call_args.kwargs["text"])
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM users").fetchone()[0], 1)

    async def test_failed_edit_does_not_break_join_or_activation(self):
        event = self.event(users=())
        self.context.bot.edit_message_text.side_effect = Forbidden("no edit rights")
        await bot.join_callback(self.update(data=f"join:{event['id']}"), self.context)
        self.assertEqual(len(self.store.participants(event["id"])), 1)
        self.assertFalse(self.store.private_active(20))
        await bot.start(self.update(private=True), self.context)
        self.assertTrue(self.store.private_active(20))
        self.assertEqual(len(self.store.participants(event["id"])), 1)
        self.assertIn("штаб Санты", self.reply.call_args.args[0])

    async def test_failed_first_card_does_not_prevent_other_edits(self):
        first = self.event()
        second = self.event("second")
        self.context.bot.edit_message_text.side_effect = [BadRequest("Message to edit not found"), None]
        await bot.start(self.update(private=True), self.context)
        self.assertEqual(self.context.bot.edit_message_text.await_count, 2)
        self.assertEqual(self.context.bot.edit_message_text.call_args.kwargs["message_id"], second["message_id"])
        self.assertTrue(self.store.private_active(20))
        self.context.bot.edit_message_text.reset_mock(side_effect=True)
        await bot.start(self.update(private=True), self.context)
        self.assertEqual(self.context.bot.edit_message_text.await_count, 2)
        self.assertEqual([e["id"] for e in self.store.participant_announcements(20)], [first["id"], second["id"]])

    async def test_failure_to_edit_does_not_break_draw_or_saved_notifications(self):
        event = self.event(users=(20, 21))
        self.context.bot.edit_message_text.side_effect = NetworkError("offline")
        await bot.draw_callback(self.update(user_id=10, data=f"draw:{event['id']}"), self.context)
        token = self.context.bot_data["draw_confirmations"][event["id"]]["token"]
        await bot.draw_callback(self.update(user_id=10, data=f"draw_yes:{event['id']}:{token}"), self.context)
        pairs = [dict(r) for r in self.store.assignments(event["id"])]
        self.assertEqual(len(pairs), 2)
        self.assertEqual(self.store.notification_counts(event["id"])["sent"], 2)
        self.assertFalse(self.store.private_active(20))
        await bot.start(self.update(private=True), self.context)
        self.assertEqual([dict(r) for r in self.store.assignments(event["id"])], pairs)

    async def test_activation_is_not_deliverability_and_does_not_change_delivery_states(self):
        event = self.event(users=(20, 21))
        self.store.complete_draw(event["id"], 10, [20, 21])
        self.store.set_notification_status(event["id"], 20, "failed")
        before = [dict(r) for r in self.store.assignments(event["id"])]
        await bot.start(self.update(private=True), self.context)
        self.assertIn("1. ✅", self.context.bot.edit_message_text.call_args.kwargs["text"])
        self.assertEqual([dict(r) for r in self.store.assignments(event["id"])], before)
        self.assertEqual(self.store.notification_counts(event["id"])["failed"], 1)

    async def test_repeated_start_handles_unchanged_card_and_does_not_duplicate_state(self):
        self.event()
        await bot.start(self.update(private=True), self.context)
        self.context.bot.edit_message_text.side_effect = BadRequest("Message is not modified")
        await bot.start(self.update(private=True), self.context)
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM users").fetchone()[0], 1)
        self.assertEqual(self.context.bot.edit_message_text.await_count, 2)


if __name__ == "__main__":
    unittest.main()
