import os
import asyncio
import sqlite3
import re
from html import escape
from pathlib import Path
from uuid import uuid4
from time import monotonic
from urllib.parse import urlsplit

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, TelegramError, Forbidden, RetryAfter
from telegram.helpers import create_deep_linked_url
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters,
    ContextTypes,
)

from storage import Storage, OPEN, CANCELLED, COMPLETED, DrawError, ParticipantsChanged
from validation import validate_title, parse_budget, format_budget
from messages import (
    GROUP_INTRO, REGISTRATION_SUCCESS, REGISTRATION_REPEAT, REGISTRATION_NOTICE,
    DRAW_COMPLETED, MAIN_MENU, WAITING, CANCELLED_GAME, CLOSED, TOO_FEW, assignment_text,
    SETUP_INVITE, SETUP_TITLE, SETUP_BUDGET, SETUP_SUCCESS,
    ACTIVATION_COPY, REGISTRATION_WARNING,
)


def is_group(update):
    return update.effective_chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)


def db(context):
    return context.bot_data["storage"]


def drafts(context):
    return context.bot_data.setdefault("drafts", {})


SETUP_TTL = 30 * 60


def draft_key(update):
    return update.effective_chat.id, update.effective_user.id


def setup_session(context, user_id, token=None):
    if token is None:
        token = context.bot_data.setdefault("active_setups", {}).get(user_id)
    for key, draft in list(drafts(context).items()):
        if draft["key"] == token:
            if draft["expires_at"] <= monotonic():
                drafts(context).pop(key, None)
                return None
            return draft if draft["owner_id"] == user_id else None
    return None


async def allowed_organizer(context, chat_id, user_id):
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
    except TelegramError:
        return False
    return member.status in {"creator", "administrator", "member"} or (
        member.status == "restricted" and member.is_member
    )


def draft_keyboard(draft, confirm=False):
    token = draft["key"]
    rows = []
    if confirm:
        rows.extend([
            [InlineKeyboardButton("🎁 Создать игру", callback_data=f"create:{token}")],
            [InlineKeyboardButton("✏️ Изменить", callback_data=f"edit_setup:{token}")],
        ])
    if draft["step"] == "edit":
        rows.extend([
            [InlineKeyboardButton("Изменить название", callback_data=f"edit_title:{token}")],
            [InlineKeyboardButton("Изменить бюджет", callback_data=f"edit_budget:{token}")],
        ])
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data=f"discard:{token}")])
    return InlineKeyboardMarkup(rows)


async def prompt_draft(message, draft):
    if draft["step"] in {"title", "edit_title"}:
        text = SETUP_TITLE
    elif draft["step"] in {"budget", "edit_budget"}:
        text = SETUP_BUDGET
    elif draft["step"] == "edit":
        text = "✏️ Что изменим?"
    else:
        text = (f"🎄 Проверим настройки игры!\n\n📝 Название: {draft['title']}\n\n"
                f"💰 Бюджет: {format_budget(draft['budget'])}")
    sent = await message.reply_text(text, reply_markup=draft_keyboard(draft, draft["step"] == "confirm"))
    draft["prompt_id"] = sent.message_id


async def start_setup(update, context, token):
    draft = setup_session(context, update.effective_user.id, token)
    if not draft:
        await update.message.reply_text("Ссылка настройки недоступна или устарела. Отправь /new в нужной группе.")
        return
    if not await allowed_organizer(context, draft["chat_id"], draft["owner_id"]):
        await update.message.reply_text("Не удалось подтвердить участие в исходной группе. Настройка недоступна.")
        return
    context.bot_data.setdefault("active_setups", {})[update.effective_user.id] = token
    await prompt_draft(update.message, draft)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == ChatType.PRIVATE:
        already_active = db(context).private_active(update.effective_user.id)
        db(context).activate_private(update.effective_user.id)
        # Every START can repair a previously failed edit; all games share the same SS-4 state.
        for event in db(context).participant_announcements(update.effective_user.id):
            await refresh_event(context, event)
        if context.args and context.args[0].startswith("setup_"):
            await start_setup(update, context, context.args[0][6:])
            return
        if context.args and context.args[0].startswith("activate_"):
            await update.message.reply_text(
                "✅ Личные сообщения уже активированы. Повторная активация не нужна."
                if already_active else "✅ Личные сообщения активированы. Регистрация в играх выполняется отдельно в группе."
            )
        await show_private_menu(update.message)


