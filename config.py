import os
from dotenv import load_dotenv

load_dotenv()

# --- Обязательные переменные (задаются в .env, см. .env.example) ---
BOT_TOKEN = os.getenv("BOT_TOKEN")            # токен бота от @BotFather
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))    # твой Telegram user_id — куда слать черновики

# CHANNEL_ID больше не используется напрямую — каналы для публикации теперь
# добавляются и удаляются прямо в боте через кнопку "📋 Мои каналы"
# (хранятся в базе данных). Переменную можно оставить пустой.

# Необязательно: ключ Unsplash для поиска фото, если на странице новости
# не нашлось подходящей картинки (og:image). Если не задан — используется
# первая крупная картинка со страницы или плейсхолдер.
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY", "")

# Язык, на который автоматически переводится заголовок и текст новости
# перед отправкой на модерацию. "ru" — русский. Поставь пустую строку "",
# чтобы отключить перевод и получать новости в оригинале.
TRANSLATE_TO = "ru"

# --- Список источников новостей ---
# Формат: ("Название источника", "URL RSS-ленты")
# Используются официальные RSS-ленты BBC Sport (по конкретным командам +
# общая футбольная лента), Sky Sports и The Guardian — проверил их лично,
# отдают свежие новости на сегодняшний день. Это надёжнее фан-блогов, у
# которых RSS часто отваливается/меняется. Если какая-то лента всё же
# перестанет работать, fetcher.py просто пропустит её с предупреждением
# в консоли, не уронив весь бот.
FEEDS = [
    # Общие ленты — трансферы, сборные, турниры, аналитика, не привязаны
    # к одному клубу
    ("Футбол в целом — BBC Sport", "https://feeds.bbci.co.uk/sport/football/rss.xml"),
    ("Футбол в целом — Sky Sports", "https://www.skysports.com/rss/12040"),
    ("Футбол в целом — The Guardian", "https://www.theguardian.com/football/rss"),

    # Ленты по отдельным клубам
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
# источника (в символах, до перевода). Меньше — короче и быстрее читать,
# больше — подробнее, но выше риск упереться в лимит подписи Telegram (1024).
ARTICLE_MAX_CHARS = 700

# Как часто проверять ленты на новые новости (в минутах)
POLL_INTERVAL_MINUTES = 1

# За сколько последних часов брать новости из ленты (независимо от того,
# когда бот запускался в прошлый раз). 24 — последние сутки.
LOOKBACK_HOURS = 24

# Верхняя граница на число записей с одной ленты за один проход
# (защита от обвала, если лента вдруг отдаст сотни записей разом)
MAX_ENTRIES_PER_FEED = 20

# Путь к файлу базы данных (SQLite) — хранит "уже обработанные" новости и черновики
DB_PATH = "bot_storage.db"

# Папка для скачанных фото/медиа из Telegram-каналов и Instagram
# (для RSS фото используются напрямую по URL, файл на диск не сохраняется)
MEDIA_DIR = "media"

# --- Мониторинг чужих Telegram-каналов (через юзербот-клиент Telethon) ---
# Работает через ТВОЙ личный Telegram-аккаунт (не через бота) — только так
# можно читать чужие каналы, не будучи их админом. Требует API-ключ с
# https://my.telegram.org (бесплатно, нужен твой номер телефона).
# При первом запуске бот попросит в консоли PyCharm ввести код подтверждения
# (придёт в Telegram) и, если включена двухфакторная аутентификация, пароль.
# После этого создаётся файл сессии — повторного логина не потребуется.
TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID")) if os.getenv("TELEGRAM_API_ID") else None
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH") or None
TELEGRAM_PHONE = os.getenv("TELEGRAM_PHONE") or None

# Username каналов для мониторинга, БЕЗ @, например: ["fabrizioromano", "someclub_channel"]
# Если список пуст — мониторинг чужих каналов просто не запускается.
TELEGRAM_CHANNELS_TO_MONITOR = [
    # "some_channel_username",
]

# --- Мониторинг Instagram (через instaloader) ---
# ВАЖНО: нестабильный источник. У Instagram нет официального публичного API
# для чтения чужих аккаунтов — instaloader эмулирует браузер, и Instagram
# может в любой момент начать требовать логин, показывать капчу или
# банить IP без предупреждения. Если аккаунт перестанет отдавать посты —
# это ограничение самого Instagram, а не баг бота.
# Username аккаунтов для мониторинга, БЕЗ @, например: ["realmadrid", "fcbarcelona"]
INSTAGRAM_ACCOUNTS_TO_MONITOR = [
    # "some_instagram_username",
]
