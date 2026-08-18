import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin, urlparse

import feedparser
import requests
from bs4 import BeautifulSoup
from deep_translator import GoogleTranslator

from config import (
    UNSPLASH_ACCESS_KEY,
    MAX_ENTRIES_PER_FEED,
    TRANSLATE_TO,
    LOOKBACK_HOURS,
    ARTICLE_MAX_CHARS,
    USE_SOURCE_IMAGES,
    DOWNLOAD_IMAGES_LOCALLY,
    MEDIA_DIR,
    FEEDS,
)

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/139.0 Safari/537.36 FootballNewsBot/2.0"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

PLACEHOLDER_IMAGE = "https://images.unsplash.com/photo-1508098682722-e99c43a406b2"

_BAD_IMAGE_MARKERS = (
    "logo", "wordmark", "masthead", "icon", "sprite", "avatar",
    "placeholder", "screenshot", "frontpage", "front-page", "newspaper",
    "headline", "cover", "magazine", "advert", "banner", "captcha",
    "diagram", "map", "coat-of-arms", "crest", "emblem", "flag",
)
_BAD_TEXT_MARKERS = (
    "subscribe to", "sign up to our newsletter", "privacy policy",
    "cookie policy", "follow us on", "read more:", "advertisement",
)
_REMOVE_SELECTORS = (
    "script", "style", "noscript", "svg", "form", "nav", "footer",
    "header", "aside", "iframe", "dialog", "template",
    ".advert", ".advertisement", ".ads", ".ad", ".cookie", ".consent",
    ".newsletter", ".related", ".recommended", ".comments", ".comment",
    ".social", ".share", ".sharing", ".breadcrumb", ".navigation",
    ".menu", ".sidebar", ".footer", ".promo", ".sponsor",
)

def _entry_published_dt(entry):
    struct_time = entry.get("published_parsed") or entry.get("updated_parsed")
    if not struct_time:
        return None
    return datetime.fromtimestamp(time.mktime(struct_time), tz=timezone.utc)

def fetch_feed_entries(feed_url: str):
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

    entries = parsed.entries
    if LOOKBACK_HOURS:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
        entries = [
            entry for entry in entries
            if _entry_published_dt(entry) is None or _entry_published_dt(entry) >= cutoff
        ]
    return entries[:MAX_ENTRIES_PER_FEED]

def entry_unique_id(entry) -> str:
    return entry.get("id") or entry.get("guid") or entry.get("link")