def private_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎁 Мои игры", callback_data="games:0")],
        [InlineKeyboardButton("🎯 Кому я дарю?", callback_data="gifts:0")],
        [InlineKeyboardButton("❓ Как это работает", callback_data="private_help")],
    ])


async def private_message(message, text, *, edit=False, **kwargs):
    if not edit:
        return await message.reply_text(text, **kwargs)
    try:
        return await message.edit_text(text, **kwargs)
    except BadRequest as error:
        if "message is not modified" not in str(error).lower():
            raise


async def show_private_menu(message, edit=False):
    await private_message(message, MAIN_MENU, edit=edit, reply_markup=private_menu_keyboard())


def private_event_text(event):
    statuses = {OPEN: "🟢 Регистрация открыта", COMPLETED: "🎲 Жеребьёвка завершена", CANCELLED: CANCELLED_GAME}
    return (f"🎅 <b>{escape(event['title'])}</b> · №{event['id']}\n"
            f"💰 Бюджет: {format_budget(event['budget'])}\n"
            f"{statuses.get(event['status'], 'Жеребьёвка ещё не завершена')}\n"
            f"👥 Участников: {event['participant_count']}")


async def show_my_games(message, context, user_id, offset=0, edit=False):
    page_size = 5
    events = db(context).participant_games(user_id, offset, page_size + 1)
    rows = []
    if not events:
        text = "🎁 Мои игры\n\nНа этой странице игр нет." if offset else (
            "🎁 Мои игры\n\nТы пока не участвуешь в играх. Нажми «🎁 Присоединиться» в группе. "
            "Если ты только организатор, управление игрой доступно через кнопку в группе."
        )
    else:
        text = "🎁 <b>Мои игры</b>\n\n" + "\n\n".join(private_event_text(e) for e in events[:page_size])
        rows.extend([
            [InlineKeyboardButton(f"{offset + i + 1}. {event['title'][:60]} · №{event['id']}", callback_data=f"game:{event['id']}:{offset}")]
            for i, event in enumerate(events[:page_size])
        ])
    navigation = []
    if offset:
        navigation.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"games:{max(0, offset - page_size)}"))
    if len(events) > page_size:
        navigation.append(InlineKeyboardButton("Далее →", callback_data=f"games:{offset + page_size}"))
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton("🏠 Главное меню", callback_data="private_menu")])
    await private_message(message, text, edit=edit, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))


async def my_games(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.message.reply_text("Открой /mygames в личном чате с ботом.")
        return
    await show_my_games(update.message, context, update.effective_user.id)


async def show_help(message, private, edit=False):
    text = (
        "ℹ️ Помощь\n\n"
        "В группе: /new или /new название — получить ссылку «⚙️ Настроить игру». "
        "Название и бюджет в RON вводятся в личном чате, там же доступны изменение и подтверждение. "
        "/cancel — отменить выбранное создание в личном чате или собственный черновик исходной группы.\n"
        "«Присоединиться» регистрирует в конкретной игре без возможности выхода. "
        "Сначала можно зарегистрироваться, а затем нажать «📩 Открыть бота» "
        "и Start («Запустить») в Telegram. Вручную вводить /start не нужно. "
        "Повторная активация для других игр не требуется. Организатор участвует только после отдельной регистрации.\n"
        "Организатор подтверждает жеребьёвку минимум для 2 участников. "
        "Отмена открытой игры и повтор неудачных уведомлений доступны через «Управление» в группе.\n\n"
        "В личном чате: /start — главное меню, /mygames — «Мои игры», /help — помощь. "
        "После жеребьёвки: «Мои игры» → игра → «Кому я дарю?». Видно только твоё сохранённое назначение. "
        "В список входят только игры, где ты зарегистрирован участником.\n"
        "Доставка Telegram не гарантируется; после сбоя возможно повторное сообщение с тем же назначением."
    )
    await private_message(message, text, edit=edit, reply_markup=private_menu_keyboard() if private else None)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == ChatType.PRIVATE or is_group(update):
        await show_help(update.message, update.effective_chat.type == ChatType.PRIVATE)


def private_navigation(back=None):
    rows = []
    if back:
        rows.append([InlineKeyboardButton("⬅️ Назад", callback_data=back)])
    rows.append([InlineKeyboardButton("🏠 Главное меню", callback_data="private_menu")])
    return rows


async def show_recipient(message, context, event, user_id, back, edit=True):
    recipient = db(context).saved_recipient(event["id"], user_id)
    text = assignment_text(event, recipient) if recipient else (
        private_event_text(event) + "\n\nСохранённое назначение не найдено. "
        "Обратись к организатору. Новая жеребьёвка не выполнялась; прежнее личное сообщение может содержать результат."
    )
    await private_message(message, text, edit=edit, parse_mode=ParseMode.HTML,
                          reply_markup=InlineKeyboardMarkup(private_navigation(back)), disable_web_page_preview=True)


async def show_gifts(message, context, user_id, offset=0):
    events = db(context).completed_assignment_games(user_id, offset, 6)
    if offset == 0 and len(events) == 1:
        await show_recipient(message, context, events[0], user_id, "private_menu")
        return
    rows = []
    if not events:
        if offset:
            text = "На этой странице завершённых игр нет."
        elif db(context).participant_games(user_id, limit=1):
            text = WAITING
        else:
            text = "Ты пока не участвуешь в играх. Нажми «🎁 Присоединиться» в объявлении игры в группе."
    else:
        text = "🎯 <b>Кому я дарю?</b>\nВыбери игру:\n\n" + "\n\n".join(private_event_text(e) for e in events[:5])
        rows = [[InlineKeyboardButton(f"{e['title'][:60]} · №{e['id']}", callback_data=f"gift:{e['id']}:{offset}")] for e in events[:5]]
    pages = []
    if offset:
        pages.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"gifts:{max(0, offset - 5)}"))
    if len(events) > 5:
        pages.append(InlineKeyboardButton("Далее →", callback_data=f"gifts:{offset + 5}"))
    if pages:
        rows.append(pages)
    rows.extend(private_navigation())
    await private_message(message, text, edit=True, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))


