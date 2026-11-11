"""SS-5.5 private setup and safe standalone publication; Telegram is mocked."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

try:
    import telegram
except ModuleNotFoundError:
    raise unittest.SkipTest("python-telegram-bot is not installed")

from telegram.error import Forbidden, NetworkError
import bot
import messages
import test_handlers as fixtures
from storage import Storage


class PrivateSetupTests(unittest.IsolatedAsyncioTestCase):
    message = fixtures.HandlerTests.message
    update = fixtures.HandlerTests.update
    setup_update = fixtures.HandlerTests.setup_update

    def setUp(self):
        fixtures.HandlerTests.setUp(self)
        self.store.close()
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "santa.sqlite3"
        self.store = Storage(self.path)
        self.context.bot_data = {"storage": self.store}

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    async def invite(self, group=-100, owner=10):
        await bot.new_event(self.update(chat_id=group, user_id=owner), self.context)
        return bot.drafts(self.context).get((group, owner))

    async def open(self, draft, user_id=None):
        user_id = draft["owner_id"] if user_id is None else user_id
        self.context.args = [f"setup_{draft['key']}"]
        await bot.start(self.update(user_id=user_id, chat_id=user_id, chat_type="private"), self.context)

    async def ready(self, group=-100, owner=10):
        draft = await self.invite(group, owner)
        await self.open(draft)
        await bot.creation_input(self.setup_update(draft, "  Secret Santa 2027  "), self.context)
        await bot.creation_input(self.setup_update(draft, "70,50"), self.context)
        return draft

    def event_count(self):
        return self.store.connection.execute("SELECT count(*) FROM events").fetchone()[0]

    async def test_group_new_exact_invitation_and_safe_link_only(self):
        draft = await self.invite()
        self.assertEqual(self.reply.call_args.args[0], messages.SETUP_INVITE)
        button = self.reply.call_args.kwargs["reply_markup"].inline_keyboard[0][0]
        self.assertEqual(button.text, "⚙️ Настроить игру")
        self.assertEqual(button.url, f"https://t.me/TestSantaBot?start=setup_{draft['key']}")
        self.assertNotIn("-100", button.url)
        self.assertEqual(len(draft["key"]), 32)
        self.assertEqual(self.reply.await_count, 1)
        self.assertEqual(self.event_count(), 0)

    async def test_private_start_activates_actual_user_and_starts_title_step(self):
        draft = await self.invite()
        await self.open(draft)
        self.assertTrue(self.store.private_active(10))
        self.assertEqual(self.reply.call_args.args[0], "🎄 Как назовём нашу игру?\n\nНапиши название, которое увидят все участники.")
        self.assertEqual(draft["step"], "title")

    async def test_manual_input_validation_and_actual_confirmation_values(self):
        draft = await self.invite()
        await self.open(draft)
        await bot.creation_input(self.setup_update(draft, " " * 3), self.context)
        self.assertEqual(draft["step"], "title")
        await bot.creation_input(self.setup_update(draft, "Ёлки & подарки"), self.context)
        self.assertEqual(self.reply.call_args.args[0], messages.SETUP_BUDGET)
        for invalid in ("0", "-5", "100000.01", "1.234", "abc"):
            await bot.creation_input(self.setup_update(draft, invalid), self.context)
            self.assertEqual(draft["step"], "budget")
        await bot.creation_input(self.setup_update(draft, "91.25"), self.context)
        self.assertIn("📝 Название: Ёлки & подарки", self.reply.call_args.args[0])
        self.assertIn("💰 Бюджет: 91,25 RON", self.reply.call_args.args[0])
        self.assertNotIn("70 RON", self.reply.call_args.args[0])

    async def test_edit_title_and_budget_separately_and_reject_stale_confirmation(self):
        draft = await self.ready()
        old = self.setup_update(draft, action="create")
        await bot.creation_callback(self.setup_update(draft, action="edit_setup"), self.context)
        await bot.creation_callback(self.setup_update(draft, action="edit_title"), self.context)
        await bot.creation_input(self.setup_update(draft, "Новое название"), self.context)
        self.assertEqual(draft["budget"], 7050)
        await bot.creation_callback(old, self.context)
        self.assertEqual(self.event_count(), 0)
        await bot.creation_callback(self.setup_update(draft, action="edit_setup"), self.context)
        await bot.creation_callback(self.setup_update(draft, action="edit_budget"), self.context)
        await bot.creation_input(self.setup_update(draft, "95"), self.context)
        self.assertEqual(draft["title"], "Новое название")
        self.assertEqual(draft["budget"], 9500)
        await bot.creation_callback(self.setup_update(draft, action="create"), self.context)
        event = self.store.get_event_by_creation_key(draft["key"])
        self.assertEqual(event["title"], "Новое название")
        self.assertEqual(event["budget"], 9500)

    async def test_correct_group_and_standalone_announcement_without_reply_metadata(self):
        draft = await self.ready(group=-555)
        await bot.creation_callback(self.setup_update(draft, action="create"), self.context)
        kwargs = self.context.bot.send_message.call_args.kwargs
        self.assertEqual(kwargs["chat_id"], -555)
        self.assertNotIn("reply_to_message_id", kwargs)
        self.assertNotIn("reply_parameters", kwargs)
        self.assertTrue(kwargs["text"].startswith(messages.GROUP_INTRO))
        self.assertIn("Secret Santa 2027", kwargs["text"])
        self.assertIn("70,50 RON", kwargs["text"])
        event = self.store.get_event_by_creation_key(draft["key"])
        self.assertEqual(event["message_id"], 700)
        self.assertEqual(self.store.publication_state(event["id"]), "published")
        self.assertEqual(self.reply.call_args.args[0], messages.SETUP_SUCCESS)

    async def test_another_user_cannot_open_or_confirm_session(self):
        draft = await self.ready()
        await self.open(draft, user_id=20)
        self.assertIn("недоступна", self.reply.call_args.args[0])
        stranger = self.update(user_id=20, chat_id=20, chat_type="private", data=f"create:{draft['key']}")
        await bot.creation_callback(stranger, self.context)
        self.assertEqual(self.event_count(), 0)
        self.context.bot.send_message.assert_not_awaited()
        self.assertEqual(draft["owner_id"], 10)

    async def test_membership_required_to_start_and_revalidated_before_publication(self):
        self.context.bot.get_chat_member.return_value = SimpleNamespace(status="left")
        self.assertIsNone(await self.invite())
        self.context.bot.get_chat_member.return_value = SimpleNamespace(status="member")
        draft = await self.ready()
        self.context.bot.get_chat_member.return_value = SimpleNamespace(status="kicked")
        await bot.creation_callback(self.setup_update(draft, action="create"), self.context)
        self.assertEqual(self.event_count(), 0)
        self.context.bot.send_message.assert_not_awaited()

    async def test_membership_lookup_failure_fails_closed(self):
        self.context.bot.get_chat_member.side_effect = NetworkError("unavailable")
        self.assertIsNone(await self.invite())
        self.assertEqual(self.event_count(), 0)

    async def test_anonymous_and_bot_commands_are_rejected(self):
        anonymous = self.update()
        anonymous.message.sender_chat = SimpleNamespace(id=-100)
        await bot.new_event(anonymous, self.context)
        automated = self.update()
        automated.effective_user.is_bot = True
        await bot.new_event(automated, self.context)
        self.assertEqual(bot.drafts(self.context), {})

    async def test_expired_start_and_confirmation_do_not_create_game(self):
        draft = await self.ready()
        with patch("bot.monotonic", return_value=draft["expires_at"] + 1):
            await bot.creation_callback(self.setup_update(draft, action="create"), self.context)
            await self.open(draft)
        self.assertIn("устарела", self.reply.call_args.args[0])
        self.assertEqual(self.event_count(), 0)

    async def test_cancel_button_and_private_command_create_no_game(self):
        first = await self.ready()
        await bot.creation_callback(self.setup_update(first, action="discard"), self.context)
        second = await self.ready(group=-200)
        await bot.cancel_creation(self.setup_update(second), self.context)
        self.assertEqual(bot.drafts(self.context), {})
        self.assertEqual(self.event_count(), 0)

    async def test_group_inputs_are_not_wizard_inputs(self):
        draft = await self.invite()
        await self.open(draft)
        await bot.creation_input(self.update(text="Групповое название"), self.context)
        self.assertEqual(draft["step"], "title")
        self.assertNotIn("title", draft)

    async def test_two_groups_switch_without_cross_publishing(self):
        first = await self.ready(group=-100)
        second = await self.ready(group=-200)
        await self.open(first)
        await bot.creation_callback(self.setup_update(first, action="create"), self.context)
        self.assertEqual(self.context.bot.send_message.call_args.kwargs["chat_id"], -100)
        await self.open(second)
        await bot.creation_callback(self.setup_update(second, action="create"), self.context)
        self.assertEqual(self.context.bot.send_message.call_args.kwargs["chat_id"], -200)
        self.assertEqual(self.event_count(), 2)

    async def test_repeat_confirmation_and_restart_never_resend_announcement(self):
        draft = await self.ready()
        confirm = self.setup_update(draft, action="create")
        await bot.creation_callback(confirm, self.context)
        self.store.close()
        self.store = Storage(self.path)
        self.context.bot_data = {"storage": self.store}
        await bot.creation_callback(confirm, self.context)
        self.assertEqual(self.event_count(), 1)
        self.context.bot.send_message.assert_awaited_once()

    async def test_definite_publication_failure_reports_no_success_and_can_retry(self):
        draft = await self.ready()
        self.context.bot.send_message.side_effect = Forbidden("no send permission")
        confirm = self.setup_update(draft, action="create")
        await bot.creation_callback(confirm, self.context)
        event = self.store.get_event_by_creation_key(draft["key"])
        self.assertIsNone(event["message_id"])
        self.assertEqual(self.store.publication_state(event["id"]), "failed")
        self.assertNotIn(messages.SETUP_SUCCESS, [call.args[0] for call in self.reply.call_args_list])
        self.context.bot.send_message.side_effect = None
        await bot.creation_callback(confirm, self.context)
        self.assertEqual(self.event_count(), 1)
        self.assertEqual(self.store.get_event(event["id"])["message_id"], 700)

    async def test_uncertain_publication_is_not_resent_even_after_restart(self):
        draft = await self.ready()
        self.context.bot.send_message.side_effect = NetworkError("unknown delivery outcome")
        confirm = self.setup_update(draft, action="create")
        await bot.creation_callback(confirm, self.context)
        await bot.creation_callback(confirm, self.context)
        self.store.close()
        self.store = Storage(self.path)
        self.context.bot_data["storage"] = self.store
        await bot.creation_callback(confirm, self.context)
        self.context.bot.send_message.assert_awaited_once()
        self.assertEqual(self.event_count(), 1)
        self.assertNotIn(messages.SETUP_SUCCESS, [call.args[0] for call in self.reply.call_args_list])

    async def test_concurrent_confirmation_claims_one_publication(self):
        draft = await self.ready()
        started, release = asyncio.Event(), asyncio.Event()
        async def send(**kwargs):
            started.set()
            await release.wait()
            return self.message(900)
        self.context.bot.send_message.side_effect = send
        confirm = self.setup_update(draft, action="create")
        first = asyncio.create_task(bot.creation_callback(confirm, self.context))
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            await bot.creation_callback(confirm, self.context)
        finally:
            release.set()
            await first
        self.context.bot.send_message.assert_awaited_once()
        self.assertEqual(self.event_count(), 1)

    async def test_unfinished_setup_is_discarded_after_restart(self):
        draft = await self.ready()
        self.context.bot_data = {"storage": self.store}
        await self.open(draft)
        self.assertIn("устарела", self.reply.call_args.args[0])
        self.assertEqual(self.event_count(), 0)


    async def test_database_failure_after_send_does_not_report_success_or_resend(self):
        draft = await self.ready()
        self.store.connection.executescript("""
            CREATE TRIGGER fail_publication BEFORE UPDATE OF state ON event_publications
            WHEN NEW.state='published'
            BEGIN SELECT RAISE(ABORT, 'injected save failure'); END;
        """)
        confirm = self.setup_update(draft, action="create")
        await bot.creation_callback(confirm, self.context)
        event = self.store.get_event_by_creation_key(draft["key"])
        self.assertIsNone(event["message_id"])
        self.assertEqual(self.store.publication_state(event["id"]), "sending")
        self.assertNotIn(messages.SETUP_SUCCESS, [call.args[0] for call in self.reply.call_args_list])
        await bot.creation_callback(confirm, self.context)
        self.context.bot.send_message.assert_awaited_once()

    async def test_published_id_is_used_by_registration_and_activation(self):
        draft = await self.ready()
        await bot.creation_callback(self.setup_update(draft, action="create"), self.context)
        event = self.store.get_event_by_creation_key(draft["key"])
        await bot.join_callback(self.update(user_id=20, data=f"join:{event['id']}"), self.context)
        self.assertEqual(self.context.bot.edit_message_text.call_args.kwargs["message_id"], 700)
        self.context.args = [f"activate_{event['id']}"]
        await bot.start(self.update(user_id=20, chat_id=20, chat_type="private"), self.context)
        self.assertEqual(self.context.bot.edit_message_text.call_args.kwargs["message_id"], 700)
        self.assertIn("📩 Связь с ботом: 1/1", self.context.bot.edit_message_text.call_args.kwargs["text"])

    async def test_cancel_after_definite_send_failure_closes_unpublished_event(self):
        draft = await self.ready()
        self.context.bot.send_message.side_effect = Forbidden("cannot send")
        await bot.creation_callback(self.setup_update(draft, action="create"), self.context)
        await bot.creation_callback(self.setup_update(draft, action="discard"), self.context)
        event = self.store.get_event_by_creation_key(draft["key"])
        self.assertEqual(event["status"], "cancelled")
        self.assertIsNone(event["message_id"])
        self.assertEqual(bot.drafts(self.context), {})


if __name__ == "__main__":
    unittest.main()