def clean_html(raw_html: str) -> str:
    if not raw_html:
        return ""
    text = BeautifulSoup(raw_html, "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()

def extract_image_from_rss_entry(entry):
    for field in ("media_content", "media_thumbnail"):
        for item in entry.get(field, []) or []:
            url = item.get("url")
            if url:
                return url

    for link in entry.get("links", []) or []:
        if link.get("type", "").startswith("image/") and link.get("href"):
            return link["href"]

    for item in entry.get("enclosures", []) or []:
        url = item.get("href") or item.get("url")
        if url and item.get("type", "").startswith("image/"):
            return url
    return None

def _normalise_url(url, base_url):
    if not url:
        return None
    url = url.strip()
    if url.startswith("//"):
        return "https:" + url
    return urljoin(base_url, url)

def _image_is_bad(url, alt="", title=""):
    value = f"{url} {alt} {title}".lower()
    return any(marker in value for marker in _BAD_IMAGE_MARKERS)

def _valid_image_url(url):
    if not url or not url.startswith(("http://", "https://")):
        return False
    try:
        response = requests.get(
            url, headers=HEADERS, timeout=10, stream=True, allow_redirects=True
        )
        content_type = response.headers.get("Content-Type", "").lower()
        response.close()
        return content_type.startswith("image/") and "svg" not in content_type
    except requests.RequestException:
        return False

def _jsonld_objects(soup):
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        objects = data if isinstance(data, list) else [data]
        for obj in objects:
            if isinstance(obj, dict) and isinstance(obj.get("@graph"), list):
                yield from obj["@graph"]
            else:
                yield obj

def _extract_jsonld_article(soup):
    best = ""
    for obj in _jsonld_objects(soup):
        if not isinstance(obj, dict):
            continue
        types = obj.get("@type", [])
        types = types if isinstance(types, list) else [types]
        if not any(str(t).lower() in {"article", "newsarticle", "reportage"} for t in types):
            continue
        body = obj.get("articleBody")
        if isinstance(body, str) and len(body.strip()) > len(best):
            best = body.strip()
    return best

def _clean_article_soup(soup):
    for selector in _REMOVE_SELECTORS:
        for node in soup.select(selector):
            node.decompose()

def _container_score(node):
    paragraphs = [
        p.get_text(" ", strip=True)
        for p in node.find_all("p")
    ]
    useful = [p for p in paragraphs if len(p) >= 35]
    text_len = sum(len(p) for p in useful)
    identity = (
        f"{node.name} {node.get('id', '')} "
        f"{' '.join(node.get('class', []))}"
    ).lower()

    score = text_len + len(useful) * 180
    if any(x in identity for x in ("article", "story", "content", "body", "main")):
        score += 900
    if any(x in identity for x in ("sidebar", "related", "comment", "footer", "nav", "advert")):
        score -= 1800
    return score, text_len

def _paragraph_text(container):
    result = []
    seen = set()

    for p in container.find_all("p"):
        text = re.sub(r"\s+", " ", p.get_text(" ", strip=True)).strip()
        if len(text) < 35:
            continue

        key = re.sub(r"\W+", "", text.lower())
        if key in seen:
            continue
        seen.add(key)

        lowered = text.lower()
        if any(marker in lowered for marker in _BAD_TEXT_MARKERS):
            continue

        result.append(text)

    return "\n\n".join(result)

def _trim_text(text, max_chars):
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) <= max_chars:
        return text

    cut = text[:max_chars]
    sentence = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if sentence >= int(max_chars * 0.65):
        return cut[:sentence + 1].strip()
    return cut.rsplit(" ", 1)[0].rstrip(" ,;:") + "…"

def extract_article_text(article_url: str, max_chars=None):
    if not article_url:
        return ""
    max_chars = max_chars or ARTICLE_MAX_CHARS

    try:
        response = requests.get(article_url, headers=HEADERS, timeout=15)
        response.raise_for_status()
    except requests.RequestException as e:
        logger.warning(f"Не удалось загрузить статью {article_url}: {e}")
        return ""

    soup = BeautifulSoup(response.text, "html.parser")

    # First choice: structured articleBody.
    structured = _extract_jsonld_article(soup)
    if structured:
        return _trim_text(structured, max_chars)

    # Second choice: semantic article containers.
    _clean_article_soup(soup)
    candidates = []
    for selector in (
        "article",
        "[itemprop='articleBody']",
        "[class*='article-body']",
        "[class*='articleBody']",
        "[class*='article-content']",
        "[class*='story-body']",
        "[class*='story-content']",
        "main",
    ):
        for node in soup.select(selector):
            score, length = _container_score(node)
            if length:
                candidates.append((score, node))

    # Last HTML fallback: largest meaningful div/section.
    if not candidates:
        for node in soup.find_all(["div", "section"]):
            score, length = _container_score(node)
            if length >= 200:
                candidates.append((score, node))

    if candidates:
        candidates.sort(key=lambda item: item[0], reverse=True)
        text = _paragraph_text(candidates[0][1])
        if text:
            return _trim_text(text, max_chars)

    return ""

