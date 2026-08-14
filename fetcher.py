import re
import time
import logging
from datetime import datetime, timezone, timedelta

import feedparser
import requests
from bs4 import BeautifulSoup
from deep_translator import GoogleTranslator

from config import UNSPLASH_ACCESS_KEY, MAX_ENTRIES_PER_FEED, TRANSLATE_TO, LOOKBACK_HOURS, ARTICLE_MAX_CHARS, USE_SOURCE_IMAGES, FEEDS, MEDIA_DIR, LOCAL_PLACEHOLDER_PATH

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
    page content').
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
    Возвращает записи RSS-ленты за последние LOOKBACK_HOURS часов.
    Лента скачивается через requests (а не напрямую через
    feedparser.parse(url)) — обходит ошибку SSL: CERTIFICATE_VERIFY_FAILED,
    характерную для Python на macOS.
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
# статей, газетные вырезки, обложки изданий, иконки/значки — то, где
# почти наверняка будет чужой заголовок/логотип, а не нейтральное фото.
_UNWANTED_FILENAME_MARKERS = [
    "newspaper", "headline", "front page", "frontpage", "cover",
    "magazine", "article", "clipping", "press", "screenshot",
    "logo", "wordmark", "masthead", "газет", "заголов", "обложк",
    "icon", "loudspeaker", "speaker icon", "audio", "pronunciation",
    "symbol", "diagram", "flag of", "coat of arms", "crest", "emblem",
    "map of", "wikimedia", "commons-logo", "question mark", "no image",
    "placeholder",
]

# Расширения файлов, которые почти никогда не бывают настоящей фотографией
# на Wikimedia Commons (значки, схемы, звук, видео).
_NON_PHOTO_EXTENSIONS = (".svg", ".gif", ".ogg", ".ogv", ".webm", ".ico")


def _looks_like_editorial_content(title: str) -> bool:
    """True, если название файла намекает на газетную вырезку/скриншот/обложку/иконку."""
    lowered = title.lower()
    return any(marker in lowered for marker in _UNWANTED_FILENAME_MARKERS)


# Признаки того, что вместо реальной статьи загрузилась страница с
# ошибкой сервера/капчей/заглушкой.
_ERROR_PAGE_MARKERS = [
    "error 500", "server error", "please try again later",
    "that's all we know", "access denied", "403 forbidden",
    "404 not found", "page not found", "captcha",
]


def _looks_like_error_page(text: str) -> bool:
    """True, если извлечённый текст похож на страницу с ошибкой, а не на статью."""
    lowered = text.lower()
    return len(text) < 600 and any(marker in lowered for marker in _ERROR_PAGE_MARKERS)


