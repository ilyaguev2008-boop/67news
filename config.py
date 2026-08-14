import os
from dotenv import load_dotenv

load_dotenv()

# --- Обязательные переменные (задаются в .env, см. .env.example) ---
BOT_TOKEN = os.getenv("BOT_TOKEN")            # токен бота от @BotFather
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))    # твой Telegram user_id — куда слать черновики

# CHANNEL_ID больше не используется напрямую — каналы для публикации теперь
# добавляются и удаляются прямо в боте через кнопку "📋 Мои каналы"
# (хранятся в базе данных). Переменную можно оставить пустой.

# Необязательно: ключ Unsplash для поиска фото по теме новости (запасной
# вариант, если ничего не нашлось на Wikipedia/Wikimedia Commons).
# Получить бесплатно на https://unsplash.com/developers
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY", "")

# Язык, на который автоматически переводится заголовок и текст новости
# перед отправкой на модерацию. "ru" — русский. Поставь пустую строку "",
# чтобы отключить перевод и получать новости в оригинале.
TRANSLATE_TO = "ru"

# --- Список источников новостей ---
FEEDS = [
    ("Футбол в целом — BBC Sport", "https://feeds.bbci.co.uk/sport/football/rss.xml"),
    ("Футбол в целом — Sky Sports", "https://www.skysports.com/rss/12040"),
    ("Футбол в целом — The Guardian", "https://www.theguardian.com/football/rss"),

    ("Real Madrid — BBC Sport", "https://feeds.bbci.co.uk/sport/football/teams/real-madrid/rss.xml"),
    ("Manchester United — BBC Sport", "https://feeds.bbci.co.uk/sport/football/teams/manchester-united/rss.xml"),
    ("Manchester City — BBC Sport", "https://feeds.bbci.co.uk/sport/football/teams/manchester-city/rss.xml"),
    ("Liverpool — BBC Sport", "https://feeds.bbci.co.uk/sport/football/teams/liverpool/rss.xml"),
    ("Tottenham Hotspur — BBC Sport", "https://feeds.bbci.co.uk/sport/football/teams/tottenham-hotspur/rss.xml"),
    ("Newcastle United — BBC Sport", "https://feeds.bbci.co.uk/sport/football/teams/newcastle-united/rss.xml"),
    ("Aston Villa — BBC Sport", "https://feeds.bbci.co.uk/sport/football/teams/aston-villa/rss.xml"),
    ("West Ham United — BBC Sport", "https://feeds.bbci.co.uk/sport/football/teams/west-ham-united/rss.xml"),
    ("Everton — BBC Sport", "https://feeds.bbci.co.uk/sport/football/teams/everton/rss.xml"),
    ("Brighton — BBC Sport", "https://feeds.bbci.co.uk/sport/football/teams/brighton-and-hove-albion/rss.xml"),
    ("PSG — BBC Sport", "https://feeds.bbci.co.uk/sport/football/teams/paris-saint-germain/rss.xml"),
    ("Barcelona — BBC Sport", "https://feeds.bbci.co.uk/sport/football/teams/barcelona/rss.xml"),
    ("Atletico Madrid — BBC Sport", "https://feeds.bbci.co.uk/sport/football/teams/atletico-madrid/rss.xml"),
    ("Chelsea — BBC Sport", "https://feeds.bbci.co.uk/sport/football/teams/chelsea/rss.xml"),
    ("Arsenal — BBC Sport", "https://feeds.bbci.co.uk/sport/football/teams/arsenal/rss.xml"),
    ("Bayern Munich — BBC Sport", "https://feeds.bbci.co.uk/sport/football/teams/bayern-munich/rss.xml"),
    ("Juventus — BBC Sport", "https://feeds.bbci.co.uk/sport/football/teams/juventus/rss.xml"),
    ("Napoli — BBC Sport", "https://feeds.bbci.co.uk/sport/football/teams/napoli/rss.xml"),
    ("Inter Milan — BBC Sport", "https://feeds.bbci.co.uk/sport/football/teams/inter-milan/rss.xml"),
    ("AC Milan — BBC Sport", "https://feeds.bbci.co.uk/sport/football/teams/ac-milan/rss.xml"),
]

# Максимальная длина текста статьи, который бот берёт со страницы
# источника (в символах, до перевода).
ARTICLE_MAX_CHARS = 380

# Если False (по умолчанию) — бот НЕ берёт фото со страницы источника
# (og:image) и не берёт миниатюру из RSS-записи — у крупных изданий эти
# фото почти всегда содержат их логотип/вотермарку. Вместо этого ищет
# нейтральное фото по теме в интернете.
USE_SOURCE_IMAGES = False

# Как часто проверять ленты на новые новости (в минутах)
POLL_INTERVAL_MINUTES = 5

# За сколько последних часов брать новости из ленты
LOOKBACK_HOURS = 24

# Верхняя граница на число записей с одной ленты за один проход
MAX_ENTRIES_PER_FEED = 20

# Путь к файлу базы данных (SQLite)
DB_PATH = "bot_storage.db"

# Папка для скачанных фото/медиа из Telegram-каналов и Instagram
MEDIA_DIR = "media"

# Локальный файл-заглушка (лежит прямо в проекте, не ссылка в интернете).
# Используется, когда не удалось найти/отправить подходящее фото к
# новости — гарантированно прикрепляется к посту.
LOCAL_PLACEHOLDER_PATH = "media/placeholder.jpg"

# --- Мониторинг чужих Telegram-каналов (через юзербот-клиент Telethon) ---
TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID")) if os.getenv("TELEGRAM_API_ID") else None
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH") or None
TELEGRAM_PHONE = os.getenv("TELEGRAM_PHONE") or None

TELEGRAM_CHANNELS_TO_MONITOR = [
    # "some_channel_username",
]

# --- Мониторинг Instagram (через instaloader, нестабильный источник) ---
INSTAGRAM_ACCOUNTS_TO_MONITOR = [
    # "some_instagram_username",
]
