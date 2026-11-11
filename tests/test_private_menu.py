"""SS-3 checks use only temporary SQLite and mocked Telegram messages."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

try:
    import telegram
except ModuleNotFoundError:
    raise unittest.SkipTest("python-telegram-bot is not installed")

import bot
from storage import Storage, COMPLETED


class PrivateMenuTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "santa.sqlite3"
        self.store = Storage(self.path)
        self.reply = AsyncMock()
        self.context = SimpleNamespace(
            bot_data={"storage": self.store}, args=[],
            bot=SimpleNamespace(username="TestSantaBot", send_message=AsyncMock()),
        )

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def update(self, user_id=20, data=None, private=True):
        user = SimpleNamespace(id=user_id)
        chat = SimpleNamespace(id=user_id if private else -100, type="private" if private else "supergroup")
        message = SimpleNamespace(reply_text=self.reply, chat_id=chat.id)
        query = SimpleNamespace(data=data, from_user=user, message=message, answer=AsyncMock()) if data else None
        return SimpleNamespace(effective_chat=chat, effective_user=user, message=message, callback_query=query)

    def event(self, key="first", participants=(20, 21), owner=10, chat=-100, completed=False):
        event = self.store.create_event(chat, owner, "<Санта & друзья>", 10050, key)
        for uid in participants:
            self.store.join_event(event["id"], uid, f"<Имя {uid}>")
            self.store.activate_private(uid)
        if completed:
            self.store.complete_draw(event["id"], owner, list(participants), generator=lambda ids: list(zip(ids, ids[1:] + ids[:1])))
        return event

    def buttons(self):
        markup = self.reply.call_args.kwargs.get("reply_markup")
        return [button for row in markup.inline_keyboard for button in row] if markup else []

    async def test_start_activates_private_chat_and_shows_main_menu(self):
        self.assertFalse(self.store.private_active(20))
        await bot.start(self.update(), self.context)
        self.assertTrue(self.store.private_active(20))
        self.assertIn("Тайный Санта", self.reply.call_args.args[0])
        self.assertEqual([b.text for b in self.buttons()], ["🎁 Мои игры", "ℹ️ Помощь"])

    async def test_start_in_group_does_not_show_private_menu(self):
        await bot.start(self.update(private=False), self.context)
        self.assertFalse(self.store.private_active(20))
        self.reply.assert_not_awaited()

    async def test_empty_mygames_explains_registration(self):
        await bot.my_games(self.update(), self.context)
        self.assertIn("пока не участвуешь", self.reply.call_args.args[0])
        self.assertIn("Присоединиться", self.reply.call_args.args[0])

    async def test_mygames_lists_only_participation_including_participating_owner(self):
        first = self.event()
        owned_only = self.event("owned", participants=(21, 22), owner=20)
        owned_participation = self.event("both", owner=20, chat=-200)
        await bot.my_games(self.update(), self.context)
        data = [b.callback_data for b in self.buttons()]
        self.assertIn(f"game:{first['id']}", data)
        self.assertIn(f"game:{owned_participation['id']}", data)
        self.assertNotIn(f"game:{owned_only['id']}", data)
        text = self.reply.call_args.args[0]
        self.assertIn("100,50 RON", text)
        self.assertIn("Регистрация открыта", text)
        self.assertIn("Участников: 2", text)
        self.assertIn("&lt;Санта &amp; друзья&gt;", text)

    async def test_mygames_group_command_never_discloses_games(self):
        self.event(completed=True)
        await bot.my_games(self.update(private=False), self.context)
        self.assertEqual(self.reply.call_args.args[0], "Открой /mygames в личном чате с ботом.")

    async def test_private_callbacks_are_rejected_in_groups(self):
        event = self.event(completed=True)
        for data in ("games:0", "private_menu", "private_help", f"game:{event['id']}", f"recipient:{event['id']}"):
            with self.subTest(data=data):
                update = self.update(data=data, private=False)
                await bot.private_callback(update, self.context)
                update.callback_query.answer.assert_awaited_once()
        self.reply.assert_not_awaited()

    async def test_menu_callbacks_show_games_help_and_main_menu(self):
        await bot.private_callback(self.update(data="games:0"), self.context)
        self.assertIn("Мои игры", self.reply.call_args.args[0])
        await bot.private_callback(self.update(data="private_help"), self.context)
        self.assertIn("/mygames", self.reply.call_args.args[0])
        await bot.private_callback(self.update(data="private_menu"), self.context)
        self.assertEqual([b.callback_data for b in self.buttons()], ["games:0", "private_help"])

    async def test_open_event_has_no_recipient_action(self):
        event = self.event()
        await bot.private_callback(self.update(data=f"game:{event['id']}"), self.context)
        self.assertIn("ещё не завершена", self.reply.call_args.args[0])
        self.assertFalse(any(b.callback_data.startswith("recipient:") for b in self.buttons()))
        self.reply.reset_mock()
        await bot.private_callback(self.update(data=f"recipient:{event['id']}"), self.context)
        self.reply.assert_not_awaited()

    async def test_cancelled_game_stays_in_history_without_assignment_action(self):
        event = self.event()
        self.store.cancel_event(event["id"], 10)
        await bot.my_games(self.update(), self.context)
        self.assertIn("Игра отменена", self.reply.call_args.args[0])
        await bot.private_callback(self.update(data=f"game:{event['id']}"), self.context)
        self.assertIn("Назначений нет", self.reply.call_args.args[0])
        self.assertFalse(any(b.callback_data.startswith("recipient:") for b in self.buttons()))
        self.reply.reset_mock()
        await bot.private_callback(self.update(data=f"recipient:{event['id']}"), self.context)
        self.reply.assert_not_awaited()

    async def test_completed_card_offers_recipient_without_disclosing_it(self):
        event = self.event(completed=True)
        await bot.private_callback(self.update(data=f"game:{event['id']}"), self.context)
        self.assertIn("Жеребьёвка завершена", self.reply.call_args.args[0])
        self.assertNotIn("tg://user", self.reply.call_args.args[0])
        self.assertIn(f"recipient:{event['id']}", [b.callback_data for b in self.buttons()])

    async def test_recipient_is_own_saved_pair_and_read_does_not_change_draw(self):
        event = self.event(participants=(20, 21, 22), completed=True)
        self.store.set_notification_status(event["id"], 20, "failed")
        before = [dict(r) for r in self.store.assignments(event["id"])]
        with patch.object(self.store, "complete_draw", side_effect=AssertionError("No new draw")):
            for _ in range(2):
                await bot.private_callback(self.update(data=f"recipient:{event['id']}"), self.context)
        text = self.reply.call_args.args[0]
        self.assertIn("&lt;Имя 21&gt;", text)
        self.assertIn("tg://user?id=21", text)
        self.assertNotIn("tg://user?id=22", text)
        self.assertEqual([dict(r) for r in self.store.assignments(event["id"])], before)
        self.context.bot.send_message.assert_not_awaited()

    async def test_organizer_and_nonparticipant_cannot_read_private_event_or_pair(self):
        event = self.event(completed=True)
        for user_id in (10, 999):
            for action in ("game", "recipient"):
                with self.subTest(user_id=user_id, action=action):
                    update = self.update(user_id=user_id, data=f"{action}:{event['id']}")
                    await bot.private_callback(update, self.context)
                    update.callback_query.answer.assert_awaited_once()
        self.reply.assert_not_awaited()

    async def test_participating_organizer_sees_only_own_pair(self):
        event = self.event(owner=20, participants=(20, 21, 22), completed=True)
        await bot.private_callback(self.update(data=f"recipient:{event['id']}"), self.context)
        self.assertIn("tg://user?id=21", self.reply.call_args.args[0])
        self.assertNotIn("tg://user?id=22", self.reply.call_args.args[0])

    async def test_invalid_or_forged_callback_never_selects_foreign_giver(self):
        event = self.event(completed=True)
        for data in (f"recipient:{event['id']}:21", "recipient:-1", "recipient:" + "9" * 30, "game:missing", "unknown:1", "recipient:999"):
            with self.subTest(data=data):
                await bot.private_callback(self.update(data=data), self.context)
        self.reply.assert_not_awaited()

    async def test_same_named_games_have_independent_saved_recipients(self):
        first = self.event(completed=True)
        second = self.event("second", participants=(20, 22), completed=True)
        await bot.private_callback(self.update(data=f"recipient:{first['id']}"), self.context)
        self.assertIn("tg://user?id=21", self.reply.call_args.args[0])
        await bot.private_callback(self.update(data=f"recipient:{second['id']}"), self.context)
        self.assertIn("tg://user?id=22", self.reply.call_args.args[0])
        self.assertNotIn("tg://user?id=21", self.reply.call_args.args[0])

    async def test_missing_assignment_is_explained_without_drawing(self):
        event = self.event()
        with self.store.connection:
            self.store.connection.execute("UPDATE events SET status = ? WHERE id = ?", (COMPLETED, event["id"]))
        with patch.object(self.store, "complete_draw", side_effect=AssertionError("No new draw")):
            await bot.private_callback(self.update(data=f"recipient:{event['id']}"), self.context)
        text = self.reply.call_args.args[0]
        self.assertIn("назначение не найдено", text)
        self.assertNotIn("tg://user", text)

    async def test_lists_and_saved_recipient_work_after_restart(self):
        event = self.event(completed=True)
        before = [dict(r) for r in self.store.assignments(event["id"])]
        self.store.close()
        self.store = Storage(self.path)
        self.context.bot_data = {"storage": self.store}
        await bot.my_games(self.update(), self.context)
        self.assertIn(f"game:{event['id']}", [b.callback_data for b in self.buttons()])
        await bot.private_callback(self.update(data=f"recipient:{event['id']}"), self.context)
        self.assertIn("tg://user?id=21", self.reply.call_args.args[0])
        self.assertEqual([dict(r) for r in self.store.assignments(event["id"])], before)

    async def test_help_command_works_in_private_and_group(self):
        for private in (True, False):
            with self.subTest(private=private):
                await bot.help_command(self.update(private=private), self.context)
                text = self.reply.call_args.args[0]
                for command in ("/start", "/mygames", "/new", "/cancel", "/help"):
                    self.assertIn(command, text)
                self.assertNotIn("tg://user", text)
                self.assertEqual(bool(self.buttons()), private)

    async def test_pagination_keeps_messages_bounded_and_handles_stale_pages(self):
        for index in range(7):
            self.store.create_event(-100, 10, "'" * 100, 100, str(index))
            event = self.store.get_event_by_creation_key(str(index))
            self.store.join_event(event["id"], 20, "Анна")
        await bot.my_games(self.update(), self.context)
        self.assertLess(len(self.reply.call_args.args[0]), 4096)
        self.assertEqual(len([b for b in self.buttons() if b.callback_data.startswith("game:")]), 5)
        self.assertIn("games:5", [b.callback_data for b in self.buttons()])
        await bot.private_callback(self.update(data="games:5"), self.context)
        self.assertEqual(len([b for b in self.buttons() if b.callback_data.startswith("game:")]), 2)
        self.assertIn("games:0", [b.callback_data for b in self.buttons()])
        await bot.private_callback(self.update(data="games:100"), self.context)
        self.assertIn("На этой странице игр нет", self.reply.call_args.args[0])

    async def test_notification_includes_private_recipient_button(self):
        event = self.event(completed=True)
        await bot.send_saved_notifications(self.context, self.store.get_event(event["id"]))
        markup = self.context.bot.send_message.call_args.kwargs["reply_markup"]
        self.assertEqual(markup.inline_keyboard[0][0].callback_data, f"recipient:{event['id']}")


if __name__ == "__main__":
    unittest.main()
