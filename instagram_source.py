import logging
import os
from datetime import datetime, timezone, timedelta

import requests

import config
import storage
from fetcher import translate_text, HEADERS, PLACEHOLDER_IMAGE

logger = logging.getLogger(__name__)

MEDIA_DIR = config.MEDIA_DIR

# instaloader — не обязательная зависимость: если не установлена (или
# Instagram-аккаунты не заданы), этот источник просто молча выключается,
# не мешая остальным работать.
try:
    import instaloader
    _INSTALOADER_AVAILABLE = True
except ImportError:
    _INSTALOADER_AVAILABLE = False

_loader = None
if _INSTALOADER_AVAILABLE:
    _loader = instaloader.Instaloader(
        download_videos=False,
        download_comments=False,
        save_metadata=False,
        quiet=True,
    )


def is_configured() -> bool:
    return _INSTALOADER_AVAILABLE and bool(config.INSTAGRAM_ACCOUNTS_TO_MONITOR)


def _download_image(url: str, shortcode: str) -> str:
    """Скачивает фото поста локально. Возвращает путь к файлу или '' при неудаче."""
    try:
        os.makedirs(MEDIA_DIR, exist_ok=True)
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        path = f"{MEDIA_DIR}/ig_{shortcode}.jpg"
        with open(path, "wb") as f:
            f.write(resp.content)
        return path
    except Exception as e:
        logger.warning(f"Instagram: не удалось скачать фото поста {shortcode}: {e}")
        return ""


def poll_instagram() -> list[str]:
    """
    СИНХРОННАЯ (блокирующая) функция — вызывать через asyncio.to_thread.

    Забирает последние посты с аккаунтов из INSTAGRAM_ACCOUNTS_TO_MONITOR.

    ВАЖНО: нестабильный источник. Instagram не даёт официального публичного
    API для чтения чужих аккаунтов — instaloader эмулирует браузер, и в
    любой момент Instagram может начать требовать логин, показать капчу
    или заблокировать IP без предупреждения. Ошибка на одном аккаунте не
    останавливает проверку остальных источников — просто логируется и
    пропускается.
    """
    if not is_configured():
        return []

    new_draft_ids = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=config.LOOKBACK_HOURS)

    for username in config.INSTAGRAM_ACCOUNTS_TO_MONITOR:
        try:
            profile = instaloader.Profile.from_username(_loader.context, username)
        except Exception as e:
            logger.warning(f"Instagram: не удалось открыть профиль @{username} (возможно, требуется логин/капча): {e}")
            continue

        try:
            checked = 0
            for post in profile.get_posts():
                checked += 1
                if checked > 10:
                    break
                if post.date_utc.replace(tzinfo=timezone.utc) < cutoff:
                    break

                eid = f"ig:{post.shortcode}"
                if storage.is_entry_seen(eid):
                    continue

                caption = (post.caption or "").strip()
                translated = translate_text(caption) if caption else ""

                image_path = _download_image(post.url, post.shortcode)

                draft_id = storage.create_draft(
                    feed_name=f"Instagram: @{username}",
                    source_link=f"https://www.instagram.com/p/{post.shortcode}/",
                    title=f"Instagram — @{username}",
                    text=f"📸 {translated}" if translated else f"📸 Новый пост @{username}",
                    image_url=image_path or PLACEHOLDER_IMAGE,
                )
                storage.mark_entry_seen(eid, "instagram")
                new_draft_ids.append(draft_id)
        except Exception as e:
            logger.warning(f"Instagram: ошибка при чтении постов @{username} (аккаунт пропущен): {e}")
            continue

    return new_draft_ids