async def private_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if update.effective_chat.type != ChatType.PRIVATE:
        await query.answer("Открой меню в личном чате с ботом.", show_alert=True)
        return
    if query.data == "private_menu":
        await query.answer()
        await show_private_menu(query.message, edit=True)
        return
    if query.data == "private_help":
        await query.answer()
        await show_help(query.message, True, edit=True)
        return
    parts = query.data.split(":")
    action, numbers = parts[0], parts[1:]
    valid_shape = ((action in {"games", "gifts", "recipient"} and len(numbers) == 1)
                   or (action == "game" and len(numbers) in {1, 2})
                   or (action in {"gift", "view"} and len(numbers) == 2))
    if not valid_shape or any(not n.isascii() or not n.isdecimal() or len(n) > 18 for n in numbers):
        await query.answer("Кнопка недоступна. Открой /mygames заново.", show_alert=True)
        return
    number = int(numbers[0])
    offset = int(numbers[1]) if len(numbers) > 1 else 0
    user_id = query.from_user.id
    if action in {"games", "gifts"}:
        await query.answer()
        if action == "games":
            await show_my_games(query.message, context, user_id, number, edit=True)
        else:
            await show_gifts(query.message, context, user_id, number)
        return
    event = db(context).participant_event(number, user_id)
    if not event:
        await query.answer("Игра недоступна или ты не зарегистрирован участником.", show_alert=True)
        return
    if action in {"recipient", "view", "gift"}:
        if event["status"] != COMPLETED:
            await query.answer(CANCELLED_GAME if event["status"] == CANCELLED else WAITING, show_alert=True)
            return
        await query.answer()
        back = f"gifts:{offset}" if action == "gift" else f"game:{number}:{offset}"
        await show_recipient(query.message, context, event, user_id, back)
        return
    await query.answer()
    text = private_event_text(event)
    rows = private_navigation(f"games:{offset}")
    if event["status"] == COMPLETED:
        rows.insert(0, [InlineKeyboardButton("🎯 Кому я дарю?", callback_data=f"view:{number}:{offset}")])
    elif event["status"] == CANCELLED:
        text += "\n\n" + CANCELLED_GAME
    else:
        text += "\n\n" + WAITING
    await private_message(query.message, text, edit=True, parse_mode=ParseMode.HTML,
                          reply_markup=InlineKeyboardMarkup(rows), disable_web_page_preview=True)


