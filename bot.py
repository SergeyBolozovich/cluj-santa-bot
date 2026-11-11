import os
import asyncio
import sqlite3
from html import escape
from pathlib import Path
from uuid import uuid4

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, TelegramError, Forbidden, RetryAfter
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters,
    ContextTypes,
)

from storage import Storage, OPEN, CANCELLED, COMPLETED, DrawError, ParticipantsChanged
from validation import validate_title, parse_budget, format_budget


def is_group(update):
    return update.effective_chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)


def db(context):
    return context.bot_data["storage"]


def drafts(context):
    return context.bot_data.setdefault("drafts", {})


def draft_key(update):
    return update.effective_chat.id, update.effective_user.id


def draft_keyboard(draft, confirm=False):
    rows = []
    if confirm:
        rows.append([InlineKeyboardButton("Создать игру", callback_data=f"create:{draft['key']}")])
    rows.append([InlineKeyboardButton("Отмена", callback_data=f"discard:{draft['key']}")])
    return InlineKeyboardMarkup(rows)


async def prompt_draft(message, draft):
    if draft["step"] == "title":
        text = "Введите название игры (1–100 символов). Ответьте на это сообщение."
    elif draft["step"] == "budget":
        text = "Введите бюджет: больше 0, до 100 000 RON, максимум 2 знака после запятой. Ответьте на это сообщение."
    else:
        text = (f"Создать игру «{draft['title']}»?\n"
                f"Бюджет: {format_budget(draft['budget'])}\nРегистрацию участника отменить нельзя.")
    sent = await message.reply_text(text, reply_markup=draft_keyboard(draft, draft["step"] == "confirm"))
    draft["prompt_id"] = sent.message_id


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == ChatType.PRIVATE:
        db(context).activate_private(update.effective_user.id)
        await update.message.reply_text(
            "🎅 Привет! Личный чат активирован. Вернись в группу и нажми «🎁 Присоединиться».\n"
            "Активация не гарантирует доставку, если бот будет заблокирован."
        )


async def new_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        await update.message.reply_text("Создать игру можно только в группе.")
        return
    key = draft_key(update)
    if key in drafts(context):
        draft = drafts(context)[key]
        await update.message.reply_text(
            "У тебя уже есть черновик. Продолжи его или отмени через /cancel и отправь /new заново."
        )
        await prompt_draft(update.message, draft)
        return
    draft = {"key": uuid4().hex, "step": "title"}
    drafts(context)[key] = draft
    if context.args:
        try:
            draft["title"] = validate_title(" ".join(context.args))
            draft["step"] = "budget"
        except ValueError as exc:
            await update.message.reply_text(str(exc))
    await prompt_draft(update.message, draft)


async def cancel_creation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        return
    removed = drafts(context).pop(draft_key(update), None)
    existing = db(context).get_event_by_creation_key(removed["key"]) if removed else None
    if existing:
        await update.message.reply_text("Игра уже создана. Для её отмены открой «⚙️ Управление».")
        await refresh_event(context, existing)
    else:
        await update.message.reply_text("Создание отменено." if removed else "Нет незавершённого создания.")


async def creation_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        return
    draft = drafts(context).get(draft_key(update))
    reply = update.message.reply_to_message
    if not draft or not reply or reply.message_id != draft.get("prompt_id"):
        return
    try:
        if draft["step"] == "title":
            draft["title"] = validate_title(update.message.text)
            draft["step"] = "budget"
        elif draft["step"] == "budget":
            draft["budget"] = parse_budget(update.message.text)
            draft["step"] = "confirm"
        else:
            return
    except ValueError as exc:
        await update.message.reply_text(str(exc))
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


