"""SS-5 templates, navigation, and privacy. No real Telegram requests."""
import unittest
from unittest.mock import AsyncMock, patch

try:
    import telegram
except ModuleNotFoundError:
    raise unittest.SkipTest("python-telegram-bot is not installed")

from telegram.error import BadRequest, Forbidden
import bot
import messages
import test_private_menu as fixtures
from storage import Storage


class FestiveUXTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixtures.PrivateMenuTests.setUp(self)
        self.edit = AsyncMock()
        self.context.bot.edit_message_text = AsyncMock()

    tearDown = fixtures.PrivateMenuTests.tearDown
    event = fixtures.PrivateMenuTests.event

    def update(self, user_id=20, data=None, private=True):
        update = fixtures.PrivateMenuTests.update(self, user_id, data, private)
        update.effective_user.full_name = f"<Имя {user_id}>"
        update.effective_user.username = None
        update.message.edit_text = self.edit
        update.message.message_id = 123
        if update.callback_query:
            update.callback_query.edit_message_reply_markup = AsyncMock()
        return update

    def buttons(self):
        call = self.edit.call_args or self.reply.call_args
        markup = call.kwargs.get("reply_markup")
        return [b for row in markup.inline_keyboard for b in row] if markup else []

    async def test_exact_intro_and_completion_templates(self):
        self.assertEqual(messages.GROUP_INTRO,
            "🎄 Санта объявляет сбор!\n\n"
            "Эльфы уже готовят списки, подарки сами себя не купят, а Новый год ждать не будет! 🎁\n\n"
            "Присоединяйся к Тайному Санте - пусть начнётся праздничная интрига!")
        self.assertEqual(messages.DRAW_COMPLETED,
            "🎲 Жеребьёвка завершена!\n\n🧝 Эльфы распределили секретные миссии!\n\n"
            "🎁 Проверь личный чат с ботом - там тебя ждёт имя счастливчика.\n\n"
            "🤫 И помни: настоящий Тайный Санта умеет хранить секреты!")

    async def test_group_intro_stable_and_registration_edits_one_card(self):
        event = self.event(participants=())
        self.store.set_message_id(event["id"], 123)
        event = self.store.get_event(event["id"])
        self.store.activate_private(20)
        await bot.refresh_event(self.context, event)
        await bot.join_callback(self.update(data=f"join:{event['id']}", private=False), self.context)
        for call in self.context.bot.edit_message_text.call_args_list:
            self.assertEqual(call.kwargs["message_id"], 123)
            self.assertTrue(call.kwargs["text"].startswith(messages.GROUP_INTRO + "\n\n"))
            self.assertEqual(call.kwargs["text"].count(messages.GROUP_INTRO), 1)
        self.assertIn("Участников: 1", self.context.bot.edit_message_text.call_args.kwargs["text"])
        self.reply.assert_not_awaited()

    async def test_exact_registration_notice_toast_and_deep_link(self):
        event = self.event(participants=())
        update = self.update(data=f"join:{event['id']}", private=False)
        await bot.join_callback(update, self.context)
        update.callback_query.answer.assert_awaited_once_with("🎉 Ты в деле! Эльфы внесли тебя в список Санты! 🧝")
        expected = ("🎉 Ты в деле!\n"
                    "🧝 Эльфы внесли тебя в секретный список Санты. Отступать поздно - Санта всё видел! 👀\n"
                    "🎁 Теперь жди жеребьёвки и своей секретной миссии!")
        self.assertTrue(self.reply.call_args.args[0].startswith(expected))
        button = self.buttons()[0]
        self.assertEqual(button.text, "📩 Открыть бота")
        self.assertEqual(button.url, f"https://t.me/TestSantaBot?start=activate_{event['id']}")
        self.assertEqual(self.reply.await_count, 1)  # Adapted existing notice, no extra post.
        repeat = self.update(data=f"join:{event['id']}", private=False)
        await bot.join_callback(repeat, self.context)
        repeat.callback_query.answer.assert_awaited_once_with("✅ Ты уже участвуешь в этой игре!")
        self.assertEqual(self.reply.await_count, 1)
        self.assertEqual(len(self.store.participants(event["id"])), 1)

    async def test_active_registration_has_no_start_prompt(self):
        event = self.event(participants=())
        self.store.activate_private(20)
        await bot.join_callback(self.update(data=f"join:{event['id']}", private=False), self.context)
        self.reply.assert_not_awaited()

    async def test_exact_main_menu_and_three_distinct_buttons(self):
        await bot.start(self.update(), self.context)
        self.assertEqual(self.reply.call_args.args[0],
            "🎅 Добро пожаловать в штаб Санты!\n\nЭльфы на связи, подарки под контролем. 🎄\n\nЧто будем делать?")
        self.assertEqual([b.text for b in self.buttons()], ["🎁 Мои игры", "🎯 Кому я дарю?", "❓ Как это работает"])
        self.assertEqual(len({b.callback_data for b in self.buttons()}), 3)

    async def test_three_menu_actions_work_by_editing_same_message(self):
        self.event()
        for data in ("games:0", "gifts:0", "private_help", "private_menu"):
            await bot.private_callback(self.update(data=data), self.context)
        self.assertEqual(self.edit.await_count, 4)
        self.reply.assert_not_awaited()

    async def test_gifts_no_joined_games_explains_group_registration(self):
        await bot.private_callback(self.update(data="gifts:0"), self.context)
        self.assertIn("«🎁 Присоединиться»", self.edit.call_args.args[0])
        self.assertIn("в группе", self.edit.call_args.args[0])

    async def test_gifts_no_completed_assignment_uses_exact_waiting_copy(self):
        self.event()
        await bot.private_callback(self.update(data="gifts:0"), self.context)
        self.assertEqual(self.edit.call_args.args[0], "⏳ Санта ещё не провёл жеребьёвку. Немного терпения!")

    async def test_gifts_one_completed_assignment_displays_directly(self):
        event = self.event(completed=True)
        before = [dict(row) for row in self.store.assignments(event["id"])]
        with patch.object(self.store, "complete_draw", side_effect=AssertionError("No draw on viewing")):
            await bot.private_callback(self.update(data="gifts:0"), self.context)
        self.assertIn("tg://user?id=21", self.edit.call_args.args[0])
        self.assertNotIn("Выбери игру", self.edit.call_args.args[0])
        self.assertEqual([dict(row) for row in self.store.assignments(event["id"])], before)

    async def test_exact_assignment_template_safe_values_and_same_dm_presentation(self):
        event = self.event(completed=True)
        await bot.send_saved_notifications(self.context, self.store.get_event(event["id"]))
        dm = next(call.args[1] for call in self.context.bot.send_message.call_args_list if call.args[0] == 20)
        await bot.private_callback(self.update(data=f"recipient:{event['id']}"), self.context)
        self.assertEqual(dm, self.edit.call_args.args[0])
        self.assertEqual(dm,
            "🧝 Тебе доставлено секретное задание!\n\n"
            "🎁 Ты даришь подарок <b>&lt;Имя 21&gt;</b>!\n\n"
            "💰 Бюджет: 100,50 RON\n\n"
            "🤫 Не выдавай себя до времени Х. Не будь как Гринч!\n\n"
            "<a href='tg://user?id=21'>Профиль получателя</a>\n"
            f"Игра: <b>&lt;Санта &amp; друзья&gt;</b> · №{event['id']}")

    async def test_gifts_multiple_assignments_selector_and_back_preserve_page(self):
        events = [self.event(str(i), completed=True) for i in range(7)]
        await bot.private_callback(self.update(data="gifts:0"), self.context)
        self.assertIn("Выбери игру", self.edit.call_args.args[0])
        self.assertNotIn("tg://user", self.edit.call_args.args[0])
        self.assertEqual(len([b for b in self.buttons() if b.callback_data.startswith("gift:")]), 5)
        self.assertIn("gifts:5", [b.callback_data for b in self.buttons()])
        await bot.private_callback(self.update(data="gifts:5"), self.context)
        choices = [b for b in self.buttons() if b.callback_data.startswith("gift:")]
        self.assertEqual(len(choices), 2)
        await bot.private_callback(self.update(data=choices[0].callback_data), self.context)
        self.assertIn("tg://user?id=21", self.edit.call_args.args[0])
        self.assertIn("gifts:5", [b.callback_data for b in self.buttons()])
        await bot.private_callback(self.update(data="gifts:5"), self.context)
        self.assertEqual(len([b for b in self.buttons() if b.callback_data.startswith("gift:")]), 2)
        self.assertEqual(len(events), 7)

    async def test_mygames_detail_and_assignment_back_keep_original_page(self):
        for i in range(7):
            self.event(str(i), completed=True)
        await bot.private_callback(self.update(data="games:5"), self.context)
        choice = next(b.callback_data for b in self.buttons() if b.callback_data.startswith("game:"))
        await bot.private_callback(self.update(data=choice), self.context)
        self.assertIn("games:5", [b.callback_data for b in self.buttons()])
        view = next(b.callback_data for b in self.buttons() if b.callback_data.startswith("view:"))
        await bot.private_callback(self.update(data=view), self.context)
        self.assertIn(choice, [b.callback_data for b in self.buttons()])
        self.assertIn("🏠 Главное меню", [b.text for b in self.buttons()])

    async def test_selector_excludes_organizer_only_and_missing_assignments(self):
        owned = self.event(participants=(21, 22), owner=20, completed=True)
        missing = self.event("missing")
        with self.store.connection:
            self.store.connection.execute("UPDATE events SET status='draw_completed' WHERE id=?", (missing["id"],))
        self.assertEqual(self.store.completed_assignment_games(20), [])
        await bot.private_callback(self.update(data="gifts:0"), self.context)
        self.assertEqual(self.edit.call_args.args[0], messages.WAITING)
        self.assertNotIn(f"№{owned['id']}", self.edit.call_args.args[0])
        await bot.private_callback(self.update(data=f"recipient:{missing['id']}"), self.context)
        self.assertIn("назначение не найдено", self.edit.call_args.args[0])

    async def test_new_actions_never_reveal_another_users_assignment(self):
        event = self.event(completed=True)
        for action in (f"gift:{event['id']}:0", f"view:{event['id']}:0"):
            await bot.private_callback(self.update(user_id=10, data=action), self.context)
            await bot.private_callback(self.update(user_id=999, data=action), self.context)
            await bot.private_callback(self.update(data=action, private=False), self.context)
        self.edit.assert_not_awaited()
        self.reply.assert_not_awaited()

    async def test_exact_closed_too_few_cancelled_and_waiting_responses(self):
        event = self.event(participants=(20,))
        draw = self.update(user_id=10, data=f"draw:{event['id']}", private=False)
        await bot.draw_callback(draw, self.context)
        draw.callback_query.answer.assert_awaited_once_with("🎅 Для жеребьёвки нужны хотя бы 2 участника.", show_alert=True)
        await bot.private_callback(self.update(data=f"game:{event['id']}"), self.context)
        self.assertIn(messages.WAITING, self.edit.call_args.args[0])
        self.store.cancel_event(event["id"], 10)
        join = self.update(data=f"join:{event['id']}", private=False)
        await bot.join_callback(join, self.context)
        join.callback_query.answer.assert_awaited_once_with("🔒 Регистрация закрыта. Санта уже составляет списки!", show_alert=True)
        await bot.private_callback(self.update(data=f"game:{event['id']}"), self.context)
        self.assertIn("🚫 Эта игра отменена организатором.", self.edit.call_args.args[0])

    async def test_completed_card_and_retries_never_post_new_celebration(self):
        event = self.event()
        await bot.draw_callback(self.update(user_id=10, data=f"draw:{event['id']}", private=False), self.context)
        self.assertNotIn(messages.GROUP_INTRO, self.reply.call_args.args[0])  # Confirmation isn't another announcement.
        token = self.context.bot_data["draw_confirmations"][event["id"]]["token"]
        self.reply.reset_mock()
        confirm = self.update(user_id=10, data=f"draw_yes:{event['id']}:{token}", private=False)
        await bot.draw_callback(confirm, self.context)
        completed_text = self.context.bot.edit_message_text.call_args.kwargs["text"]
        self.assertEqual(completed_text.count(messages.DRAW_COMPLETED), 1)
        self.assertTrue(completed_text.startswith(messages.GROUP_INTRO))
        await bot.retry_callback(self.update(user_id=10, data=f"retry:{event['id']}", private=False), self.context)
        self.store.close()
        self.store = Storage(self.path)
        self.context.bot_data = {"storage": self.store}
        await bot.retry_callback(self.update(user_id=10, data=f"retry:{event['id']}", private=False), self.context)
        self.assertEqual(self.context.bot.edit_message_text.call_args.kwargs["text"], completed_text)
        self.assertEqual(self.context.bot.send_message.await_count, 2)
        self.reply.assert_not_awaited()

    async def test_notification_statistics_are_organizer_only_callback_not_group_text(self):
        event = self.event()
        self.store.complete_draw(event["id"], 10, [20, 21])
        self.context.bot.send_message.side_effect = Forbidden("blocked")
        query = self.update(user_id=10, data=f"retry:{event['id']}", private=False)
        await bot.retry_callback(query, self.context)
        self.assertIn("ошибок: 2", query.callback_query.answer.call_args.args[0])
        self.assertTrue(query.callback_query.answer.call_args.kwargs["show_alert"])
        self.reply.assert_not_awaited()
        manage = self.update(user_id=10, data=f"manage:{event['id']}", private=False)
        await bot.management_callback(manage, self.context)
        self.assertIn("ошибок: 2", manage.callback_query.answer.call_args.args[0])
        self.assertNotIn("ошибок:", self.reply.call_args.args[0])
        self.assertNotIn("tg://user", self.reply.call_args.args[0])

    async def test_unchanged_inline_menu_does_not_post_a_new_message(self):
        self.edit.side_effect = BadRequest("Message is not modified")
        await bot.private_callback(self.update(data="private_menu"), self.context)
        self.reply.assert_not_awaited()

    async def test_new_navigation_and_saved_assignment_survive_restart(self):
        event = self.event(completed=True)
        self.store.close()
        self.store = Storage(self.path)
        self.context.bot_data = {"storage": self.store}
        for data in ("gifts:0", f"view:{event['id']}:0", f"recipient:{event['id']}"):
            await bot.private_callback(self.update(data=data), self.context)
            self.assertIn("tg://user?id=21", self.edit.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