async def new_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        await update.message.reply_text("Начни создание командой /new в нужной группе.")
        return
    user = update.effective_user
    if (getattr(user, "is_bot", False) or getattr(update.message, "sender_chat", None)
            or not await allowed_organizer(context, update.effective_chat.id, user.id)):
        await update.message.reply_text("Создание доступно участнику группы от своего имени. Не удалось подтвердить права.")
        return
    key = draft_key(update)
    draft = drafts(context).get(key)
    if not draft or draft["expires_at"] <= monotonic():
        draft = {"key": uuid4().hex, "step": "title", "chat_id": key[0], "owner_id": key[1],
                 "expires_at": monotonic() + SETUP_TTL}
        drafts(context)[key] = draft
    await update.message.reply_text(
        SETUP_INVITE,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
            "⚙️ Настроить игру", url=create_deep_linked_url(context.bot.username, f"setup_{draft['key']}")
        )]]),
    )


async def discard_setup(context, draft):
    event = db(context).get_event_by_creation_key(draft["key"])
    if event:
        state = db(context).publication_state(event["id"])
        if event["message_id"] is not None or state in {"sending", "uncertain"}:
            return False
        db(context).cancel_event(event["id"], draft["owner_id"])
    drafts(context).pop((draft["chat_id"], draft["owner_id"]), None)
    active = context.bot_data.setdefault("active_setups", {})
    if active.get(draft["owner_id"]) == draft["key"]:
        active.pop(draft["owner_id"], None)
    return True


async def cancel_creation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_group(update):
        draft = drafts(context).get(draft_key(update))
    elif update.effective_chat.type == ChatType.PRIVATE:
        draft = setup_session(context, update.effective_user.id)
    else:
        return
    if not draft:
        text = "Нет незавершённого создания."
    elif await discard_setup(context, draft):
        text = "Создание отменено."
    else:
        text = "Публикация уже выполнена или её исход неизвестен. Проверь исходную группу; повторная отправка заблокирована."
    await update.message.reply_text(text)


async def creation_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    draft = setup_session(context, update.effective_user.id)
    if not draft:
        return
    reply = update.message.reply_to_message
    if reply and reply.message_id != draft.get("prompt_id"):
        return
    try:
        if draft["step"] in {"title", "edit_title"}:
            editing = draft["step"] == "edit_title"
            draft["title"] = validate_title(update.message.text)
            draft["step"] = "confirm" if editing else "budget"
        elif draft["step"] in {"budget", "edit_budget"}:
            draft["budget"] = parse_budget(update.message.text)
            draft["step"] = "confirm"
        else:
            return
    except ValueError as error:
        await update.message.reply_text(str(error))
    await prompt_draft(update.message, draft)


def event_keyboard(event):
    eid = event["id"]
    rows = []
    if event["status"] == OPEN:
        rows.extend([
            [InlineKeyboardButton("🎁 Присоединиться", callback_data=f"join:{eid}")],
            [InlineKeyboardButton("🎲 Провести жеребьёвку", callback_data=f"draw:{eid}")],
        ])
    rows.append([InlineKeyboardButton("⚙️ Управление", callback_data=f"manage:{eid}")])
    return InlineKeyboardMarkup(rows)


def render_event_text(event, participants, username, announcement=True):
    active = [bool(p["private_active"]) if "private_active" in p.keys() else False for p in participants]
    status = {OPEN: "🟢 Регистрация открыта", CANCELLED: CANCELLED_GAME, COMPLETED: "Статус: жеребьёвка проведена"}[event["status"]]
    text = ((GROUP_INTRO + "\n\n" if announcement else "") + f"🎅 <b>{escape(event['title'])}</b>\n🎁 Бюджет: {format_budget(event['budget'])}\n"
            f"👥 Участников: {len(participants)}\n📩 Связь с ботом: {sum(active)}/{len(participants)}\n{status}\n\n"
            f"{ACTIVATION_COPY}\n\n"
            f"<a href='{escape(activation_url(username, event['id']))}'>📩 Открыть бота</a>\n\n"
            f"{REGISTRATION_WARNING}\n\n")
    if event["status"] == COMPLETED:
        text += DRAW_COMPLETED + "\n\n"
    text += "✅ — личный чат активирован\n⏳ — ожидает активации\n\n<b>Участники:</b>\n"
    if not participants:
        return text + "— пока никого"
    # Stay below Telegram's limit even with escaped names.
    for index, participant in enumerate(participants):
        marker = "✅" if active[index] else "⏳"
        line = f"{index + 1}. {marker} {escape(participant['display_name'][:160])}\n"
        if len(text) + len(line) > 3500:
            text += f"… ещё {len(participants) - index} участников"
            break
        text += line
    return text


