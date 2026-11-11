"""Offline handler tests: no Application, polling, real token or HTTP requests."""
import unittest
import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

try:
    import telegram
except ModuleNotFoundError:
    raise unittest.SkipTest("python-telegram-bot is not installed")

import bot
from telegram.error import Forbidden, NetworkError
from storage import Storage, OPEN, CANCELLED, COMPLETED


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Storage(":memory:")
        self.message_id = 100

        async def reply(*args, **kwargs):
            self.message_id += 1
            return self.message(self.message_id)

        self.reply = AsyncMock(side_effect=reply)
        self.context = SimpleNamespace(
            args=[], bot_data={"storage": self.store},
            bot=SimpleNamespace(username="TestSantaBot", edit_message_text=AsyncMock(), send_message=AsyncMock(),
                                get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member")),
                                get_chat=AsyncMock(return_value=SimpleNamespace(id=-100, type="group", username=None, invite_link=None))),
        )
        self.context.bot.send_message.return_value = self.message(700)

    def tearDown(self):
        self.store.close()

    def message(self, message_id=1, text="", reply_to=None):
        return SimpleNamespace(
            message_id=message_id, chat_id=-100, text=text,
            reply_to_message=reply_to, reply_text=self.reply, edit_text=self.reply,
        )

    def update(self, user_id=10, chat_id=-100, chat_type="supergroup", text="", reply_to=None, data=None):
        user = SimpleNamespace(id=user_id, full_name="Анна <Иванова>", username="anna")
        message = self.message(text=text, reply_to=reply_to)
        message.chat_id = chat_id
        query = None
        if data:
            query = SimpleNamespace(
                data=data, message=message, from_user=user, answer=AsyncMock(),
                edit_message_text=AsyncMock(), edit_message_reply_markup=AsyncMock(),
            )
        return SimpleNamespace(
            effective_user=user, effective_chat=SimpleNamespace(id=chat_id, type=chat_type),
            message=message, callback_query=query,
        )

    async def open_setup(self, user_id=10, chat_id=-100):
        await bot.new_event(self.update(user_id=user_id, chat_id=chat_id), self.context)
        draft = bot.drafts(self.context)[(chat_id, user_id)]
        self.context.args = [f"setup_{draft['key']}"]
        await bot.start(self.update(user_id=user_id, chat_id=user_id, chat_type="private"), self.context)
        return draft

    def setup_update(self, draft, text="", action=None):
        update = self.update(user_id=draft["owner_id"], chat_id=draft["owner_id"], chat_type="private",
                             text=text, data=f"{action}:{draft['key']}" if action else None)
        update.message.message_id = draft["prompt_id"]
        return update

    async def test_creation_with_arguments_and_repeated_confirmation(self):
        self.context.args = ["Название", "в", "группе"]
        draft = await self.open_setup()
        self.assertEqual(draft["step"], "title")
        await bot.creation_input(self.setup_update(draft, "Тайный Санта"), self.context)
        await bot.creation_input(self.setup_update(draft, "100,50"), self.context)
        confirm = self.setup_update(draft, action="create")
        await bot.creation_callback(confirm, self.context)
        event = self.store.get_event_by_creation_key(draft["key"])
        self.assertEqual(event["title"], "Тайный Санта")
        self.assertEqual(event["budget"], 10050)
        self.assertEqual(event["message_id"], 700)
        self.assertEqual(event["chat_id"], -100)
        self.assertEqual(self.store.participants(event["id"]), [])
        await bot.creation_callback(confirm, self.context)
        self.context.bot.send_message.assert_awaited_once()

    async def test_creation_without_title_and_invalid_budget(self):
        draft = await self.open_setup()
        await bot.creation_input(self.setup_update(draft, "  Санта  "), self.context)
        await bot.creation_input(self.setup_update(draft, "0"), self.context)
        self.assertEqual(draft["title"], "Санта")
        self.assertEqual(draft["step"], "budget")
        self.assertNotIn("budget", draft)

    async def test_replies_are_scoped_to_organizer_chat_and_prompt(self):
        draft = await self.open_setup()
        for update in (self.update(user_id=20, chat_id=20, chat_type="private", text="Чужой"),
                       self.update(text="Группа"),
                       self.setup_update(draft, "Старый ответ")):
            if update.message.text == "Старый ответ":
                update.message.reply_to_message = self.message(999)
            await bot.creation_input(update, self.context)
        self.assertEqual(draft["step"], "title")

    async def test_creation_is_group_only(self):
        await bot.new_event(self.update(chat_type="private"), self.context)
        self.assertEqual(bot.drafts(self.context), {})

    async def test_distinct_drafts_and_cancellation(self):
        first = await self.open_setup()
        second = await self.open_setup(user_id=20)
        foreign = self.update(user_id=20, chat_id=20, chat_type="private", data=f"discard:{first['key']}")
        await bot.creation_callback(foreign, self.context)
        self.assertIn((-100, 10), bot.drafts(self.context))
        await bot.cancel_creation(self.update(), self.context)
        self.assertNotIn((-100, 10), bot.drafts(self.context))
        await bot.creation_callback(self.setup_update(second, action="discard"), self.context)
        self.assertEqual(bot.drafts(self.context), {})

    async def test_stale_creation_button_after_restart(self):
        update = self.update(chat_type="private", data="create:" + "a" * 32)
        await bot.creation_callback(update, self.context)
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM events").fetchone()[0], 0)
        update.callback_query.answer.assert_awaited_once()

    async def test_saved_confirmation_recovery_keeps_new_draft(self):
        event = self.store.create_event(-100, 10, "Санта", 100, "a" * 32, 123)
        draft = await self.open_setup()
        await bot.creation_callback(self.update(chat_type="private", data="create:" + "a" * 32), self.context)
        self.assertIs(bot.drafts(self.context)[(-100, 10)], draft)
        self.assertEqual(self.store.get_event(event["id"])["message_id"], 123)
        self.context.bot.send_message.assert_not_awaited()

    async def test_join_independent_events_and_duplicate_updates(self):
        first = self.store.create_event(-100, 10, "Первая", 100, "first", 123)
        second = self.store.create_event(-100, 10, "Вторая", 200, "second", 124)
        for event in (first, first, second):
            await bot.join_callback(self.update(user_id=20, data=f"join:{event['id']}"), self.context)
        self.assertEqual(len(self.store.participants(first["id"])), 1)
        self.assertEqual(len(self.store.participants(second["id"])), 1)
        self.assertEqual(self.context.bot.edit_message_text.call_args.kwargs["message_id"], 124)

    async def test_wrong_group_cannot_join_or_manage(self):
        event = self.store.create_event(-100, 10, "Санта", 100, "first", 123)
        await bot.join_callback(self.update(chat_id=-200, data=f"join:{event['id']}"), self.context)
        await bot.management_callback(self.update(chat_id=-200, data=f"cancel_yes:{event['id']}"), self.context)
        self.assertEqual(self.store.participants(event["id"]), [])
        self.assertEqual(self.store.get_event(event["id"])["status"], OPEN)

    async def test_management_requires_owner_and_confirmation(self):
        event = self.store.create_event(-100, 10, "Санта", 100, "first", 123)
        await bot.management_callback(self.update(user_id=20, data=f"cancel_yes:{event['id']}"), self.context)
        self.assertEqual(self.store.get_event(event["id"])["status"], OPEN)
        await bot.management_callback(self.update(data=f"manage:{event['id']}"), self.context)
        await bot.management_callback(self.update(data=f"cancel_ask:{event['id']}"), self.context)
        self.assertEqual(self.store.get_event(event["id"])["status"], OPEN)
        await bot.management_callback(self.update(data=f"cancel_yes:{event['id']}"), self.context)
        self.assertEqual(self.store.get_event(event["id"])["status"], CANCELLED)
        await bot.join_callback(self.update(user_id=20, data=f"join:{event['id']}"), self.context)
        self.assertEqual(self.store.participants(event["id"]), [])

    async def test_private_activation_only(self):
        await bot.start(self.update(), self.context)
        self.assertFalse(self.store.private_active(10))
        await bot.start(self.update(chat_type="private"), self.context)
        self.assertTrue(self.store.private_active(10))

    def test_rendering_escapes_html_and_bounds_long_lists(self):
        event = self.store.create_event(-100, 10, "<Санта & друзья>", 100, "first", 123)
        participants = [{"display_name": "<Анна>" * 30} for _ in range(500)]
        text = bot.render_event_text(event, participants, "TestSantaBot")
        self.assertIn("&lt;Санта &amp; друзья&gt;", text)
        self.assertIn("&lt;Анна&gt;", text)
        self.assertIn("Участников: 500", text)
        self.assertLess(len(text), 4096)

    def ready_event(self, count=3):
        event = self.store.create_event(-100, 10, "Санта", 10050, "draw", 123)
        for uid in range(20, 20 + count):
            self.store.join_event(event["id"], uid, f"Участник {uid}")
            self.store.activate_private(uid)
        return event

    async def request_draw(self, event):
        await bot.draw_callback(self.update(data=f"draw:{event['id']}"), self.context)
        token = self.context.bot_data["draw_confirmations"][event["id"]]["token"]
        return self.update(data=f"draw_yes:{event['id']}:{token}")

    async def test_draw_requires_confirmation_and_two_participants_work(self):
        event = self.ready_event(2)
        confirm = await self.request_draw(event)
        self.assertEqual(self.store.assignments(event["id"]), [])
        self.context.bot.send_message.assert_not_awaited()
        confirmation_text = self.reply.call_args.args[0]
        self.assertIn("100,50 RON", confirmation_text)
        self.assertIn("Участников: 2", confirmation_text)
        await bot.draw_callback(confirm, self.context)
        pairs = self.store.assignments(event["id"])
        self.assertEqual([(r["giver_user_id"], r["receiver_user_id"]) for r in pairs], [(20, 21), (21, 20)])
        self.assertEqual(self.store.get_event(event["id"])["status"], COMPLETED)
        self.assertEqual(self.store.notification_counts(event["id"])["sent"], 2)

    async def test_draw_is_organizer_only(self):
        event = self.ready_event()
        await bot.draw_callback(self.update(user_id=20, data=f"draw:{event['id']}"), self.context)
        self.assertNotIn(event["id"], self.context.bot_data.get("draw_confirmations", {}))
        confirm = await self.request_draw(event)
        confirm.effective_user.id = 20
        await bot.draw_callback(confirm, self.context)
        self.assertEqual(self.store.assignments(event["id"]), [])

    async def test_draw_requires_at_least_two(self):
        event = self.ready_event(1)
        await bot.draw_callback(self.update(data=f"draw:{event['id']}"), self.context)
        self.assertNotIn(event["id"], self.context.bot_data.get("draw_confirmations", {}))
        self.context.bot.send_message.assert_not_awaited()

    async def test_back_invalidates_confirmation(self):
        event = self.ready_event()
        confirm = await self.request_draw(event)
        back = self.update(data=confirm.callback_query.data.replace("draw_yes:", "draw_no:"))
        await bot.draw_callback(back, self.context)
        await bot.draw_callback(confirm, self.context)
        self.assertEqual(self.store.assignments(event["id"]), [])
        self.context.bot.send_message.assert_not_awaited()

    async def test_participant_change_requires_new_confirmation(self):
        event = self.ready_event()
        confirm = await self.request_draw(event)
        old_token = confirm.callback_query.data
        self.store.join_event(event["id"], 50, "Новый")
        await bot.draw_callback(confirm, self.context)
        self.assertEqual(self.store.assignments(event["id"]), [])
        self.context.bot.send_message.assert_not_awaited()
        new_token = self.context.bot_data["draw_confirmations"][event["id"]]["token"]
        self.assertNotIn(new_token, old_token)
        self.assertIn(50, self.context.bot_data["draw_confirmations"][event["id"]]["ids"])

    async def test_count_revalidated_at_confirmation(self):
        event = self.ready_event()
        confirm = await self.request_draw(event)
        # Simulate changed persistent state; normal UI does not allow withdrawal.
        with self.store.connection:
            self.store.connection.execute("DELETE FROM participants WHERE telegram_user_id != 20")
        await bot.draw_callback(confirm, self.context)
        self.assertEqual(self.store.assignments(event["id"]), [])
        self.context.bot.send_message.assert_not_awaited()

    async def test_cancelled_event_rejected_at_confirmation(self):
        event = self.ready_event()
        confirm = await self.request_draw(event)
        self.store.cancel_event(event["id"], 10)
        await bot.draw_callback(confirm, self.context)
        self.assertEqual(self.store.assignments(event["id"]), [])
        self.context.bot.send_message.assert_not_awaited()

    async def test_completed_event_cannot_regenerate(self):
        event = self.ready_event()
        confirm = await self.request_draw(event)
        await bot.draw_callback(confirm, self.context)
        pairs = [dict(r) for r in self.store.assignments(event["id"])]
        self.context.bot.send_message.reset_mock()
        with patch("storage.make_pairs", side_effect=AssertionError("No regeneration")):
            await bot.draw_callback(confirm, self.context)
            await bot.draw_callback(self.update(data=f"draw:{event['id']}"), self.context)
        self.assertEqual([dict(r) for r in self.store.assignments(event["id"])], pairs)
        self.context.bot.send_message.assert_not_awaited()

    async def test_missing_private_activation_does_not_block_draw(self):
        event = self.ready_event()
        confirm = await self.request_draw(event)
        with self.store.connection:
            self.store.connection.execute("DELETE FROM users WHERE telegram_user_id = 20")
        await bot.draw_callback(confirm, self.context)
        self.assertEqual(len(self.store.assignments(event["id"])), 3)
        self.assertEqual(self.store.get_event(event["id"])["status"], COMPLETED)
        self.assertEqual(self.context.bot.send_message.await_count, 3)

    async def test_unreachable_chats_keep_completed_draw_and_failed_notifications(self):
        event = self.ready_event()
        self.context.bot.send_message.side_effect = Forbidden("blocked")
        confirm = await self.request_draw(event)
        await bot.draw_callback(confirm, self.context)
        self.assertEqual(self.store.get_event(event["id"])["status"], COMPLETED)
        self.assertEqual(len(self.store.assignments(event["id"])), 3)
        self.assertEqual(self.store.notification_counts(event["id"])["failed"], 3)
        self.assertIn("ошибок: 3", confirm.callback_query.answer.call_args.args[0])

    async def test_assignments_are_committed_before_first_result_send(self):
        event = self.ready_event()
        observed = []

        async def observe(chat_id, text, **kwargs):
            if "Ты даришь подарок " in text:
                self.assertEqual(self.store.get_event(event["id"])["status"], COMPLETED)
                self.assertEqual(len(self.store.assignments(event["id"])), 3)
                self.assertFalse(self.store.connection.in_transaction)
                self.assertIn(chat_id, (20, 21, 22))
                observed.append(chat_id)

        self.context.bot.send_message.side_effect = observe
        confirm = await self.request_draw(event)
        await bot.draw_callback(confirm, self.context)
        self.assertCountEqual(observed, [20, 21, 22])
        for call in self.reply.call_args_list:
            self.assertNotIn("Ты даришь подарок ", call.args[0])

    async def test_database_failure_never_sends_assignment(self):
        event = self.ready_event()
        confirm = await self.request_draw(event)
        with patch.object(self.store, "complete_draw", side_effect=sqlite3.OperationalError("test")):
            await bot.draw_callback(confirm, self.context)
        self.assertEqual(self.store.assignments(event["id"]), [])
        self.assertEqual(self.store.get_event(event["id"])["status"], OPEN)
        self.context.bot.send_message.assert_not_awaited()

    async def test_notification_failure_continues_and_retry_preserves_pairs(self):
        event = self.ready_event()

        async def fail_one(chat_id, text, **kwargs):
            if "Ты даришь подарок " in text and chat_id == 20:
                raise NetworkError("uncertain outcome")

        self.context.bot.send_message.side_effect = fail_one
        confirm = await self.request_draw(event)
        await bot.draw_callback(confirm, self.context)
        pairs = [(r["giver_user_id"], r["receiver_user_id"]) for r in self.store.assignments(event["id"])]
        self.assertEqual(self.store.notification_counts(event["id"]), {"sent": 2, "pending": 0, "failed": 1})
        self.assertIn("ошибок: 1", confirm.callback_query.answer.call_args.args[0])
        self.context.bot.send_message.reset_mock()
        self.context.bot.send_message.side_effect = None
        with patch.object(self.store, "complete_draw", side_effect=AssertionError("No draw on retry")):
            await bot.retry_callback(self.update(data=f"retry:{event['id']}"), self.context)
        self.context.bot.send_message.assert_awaited_once()
        self.assertEqual(self.context.bot.send_message.call_args.args[0], 20)
        self.assertEqual([(r["giver_user_id"], r["receiver_user_id"]) for r in self.store.assignments(event["id"])], pairs)
        self.assertEqual(self.store.notification_counts(event["id"])["sent"], 3)

    async def test_retry_rejects_nonorganizer_and_open_event(self):
        event = self.ready_event()
        await bot.retry_callback(self.update(data=f"retry:{event['id']}"), self.context)
        self.store.complete_draw(event["id"], 10, [20, 21, 22])
        await bot.retry_callback(self.update(user_id=20, data=f"retry:{event['id']}"), self.context)
        await bot.retry_callback(self.update(chat_id=-200, data=f"retry:{event['id']}"), self.context)
        self.context.bot.send_message.assert_not_awaited()

    async def test_pending_notifications_can_retry_after_restart(self):
        event = self.ready_event()
        self.store.complete_draw(event["id"], 10, [20, 21, 22])
        self.context.bot_data = {"storage": self.store}  # No confirmations or locks survive restart.
        await bot.retry_callback(self.update(data=f"retry:{event['id']}"), self.context)
        self.assertEqual(self.store.notification_counts(event["id"])["sent"], 3)
        self.assertEqual(self.context.bot.send_message.await_count, 3)

    async def test_crash_after_send_can_deliver_same_pair_again(self):
        event = self.ready_event(2)
        self.store.complete_draw(event["id"], 10, [20, 21])
        with patch.object(self.store, "set_notification_status", side_effect=sqlite3.OperationalError("crash")):
            with self.assertRaises(sqlite3.OperationalError):
                await bot.retry_callback(self.update(data=f"retry:{event['id']}"), self.context)
        first = self.context.bot.send_message.call_args
        self.assertEqual(self.store.notification_counts(event["id"])["pending"], 2)
        await bot.retry_callback(self.update(data=f"retry:{event['id']}"), self.context)
        repeated = self.context.bot.send_message.call_args_list[1]
        self.assertEqual(first, repeated)
        self.assertEqual(self.store.notification_counts(event["id"])["sent"], 2)

    async def test_concurrent_retry_does_not_send_twice(self):
        event = self.ready_event(2)
        self.store.complete_draw(event["id"], 10, [20, 21])
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_send(*args, **kwargs):
            started.set()
            await release.wait()

        self.context.bot.send_message.side_effect = slow_send
        first = asyncio.create_task(bot.retry_callback(self.update(data=f"retry:{event['id']}"), self.context))
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            await bot.retry_callback(self.update(data=f"retry:{event['id']}"), self.context)
        finally:
            release.set()
            await first
        self.assertEqual(self.context.bot.send_message.await_count, 2)

    async def test_concurrent_confirmation_commits_one_draw(self):
        event = self.ready_event(2)
        confirm = await self.request_draw(event)
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_send(*args, **kwargs):
            started.set()
            await release.wait()

        self.context.bot.send_message.side_effect = slow_send
        first = asyncio.create_task(bot.draw_callback(confirm, self.context))
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            await bot.draw_callback(confirm, self.context)
        finally:
            release.set()
            await first
        self.assertEqual(len(self.store.assignments(event["id"])), 2)
        self.assertEqual(self.context.bot.send_message.await_count, 2)  # Results only, after commit.

    async def test_completed_management_shows_counts_and_retry_only(self):
        event = self.ready_event()
        self.store.complete_draw(event["id"], 10, [20, 21, 22])
        update = self.update(data=f"manage:{event['id']}")
        await bot.management_callback(update, self.context)
        text = self.reply.call_args.args[0]
        self.assertIn("ожидают: 3", update.callback_query.answer.call_args.args[0])
        self.assertNotIn("ожидают:", text)
        self.assertNotIn("tg://user", text)
        markup = self.reply.call_args.kwargs["reply_markup"]
        self.assertEqual(markup.inline_keyboard[0][0].callback_data, f"retry:{event['id']}")


if __name__ == "__main__":
    unittest.main()
