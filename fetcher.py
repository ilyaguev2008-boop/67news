import re
import time
import logging
from datetime import datetime, timezone, timedelta

import feedparser
import requests
from bs4 import BeautifulSoup
from deep_translator import GoogleTranslator

from config import UNSPLASH_ACCESS_KEY, MAX_ENTRIES_PER_FEED, TRANSLATE_TO, LOOKBACK_HOURS

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; FootballNewsBot/1.0; +https://example.com/bot)"
}

PLACEHOLDER_IMAGE = "https://images.unsplash.com/photo-1508098682722-e99c43a406b2"  # общее фото футбольного мяча


def clean_html(raw_html: str) -> str:
    """Убирает HTML-теги из текста RSS summary."""
    if not raw_html:
        return ""
    text = BeautifulSoup(raw_html, "html.parser").get_text(separator=" ")
    return re.sub(r"\s+", " ", text).strip()


def _entry_published_dt(entry):
    """Возвращает datetime публикации записи (UTC), если он есть в RSS."""
    struct_time = entry.get("published_parsed") or entry.get("updated_parsed")
    if not struct_time:
        return None
    return datetime.fromtimestamp(time.mktime(struct_time), tz=timezone.utc)


def fetch_feed_entries(feed_url: str):
    """
    Возвращает записи RSS-ленты за последние LOOKBACK_HOURS часов
    (config.py). Записи без даты публикации в RSS не отбрасываются —
    попадают в выдачу как есть, чтобы не терять новости из-за
    нестандартного формата ленты. Ограничено MAX_ENTRIES_PER_FEED на
    случай, если лента отдаёт слишком много записей разом.

    Лента скачивается через requests (а не напрямую через
    feedparser.parse(url)) — у requests свой пакет сертификатов (certifi),
    который не зависит от системных настроек Python. Это защищает от
    ошибки SSL: CERTIFICATE_VERIFY_FAILED, характерной для Python на
    macOS, поставленного через python.org-инсталлятор.
    """
    try:
        response = requests.get(feed_url, headers=HEADERS, timeout=15)
        response.raise_for_status()
    except requests.RequestException as e:
        logger.warning(f"Не удалось скачать ленту {feed_url}: {e}")
        return []

    parsed = feedparser.parse(response.content)
    if parsed.bozo and not parsed.entries:
        logger.warning(f"Не удалось разобрать ленту {feed_url}: {parsed.bozo_exception}")
        return []

    all_entries = parsed.entries
    logger.info(f"{feed_url}: в ленте всего {len(all_entries)} записей")

    if LOOKBACK_HOURS:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
        recent = []
        for entry in all_entries:
            pub_dt = _entry_published_dt(entry)
            if pub_dt is None or pub_dt >= cutoff:
                recent.append(entry)
        logger.info(f"{feed_url}: из них за последние {LOOKBACK_HOURS} ч. — {len(recent)}")
        all_entries = recent

    return all_entries[:MAX_ENTRIES_PER_FEED]


def entry_unique_id(entry) -> str:
    return entry.get("id") or entry.get("guid") or entry.get("link")


def extract_image_from_rss_entry(entry) -> str | None:
    """Пытается достать картинку прямо из RSS-записи (enclosure/media)."""
    if "media_content" in entry and entry.media_content:
        url = entry.media_content[0].get("url")
        if url:
            return url
    if "media_thumbnail" in entry and entry.media_thumbnail:
        url = entry.media_thumbnail[0].get("url")
        if url:
            return url
    for link in entry.get("links", []):
        if link.get("type", "").startswith("image/"):
            return link.get("href")
    return None


