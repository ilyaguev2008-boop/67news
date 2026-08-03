import re
import time
import logging
from datetime import datetime, timezone, timedelta

import feedparser
import requests
from bs4 import BeautifulSoup
from deep_translator import GoogleTranslator

from config import UNSPLASH_ACCESS_KEY, MAX_ENTRIES_PER_FEED, TRANSLATE_TO, LOOKBACK_HOURS, ARTICLE_MAX_CHARS, USE_SOURCE_IMAGES, FEEDS

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; FootballNewsBot/1.0; +https://example.com/bot)"
}

PLACEHOLDER_IMAGE = "https://images.unsplash.com/photo-1508098682722-e99c43a406b2"  # общее фото футбольного мяча


def is_valid_image_url(url: str) -> bool:
    """
    Проверяет, что ссылка реально отдаёт изображение (Content-Type
    начинается с 'image/'), а не HTML-страницу, SVG или что-то ещё,
    что Telegram откажется принять как фото ('wrong type of the web
    page content'). Используется перед тем, как отдать URL в send_photo.
    """
    try:
        resp = requests.head(url, headers=HEADERS, timeout=8, allow_redirects=True)
        content_type = resp.headers.get("Content-Type", "")
        if content_type.startswith("image/") and "svg" not in content_type:
            return True
    except requests.RequestException:
        pass
    return False


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
    попадают в выдачу как есть. Ограничено MAX_ENTRIES_PER_FEED.

    Лента скачивается через requests (а не напрямую через
    feedparser.parse(url)) — у requests свой пакет сертификатов (certifi),
    независимый от системных настроек Python. Это обходит ошибку
    SSL: CERTIFICATE_VERIFY_FAILED, характерную для Python на macOS,
    поставленного через python.org-инсталлятор.
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

    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src")
        if src and src.startswith("http"):
            return src

    return None


# Признаки в названии файла, по которым отсеиваем вероятные скриншоты
# статей, газетные вырезки, обложки изданий — то, где почти наверняка
# будет чужой заголовок или логотип издания, а не нейтральное фото.
_UNWANTED_FILENAME_MARKERS = [
    "newspaper", "headline", "front page", "frontpage", "cover",
    "magazine", "article", "clipping", "press", "screenshot",
    "logo", "wordmark", "masthead", "газет", "заголов", "обложк",
]


def _looks_like_editorial_content(title: str) -> bool:
    """True, если название файла намекает на газетную вырезку/скриншот/обложку."""
    lowered = title.lower()
    return any(marker in lowered for marker in _UNWANTED_FILENAME_MARKERS)


