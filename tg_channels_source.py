import logging
import os

import config
import storage
from fetcher import translate_text, PLACEHOLDER_IMAGE

logger = logging.getLogger(__name__)

MEDIA_DIR = config.MEDIA_DIR

# Клиент создаётся только если заданы API-ключи — иначе модуль просто
# ничего не делает, не мешая остальным источникам работать.
telethon_client = None

if config.TELEGRAM_API_ID and config.TELEGRAM_API_HASH:
    from telethon import TelegramClient, events

    telethon_client = TelegramClient(
        "userbot_session", config.TELEGRAM_API_ID, config.TELEGRAM_API_HASH
    )


def is_configured() -> bool:
    return bool(telethon_client and config.TELEGRAM_CHANNELS_TO_MONITOR)


async def _handle_new_message(event):
    try:
        message = event.message
        eid = f"tg:{event.chat_id}:{message.id}"
        if storage.is_entry_seen(eid):
            return

        text = (message.message or "").strip()
        if not text:
            # пост без текста (например, чистое фото без подписи) — пропускаем,
            # у нас нет что переводить и показывать
            storage.mark_entry_seen(eid, "telegram-channel")
            return

        chat = await event.get_chat()
        channel_title = getattr(chat, "title", None) or getattr(chat, "username", "Telegram-канал")
        username = getattr(chat, "username", None)
        source_link = f"https://t.me/{username}/{message.id}" if username else ""

        image_path = ""
        if message.photo:
            os.makedirs(MEDIA_DIR, exist_ok=True)
            try:
                image_path = await message.download_media(file=f"{MEDIA_DIR}/") or ""
            except Exception as e:
                logger.warning(f"Не удалось скачать фото из Telegram-канала {channel_title}: {e}")
                image_path = ""

        translated = translate_text(text)

        draft_id = storage.create_draft(
            feed_name=f"TG-канал: {channel_title}",
            source_link=source_link,
            title=channel_title,
            text=f"📢 {translated}",
            image_url=image_path or PLACEHOLDER_IMAGE,
        )
        storage.mark_entry_seen(eid, "telegram-channel")
        logger.info(f"[TG-канал: {channel_title}] новый пост -> черновик {draft_id}")
    except Exception as e:
        logger.warning(f"Ошибка обработки поста из чужого Telegram-канала: {e}")


async def start_telegram_monitor():
    """
    Подключает юзербот-клиент и подписывается на новые сообщения из
    TELEGRAM_CHANNELS_TO_MONITOR. При первом запуске потребует ввести код
    подтверждения (и, если включена 2FA, пароль) прямо в консоли.
    Ничего не делает, если API-ключи или список каналов не заданы.
    """
    if not is_configured():
        logger.info(
            "Мониторинг чужих Telegram-каналов выключен "
            "(не заданы TELEGRAM_API_ID/TELEGRAM_API_HASH или пуст TELEGRAM_CHANNELS_TO_MONITOR)."
        )
        return

    telethon_client.add_event_handler(
        _handle_new_message,
        events.NewMessage(chats=config.TELEGRAM_CHANNELS_TO_MONITOR),
    )

    await telethon_client.start(phone=config.TELEGRAM_PHONE)
    logger.info(f"Юзербот подключён. Слежу за каналами: {config.TELEGRAM_CHANNELS_TO_MONITOR}")
