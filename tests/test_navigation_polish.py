"""SS-5.6 return links and registration copy; all Telegram access is mocked."""
import unittest
from types import SimpleNamespace

try:
    import telegram
except ModuleNotFoundError:
    raise unittest.SkipTest("python-telegram-bot is not installed")

from telegram.error import Forbidden
import bot
import messages
import test_private_setup as fixtures
from storage import Storage


class NavigationPolishTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.PrivateSetupTests.setUp
    tearDown = fixtures.PrivateSetupTests.tearDown
    message = fixtures.PrivateSetupTests.message
    update = fixtures.PrivateSetupTests.update
    setup_update = fixtures.PrivateSetupTests.setup_update
    invite = fixtures.PrivateSetupTests.invite
    open = fixtures.PrivateSetupTests.open
    ready = fixtures.PrivateSetupTests.ready

    def chat(self, chat_id=-1001234567890, kind="supergroup", username=None, invite=None):
        return SimpleNamespace(id=chat_id, type=kind, username=username, invite_link=invite)

    def return_button(self):
        markup = self.reply.call_args.kwargs.get("reply_markup")
        return markup.inline_keyboard[0][0] if markup else None

    async def test_publication_returns_direct_public_message_link(self):
        draft = await self.ready(group=-1001234567890)
        self.context.bot.get_chat.return_value = self.chat(username="Actual_Cluj_Group")
        await bot.creation_callback(self.setup_update(draft, action="create"), self.context)
        self.assertEqual(self.reply.call_args.args[0], messages.SETUP_SUCCESS)
        self.assertEqual(self.return_button().text, "↩️ Вернуться в группу")
        self.assertEqual(self.return_button().url, "https://t.me/Actual_Cluj_Group/700")
        self.context.bot.get_chat.assert_awaited_once_with(-1001234567890)
        sent = self.context.bot.send_message.call_args.kwargs
        self.assertEqual(sent["chat_id"], -1001234567890)
        self.assertNotIn("reply_to_message_id", sent)
        self.assertNotIn("reply_parameters", sent)

    async def test_publication_returns_direct_private_supergroup_message_link(self):
        draft = await self.ready(group=-1001234567890)
        self.context.bot.get_chat.return_value = self.chat()
        await bot.creation_callback(self.setup_update(draft, action="create"), self.context)
        self.assertEqual(self.return_button().url, "https://t.me/c/1234567890/700")
        self.assertEqual(self.store.get_event_by_creation_key(draft["key"])["message_id"], 700)

    async def test_basic_private_group_uses_real_existing_invite_only(self):
        draft = await self.ready(group=-12345)
        self.context.bot.get_chat.return_value = self.chat(-12345, "group", invite="https://t.me/+ActualInvite_123")
        await bot.creation_callback(self.setup_update(draft, action="create"), self.context)
        self.assertEqual(self.return_button().url, "https://t.me/+ActualInvite_123")

    async def test_missing_link_still_confirms_without_button(self):
        draft = await self.ready(group=-12345)
        self.context.bot.get_chat.return_value = self.chat(-12345, "group")
        await bot.creation_callback(self.setup_update(draft, action="create"), self.context)
        self.assertIsNone(self.return_button())
        self.assertEqual(self.reply.call_args.args[0], messages.SETUP_SUCCESS)
        self.assertEqual(self.store.publication_state(self.store.get_event_by_creation_key(draft["key"])["id"]), "published")

    async def test_metadata_lookup_failure_does_not_undo_publication(self):
        draft = await self.ready()
        self.context.bot.get_chat.side_effect = Forbidden("cannot inspect group")
        await bot.creation_callback(self.setup_update(draft, action="create"), self.context)
        self.assertIsNone(self.return_button())
        self.assertEqual(self.reply.call_args.args[0], messages.SETUP_SUCCESS)
        self.assertEqual(self.store.get_event_by_creation_key(draft["key"])["message_id"], 700)

    async def test_metadata_for_wrong_group_never_generates_button(self):
        draft = await self.ready(group=-1001234567890)
        self.context.bot.get_chat.return_value = self.chat(-1009999999999, username="Other_Group")
        await bot.creation_callback(self.setup_update(draft, action="create"), self.context)
        self.assertIsNone(self.return_button())

    async def test_repeat_confirmation_after_restart_keeps_link_without_republication(self):
        draft = await self.ready(group=-1001234567890)
        self.context.bot.get_chat.return_value = self.chat(username="Actual_Cluj_Group")
        confirm = self.setup_update(draft, action="create")
        await bot.creation_callback(confirm, self.context)
        self.store.close()
        self.store = Storage(self.path)
        self.context.bot_data = {"storage": self.store}
        await bot.creation_callback(confirm, self.context)
        self.assertEqual(self.return_button().url, "https://t.me/Actual_Cluj_Group/700")
        self.context.bot.send_message.assert_awaited_once()

    def test_supported_public_and_private_link_formats(self):
        self.assertEqual(bot.group_return_url(self.chat(username="Real_Group"), 42), "https://t.me/Real_Group/42")
        self.assertEqual(bot.group_return_url(self.chat(), 42), "https://t.me/c/1234567890/42")
        self.assertEqual(bot.group_return_url(self.chat(-54321, "group", invite="https://t.me/joinchat/Actual_Code"), 42), "https://t.me/joinchat/Actual_Code")
        self.assertEqual(bot.group_return_url(self.chat(username="Real_Group"), None), "https://t.me/Real_Group")

    def test_unsupported_group_ids_and_invalid_urls_are_not_fabricated(self):
        cases = [
            self.chat(-12345, "group"), self.chat(-10012345, "group"),
            self.chat(-12345, "supergroup"), self.chat(-100, "supergroup"),
            self.chat(-10012345, "channel", "A_Channel"),
            self.chat(-12345, "group", "bad/name"),
            self.chat(-12345, "group", invite="https://example.com/+BadInvite"),
            self.chat(-12345, "group", invite="https://t.me.evil.test/+BadInvite"),
            self.chat(-12345, "group", invite="https://t.me/+"),
            self.chat(-12345, "group", invite="https://[bad"),
        ]
        for chat in cases:
            with self.subTest(chat=chat):
                self.assertIsNone(bot.group_return_url(chat, 42))
        self.assertIsNone(bot.group_return_url(self.chat(), -1))

    def test_approved_activation_copy_and_deep_link_appear_once_without_start_instruction(self):
        event = self.store.create_event(-100, 10, "Наша игра", 7000, "copy")
        text = bot.render_event_text(event, [], "TestSantaBot")
        expected = ("📩 Секретная миссия ждёт тебя!\n\n"
                    "🧝 Чтобы эльфы смогли доставить тебе секретное задание, нажми «🎁 Присоединиться», а затем открой личный чат с ботом и активируй его.\n\n"
                    "🎄 Уже активировал бота? Тогда всё готово!")
        self.assertEqual(text.count(expected), 1)
        self.assertEqual(text.count("📩 Открыть бота"), 1)
        self.assertIn(f"https://t.me/TestSantaBot?start=activate_{event['id']}", text)
        self.assertNotIn("Start", text)
        self.assertNotIn("START", text)
        self.assertNotIn("/start", text)
        self.assertIn("📩 Связь с ботом: 0/0", text)

    async def test_warning_is_visible_before_join_and_activation_tracking_is_preserved(self):
        event = self.store.create_event(-100, 10, "Наша игра", 7000, "copy", 700)
        text = bot.render_event_text(event, [], "TestSantaBot")
        self.assertEqual(text.count("🎅 Вписался за Санту — теперь отвечай! 🎁 Обратных билетов у эльфов нет."), 1)
        self.assertNotIn("Регистрация необратима.", text)
        self.assertIn(f"join:{event['id']}", [b.callback_data for row in bot.event_keyboard(event).inline_keyboard for b in row])
        self.store.activate_private(20)
        self.reply.reset_mock()
        await bot.join_callback(self.update(user_id=20, data=f"join:{event['id']}"), self.context)
        self.reply.assert_not_awaited()  # Already activated: no new activation notice.
        rendered = self.context.bot.edit_message_text.call_args.kwargs["text"]
        self.assertIn("📩 Связь с ботом: 1/1", rendered)
        self.assertIn("1. ✅", rendered)
        self.assertIn(messages.REGISTRATION_WARNING, rendered)


if __name__ == "__main__":
    unittest.main()