def search_wikipedia_person_photo(name: str) -> str | None:
    """
    Самый точный источник для КОНКРЕТНОГО человека: запрашивает у Wikipedia
    статью по имени и забирает её "pageimage" — фото из карточки статьи.
    У футболистов (даже не самых известных — уровня второго/третьего
    эшелона), если у них вообще есть статья в Wikipedia, почти всегда
    есть и фото в карточке. Это гораздо точнее, чем полнотекстовый поиск
    по файлам на Wikimedia Commons, который выше по шансам возвращает
    нерелевантный результат.
    """
    if not name:
        return None
    try:
        resp = requests.get(
            "https://en.wikipedia.org/w/api.php",
            params={
                "action": "query",
                "titles": name,
                "prop": "pageimages",
                "piprop": "thumbnail",
                "pithumbsize": 1000,
                "redirects": 1,
                "format": "json",
            },
            headers=HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        pages = resp.json().get("query", {}).get("pages", {})
        for page in pages.values():
            if "missing" in page:
                continue
            thumbnail = page.get("thumbnail")
            if not thumbnail:
                continue
            url = thumbnail.get("source")
            if not url or not url.lower().endswith((".jpg", ".jpeg", ".png")):
                continue
            if is_valid_image_url(url):
                return url
    except requests.RequestException as e:
        logger.warning(f"Wikipedia pageimage-поиск не сработал ({name!r}): {e}")
    return None


def search_wikimedia_image(query: str) -> str | None:
    """
    Полнотекстовый поиск по файлам Wikimedia Commons — запасной вариант,
    когда прямой поиск статьи в Wikipedia (search_wikipedia_person_photo)
    не сработал (например, запрос — не имя конкретного человека, а
    название клуба или тема новости).
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
            if page_title.lower().endswith(_NON_PHOTO_EXTENSIONS):
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


def search_unsplash_image(query: str) -> str | None:
    """Поиск через Unsplash — требует API-ключ (config.UNSPLASH_ACCESS_KEY)."""
    if not UNSPLASH_ACCESS_KEY:
        return None
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
    return None


def search_fallback_image(query: str, person_name: str = "") -> str:
    """
    Ищет фото по теме в интернете. Если известно конкретное имя
    футболиста (person_name) — сначала пробует точный портрет через
    Wikipedia pageimage (самый релевантный источник для человека), потом
    общий поиск по Wikimedia Commons, и в конце Unsplash (если задан
    ключ). Возвращает общий плейсхолдер, только если вообще ничего не
    нашлось.
    """
    if person_name:
        image = search_wikipedia_person_photo(person_name)
        if image:
            return image

    image = search_wikimedia_image(query)
    if image:
        return image

    image = search_unsplash_image(query)
    if image:
        return image

    return PLACEHOLDER_IMAGE


def search_image_with_broadening(specific_query: str, broader_query: str, person_name: str = "") -> str:
    """
    Малоизвестные игроки/клубы часто не находятся по точному запросу —
    пробуем сузить поиск постепенно: сначала точный запрос (с приоритетом
    на точный портрет игрока, если есть имя), затем более общий запрос
    (обычно клуб или "football"), и только если совсем ничего не нашлось
    — общий плейсхолдер.
    """
    if specific_query and specific_query != broader_query:
        image = search_fallback_image(specific_query, person_name=person_name)
        if image != PLACEHOLDER_IMAGE:
            return image

    return search_fallback_image(broader_query or "football")


def download_and_prepare_image(url: str) -> str | None:
    """
    Скачивает фото по ссылке, проверяет, что это реально изображение (а
    не HTML-страница/редирект), и сохраняет локально в виде JPEG — так
    Telegram получает уже проверенный и подготовленный файл, а не просто
    ссылку, за которой сам может получить что-то неожиданное (WebP/AVIF
    без поддержки, страницу с ошибкой, редирект, требование cookies и
    т.п.). Возвращает локальный путь к файлу или None, если скачать/
    обработать не удалось — тогда вызывающий код переходит к плейсхолдеру.
    """
    import os
    import io
    import hashlib

    try:
        resp = requests.get(url, headers=HEADERS, timeout=12)
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "")
        if not content_type.startswith("image/"):
            logger.warning(f"Ссылка на фото отдала не изображение ({content_type}): {url}")
            return None

        from PIL import Image
        img = Image.open(io.BytesIO(resp.content))
        img = img.convert("RGB")  # приводим WebP/PNG-с-альфаканалом/CMYK и т.п. к обычному RGB

        os.makedirs(MEDIA_DIR, exist_ok=True)
        filename = hashlib.md5(url.encode()).hexdigest()[:16] + ".jpg"
        path = os.path.join(MEDIA_DIR, filename)
        img.save(path, "JPEG", quality=85)
        return path
    except Exception as e:
        logger.warning(f"Не удалось скачать/обработать фото {url}: {e}")
        return None


def get_image_for_entry(entry, article_url: str, search_query: str, broader_query: str = "", person_name: str = "") -> str:
    """
    По умолчанию НЕ берёт фото со страницы источника (og:image) и не
    берёт миниатюру из RSS — у крупных изданий эти фото почти всегда
    содержат их логотип/вотермарку. Вместо этого ищет нейтральное фото
    по теме в интернете, с приоритетом на точный портрет конкретного
    футболиста (см. search_fallback_image). Найденную ссылку скачивает и
    проверяет локально (download_and_prepare_image) — если это не
    получилось, использует гарантированный локальный плейсхолдер. Если
    понадобится вернуть старое поведение — см. config.USE_SOURCE_IMAGES.
    """
    if USE_SOURCE_IMAGES:
        image = extract_image_from_article(article_url)
        if image:
            local = download_and_prepare_image(image)
            if local:
                return local
        image = extract_image_from_rss_entry(entry)
        if image:
            local = download_and_prepare_image(image)
            if local:
                return local

    candidate_url = search_image_with_broadening(search_query, broader_query or search_query, person_name=person_name)

    if candidate_url == PLACEHOLDER_IMAGE:
        return LOCAL_PLACEHOLDER_PATH

    local_path = download_and_prepare_image(candidate_url)
    return local_path or LOCAL_PLACEHOLDER_PATH


# Фразы-маркеры "мусорных" абзацев — реклама, подписки, навигация,
# related-статьи и т.п., которые попадаются даже внутри <article>
_BOILERPLATE_MARKERS = [
    "sign up", "subscribe", "newsletter", "follow us", "related:",
    "read more:", "share this", "advertisement", "sponsored",
    "all rights reserved", "cookie", "click here", "watch:",
    "подпишись", "подписывайся", "реклама", "читайте также",
    "подробнее:",
]


def _is_boilerplate_paragraph(text: str) -> bool:
    """True для абзацев-мусора: реклама, подписки, навигация и т.п."""
    lowered = text.lower()
    return any(marker in lowered for marker in _BOILERPLATE_MARKERS)


def _extract_with_readability(html: str) -> list[str] | None:
    """
    Пытается вычленить основной текст статьи через readability-lxml —
    библиотеку, которая (в отличие от простого перебора всех <p>) умеет
    отличать основной контент от меню/сайдбара/рекламы примерно так же,
    как это делает "режим чтения" в браузере. Возвращает список абзацев
    или None, если библиотека недоступна или не смогла ничего вычленить.
    """
    try:
        from readability import Document
    except ImportError:
        return None

    try:
        doc = Document(html)
        summary_html = doc.summary()
    except Exception as e:
        logger.warning(f"readability не смогла разобрать статью: {e}")
        return None

    soup = BeautifulSoup(summary_html, "html.parser")
    paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
    paragraphs = [p for p in paragraphs if len(p) > 40 and not _is_boilerplate_paragraph(p)]
    return paragraphs or None


def _extract_with_fallback(html: str) -> list[str]:
    """
    Запасной вариант, если readability-lxml не установлена или не
    справилась: ищем <article>, иначе всю страницу, берём все <p>,
    отфильтровываем короткие/мусорные абзацы.
    """
    soup = BeautifulSoup(html, "html.parser")
    container = soup.find("article") or soup

    paragraphs = [p.get_text(" ", strip=True) for p in container.find_all("p")]
    return [p for p in paragraphs if len(p) > 40 and not _is_boilerplate_paragraph(p)]


def extract_article_text(article_url: str) -> str:
    """
    Заходит на страницу статьи и собирает основной текст, сохраняя
    разбивку на абзацы (соединены \\n\\n, а не в одну сплошную строку).
    Приоритет — readability-lxml (умное вычленение основного контента,
    отличает статью от меню/рекламы/сайдбара); если недоступна или не
    справилась — запасной способ (все <p> внутри <article>, с фильтром
    явного мусора).

    Больше НЕ обрезает текст по длине — полный текст сохраняется и
    переводится целиком; финальная обрезка под лимит поста происходит
    отдельно, непосредственно перед отправкой (см. bot.py:truncate_caption
    и config.ARTICLE_MAX_CHARS, применяемый уже к переведённому тексту).
    Если вместо статьи загрузилась страница с ошибкой сервера —
    возвращает пустую строку, чтобы взять запасной текст из RSS summary.
    """
    try:
        resp = requests.get(article_url, headers=HEADERS, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning(f"Не удалось загрузить текст статьи {article_url}: {e}")
        return ""

    paragraphs = _extract_with_readability(resp.text)
    if not paragraphs:
        paragraphs = _extract_with_fallback(resp.text)

    text = "\n\n".join(paragraphs)

    if _looks_like_error_page(text):
        logger.warning(f"Похоже на страницу с ошибкой вместо статьи ({article_url}), беру RSS summary")
        return ""

    return text


def _translate_single(text: str, translator) -> str:
    """Переводит один кусок текста (< 4500 симв. — лимит Google Translate за раз), с разбивкой по предложениям при необходимости."""
    if len(text) <= 4500:
        return translator.translate(text)
    chunks = re.split(r"(?<=[.!?])\s+", text)
    translated_chunks = [translator.translate(chunk) for chunk in chunks if chunk.strip()]
    return " ".join(translated_chunks)


def translate_text(text: str) -> str:
    """
    Переводит текст на язык из config.TRANSLATE_TO, сохраняя разбивку на
    абзацы (текст с \\n\\n переводится по абзацам, а не одним сплошным
    куском — так итоговое форматирование не "слипается"). Если перевод
    не удался — возвращает оригинал.
    """
    if not text or not TRANSLATE_TO:
        return text
    try:
        translator = GoogleTranslator(source="auto", target=TRANSLATE_TO)
        if "\n\n" not in text:
            return _translate_single(text, translator)

        paragraphs = text.split("\n\n")
        translated_paragraphs = [_translate_single(p, translator) for p in paragraphs if p.strip()]
        return "\n\n".join(translated_paragraphs)
    except Exception as e:
        logger.warning(f"Не удалось перевести текст: {e}")
        return text


def pick_headline_icon(title: str) -> str:
    """Подбирает иконку под заголовок: цитаты — 🗣, трансферы — 👀, обычные новости — 📰."""
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
    заглавной буквы — исключая уже известные названия клубов и служебные
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


def pick_image_search_query(title_en: str, body_en: str, fallback: str) -> tuple[str, str, str]:
    """
    Разбирает текст новости и определяет, каких футболистов/клубы она
    касается. Возвращает (точный_запрос, запасной_запрос, имя_игрока):
    - точный: "игрок клуб" > игрок > клуб > исходный заголовок
    - запасной: клуб (если есть) > "football"
    - имя_игрока: отдельно, для точного поиска портрета через Wikipedia
    """
    combined = f"{title_en} {body_en[:300]}"

    player = find_player_name(title_en) or find_player_name(combined)
    club = find_club_mention(combined)

    broader = club or "football"

    if player and club:
        return f"{player} {club}", broader, player
    if player:
        return player, broader, player
    if club:
        return club, broader, ""
    return fallback, "football", ""


def _truncate_paragraphs(text: str, max_chars: int) -> str:
    """
    Обрезает текст под лимит длины, стараясь резать по границе абзаца, а
    не посреди слова/предложения — так итоговый пост выглядит аккуратнее.
    Если даже первый абзац длиннее лимита — обрезает его по словам.
    """
    if len(text) <= max_chars:
        return text

    paragraphs = text.split("\n\n")
    result = []
    used = 0
    for p in paragraphs:
        # +2 за "\n\n", которые добавятся при склейке
        if used + len(p) + 2 > max_chars:
            break
        result.append(p)
        used += len(p) + 2

    if result:
        return "\n\n".join(result) + "…"

    # даже один абзац не влезает целиком — обрезаем по словам
    return text[:max_chars].rsplit(" ", 1)[0] + "…"


def build_entry_content(entry, article_url: str, title: str):
    """
    Читает и анализирует пост -> определяет игроков/клубы -> ищет фото
    именно по ним (приоритет — точный портрет игрока через Wikipedia) ->
    переводит текст для самого поста -> обрезает под лимит длины поста
    уже ПОСЛЕ перевода (config.ARTICLE_MAX_CHARS), чтобы не терять
    информацию на середине обработки.

    Возвращает (None, None), если сама RSS-запись выглядит как сбой
    сайта-источника — вызывающий код должен пропустить такую запись.
    """
    title_en = entry.get("title", "").strip()

    if _looks_like_error_page(title_en):
        logger.warning(f"Заголовок похож на страницу с ошибкой сайта-источника, пропускаю запись: {title_en!r}")
        return None, None

    rss_summary_en = clean_html(entry.get("summary", ""))

    full_text_en = extract_article_text(article_url) if article_url else ""
    body_en = full_text_en if len(full_text_en) > len(rss_summary_en) else rss_summary_en

    search_query, broader_query, person_name = pick_image_search_query(title_en, body_en, fallback=title_en)
    image_url = get_image_for_entry(entry, article_url, search_query, broader_query, person_name)

    icon = pick_headline_icon(title_en)
    title_ru = translate_text(title_en)
    body_ru = translate_text(body_en)
    body_ru = _truncate_paragraphs(body_ru, ARTICLE_MAX_CHARS)

    if not body_ru:
        text = f"{icon} {title_ru}"
    elif icon == "🗣":
        # цитатный пост — оформляем как прямую речь, в кавычках-ёлочках
        text = f"{icon} {title_ru}\n\n«{body_ru}»"
    else:
        # обычная новость — нормальными абзацами, без кавычек вокруг всего текста
        text = f"{icon} {title_ru}\n\n{body_ru}"

    return text, image_url