async def refresh_event(context, event):
    try:
        await context.bot.edit_message_text(
            chat_id=event["chat_id"], message_id=event["message_id"],
            text=render_event_text(event, db(context).group_participants(event["id"]), context.bot.username),
            reply_markup=event_keyboard(event), parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except BadRequest as error:
        return "message is not modified" in str(error).lower()
    except TelegramError:
        # Deleted cards, lost permissions and temporary failures must never undo saved state.
        # No replacement group post: a subsequent START/action can retry this same card.
        return False
    return True


def group_return_url(chat, message_id):
    """Use only Telegram chat metadata; basic private groups have no /c/ message links."""
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return None
    username = getattr(chat, "username", None)
    if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,32}", username):
        username = None
    valid_message = type(message_id) is int and message_id > 0
    if username:
        base = f"https://t.me/{username}"
        return f"{base}/{message_id}" if valid_message else base
    chat_id = str(chat.id)
    if chat.type == ChatType.SUPERGROUP and re.fullmatch(r"-100[1-9][0-9]*", chat_id) and valid_message:
        return f"https://t.me/c/{chat_id[4:]}/{message_id}"
    invite = getattr(chat, "invite_link", None)
    if isinstance(invite, str):
        try:
            parsed = urlsplit(invite)
        except ValueError:
            return None
        if (parsed.scheme == "https" and parsed.netloc == "t.me" and not parsed.query and not parsed.fragment
                and re.fullmatch(r"/(?:\+|joinchat/)[A-Za-z0-9_-]+", parsed.path)):
            return invite
    return None


async def publication_confirmation(message, context, event, text=SETUP_SUCCESS):
    url = None
    try:
        chat = await context.bot.get_chat(event["chat_id"])
        if chat.id == event["chat_id"]:
            url = group_return_url(chat, event["message_id"])
    except TelegramError:
        pass  # A missing optional link must not turn a committed publication into a failure.
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Вернуться в группу", url=url)]]) if url else None
    await message.reply_text(text, reply_markup=markup)


async def creation_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if update.effective_chat.type != ChatType.PRIVATE:
        await query.answer("Настройка доступна только в личном чате. Начни с /new в группе.", show_alert=True)
        return
    action, token = query.data.split(":", 1)
    user_id = query.from_user.id
    existing = db(context).get_event_by_creation_key(token)
    if existing and existing["organizer_id"] != user_id:
        await query.answer("Эта настройка принадлежит другому организатору.", show_alert=True)
        return
    if existing and existing["message_id"] is not None:
        await query.answer("Игра уже опубликована.")
        await publication_confirmation(query.message, context, existing,
                                       "Игра уже опубликована в исходной группе. Текущий статус смотри в её карточке.")
        return
    draft = setup_session(context, user_id, token)
    if not draft:
        await query.answer("Настройка устарела. Начни с /new в исходной группе.", show_alert=True)
        return
    if action == "discard":
        removed = await discard_setup(context, draft)
        await query.answer("Создание отменено." if removed else "Исход публикации неизвестен. Проверь группу.", show_alert=True)
        if removed:
            await query.edit_message_reply_markup(reply_markup=None)
        return
    if query.message.message_id != draft.get("prompt_id"):
        await query.answer("Это старое сообщение настройки. Используй последнее или открой ссылку заново.", show_alert=True)
        return
    if existing:
        # Values are frozen once a publication attempt has been claimed.
        if action != "create":
            await query.answer("После попытки публикации изменение настроек недоступно. Повтори публикацию или отмени создание.", show_alert=True)
            return
    elif action == "edit_setup" and draft["step"] == "confirm":
        draft["step"] = "edit"
    elif action in {"edit_title", "edit_budget"} and draft["step"] == "edit":
        draft["step"] = action
    elif action != "create" or draft["step"] != "confirm":
        await query.answer("Сначала введи название и бюджет.", show_alert=True)
        return
    if action != "create":
        context.bot_data.setdefault("active_setups", {})[user_id] = token
        await query.answer()
        await prompt_draft(query.message, draft)
        return
    if not await allowed_organizer(context, draft["chat_id"], user_id):
        await query.answer("Публикация запрещена: не удалось подтвердить участие в исходной группе.", show_alert=True)
        return
    if setup_session(context, user_id, token) is not draft or draft["step"] != "confirm":
        await query.answer("Настройка истекла или изменилась. Начни с /new в исходной группе.", show_alert=True)
        return
    event = db(context).create_event(draft["chat_id"], user_id, draft["title"], draft["budget"], token)
    state = db(context).claim_publication(event["id"])
    if state != "send":
        await query.answer("Публикация уже выполнена или её исход неизвестен. Проверь группу; повтор не отправлен.", show_alert=True)
        return
    await query.answer("Публикую игру…")
    try:
        announcement = await context.bot.send_message(
            chat_id=event["chat_id"], text=render_event_text(event, [], context.bot.username),
            parse_mode=ParseMode.HTML, disable_web_page_preview=True, reply_markup=event_keyboard(event),
        )
    except (Forbidden, BadRequest, RetryAfter):
        db(context).record_publication(event["id"], "failed")
        await query.message.reply_text("Не удалось опубликовать игру. Проверь права бота в группе и повтори «🎁 Создать игру». Успех не подтверждён.")
        return
    except TelegramError:
        db(context).record_publication(event["id"], "uncertain")
        await query.message.reply_text("Не удалось подтвердить результат отправки. Объявление могло появиться в группе. Повторная отправка заблокирована во избежание дубликата; требуется ручная проверка.")
        return
    try:
        db(context).record_publication(event["id"], "published", announcement.message_id)
    except sqlite3.Error:
        await query.message.reply_text("Объявление отправлено, но ID не удалось сохранить. Не повторяй создание: проверь группу и восстанови ID сообщения в базе вручную.")
        return
    drafts(context).pop((draft["chat_id"], user_id), None)
    active = context.bot_data.setdefault("active_setups", {})
    if active.get(user_id) == token:
        active.pop(user_id, None)
    await query.edit_message_reply_markup(reply_markup=None)
    await publication_confirmation(query.message, context, db(context).get_event(event["id"]))