def render_event_text(event, participants, username):
    status = {OPEN: "🟢 Регистрация открыта", CANCELLED: "⛔ Игра отменена", COMPLETED: "🎲 Жеребьёвка завершена"}[event["status"]]
    text = (f"🎅 <b>{escape(event['title'])}</b>\n🎁 Бюджет: {format_budget(event['budget'])}\n"
            f"👥 Участников: {len(participants)}\n{status}\n\n"
            f"Перед жеребьёвкой открой <a href='https://t.me/{escape(username)}'>бота</a> и нажми /start.\n"
            "Регистрация необратима.\n\n<b>Участники:</b>\n")
    if not participants:
        return text + "— пока никого"
    # Stay below Telegram's limit even with escaped names.
    for index, participant in enumerate(participants):
        line = f"{index + 1}. {escape(participant['display_name'][:160])}\n"
        if len(text) + len(line) > 3500:
            text += f"… ещё {len(participants) - index} участников"
            break
        text += line
    return text


async def refresh_event(context, event):
    try:
        await context.bot.edit_message_text(
            chat_id=event["chat_id"], message_id=event["message_id"],
            text=render_event_text(event, db(context).participants(event["id"]), context.bot.username),
            reply_markup=event_keyboard(event), parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


async def creation_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_group(update):
        await query.answer("Создание доступно только в группе.")
        return
    action, token = query.data.split(":", 1)
    key = draft_key(update)
    existing = db(context).get_event_by_creation_key(token)
    if existing:
        if (existing["chat_id"], existing["organizer_id"]) != key:
            await query.answer("Это создание принадлежит другому организатору.", show_alert=True)
            return
        await query.answer("Игра уже создана. Обновляю карточку.")
        await refresh_event(context, existing)
        current = drafts(context).get(key)
        if current and current["key"] == token:
            drafts(context).pop(key, None)
        return
    draft = drafts(context).get(key)
    if not draft or draft["key"] != token:
        await query.answer("Черновик недоступен. Начни с /new или используй свой черновик.", show_alert=True)
        return
    if action == "discard":
        drafts(context).pop(key)
        await query.answer("Создание отменено.")
        await query.edit_message_reply_markup(reply_markup=None)
        return
    if draft["step"] != "confirm":
        await query.answer("Сначала введи название и бюджет.", show_alert=True)
        return
    # Reuse the confirmation message as the event card: no send/commit gap.
    event = db(context).create_event(
        key[0], key[1], draft["title"], draft["budget"], token, query.message.message_id
    )
    await query.answer("Публикую игру…")
    if event["message_id"] is None:
        db(context).set_message_id(event["id"], query.message.message_id)
    await refresh_event(context, db(context).get_event(event["id"]))
    drafts(context).pop(key, None)


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
        await query.answer("Регистрация закрыта.", show_alert=True)
        await refresh_event(context, event)
        return
    user = query.from_user
    name = f"{user.full_name} (@{user.username})" if user.username else user.full_name
    added = db(context).join_event(event["id"], user.id, name)
    await query.answer("🎉 Ты добавлен! Открой бота и нажми /start." if added else "Ты уже участвуешь.")
    await refresh_event(context, db(context).get_event(event["id"]))


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
        await query.answer()
        counts = db(context).notification_counts(event["id"])
        text = (f"Жеребьёвка «{event['title']}» завершена.\n"
                f"Отправлено: {counts['sent']}; ошибок: {counts['failed']}; ожидают: {counts['pending']}.\n"
                "Назначения не меняются при повторной отправке.")
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
    participants = db(context).participants(event["id"])
    token = uuid4().hex
    context.bot_data.setdefault("draw_confirmations", {})[event["id"]] = {
        "token": token,
        "ids": [p["telegram_user_id"] for p in participants],
    }
    text = render_event_text(event, participants, context.bot.username)
    text += "\n\nПодтвердить жеребьёвку для этого состава? После сохранения изменить пары нельзя."
    eid = event["id"]
    await query.message.reply_text(
        text, parse_mode=ParseMode.HTML, disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Подтвердить жеребьёвку", callback_data=f"draw_yes:{eid}:{token}")],
            [InlineKeyboardButton("Назад", callback_data=f"draw_no:{eid}:{token}")],
        ]),
    )


def telegram_failure_reason(error):
    if isinstance(error, Forbidden):
        return "личный чат недоступен или бот заблокирован"
    if isinstance(error, RetryAfter):
        return "Telegram ограничил частоту отправки; попробуйте позднее"
    return "временная ошибка Telegram или сети; попробуйте позднее"