def extract_image_from_article(article_url: str) -> str | None:
    """Заходит на страницу статьи и ищет og:image / twitter:image / первую крупную картинку."""
    try:
        resp = requests.get(article_url, headers=HEADERS, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning(f"Не удалось загрузить страницу {article_url}: {e}")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    og_image = soup.find("meta", property="og:image")
    if og_image and og_image.get("content"):
        return og_image["content"]

    twitter_image = soup.find("meta", attrs={"name": "twitter:image"})
    if twitter_image and twitter_image.get("content"):
        return twitter_image["content"]

    # fallback: первая достаточно большая картинка в теле статьи
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src")
        if src and src.startswith("http"):
            return src

    return None


def search_fallback_image(query: str) -> str:
    """Если у статьи вообще нет картинки — ищем через Unsplash (если задан ключ) либо возвращаем плейсхолдер."""
    if not UNSPLASH_ACCESS_KEY:
        return PLACEHOLDER_IMAGE
    try:
        resp = requests.get(
            "https://api.unsplash.com/search/photos",
            params={"query": query, "per_page": 1, "orientation": "landscape"},
            headers={"Authorization": f"Client-ID {UNSPLASH_ACCESS_KEY}"},
            timeout=10,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if results:
            return results[0]["urls"]["regular"]
    except requests.RequestException as e:
        logger.warning(f"Unsplash fallback не сработал: {e}")
    return PLACEHOLDER_IMAGE


def get_image_for_entry(entry, article_url: str, title: str) -> str:
    """
    Порядок: og:image самой статьи (обычно полноразмерное фото,
    1000+ px в ширину) -> картинка из RSS-записи (часто маленькая
    миниатюра, например 130x76 у BBC) -> поиск по теме -> плейсхолдер.
    """
    image = extract_image_from_article(article_url)
    if image:
        return image

    image = extract_image_from_rss_entry(entry)
    if image:
        return image

    return search_fallback_image(title)


def extract_article_text(article_url: str, max_chars: int = 1200) -> str:
    """
    Заходит на страницу статьи и собирает основной текст (абзацы <p>).
    Отсекает короткие служебные строки (меню, подписи под фото и т.п.),
    оставляя только содержательные абзацы. Обрезает по max_chars.
    """
    try:
        resp = requests.get(article_url, headers=HEADERS, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning(f"Не удалось загрузить текст статьи {article_url}: {e}")
        return ""

    soup = BeautifulSoup(resp.text, "html.parser")
    container = soup.find("article") or soup

    paragraphs = [p.get_text(" ", strip=True) for p in container.find_all("p")]
    # оставляем только содержательные абзацы — короткие строки обычно навигация/подписи
    meaningful = [p for p in paragraphs if len(p) > 40]
    text = " ".join(meaningful)

    if len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0] + "…"

    return text


def translate_text(text: str) -> str:
    """Переводит текст на язык из config.TRANSLATE_TO. Если перевод не удался — возвращает оригинал."""
    if not text or not TRANSLATE_TO:
        return text
    try:
        # deep-translator режет длинные тексты по лимиту символов сам не всегда корректно,
        # поэтому подстраховываемся ручным разбиением на предложения при необходимости.
        translator = GoogleTranslator(source="auto", target=TRANSLATE_TO)
        if len(text) <= 4500:
            return translator.translate(text)
        # длинный текст — переводим по частям
        chunks = re.split(r"(?<=[.!?])\s+", text)
        translated_chunks = [translator.translate(chunk) for chunk in chunks if chunk.strip()]
        return " ".join(translated_chunks)
    except Exception as e:
        logger.warning(f"Не удалось перевести текст: {e}")
        return text


def build_draft_text(entry, article_url: str = "") -> str:
    """
    Собирает текст поста. Приоритет — полный текст статьи (даёт гораздо
    больше содержания, чем короткий RSS-summary); если его не удалось
    достать — используется summary из самой RSS-записи как запасной
    вариант.
    """
    title = entry.get("title", "").strip()
    rss_summary = clean_html(entry.get("summary", ""))

    full_text = extract_article_text(article_url) if article_url else ""
    body = full_text if len(full_text) > len(rss_summary) else rss_summary

    title = translate_text(title)
    body = translate_text(body)

    return f"⚽ {title}\n\n{body}"
