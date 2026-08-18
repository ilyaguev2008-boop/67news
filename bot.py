import asyncio
import logging

from aiogram.types import URLInputFile

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    FSInputFile,
)
from aiogram.exceptions import TelegramBadRequest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import config
import storage
from fetcher import (
    fetch_feed_entries,
    entry_unique_id,
    build_entry_content,
)
from instagram_source import poll_instagram
from tg_channels_source import start_telegram_monitor, is_configured as tg_channels_configured

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

review_queues: dict[int, list[str]] = {}

poll_lock = asyncio.Lock()

BTN_CHANNELS = "📋 Мои каналы"
BTN_CHECK_FEED = "🔄 Проверить ленту новостей"

main_menu_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_CHANNELS), KeyboardButton(text=BTN_CHECK_FEED)]],
    resize_keyboard=True,
)


class EditDraft(StatesGroup):
    waiting_for_new_text = State()


class AddChannel(StatesGroup):
    waiting_for_channel = State()


def moderation_keyboard(draft_id: str, source_link: str = None) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve:{draft_id}"),
            InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"edit:{draft_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{draft_id}"),
        ]
    ]
    if source_link:
        rows.append([InlineKeyboardButton(text="🔗 Источник", url=source_link)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def source_only_keyboard(source_link: str) -> InlineKeyboardMarkup | None:
    if not source_link:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔗 Источник", url=source_link)]])


def channel_choice_keyboard(draft_id: str) -> InlineKeyboardMarkup:
    channels = storage.list_channels()
    rows = [
        [InlineKeyboardButton(text=ch["title"] or ch["channel_id"], callback_data=f"publish:{draft_id}:{ch['channel_id']}")]
        for ch in channels
    ]
    rows.append([InlineKeyboardButton(text="🚫 Отмена", callback_data=f"cancelpublish:{draft_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def channels_menu_keyboard() -> InlineKeyboardMarkup:
    channels = storage.list_channels()
    rows = [
        [InlineKeyboardButton(text=f"❌ {ch['title'] or ch['channel_id']}", callback_data=f"removechannel:{ch['channel_id']}")]
        for ch in channels
    ]
    rows.append([InlineKeyboardButton(text="➕ Добавить канал", callback_data="addchannel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


CAPTION_LIMIT = 1024


def truncate_caption(text: str, suffix: str = "") -> str:
    budget = CAPTION_LIMIT - len(suffix)
    if len(text) <= budget:
        return text + suffix
    trimmed = text[: budget - 1].rsplit(" ", 1)[0] + "…"
    return trimmed + suffix



CAPTION_LIMIT = getattr(config, "TELEGRAM_CAPTION_LIMIT", 1000)


def split_long_text(text: str, limit: int = CAPTION_LIMIT):
    if len(text) <= limit:
        return text, ""

    first = text[:limit]
    cut = max(
        first.rfind("\n\n"),
        first.rfind("\n"),
        first.rfind(". "),
        first.rfind("! "),
        first.rfind("? "),
        first.rfind(" "),
    )
    if cut < int(limit * 0.60):
        cut = limit

    return first[:cut].rstrip(), text[cut:].lstrip()


def photo_input(image_url: str):
    if image_url.startswith(("http://", "https://")):
        return URLInputFile(image_url)
    return FSInputFile(image_url)


async def send_draft_to_admin(draft: dict):
    caption, rest = split_long_text(draft["text"])
    kb = moderation_keyboard(draft["draft_id"], draft.get("source_link"))

    try:
        await bot.send_photo(
            chat_id=config.ADMIN_ID,
            photo=photo_input(draft["image_url"]),
            caption=caption,
            reply_markup=kb,
        )
        if rest:
            await bot.send_message(
                config.ADMIN_ID,
                f"📄 Продолжение:\n\n{rest}",
            )
    except Exception as e:
        logger.warning(f"Не удалось отправить фото черновика: {e}")
        try:
            await bot.send_photo(
                chat_id=config.ADMIN_ID,
                photo=FSInputFile(config.LOCAL_PLACEHOLDER_PATH),
                caption=caption,
                reply_markup=kb,
            )
            if rest:
                await bot.send_message(
                    config.ADMIN_ID,
                    f"📄 Продолжение:\n\n{rest}",
                )
        except Exception as e2:
            logger.error(f"Не удалось отправить даже placeholder: {e2}")
            await bot.send_message(
                config.ADMIN_ID,
                draft["text"],
                reply_markup=kb,
            )


async def send_next_in_queue(admin_id: int):
    queue = review_queues.get(admin_id, [])
    while queue:
        draft_id = queue.pop(0)
        draft = storage.get_draft(draft_id)
        if draft and draft["status"] == "pending":
            await bot.send_message(admin_id, "Следующая новость на проверку:")
            await send_draft_to_admin(draft)
            return
    await bot.send_message(admin_id, "✅ Все новости из этой проверки просмотрены.", reply_markup=main_menu_kb)


async def poll_feeds() -> list[str]:
    if not config.FEEDS:
        logger.warning("Список FEEDS пуст — добавь RSS-ссылки в config.py")
        return []

    new_draft_ids = []

    for feed_name, feed_url in config.FEEDS:
        entries = await asyncio.to_thread(fetch_feed_entries, feed_url)
        already_seen_count = 0
        new_from_this_feed = 0

        for entry in entries:
            eid = entry_unique_id(entry)
            if not eid or storage.is_entry_seen(eid):
                already_seen_count += 1
                continue

            article_url = entry.get("link", "")
            title = entry.get("title", "Без названия")

            text, image_url = await asyncio.to_thread(
                build_entry_content, entry, article_url, title
            )

            if text is None:
                storage.mark_entry_seen(eid, feed_name)
                already_seen_count += 1
                continue

            draft_id = storage.create_draft(
                feed_name=feed_name,
                source_link=article_url,
                title=title,
                text=text,
                image_url=image_url,
            )
            storage.mark_entry_seen(eid, feed_name)
            new_draft_ids.append(draft_id)
            new_from_this_feed += 1

        logger.info(
            f"[{feed_name}] за период отбора: {len(entries)}, "
            f"уже видели раньше: {already_seen_count}, новых: {new_from_this_feed}"
        )

    logger.info(f"Итого новых черновиков из RSS за проверку: {len(new_draft_ids)}")
    return new_draft_ids


async def poll_all_sources() -> int:
    async with poll_lock:
        rss_ids = await poll_feeds()

        instagram_ids = []
        if config.INSTAGRAM_ACCOUNTS_TO_MONITOR:
            instagram_ids = await asyncio.to_thread(poll_instagram)
            if instagram_ids:
                logger.info(f"Instagram: новых постов — {len(instagram_ids)}")

        return len(rss_ids) + len(instagram_ids)


async def scheduled_poll_feeds():
    total_new = await poll_all_sources()
    if total_new:
        logger.info(f"Найдено новых новостей: {total_new}. Используй '{BTN_CHECK_FEED}', чтобы их просмотреть.")


@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "Привет! Я слежу за футбольными источниками и помогаю тебе публиковать новости в твои каналы.\n\n"
        f"«{BTN_CHANNELS}» — управление каналами, куда публикуются новости.\n"
        f"«{BTN_CHECK_FEED}» — проверить источники и просмотреть новые новости по одной.",
        reply_markup=main_menu_kb,
    )


@dp.message(F.text == BTN_CHANNELS)
async def show_channels(message: Message):
    channels = storage.list_channels()
    if channels:
        text = "Твои каналы (нажми, чтобы удалить):"
    else:
        text = "Пока нет ни одного канала. Добавь первый:"
    await message.answer(text, reply_markup=channels_menu_keyboard())


@dp.message(F.text == BTN_CHECK_FEED)
async def check_feed(message: Message):
    if not storage.list_channels():
        await message.answer(
            f"Сначала добавь хотя бы один канал через «{BTN_CHANNELS}» — иначе публиковать будет некуда."
        )
        return

    await message.answer("Проверяю источники…")
    await poll_all_sources()

    pending_ids = storage.get_pending_draft_ids()

    if not pending_ids:
        await message.answer("Новых новостей нет.", reply_markup=main_menu_kb)
        return

    review_queues[message.from_user.id] = pending_ids
    await message.answer(f"Новостей на проверку: {len(pending_ids)}. Показываю по одной:")
    await send_next_in_queue(message.from_user.id)


@dp.callback_query(F.data == "addchannel")
async def handle_add_channel(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AddChannel.waiting_for_channel)
    await callback.message.answer(
        "Перешли сюда любое сообщение из канала (бот должен быть в нём администратором),\n"
        "или пришли @username канала, или его числовой ID (-100...)."
    )
    await callback.answer()


@dp.message(AddChannel.waiting_for_channel)
async def handle_channel_input(message: Message, state: FSMContext):
    chat = None

    if message.forward_from_chat:
        chat = message.forward_from_chat
    elif message.text:
        identifier = message.text.strip()
        try:
            chat = await bot.get_chat(identifier)
        except TelegramBadRequest as e:
            await message.answer(
                f"Не удалось найти такой канал: {e}\n"
                "Проверь, что бот добавлен в канал как администратор, и попробуй снова."
            )
            return

    if not chat:
        await message.answer("Не понял — пришли пересланное сообщение из канала, @username или ID.")
        return

    storage.add_channel(str(chat.id), chat.title or chat.username or str(chat.id))
    await state.clear()
    await message.answer(f"Добавил канал: {chat.title or chat.id}")
    await message.answer("Твои каналы:", reply_markup=channels_menu_keyboard())


@dp.callback_query(F.data.startswith("removechannel:"))
async def handle_remove_channel(callback: CallbackQuery):
    channel_id = callback.data.split(":", 1)[1]
    storage.remove_channel(channel_id)
    await callback.answer("Канал удалён.")
    await callback.message.edit_text("Твои каналы (нажми, чтобы удалить):", reply_markup=channels_menu_keyboard())


@dp.callback_query(F.data.startswith("approve:"))
async def handle_approve(callback: CallbackQuery):
    draft_id = callback.data.split(":", 1)[1]
    draft = storage.get_draft(draft_id)

    if not draft or draft["status"] != "pending":
        await callback.answer("Черновик уже обработан или не найден.", show_alert=True)
        return

    channels = storage.list_channels()
    if not channels:
        await callback.answer(f"Сначала добавь канал через «{BTN_CHANNELS}».", show_alert=True)
        return

    if len(channels) == 1:
        await publish_draft(callback, draft_id, channels[0]["channel_id"])
        return

    await callback.message.edit_caption(
        caption=truncate_caption(draft["text"], "\n\n👉 В какой канал опубликовать?"),
        reply_markup=channel_choice_keyboard(draft_id),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("publish:"))
async def handle_publish(callback: CallbackQuery):
    _, draft_id, channel_id = callback.data.split(":", 2)
    await publish_draft(callback, draft_id, channel_id)


async def publish_draft(callback: CallbackQuery, draft_id: str, channel_id: str):
    draft = storage.get_draft(draft_id)
    if not draft or draft["status"] != "pending":
        await callback.answer("Черновик уже обработан или не найден.", show_alert=True)
        return

    caption, rest = split_long_text(draft["text"])
    source_kb = source_only_keyboard(draft.get("source_link"))

    try:
        await bot.send_photo(
            chat_id=channel_id,
            photo=photo_input(draft["image_url"]),
            caption=caption,
            reply_markup=source_kb,
        )
        if rest:
            await bot.send_message(chat_id=channel_id, text=rest)
    except Exception as e:
        logger.warning(f"Не удалось опубликовать фото в канал {channel_id} ({e}), пробую локальный плейсхолдер")
        try:
            await bot.send_photo(
                chat_id=channel_id,
                photo=FSInputFile(config.LOCAL_PLACEHOLDER_PATH),
                caption=caption,
                reply_markup=source_kb,
            )
            if rest:
                await bot.send_message(chat_id=channel_id, text=rest)
        except Exception as e2:
            logger.error(f"Ошибка публикации в канал {channel_id}: {e2}")
            await callback.answer(f"Ошибка публикации: {e2}", show_alert=True)
            return

    storage.update_draft_status(draft_id, "approved", published_channel_id=channel_id)
    await callback.message.edit_caption(
        caption=truncate_caption(draft["text"], "\n\n✅ ОПУБЛИКОВАНО"),
        reply_markup=source_kb,
    )

    await callback.answer("Опубликовано!")
    await send_next_in_queue(callback.from_user.id)


@dp.callback_query(F.data.startswith("cancelpublish:"))
async def handle_cancel_publish(callback: CallbackQuery):
    draft_id = callback.data.split(":", 1)[1]
    draft = storage.get_draft(draft_id)
    if not draft:
        await callback.answer("Черновик не найден.", show_alert=True)
        return
    await callback.message.edit_caption(
        caption=truncate_caption(draft["text"]),
        reply_markup=moderation_keyboard(draft_id, draft.get("source_link")),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("reject:"))
async def handle_reject(callback: CallbackQuery):
    draft_id = callback.data.split(":", 1)[1]
    draft = storage.get_draft(draft_id)

    if not draft or draft["status"] != "pending":
        await callback.answer("Черновик уже обработан или не найден.", show_alert=True)
        return

    storage.update_draft_status(draft_id, "rejected")
    await callback.message.edit_caption(
        caption=truncate_caption(draft["text"], "\n\n❌ ОТКЛОНЕНО"),
        reply_markup=None,
    )
    await callback.answer("Отклонено.")
    await send_next_in_queue(callback.from_user.id)


@dp.callback_query(F.data.startswith("edit:"))
async def handle_edit(callback: CallbackQuery, state: FSMContext):
    draft_id = callback.data.split(":", 1)[1]
    draft = storage.get_draft(draft_id)

    if not draft or draft["status"] != "pending":
        await callback.answer("Черновик уже обработан или не найден.", show_alert=True)
        return

    await state.set_state(EditDraft.waiting_for_new_text)
    await state.update_data(draft_id=draft_id)
    await callback.message.answer("Пришли новый текст для этой новости одним сообщением.")
    await callback.answer()


@dp.message(EditDraft.waiting_for_new_text)
async def handle_new_text(message: Message, state: FSMContext):
    data = await state.get_data()
    draft_id = data.get("draft_id")
    draft = storage.get_draft(draft_id)

    if not draft or draft["status"] != "pending":
        await message.answer("Этот черновик уже нельзя редактировать.")
        await state.clear()
        return

    storage.update_draft_text(draft_id, (message.text or message.caption or "").strip())
    await state.clear()

    updated = storage.get_draft(draft_id)
    await message.answer("Текст обновлён. Вот превью:")
    await send_draft_to_admin(updated)


async def main():
    storage.init_db()

    scheduler = AsyncIOScheduler()
    scheduler.add_job(scheduled_poll_feeds, "interval", minutes=config.POLL_INTERVAL_MINUTES)
    scheduler.start()

    if tg_channels_configured():
        from tg_channels_source import telethon_client
        await start_telegram_monitor()
        asyncio.create_task(telethon_client.run_until_disconnected())

    logger.info("Бот запущен.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
