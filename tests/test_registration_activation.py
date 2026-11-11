"""Group registration and independent, one-time private activation."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

try:
    import telegram
except ModuleNotFoundError:
    raise unittest.SkipTest("python-telegram-bot is not installed")

import bot
from telegram.error import Forbidden
from storage import Storage, OPEN


class RegistrationActivationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "santa.sqlite3"
        self.store = Storage(self.path)
        self.reply = AsyncMock()
        self.context = SimpleNamespace(
            args=[], bot_data={"storage": self.store},
            bot=SimpleNamespace(username="TestSantaBot", edit_message_text=AsyncMock(), send_message=AsyncMock()),
        )

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def event(self, key="first", chat=-100):
        return self.store.create_event(chat, 10, "Санта", 10050, key, 123)

    def update(self, user_id=20, data=None, private=False, chat_id=-100):
        user = SimpleNamespace(id=user_id, full_name=f"<Участник {user_id}>", username=f"user{user_id}")
        chat = SimpleNamespace(id=user_id if private else chat_id, type="private" if private else "supergroup")
        message = SimpleNamespace(reply_text=self.reply, chat_id=chat.id)
        query = SimpleNamespace(
            data=data, from_user=user, message=message, answer=AsyncMock(),
            edit_message_text=AsyncMock(), edit_message_reply_markup=AsyncMock(),
        ) if data else None
        return SimpleNamespace(effective_user=user, effective_chat=chat, message=message, callback_query=query)

    async def join(self, event, user_id=20):
        update = self.update(user_id=user_id, data=f"join:{event['id']}", chat_id=event["chat_id"])
        await bot.join_callback(update, self.context)
        return update

    async def activate(self, event, user_id=20):
        self.context.args = [f"activate_{event['id']}"]
        await bot.start(self.update(user_id=user_id, private=True), self.context)

    async def test_join_without_activation_persists_and_offers_addressed_deep_link(self):
        event = self.event()
        await self.join(event)
        self.assertEqual(len(self.store.participants(event["id"])), 1)
        self.assertFalse(self.store.private_active(20))
        text = self.reply.call_args.args[0]
        self.assertIn("tg://user?id=20", text)
        self.assertIn("&lt;Участник 20&gt;", text)
        button = self.reply.call_args.kwargs["reply_markup"].inline_keyboard[0][0]
        self.assertEqual(button.text, "📩 Активировать личные сообщения")
        self.assertEqual(button.url, f"https://t.me/TestSantaBot?start=activate_{event['id']}")
        self.context.bot.send_message.assert_not_awaited()  # Never try an unsolicited DM.

    async def test_duplicates_use_exact_callback_and_do_not_repeat_activation_prompt(self):
        event = self.event()
        await self.join(event)
        self.reply.reset_mock()
        update = await self.join(event)
        update.callback_query.answer.assert_awaited_once_with("✅ Вы уже участвуете в этой игре")
        self.assertEqual(len(self.store.participants(event["id"])), 1)
        self.reply.assert_not_awaited()

    async def test_participant_names_count_and_open_button_are_refreshed(self):
        event = self.event()
        await self.join(event, 20)
        await self.join(event, 21)
        edit = self.context.bot.edit_message_text.call_args.kwargs
        self.assertEqual(edit["message_id"], 123)
        self.assertIn("Участников: 2", edit["text"])
        self.assertIn("&lt;Участник 20&gt;", edit["text"])
        self.assertIn("&lt;Участник 21&gt;", edit["text"])
        self.assertIn(f"join:{event['id']}", [b.callback_data for row in edit["reply_markup"].inline_keyboard for b in row])

    async def test_start_deep_link_activates_actual_sender_and_keeps_registration(self):
        event = self.event()
        await self.join(event)
        participants = [dict(p) for p in self.store.participants(event["id"])]
        await self.activate(event)
        self.assertTrue(self.store.private_active(20))
        self.assertFalse(self.store.private_active(21))
        self.assertEqual([dict(p) for p in self.store.participants(event["id"])], participants)
        self.assertEqual(self.store.get_event(event["id"])["status"], OPEN)
        self.assertEqual(self.store.assignments(event["id"]), [])
        self.assertTrue(any("Личные сообщения активированы" in call.args[0] for call in self.reply.call_args_list))

    async def test_shared_link_does_not_activate_another_user_or_register_sender(self):
        event = self.event()
        await self.join(event, 20)
        await self.activate(event, 21)
        self.assertTrue(self.store.private_active(21))
        self.assertFalse(self.store.private_active(20))
        self.assertEqual([p["telegram_user_id"] for p in self.store.participants(event["id"])], [20])

    async def test_repeated_activation_is_idempotent_and_survives_restart(self):
        event = self.event()
        await self.activate(event)
        first_date = self.store.connection.execute("SELECT private_started_at FROM users WHERE telegram_user_id=20").fetchone()[0]
        await self.activate(event)
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM users").fetchone()[0], 1)
        self.assertEqual(self.store.connection.execute("SELECT private_started_at FROM users WHERE telegram_user_id=20").fetchone()[0], first_date)
        self.assertTrue(any("уже активированы" in call.args[0] for call in self.reply.call_args_list))
        self.store.close()
        self.store = Storage(self.path)
        self.context.bot_data = {"storage": self.store}
        self.reply.reset_mock()
        await self.join(event)
        self.assertTrue(self.store.private_active(20))
        self.reply.assert_not_awaited()

    async def test_already_active_registration_does_not_require_activation(self):
        event = self.event()
        self.store.activate_private(20)
        await self.join(event)
        self.assertEqual(len(self.store.participants(event["id"])), 1)
        self.reply.assert_not_awaited()

    async def test_multiple_games_independent_with_shared_private_activation(self):
        first = self.event()
        second = self.event("second")
        third = self.event("third", -200)
        await self.join(first)
        self.assertEqual(self.store.participants(second["id"]), [])
        await self.activate(first)
        self.reply.reset_mock()
        await self.join(second)
        await self.join(third)
        for event in (first, second, third):
            self.assertEqual([p["telegram_user_id"] for p in self.store.participants(event["id"])], [20])
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM users").fetchone()[0], 1)
        self.reply.assert_not_awaited()

    async def test_cancellation_removes_join_for_everyone_but_keeps_management(self):
        event = self.event()
        await self.join(event)
        await bot.management_callback(self.update(user_id=10, data=f"cancel_yes:{event['id']}"), self.context)
        markup = self.context.bot.edit_message_text.call_args.kwargs["reply_markup"]
        data = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertNotIn(f"join:{event['id']}", data)
        self.assertIn(f"manage:{event['id']}", data)
        await self.join(event, 21)
        self.assertEqual(len(self.store.participants(event["id"])), 1)

    async def test_completed_draw_removes_join_and_does_not_require_reactivation(self):
        event = self.event()
        for user_id in (20, 21):
            await self.join(event, user_id)
            await self.activate(event, user_id)
        await bot.draw_callback(self.update(user_id=10, data=f"draw:{event['id']}"), self.context)
        token = self.context.bot_data["draw_confirmations"][event["id"]]["token"]
        await bot.draw_callback(self.update(user_id=10, data=f"draw_yes:{event['id']}:{token}"), self.context)
        markup = self.context.bot.edit_message_text.call_args.kwargs["reply_markup"]
        data = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertNotIn(f"join:{event['id']}", data)
        self.assertIn(f"manage:{event['id']}", data)
        self.assertEqual(len(self.store.assignments(event["id"])), 2)

    async def test_draw_before_activation_retains_results_for_later_private_view(self):
        event = self.event()
        for user_id in (20, 21, 22):
            await self.join(event, user_id)
        await self.activate(event, 22)
        async def deliver(chat_id, text, **kwargs):
            self.assertEqual(len(self.store.assignments(event["id"])), 3)
            self.assertFalse(self.store.connection.in_transaction)
            if chat_id != 22:
                raise Forbidden("private chat not started")
        self.context.bot.send_message.side_effect = deliver
        self.reply.reset_mock()
        await bot.draw_callback(self.update(user_id=10, data=f"draw:{event['id']}"), self.context)
        token = self.context.bot_data["draw_confirmations"][event["id"]]["token"]
        self.assertEqual(self.store.assignments(event["id"]), [])
        self.context.bot.send_message.assert_not_awaited()
        await bot.draw_callback(self.update(user_id=10, data=f"draw_yes:{event['id']}:{token}"), self.context)
        self.assertEqual(self.store.notification_counts(event["id"]), {"sent": 1, "failed": 2, "pending": 0})
        pairs = [dict(row) for row in self.store.assignments(event["id"])]
        report = self.reply.call_args.args[0]
        self.assertIn("Отправлено: 1; ошибок: 2", report)
        self.assertNotIn("tg://user", report)
        self.store.close()
        self.store = Storage(self.path)
        self.context.bot_data = {"storage": self.store}
        await self.activate(event, 20)
        await bot.my_games(self.update(user_id=20, private=True), self.context)
        self.assertIn(f"game:{event['id']}", [b.callback_data for row in self.reply.call_args.kwargs["reply_markup"].inline_keyboard for b in row])
        await bot.private_callback(self.update(user_id=20, data=f"recipient:{event['id']}", private=True), self.context)
        recipient = self.store.saved_recipient(event["id"], 20)
        self.assertIn(f"tg://user?id={recipient['receiver_user_id']}", self.reply.call_args.args[0])
        self.assertEqual([dict(row) for row in self.store.assignments(event["id"])], pairs)

    async def test_deep_link_start_in_group_does_not_activate(self):
        event = self.event()
        self.context.args = [f"activate_{event['id']}"]
        await bot.start(self.update(), self.context)
        self.assertFalse(self.store.private_active(20))
        self.reply.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
