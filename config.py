import os
from dotenv import load_dotenv

load_dotenv()

# --- Обязательные переменные (задаются в .env, см. .env.example) ---
BOT_TOKEN = os.getenv("BOT_TOKEN")            # токен бота от @BotFather
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))    # твой Telegram user_id — куда слать черновики
CHANNEL_ID = os.getenv("CHANNEL_ID")          # @username канала или -100xxxxxxxxxx

# Необязательно: ключ Unsplash для поиска фото, если на странице новости
# не нашлось подходящей картинки (og:image). Если не задан — используется
# первая крупная картинка со страницы или плейсхолдер.
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY", "")

# Язык, на который автоматически переводится заголовок и текст новости
# перед отправкой на модерацию. "ru" — русский. Поставь пустую строку "",
# чтобы отключить перевод и получать новости в оригинале.
TRANSLATE_TO = "ru"

# --- Список источников новостей (RSS-ленты фан-сайтов клубов) ---
# Формат: ("Название источника", "URL RSS-ленты")
# Это независимые (не официальные) фан-сайты/блоги — часть сети SB Nation
# и другие крупные англоязычные фан-ресурсы. Ссылки собраны из открытых
# каталогов RSS-лент; если какая-то лента перестанет работать (сайты
# иногда меняют структуру), fetcher.py просто пропустит её с предупреждением
# в консоли, не уронив весь бот — замени нерабочую ссылку на альтернативу.
FEEDS = [
    ("Real Madrid — Managing Madrid", "https://www.managingmadrid.com/rss/current.xml"),
    ("Manchester United — The Busby Babe", "https://www.thebusbybabe.com/rss/current.xml"),
    ("Manchester City — Man City News", "https://www.mancitynews.com/feed"),
    ("PSG — PSG Talk", "https://psgtalk.com/feed"),
    ("Barcelona — Barca Universal", "https://barcauniversal.com/feed"),
    ("Atletico Madrid — Into The Calderon", "https://www.intothecalderon.com/rss/current.xml"),
    ("Chelsea — Talk Chelsea", "https://talkchelsea.net/feed"),
    ("Arsenal — Arseblog", "https://arseblog.com/feed"),
    ("Bayern Munich — Miasanrot", "https://miasanrot.com/feed"),
    ("Inter & Milan — Football Italia", "https://football-italia.net/feed"),
]

# Как часто проверять ленты на новые новости (в минутах)
POLL_INTERVAL_MINUTES = 10

# Сколько последних записей из каждой ленты рассматривать за один проход
# (защита от обвала стартового большого списка при первом запуске)
MAX_ENTRIES_PER_FEED = 5

# Путь к файлу базы данных (SQLite) — хранит "уже обработанные" новости и черновики
DB_PATH = "bot_storage.db"
