import asyncio
import logging

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

# Очередь черновиков на проверку для каждого админа: {admin_id: [draft_id, draft_id, ...]}
review_queues: dict[int, list[str]] = {}

# Не даёт двум проверкам источников (фоновой по расписанию и ручной по
# кнопке) идти одновременно — иначе при 23 источниках + скачивании
# текста/фото/перевода один проход может не уложиться в
# POLL_INTERVAL_MINUTES, и следующий запуск стартует поверх ещё идущего.
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


# ---------- Клавиатуры ----------

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


# ---------- Отправка черновика ----------

CAPTION_LIMIT = 1024  # жёсткий лимит Telegram на подпись к фото


def truncate_caption(text: str, suffix: str = "") -> str:
    """
    Обрезает текст под лимит подписи Telegram (1024 символа), оставляя
    место под суффикс (например, '\\n\\n✅ ОПУБЛИКОВАНО'). Используется
    везде, где caption собирается из draft['text'] — с тех пор как текст
    стал полным пересказом статьи, а не коротким RSS-summary, он почти
    всегда длиннее лимита.
    """
    budget = CAPTION_LIMIT - len(suffix)
    if len(text) <= budget:
        return text + suffix
    trimmed = text[: budget - 1].rsplit(" ", 1)[0] + "…"
    return trimmed + suffix


def photo_input(image_url: str):
    """
    RSS-источники дают ссылку (http/https) — Telegram сам её скачает.
    Telegram-каналы и Instagram дают путь к уже скачанному локальному
    файлу — в этом случае нужно обернуть в FSInputFile, иначе aiogram
    попытается воспринять путь как ссылку и упадёт.
    """
    if image_url.startswith("http://") or image_url.startswith("https://"):
        return image_url
    return FSInputFile(image_url)


async def send_draft_to_admin(draft: dict):
    caption = truncate_caption(draft["text"])
    kb = moderation_keyboard(draft["draft_id"], draft.get("source_link"))

    try:
        await bot.send_photo(
            chat_id=config.ADMIN_ID,
            photo=photo_input(draft["image_url"]),
            caption=caption,
            reply_markup=kb,
        )
        return
    except Exception as e:
        logger.warning(f"Не удалось отправить фото черновика {draft['draft_id']} ({e}), пробую плейсхолдер")

    try:
        from fetcher import PLACEHOLDER_IMAGE
        await bot.send_photo(
            chat_id=config.ADMIN_ID,
            photo=PLACEHOLDER_IMAGE,
            caption=caption,
            reply_markup=kb,
        )
        return
    except Exception as e:
        # Даже плейсхолдер иногда не проходит (временный сбой на стороне
        # Telegram) — последний рубеж: отправляем просто текст без фото,
        # чтобы проверка новостей не падала целиком и очередь двигалась дальше.
        logger.warning(f"Плейсхолдер тоже не отправился ({e}), шлю текстом без фото")
        await bot.send_message(
            chat_id=config.ADMIN_ID,
            text=caption,
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


# ---------- Получение новостей ----------

async def poll_feeds() -> list[str]:
    """Проверяет все RSS-ленты, создаёт черновики для новых новостей. Возвращает список ID новых черновиков."""
    if not config.FEEDS:
        logger.warning("Список FEEDS пуст — добавь RSS-ссылки в config.py")
        return []

    new_draft_ids = []

    for feed_name, feed_url in config.FEEDS:
        # Скачивание и разбор RSS — блокирующая сетевая операция,
        # уводим в отдельный поток, чтобы не морозить event loop бота
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

            # Текст статьи + перевод + поиск фото — тоже блокирующие
            # сетевые запросы, тоже в отдельный поток
            text, image_url = await asyncio.to_thread(
                build_entry_content, entry, article_url, title
            )

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
    """
    Проверяет все источники, которые работают по опросу (RSS + Instagram).
    Telegram-каналы сюда не входят — они работают отдельно, событийно,
    через юзербот (см. tg_channels_source.py), новые посты оттуда
    появляются в базе сами по себе, без необходимости их "опрашивать".
    Возвращает суммарное количество новых черновиков.

    Использует poll_lock: если проверка уже идёт (фоновая по расписанию
    или другая ручная по кнопке), новый вызов просто дождётся её
    завершения вместо того, чтобы стартовать ещё один параллельный
    проход по тем же 23 источникам.
    """
    async with poll_lock:
        rss_ids = await poll_feeds()

        instagram_ids = []
        if config.INSTAGRAM_ACCOUNTS_TO_MONITOR:
            instagram_ids = await asyncio.to_thread(poll_instagram)
            if instagram_ids:
                logger.info(f"Instagram: новых постов — {len(instagram_ids)}")

        return len(rss_ids) + len(instagram_ids)


async def scheduled_poll_feeds():
    """Фоновая проверка по расписанию — просто копит новые черновики, не спамя админа."""
    total_new = await poll_all_sources()
    if total_new:
        logger.info(f"Найдено новых новостей: {total_new}. Используй '{BTN_CHECK_FEED}', чтобы их просмотреть.")


# ---------- Команды и главное меню ----------

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
    await poll_all_sources()  # подтягивает самое свежее прямо сейчас (RSS + Instagram)

    # Берём ВСЕ ещё не просмотренные черновики — включая те, что фоновый
    # планировщик уже успел накопить между твоими проверками
    pending_ids = storage.get_pending_draft_ids()

    if not pending_ids:
        await message.answer("Новых новостей нет.", reply_markup=main_menu_kb)
        return

    review_queues[message.from_user.id] = pending_ids
    await message.answer(f"Новостей на проверку: {len(pending_ids)}. Показываю по одной:")
    await send_next_in_queue(message.from_user.id)


# ---------- Управление каналами ----------

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


# ---------- Модерация черновиков ----------

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

    caption = truncate_caption(draft["text"])

    try:
        await bot.send_photo(
            chat_id=channel_id,
            photo=photo_input(draft["image_url"]),
            caption=caption,
            reply_markup=source_only_keyboard(draft.get("source_link")),
        )
        storage.update_draft_status(draft_id, "approved", published_channel_id=channel_id)
        await callback.message.edit_caption(
            caption=truncate_caption(draft["text"], "\n\n✅ ОПУБЛИКОВАНО"),
            reply_markup=source_only_keyboard(draft.get("source_link")),
        )
    except Exception as e:
        logger.error(f"Ошибка публикации в канал {channel_id}: {e}")
        await callback.answer(f"Ошибка публикации: {e}", show_alert=True)
        return

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

    storage.update_draft_text(draft_id, message.text)
    await state.clear()

    updated = storage.get_draft(draft_id)
    await message.answer("Текст обновлён. Вот превью:")
    await send_draft_to_admin(updated)


# ---------- Запуск ----------

async def main():
    storage.init_db()

    scheduler = AsyncIOScheduler()
    scheduler.add_job(scheduled_poll_feeds, "interval", minutes=config.POLL_INTERVAL_MINUTES)
    scheduler.start()

    if tg_channels_configured():
        # Подключаем юзербот и запускаем его слушать новые посты фоновой
        # задачей — параллельно с основным ботом, в том же event loop.
        # При первом запуске здесь потребуется ввести код подтверждения
        # из Telegram (и, возможно, пароль от 2FA) прямо в консоли.
        from tg_channels_source import telethon_client
        await start_telegram_monitor()
        asyncio.create_task(telethon_client.run_until_disconnected())

    logger.info("Бот запущен.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