async def check_private_delivery(context, event, participants):
    problems = []
    for participant in participants:
        uid = participant["telegram_user_id"]
        if not db(context).private_active(uid):
            problems.append((participant["display_name"], "нужно нажать /start в личном чате"))
    if problems:
        return problems
    for participant in participants:
        try:
            await context.bot.send_message(
                participant["telegram_user_id"],
                f"🎅 Проверка личного чата перед жеребьёвкой «{event['title']}». Результат придёт отдельным сообщением.",
            )
        except TelegramError as error:
            problems.append((participant["display_name"], telegram_failure_reason(error)))
    return problems


async def send_saved_notifications(context, event):
    """Called under the event lock, strictly after complete_draw has committed."""
    for assignment in db(context).assignments(event["id"], unsent_only=True):
        try:
            await context.bot.send_message(
                assignment["giver_user_id"],
                f"🎅 <b>{escape(event['title'])}</b>\n\nЖеребьёвка состоялась!\n\n"
                f"🎁 Ты даришь подарок: <b>{escape(assignment['receiver_name'])}</b> "
                f"<a href='tg://user?id={assignment['receiver_user_id']}'>профиль</a>\n\n"
                f"💰 Бюджет: {format_budget(event['budget'])}\n\n🤫 Не раскрывай тайну!",
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            # Never store or log raw Telegram errors: they may contain URLs/secrets.
            db(context).set_notification_status(event["id"], assignment["giver_user_id"], "failed")
        else:
            # A crash before this write leaves pending/failed; retry keeps the same pair.
            db(context).set_notification_status(event["id"], assignment["giver_user_id"], "sent")


async def report_notifications(query, context, event):
    counts = db(context).notification_counts(event["id"])
    remaining = counts["pending"] + counts["failed"]
    text = (f"Жеребьёвка «{event['title']}» сохранена. "
            f"Отправлено: {counts['sent']}; ошибок: {counts['failed']}; ожидают: {counts['pending']}.")
    if remaining:
        text += " Не все уведомления отправлены. Организатор может повторить их через «⚙️ Управление». Пары не изменятся."
    await query.message.reply_text(text)
    try:
        await refresh_event(context, event)
    except TelegramError:
        await query.message.reply_text("Не удалось обновить карточку группы. Назначения сохранены; повтори «⚙️ Управление».")


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
        await query.answer("Нужно минимум 2 участника.", show_alert=True)
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
        await query.answer("Проверяю личные чаты…")
        problems = await check_private_delivery(context, event, participants)
        if problems:
            details = "\n".join(f"• {name[:80]}: {reason}" for name, reason in problems)
            await query.message.reply_text(
                "Жеребьёвка не началась. Исправьте проблемы и запросите новое подтверждение:\n" + details[:3000]
            )
            return
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
    app.add_handler(CommandHandler("new", new_event))
    app.add_handler(CommandHandler("cancel", cancel_creation))
    app.add_handler(CallbackQueryHandler(creation_callback, pattern=r"^(create|discard):[a-f0-9]{32}$"))
    app.add_handler(CallbackQueryHandler(join_callback, pattern=r"^join:[0-9]+$"))
    app.add_handler(CallbackQueryHandler(management_callback, pattern=r"^(manage|cancel_ask|cancel_yes|cancel_no):[0-9]+$"))
    app.add_handler(CallbackQueryHandler(draw_callback, pattern=r"^draw:[0-9]+$"))
    app.add_handler(CallbackQueryHandler(draw_callback, pattern=r"^draw_(yes|no):[0-9]+:[a-f0-9]{32}$"))
    app.add_handler(CallbackQueryHandler(retry_callback, pattern=r"^retry:[0-9]+$"))
    app.add_handler(CallbackQueryHandler(legacy_callback, pattern=r"^(join|draw|noop)$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS, creation_input))
    app.add_error_handler(on_error)
    app.run_polling()


if __name__ == "__main__":
    main()