async def event_from_query(update, context):
    query = update.callback_query
    if not is_group(update):
        await query.answer("Действие доступно только в группе.")
        return None
    event = db(context).get_event(int(query.data.split(":")[1]))
    if not event or event["chat_id"] != query.message.chat_id:
        await query.answer("Игра недоступна.", show_alert=True)
        return None
    return event


async def join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    event = await event_from_query(update, context)
    if not event:
        return
    if event["status"] != OPEN:
        await query.answer(CLOSED, show_alert=True)
        await refresh_event(context, event)
        return
    user = query.from_user
    name = f"{user.full_name} (@{user.username})" if user.username else user.full_name
    added = db(context).join_event(event["id"], user.id, name)
    await query.answer(REGISTRATION_SUCCESS if added else REGISTRATION_REPEAT)
    await refresh_event(context, db(context).get_event(event["id"]))
    if added and not db(context).private_active(user.id):
        await query.message.reply_text(
            REGISTRATION_NOTICE + f"\n\n<a href='tg://user?id={user.id}'>{escape(user.full_name)}</a>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📩 Открыть бота", url=activation_url(context.bot.username, event["id"]))],
            ]),
        )


def activation_url(username, event_id):
    # The link carries event context only. Telegram's /start sender is the identity.
    return create_deep_linked_url(username, f"activate_{event_id}")


async def management_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    event = await event_from_query(update, context)
    if not event:
        return
    if query.from_user.id != event["organizer_id"]:
        await query.answer("Управление доступно только организатору.", show_alert=True)
        return
    action = query.data.split(":")[0]
    if event["status"] == COMPLETED and action == "manage":
        counts = db(context).notification_counts(event["id"])
        await query.answer(notification_summary(counts), show_alert=True)
        text = (f"⚙️ Управление игрой «{event['title']}».\n"
                "Статистика показана только организатору. Назначения не меняются при повторной отправке.")
        if sum(counts.values()) == 0:
            text += "\nЭто игра старой версии без сохранённых пар: восстановление и повтор недоступны."
        buttons = [[InlineKeyboardButton("Повторить неудачные уведомления", callback_data=f"retry:{event['id']}")]] if counts["pending"] + counts["failed"] else []
        await query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)
        # A deleted/stale group card must not hide access to retry.
        try:
            await refresh_event(context, event)
        except TelegramError:
            pass
        return
    if event["status"] != OPEN:
        await query.answer("Игра отменена или жеребьёвка уже завершена. Отмена недоступна.", show_alert=True)
        await refresh_event(context, event)
        return
    await query.answer()
    if action == "cancel_yes":
        changed = db(context).cancel_event(event["id"], query.from_user.id)
        await refresh_event(context, db(context).get_event(event["id"]))
        await query.edit_message_text("Игра отменена." if changed else "Отмена уже недоступна.")
    elif action == "cancel_no":
        await query.edit_message_text("Отмена игры не подтверждена.")
    else:
        eid = event["id"]
        if action == "manage":
            text = f"Управление игрой «{event['title']}»"
            buttons = [[InlineKeyboardButton("Отменить игру", callback_data=f"cancel_ask:{eid}")]]
        else:
            text = f"Отменить игру «{event['title']}»? Это действие нельзя отменить."
            buttons = [[InlineKeyboardButton("Да, отменить игру", callback_data=f"cancel_yes:{eid}")],
                       [InlineKeyboardButton("Назад", callback_data=f"cancel_no:{eid}")]]
        await query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))