def search_wikimedia_image(query: str) -> str | None:
    """
    Бесплатный поиск фото без API-ключа — через Wikimedia Commons.
    Качество/релевантность ниже, чем у Unsplash, но не требует настройки
    и не привязан к лимитам стороннего платного API. Проверяет несколько
    кандидатов через is_valid_image_url, чтобы не отдать Telegram
    битую/невалидную ссылку, и пропускает файлы, чьё название намекает
    на газетную вырезку/скриншот/обложку издания (см.
    _looks_like_editorial_content) — там почти наверняка будет чужой
    заголовок или логотип, а не нейтральное фото.
    """
    try:
        resp = requests.get(
            "https://commons.wikimedia.org/w/api.php",
            params={
                "action": "query",
                "generator": "search",
                "gsrsearch": f"{query} football",
                "gsrnamespace": 6,  # namespace 6 = файлы
                "gsrlimit": 8,
                "prop": "imageinfo",
                "iiprop": "url",
                "iiurlwidth": 1200,
                "format": "json",
            },
            headers=HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        pages = resp.json().get("query", {}).get("pages", {})
        for page in pages.values():
            page_title = page.get("title", "")
            if _looks_like_editorial_content(page_title):
                continue

            imageinfo = page.get("imageinfo")
            if not imageinfo:
                continue
            url = imageinfo[0].get("thumburl") or imageinfo[0].get("url")
            if not url or not url.lower().endswith((".jpg", ".jpeg", ".png")):
                continue
            if is_valid_image_url(url):
                return url
    except requests.RequestException as e:
        logger.warning(f"Wikimedia-поиск не сработал: {e}")
    return None


def search_fallback_image(query: str) -> str:
    """
    Ищет фото по теме в интернете — сначала Wikimedia Commons (бесплатно,
    без ключа), затем Unsplash (если задан UNSPLASH_ACCESS_KEY), и только
    если оба варианта не дали результата — общий плейсхолдер.
    """
    image = search_wikimedia_image(query)
    if image:
        return image

    if UNSPLASH_ACCESS_KEY:
        try:
            resp = requests.get(
                "https://api.unsplash.com/search/photos",
                params={"query": query, "per_page": 5, "orientation": "landscape"},
                headers={"Authorization": f"Client-ID {UNSPLASH_ACCESS_KEY}"},
                timeout=10,
            )
            resp.raise_for_status()
            for photo in resp.json().get("results", []):
                description = f"{photo.get('description') or ''} {photo.get('alt_description') or ''}"
                if _looks_like_editorial_content(description):
                    continue
                url = photo["urls"]["regular"]
                if is_valid_image_url(url):
                    return url
        except requests.RequestException as e:
            logger.warning(f"Unsplash fallback не сработал: {e}")

    return PLACEHOLDER_IMAGE


def get_image_for_entry(entry, article_url: str, search_query: str) -> str:
    """
    По умолчанию НЕ берёт фото со страницы источника (og:image) и не
    берёт миниатюру из RSS — у крупных изданий (BBC, Sky и т.п.) эти
    фото почти всегда содержат их логотип/вотермарку. Вместо этого сразу
    ищет нейтральное фото по теме в интернете (Wikimedia Commons /
    Unsplash), используя search_query — как правило, имя футболиста и/или
    название клуба, извлечённые из текста новости (см.
    pick_image_search_query), а не общий заголовок. Если понадобится
    вернуть старое поведение — см. config.USE_SOURCE_IMAGES.
    """
    if USE_SOURCE_IMAGES:
        image = extract_image_from_article(article_url)
        if image:
            return image
        image = extract_image_from_rss_entry(entry)
        if image:
            return image

    return search_fallback_image(search_query)


def extract_article_text(article_url: str, max_chars: int = None) -> str:
    """
    Заходит на страницу статьи и собирает основной текст (абзацы <p>).
    Отсекает короткие служебные строки (меню, подписи под фото и т.п.),
    оставляя только содержательные абзацы. Обрезает по max_chars
    (по умолчанию — config.ARTICLE_MAX_CHARS).
    """
    if max_chars is None:
        max_chars = ARTICLE_MAX_CHARS

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


def pick_headline_icon(title: str) -> str:
    """
    Подбирает иконку под заголовок, приближаясь к стилю крупных
    футбольных Telegram-каналов: цитаты/прямая речь — 🗣, срочные
    новости/трансферы — 👀, обычные новости — 📰.
    """
    lowered = title.lower()
    quote_markers = [" says", " on ", ":", "'", "\u2018", "\u2019"]
    transfer_markers = ["transfer", "sign", "deal", "move to", "join"]

    if any(m in lowered for m in quote_markers):
        return "🗣"
    if any(m in lowered for m in transfer_markers):
        return "👀"
    return "📰"


def _known_club_names() -> list[str]:
    """Список названий клубов — берётся из твоих же источников в config.FEEDS."""
    names = []
    for label, _ in FEEDS:
        club = label.split("—")[0].strip()
        if club and "целом" not in club.lower():
            names.append(club)
    return names


CLUB_NAMES = _known_club_names()

_NAME_STOPWORDS = {
    "The", "This", "That", "After", "Before", "During", "According",
    "However", "Meanwhile", "Following", "Despite", "Manchester",
}


def find_club_mention(text: str) -> str | None:
    """Ищет упоминание клуба (из CLUB_NAMES) в тексте."""
    lowered = text.lower()
    for club in CLUB_NAMES:
        if club.lower() in lowered:
            return club
    return None


def find_player_name(text: str) -> str | None:
    """
    Простая эвристика для имени футболиста: два подряд идущих слова с
    заглавной буквы (типичный паттерн "Имя Фамилия" в англоязычном
    заголовке) — исключая уже известные названия клубов и служебные
    слова.
    """
    candidates = re.findall(r"\b[A-Z][a-zA-Z'\-]+ [A-Z][a-zA-Z'\-]+\b", text)
    for candidate in candidates:
        first_word = candidate.split()[0]
        if first_word in _NAME_STOPWORDS:
            continue
        if any(candidate.lower() in club.lower() or club.lower() in candidate.lower() for club in CLUB_NAMES):
            continue
        return candidate
    return None


def pick_image_search_query(title_en: str, body_en: str, fallback: str) -> str:
    """
    Шаг 2 и 3 алгоритма: разбирает текст новости (заголовок + начало
    статьи, на английском — до перевода, так эвристика по заглавным
    буквам работает надёжнее) и определяет, каких футболистов/клубы она
    касается. Приоритет для поиска фото: конкретный игрок > клуб >
    исходный заголовок как крайний случай, если ни то ни другое не нашлось.
    """
    combined = f"{title_en} {body_en[:300]}"

    player = find_player_name(title_en) or find_player_name(combined)
    club = find_club_mention(combined)

    if player and club:
        return f"{player} {club}"
    if player:
        return player
    if club:
        return club
    return fallback


def build_entry_content(entry, article_url: str, title: str):
    """
    Шаг 1 алгоритма (чтение и анализ поста) + сборка текста и подбор
    фото в один блокирующий проход — чтобы вызывающий код мог отдать его
    целиком в отдельный поток через asyncio.to_thread и не блокировать
    event loop бота на время сетевых запросов.

    Порядок: читаем заголовок и текст статьи (на английском) -> из них
    определяем игроков/клубы (pick_image_search_query) -> ищем фото
    именно по ним, а не по общей теме -> уже потом переводим текст для
    самого поста.
    """
    title_en = entry.get("title", "").strip()
    rss_summary_en = clean_html(entry.get("summary", ""))

    full_text_en = extract_article_text(article_url) if article_url else ""
    body_en = full_text_en if len(full_text_en) > len(rss_summary_en) else rss_summary_en

    search_query = pick_image_search_query(title_en, body_en, fallback=title_en)
    image_url = get_image_for_entry(entry, article_url, search_query)

    icon = pick_headline_icon(title_en)
    title_ru = translate_text(title_en)
    body_ru = translate_text(body_en)

    text = f"{icon} {title_ru}\n\n«{body_ru}»" if body_ru else f"{icon} {title_ru}"

    return text, image_url
