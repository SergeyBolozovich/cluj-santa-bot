"""Offline handler tests: no Application, polling, real token or HTTP requests."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

try:
    import telegram
except ModuleNotFoundError:
    raise unittest.SkipTest("python-telegram-bot is not installed")

import bot
from storage import Storage, OPEN, CANCELLED


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
            bot=SimpleNamespace(username="TestSantaBot", edit_message_text=AsyncMock()),
        )

    def tearDown(self):
        self.store.close()

    def message(self, message_id=1, text="", reply_to=None):
        return SimpleNamespace(
            message_id=message_id, chat_id=-100, text=text,
            reply_to_message=reply_to, reply_text=self.reply,
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

    async def test_creation_with_arguments_and_repeated_confirmation(self):
        self.context.args = ["Тайный", "Санта"]
        update = self.update()
        await bot.new_event(update, self.context)
        draft = bot.drafts(self.context)[(-100, 10)]
        self.assertEqual(draft["title"], "Тайный Санта")
        self.assertEqual(draft["step"], "budget")
        reply_to = self.message(draft["prompt_id"])
        await bot.creation_input(self.update(text="100,50", reply_to=reply_to), self.context)
        self.assertEqual(draft["step"], "confirm")
        self.assertEqual(draft["budget"], 10050)
        confirm = self.update(data=f"create:{draft['key']}")
        await bot.creation_callback(confirm, self.context)
        event = self.store.get_event_by_creation_key(draft["key"])
        self.assertEqual(event["budget"], 10050)
        self.assertEqual(self.store.participants(event["id"]), [])
        self.assertNotIn((-100, 10), bot.drafts(self.context))
        await bot.creation_callback(confirm, self.context)
        self.assertEqual(self.store.get_event_by_creation_key(draft["key"])["id"], event["id"])
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM events").fetchone()[0], 1)

    async def test_creation_without_title_and_invalid_budget(self):
        await bot.new_event(self.update(), self.context)
        draft = bot.drafts(self.context)[(-100, 10)]
        self.assertEqual(draft["step"], "title")
        await bot.creation_input(self.update(text="  Санта  ", reply_to=self.message(draft["prompt_id"])), self.context)
        self.assertEqual(draft["title"], "Санта")
        await bot.creation_input(self.update(text="0", reply_to=self.message(draft["prompt_id"])), self.context)
        self.assertEqual(draft["step"], "budget")
        self.assertNotIn("budget", draft)

    async def test_replies_are_scoped_to_organizer_chat_and_prompt(self):
        await bot.new_event(self.update(), self.context)
        draft = bot.drafts(self.context)[(-100, 10)]
        prompt = self.message(draft["prompt_id"])
        for update in (self.update(user_id=20, text="Чужое", reply_to=prompt),
                       self.update(chat_id=-200, text="Чужое", reply_to=prompt),
                       self.update(text="Без ответа"),
                       self.update(text="Старый ответ", reply_to=self.message(999))):
            await bot.creation_input(update, self.context)
        self.assertEqual(draft["step"], "title")

    async def test_creation_is_group_only(self):
        await bot.new_event(self.update(chat_type="private"), self.context)
        self.assertEqual(bot.drafts(self.context), {})

    async def test_distinct_drafts_and_cancellation(self):
        await bot.new_event(self.update(), self.context)
        await bot.new_event(self.update(user_id=20), self.context)
        first = bot.drafts(self.context)[(-100, 10)]
        await bot.new_event(self.update(), self.context)
        self.assertIs(bot.drafts(self.context)[(-100, 10)], first)
        foreign = self.update(user_id=20, data=f"discard:{first['key']}")
        await bot.creation_callback(foreign, self.context)
        self.assertIn((-100, 10), bot.drafts(self.context))
        await bot.cancel_creation(self.update(), self.context)
        self.assertNotIn((-100, 10), bot.drafts(self.context))
        self.assertIn((-100, 20), bot.drafts(self.context))
        second = bot.drafts(self.context)[(-100, 20)]
        await bot.creation_callback(self.update(user_id=20, data=f"discard:{second['key']}"), self.context)
        self.assertEqual(bot.drafts(self.context), {})

    async def test_stale_creation_button_after_restart(self):
        update = self.update(data="create:" + "a" * 32)
        await bot.creation_callback(update, self.context)
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM events").fetchone()[0], 0)
        update.callback_query.answer.assert_awaited_once()

    async def test_saved_confirmation_recovery_keeps_new_draft(self):
        event = self.store.create_event(-100, 10, "Санта", 100, "a" * 32, 123)
        await bot.new_event(self.update(), self.context)
        current = bot.drafts(self.context)[(-100, 10)]
        await bot.creation_callback(self.update(data="create:" + "a" * 32), self.context)
        self.assertIs(bot.drafts(self.context)[(-100, 10)], current)
        self.assertEqual(self.context.bot.edit_message_text.call_args.kwargs["message_id"], event["message_id"])

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


if __name__ == "__main__":
    unittest.main()