def event_lock(context, event_id):
    return context.bot_data.setdefault("event_locks", {}).setdefault(event_id, asyncio.Lock())


async def show_draw_confirmation(query, context, event):
    participants = db(context).group_participants(event["id"])
    token = uuid4().hex
    context.bot_data.setdefault("draw_confirmations", {})[event["id"]] = {
        "token": token,
        "ids": [p["telegram_user_id"] for p in participants],
    }
    text = render_event_text(event, participants, context.bot.username, announcement=False)
    text += "\n\nПодтвердить жеребьёвку для этого состава? После сохранения изменить пары нельзя."
    eid = event["id"]
    await query.message.reply_text(
        text, parse_mode=ParseMode.HTML, disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Подтвердить жеребьёвку", callback_data=f"draw_yes:{eid}:{token}")],
            [InlineKeyboardButton("Назад", callback_data=f"draw_no:{eid}:{token}")],
        ]),
    )


async def send_saved_notifications(context, event):
    """Called under the event lock, strictly after complete_draw has committed."""
    for assignment in db(context).assignments(event["id"], unsent_only=True):
        try:
            await context.bot.send_message(
                assignment["giver_user_id"],
                assignment_text(event, assignment),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🎯 Кому я дарю?", callback_data=f"recipient:{event['id']}")],
                ]),
            )
        except TelegramError:
            # Never store or log raw Telegram errors: they may contain URLs/secrets.
            db(context).set_notification_status(event["id"], assignment["giver_user_id"], "failed")
        else:
            # A crash before this write leaves pending/failed; retry keeps the same pair.
            db(context).set_notification_status(event["id"], assignment["giver_user_id"], "sent")


async def report_notifications(query, context, event):
    counts = db(context).notification_counts(event["id"])
    try:
        await query.answer(notification_summary(counts), show_alert=True)
    except TelegramError:
        # A long delivery may outlive the callback. A fresh Manage callback shows counts.
        pass
    try:
        await refresh_event(context, event)
    except TelegramError:
        await query.message.reply_text("Не удалось обновить карточку группы. Назначения сохранены; повтори «⚙️ Управление».")


def notification_summary(counts):
    text = f"Отправлено: {counts['sent']}; ошибок: {counts['failed']}; ожидают: {counts['pending']}."
    if counts["pending"] + counts["failed"]:
        text += " Повтор — через «Управление». Пары сохранены."
    return text