def extract_image_from_article(article_url: str):
    if not article_url:
        return None

    try:
        response = requests.get(article_url, headers=HEADERS, timeout=15)
        response.raise_for_status()
    except requests.RequestException as e:
        logger.warning(f"Не удалось открыть страницу для поиска фото: {e}")
        return None

    soup = BeautifulSoup(response.text, "html.parser")

    # 1. OpenGraph/Twitter.
    for attrs in (
        {"property": "og:image"},
        {"property": "og:image:url"},
        {"name": "twitter:image"},
        {"name": "twitter:image:src"},
    ):
        node = soup.find("meta", attrs=attrs)
        if node and node.get("content"):
            url = _normalise_url(node["content"], article_url)
            if url and not _image_is_bad(url) and _valid_image_url(url):
                return url

    # 2. JSON-LD image.
    for obj in _jsonld_objects(soup):
        if not isinstance(obj, dict):
            continue
        image = obj.get("image")
        values = image if isinstance(image, list) else [image]
        for item in values:
            url = item.get("url") if isinstance(item, dict) else item
            url = _normalise_url(url, article_url)
            if url and not _image_is_bad(url) and _valid_image_url(url):
                return url

    # 3. Large article images.
    candidates = []
    for img in soup.find_all("img"):
        src = (
            img.get("src")
            or img.get("data-src")
            or img.get("data-lazy-src")
            or img.get("data-original")
        )
        srcset = img.get("srcset") or img.get("data-srcset")
        if srcset:
            src = srcset.split(",")[-1].strip().split(" ")[0]

        url = _normalise_url(src, article_url)
        if not url or _image_is_bad(url, img.get("alt", ""), img.get("title", "")):
            continue

        try:
            width = int(img.get("width", 0) or 0)
            height = int(img.get("height", 0) or 0)
        except ValueError:
            width = height = 0

        score = width * height
        if img.parent and img.parent.name in {"figure", "picture"}:
            score += 500000
        candidates.append((score, url))

    candidates.sort(reverse=True)
    for _, url in candidates[:15]:
        if _valid_image_url(url):
            return url
    return None

def search_wikipedia_person_photo(name):
    if not name:
        return None
    try:
        response = requests.get(
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
        response.raise_for_status()
        pages = response.json().get("query", {}).get("pages", {})
        for page in pages.values():
            thumb = page.get("thumbnail")
            if thumb and _valid_image_url(thumb.get("source")):
                return thumb["source"]
    except requests.RequestException as e:
        logger.warning(f"Wikipedia photo search failed: {e}")
    return None

def search_wikimedia_image(query):
    if not query:
        return None
    try:
        response = requests.get(
            "https://commons.wikimedia.org/w/api.php",
            params={
                "action": "query",
                "generator": "search",
                "gsrsearch": query,
                "gsrnamespace": 6,
                "gsrlimit": 10,
                "prop": "imageinfo",
                "iiprop": "url",
                "iiurlwidth": 1200,
                "format": "json",
            },
            headers=HEADERS,
            timeout=10,
        )
        response.raise_for_status()
        pages = response.json().get("query", {}).get("pages", {})
        for page in pages.values():
            title = page.get("title", "").lower()
            if any(x in title for x in _BAD_IMAGE_MARKERS):
                continue
            info = page.get("imageinfo") or []
            if not info:
                continue
            url = info[0].get("thumburl") or info[0].get("url")
            if _valid_image_url(url):
                return url
    except requests.RequestException as e:
        logger.warning(f"Wikimedia photo search failed: {e}")
    return None

def search_unsplash_image(query):
    if not UNSPLASH_ACCESS_KEY or not query:
        return None
    try:
        response = requests.get(
            "https://api.unsplash.com/search/photos",
            params={"query": query, "per_page": 5, "orientation": "landscape"},
            headers={"Authorization": f"Client-ID {UNSPLASH_ACCESS_KEY}"},
            timeout=10,
        )
        response.raise_for_status()
        for photo in response.json().get("results", []):
            url = photo.get("urls", {}).get("regular")
            if url and _valid_image_url(url):
                return url
    except requests.RequestException as e:
        logger.warning(f"Unsplash search failed: {e}")
    return None

def search_fallback_image(query, person_name=""):
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

def _download_image(url, label="news"):
    if not url or not DOWNLOAD_IMAGES_LOCALLY:
        return None

    os.makedirs(MEDIA_DIR, exist_ok=True)
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        ext = ".jpg"

    path = os.path.join(MEDIA_DIR, f"news_{digest}{ext}")
    if os.path.exists(path) and os.path.getsize(path) > 10000:
        return path

    try:
        response = requests.get(
            url, headers=HEADERS, timeout=20, allow_redirects=True
        )
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "").lower()
        if not content_type.startswith("image/") or "svg" in content_type:
            return None
        if len(response.content) < 10000:
            return None

        with open(path, "wb") as file:
            file.write(response.content)
        return path
    except requests.RequestException as e:
        logger.warning(f"Не удалось скачать фото {label}: {e}")
        return None

