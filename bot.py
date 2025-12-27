import os
import random
from typing import Dict, Set, List

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ChatType, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# ================= CONFIG =================

TOKEN = os.getenv("BOT_TOKEN")
BOT_USERNAME = "Cluj_Secret_Santa_bot"

JOIN_CB = "join"
DRAW_CB = "draw"
NOOP_CB = "noop"

events: Dict[int, Dict] = {}
users_started_private: Set[int] = set()

# ================= HELPERS =================

def is_group(update: Update) -> bool:
    return update.effective_chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)


def render_event_text(title: str, participants: List[str]) -> str:
    users = "\n".join(f"• {name}" for name in participants) if participants else "— пока никого"

    return (
        f"🎁 <b>{title}</b>\n\n"
        "Нажмите кнопку ниже <b>«Присоединиться»</b>, чтобы участвовать.\n\n"
        "⚠️⚠️⚠️ <b>Важно!!! Перед жеребьёвкой необходимо:</b>\n"
        "Перейти по ссылке или зайти в бот "
        f"<a href='https://t.me/{BOT_USERNAME}'>@{BOT_USERNAME}</a> и нажать /start.\n"
        "Это необходимо для возможности боту отправлять личное сообщение!\n\n"
        "👥 <b>Участники:</b>\n"
        f"{users}"
    )


def build_keyboard(draw_finished: bool = False) -> InlineKeyboardMarkup:
    if draw_finished:
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("🎲 Жеребьёвка завершена", callback_data=NOOP_CB)]]
        )

    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Присоединиться", callback_data=JOIN_CB)],
            [InlineKeyboardButton("🎲 Жеребьёвка", callback_data=DRAW_CB)],
        ]
    )


def make_pairs(ids: List[int]):
    for _ in range(1000):
        shuffled = ids[:]
        random.shuffle(shuffled)
        if all(a != b for a, b in zip(ids, shuffled)):
            return list(zip(ids, shuffled))
    return None


async def get_participant_names(chat_id, context, ids):
    result = []
    for uid in ids:
        try:
            member = await context.bot.get_chat_member(chat_id, uid)
            u = member.user
            result.append(f"{u.full_name} (@{u.username})" if u.username else u.full_name)
        except Exception:
            pass
    return result


def build_user_handle(user) -> str:
    if user.username:
        return f"@{user.username}"
    return f"<a href='tg://user?id={user.id}'>профиль</a>"

# ================= HANDLERS =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == ChatType.PRIVATE:
        users_started_private.add(update.effective_user.id)
        await update.message.reply_text(
            "🎅 Привет!\n\n"
            "Я бот Тайного Санты.\n"
            "Ты уже написал мне — значит я смогу прислать тебе результат жеребьёвки 👍\n\n"
            "Теперь возвращайся в группу и нажми «✅ Присоединиться»."
        )


async def new_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        return

    chat_id = update.effective_chat.id
    owner_id = update.effective_user.id
    title = " ".join(context.args) or "Secret Santa 🎄"

    msg = await update.message.reply_text(
        render_event_text(title, []),
        reply_markup=build_keyboard(),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )

    events[chat_id] = {
        "owner_id": owner_id,
        "participants": set(),
        "drawn": False,
        "title": title,
        "message_id": msg.message_id,
    }


async def join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat_id
    user_id = query.from_user.id

    event = events.get(chat_id)
    if not event or event["drawn"]:
        await query.answer()
        return

    # 🔴 КРИТИЧЕСКИЙ ФИКС
    if user_id in event["participants"]:
        await query.answer("🎄 Ты уже участвуешь", show_alert=False)
        return

    event["participants"].add(user_id)

    names = await get_participant_names(chat_id, context, event["participants"])

    await query.message.edit_text(
        render_event_text(event["title"], names),
        reply_markup=build_keyboard(),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
    await query.answer("🎉 Ты добавлен!")


async def draw_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat_id
    user_id = query.from_user.id
    event = events.get(chat_id)

    if not event:
        await query.answer()
        return

    if user_id != event["owner_id"]:
        await query.answer("⛔ Только создатель может запускать жеребьёвку", show_alert=True)
        return

    ids = list(event["participants"])
    if len(ids) < 2:
        await query.answer("Нужно минимум 2 участника", show_alert=True)
        return

    not_ready = [uid for uid in ids if uid not in users_started_private]
    if not_ready:
        await query.answer("⚠️ Не все нажали /start в личке", show_alert=True)
        return

    pairs = make_pairs(ids)
    if not pairs:
        await query.answer("Ошибка жеребьёвки", show_alert=True)
        return

    for giver_id, receiver_id in pairs:
        member = await context.bot.get_chat_member(chat_id, receiver_id)
        u = member.user
        handle = build_user_handle(u)

        await context.bot.send_message(
            giver_id,
            (
                "🎅 Ты Тайный Санта для:\n\n"
                f"🎁 <b>{u.full_name}</b> {handle}\n\n"
                "Не раскрывай тайну 😉"
            ),
            parse_mode=ParseMode.HTML,
        )

    event["drawn"] = True

    await query.message.edit_text(
        render_event_text(event["title"], []),
        reply_markup=build_keyboard(draw_finished=True),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )

    await context.bot.send_message(chat_id, "🎉 Жеребьёвка проведена!")
    await query.answer("🎲 Готово!")


async def noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()

# ================= MAIN =================

def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN не найден")

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("new", new_event))
    app.add_handler(CallbackQueryHandler(join_callback, pattern=f"^{JOIN_CB}$"))
    app.add_handler(CallbackQueryHandler(draw_callback, pattern=f"^{DRAW_CB}$"))
    app.add_handler(CallbackQueryHandler(noop_callback, pattern=f"^{NOOP_CB}$"))

    app.run_polling()

if __name__ == "__main__":
    main()