async def draw_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    event = await event_from_query(update, context)
    if not event:
        return
    if query.from_user.id != event["organizer_id"]:
        await query.answer("Только организатор может запускать жеребьёвку.", show_alert=True)
        return
    if event["status"] != OPEN:
        await query.answer("Жеребьёвка недоступна: игра отменена или завершена.", show_alert=True)
        return
    participants = db(context).participants(event["id"])
    if len(participants) < 2:
        await query.answer(TOO_FEW, show_alert=True)
        return
    action = query.data.split(":")[0]
    confirmations = context.bot_data.setdefault("draw_confirmations", {})
    if action == "draw":
        await query.answer()
        await show_draw_confirmation(query, context, event)
        return
    saved = confirmations.get(event["id"])
    token = query.data.split(":")[2]
    if not saved or saved["token"] != token:
        await query.answer("Подтверждение устарело. Нажми «Провести жеребьёвку» заново.", show_alert=True)
        return
    if action == "draw_no":
        confirmations.pop(event["id"], None)
        await query.answer("Жеребьёвка не запущена.")
        await query.edit_message_reply_markup(reply_markup=None)
        return
    ids = [p["telegram_user_id"] for p in participants]
    if set(ids) != set(saved["ids"]):
        await query.answer("Состав изменился. Проверь новое подтверждение.", show_alert=True)
        await show_draw_confirmation(query, context, event)
        return
    lock = event_lock(context, event["id"])
    if lock.locked():
        await query.answer("Для этой игры уже выполняется операция. Подожди.", show_alert=True)
        return
    async with lock:
        # Consume confirmation before network requests; repeated taps cannot replay it.
        confirmations.pop(event["id"], None)
        await query.answer("Сохраняю жеребьёвку…")
        try:
            db(context).complete_draw(event["id"], query.from_user.id, ids)
        except ParticipantsChanged:
            await query.message.reply_text("Состав изменился во время проверки. Требуется новое подтверждение.")
            current = db(context).get_event(event["id"])
            if current["status"] == OPEN:
                await show_draw_confirmation(query, context, current)
            return
        except (DrawError, sqlite3.Error, ValueError):
            await query.message.reply_text(
                "Не удалось сохранить жеребьёвку: статус изменился или произошла ошибка базы данных. "
                "Новые назначения не отправлены. Проверь игру и запроси подтверждение заново."
            )
            return
        completed = db(context).get_event(event["id"])
        try:
            await refresh_event(context, completed)
        except TelegramError:
            pass  # Notification delivery must not depend on the group message being editable.
        await send_saved_notifications(context, completed)
        await report_notifications(query, context, completed)


async def retry_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    event = await event_from_query(update, context)
    if not event:
        return
    if query.from_user.id != event["organizer_id"]:
        await query.answer("Повтор доступен только организатору.", show_alert=True)
        return
    if event["status"] != COMPLETED:
        await query.answer("Повтор доступен только после сохранённой жеребьёвки.", show_alert=True)
        return
    if not db(context).assignments(event["id"]):
        await query.answer("Для этой игры нет сохранённых назначений. Повторная жеребьёвка запрещена.", show_alert=True)
        return
    lock = event_lock(context, event["id"])
    if lock.locked():
        await query.answer("Рассылка уже выполняется. Подожди.", show_alert=True)
        return
    async with lock:
        await query.answer("Повторяю неотправленные уведомления…")
        await send_saved_notifications(context, event)
        await report_notifications(query, context, event)


async def legacy_callback(update, context):
    await update.callback_query.answer("Старая кнопка недоступна. Создай новую игру через /new.", show_alert=True)


async def on_error(update, context):
    # Exception text may contain token-bearing URLs; do not log it.
    if update and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "Не удалось выполнить действие или обновить сообщение. "
                "Сохранённые данные не удалены; попробуй ещё раз."
            )
        except TelegramError:
            pass


async def shutdown(application):
    application.bot_data["storage"].close()


def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN не найден")
    default_path = Path(__file__).resolve().parent / "data" / "santa.sqlite3"
    storage = Storage(os.getenv("DATABASE_PATH") or default_path)
    app = Application.builder().token(token).concurrent_updates(False).post_shutdown(shutdown).build()
    app.bot_data["storage"] = storage
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("mygames", my_games))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("new", new_event))
    app.add_handler(CommandHandler("cancel", cancel_creation))
    app.add_handler(CallbackQueryHandler(creation_callback, pattern=r"^(create|discard|edit_setup|edit_title|edit_budget):[a-f0-9]{32}$"))
    app.add_handler(CallbackQueryHandler(join_callback, pattern=r"^join:[0-9]+$"))
    app.add_handler(CallbackQueryHandler(management_callback, pattern=r"^(manage|cancel_ask|cancel_yes|cancel_no):[0-9]+$"))
    app.add_handler(CallbackQueryHandler(draw_callback, pattern=r"^draw:[0-9]+$"))
    app.add_handler(CallbackQueryHandler(draw_callback, pattern=r"^draw_(yes|no):[0-9]+:[a-f0-9]{32}$"))
    app.add_handler(CallbackQueryHandler(retry_callback, pattern=r"^retry:[0-9]+$"))
    app.add_handler(CallbackQueryHandler(private_callback, pattern=r"^(?:private_menu|private_help|(?:games|game|recipient|gifts|gift|view):.*)$"))
    app.add_handler(CallbackQueryHandler(legacy_callback, pattern=r"^(join|draw|noop)$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, creation_input))
    app.add_error_handler(on_error)
    app.run_polling()


if __name__ == "__main__":
    main()