def translate_text(text):
    if not text or not TRANSLATE_TO:
        return text
    try:
        translator = GoogleTranslator(source="auto", target=TRANSLATE_TO)
        if len(text) <= 4500:
            return translator.translate(text)

        parts = re.split(r"(?<=[.!?])\s+", text)
        chunks, current = [], ""
        for part in parts:
            if not part:
                continue
            if len(current) + len(part) + 1 <= 4500:
                current += (" " if current else "") + part
            else:
                if current:
                    chunks.append(translator.translate(current))
                current = part
        if current:
            chunks.append(translator.translate(current))
        return " ".join(chunks)
    except Exception as e:
        logger.warning(f"Не удалось перевести текст: {e}")
        return text

def _club_names():
    names = []
    for label, _ in FEEDS:
        if "—" in label:
            name = label.split("—")[0].strip()
            if name and "целом" not in name.lower():
                names.append(name)
    return names

CLUB_NAMES = _club_names()

def find_club_mention(text):
    lowered = text.lower()
    for club in CLUB_NAMES:
        if club.lower() in lowered:
            return club
    return None

def find_player_name(text):
    candidates = re.findall(r"\b[A-Z][a-zA-Z'-]+ [A-Z][a-zA-Z'-]+\b", text)
    stop = {
        "The", "This", "That", "After", "Before", "During", "According",
        "However", "Meanwhile", "Following", "Despite",
    }
    for candidate in candidates:
        if candidate.split()[0] in stop:
            continue
        return candidate
    return None

def build_entry_content(entry, article_url, title):
    title_en = (entry.get("title") or title or "").strip()
    rss_summary = clean_html(entry.get("summary", ""))

    article_text = extract_article_text(article_url) if article_url else ""
    # Real article text wins. RSS is fallback only.
    body_en = article_text if len(article_text) >= 120 else rss_summary

    player = find_player_name(f"{title_en} {body_en[:700]}")
    club = find_club_mention(f"{title_en} {body_en[:700]}")
    search_query = " ".join(x for x in (player, club) if x) or title_en
    broad_query = club or "football"

    # Image priority:
    # source article -> RSS -> Wikipedia/Wikimedia/Unsplash -> placeholder.
    image_candidates = []
    if USE_SOURCE_IMAGES:
        source_image = extract_image_from_article(article_url)
        if source_image:
            image_candidates.append(source_image)

        rss_image = extract_image_from_rss_entry(entry)
        if rss_image:
            image_candidates.append(rss_image)

    image_candidates.append(search_fallback_image(search_query, player))

    image_url = PLACEHOLDER_IMAGE
    for candidate in image_candidates:
        if not candidate:
            continue
        if DOWNLOAD_IMAGES_LOCALLY:
            local = _download_image(candidate, search_query)
            if local:
                image_url = local
                break
        elif _valid_image_url(candidate):
            image_url = candidate
            break

    title_ru = translate_text(title_en)
    body_ru = translate_text(body_en)
    body_ru = re.sub(r"\n{3,}", "\n\n", body_ru).strip()

    text = f"📰 {title_ru}"
    if body_ru:
        text += f"\n\n{body_ru}"

    return text.strip(), image_url
