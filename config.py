# config.py — загрузка .env и константы проекта
import os
from pathlib import Path

ENV_PATH = Path(__file__).parent / ".env"


def _load_env() -> None:
    if not ENV_PATH.exists():
        raise RuntimeError(f".env не найден: {ENV_PATH}")
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()

VK_GROUP_TOKEN = os.environ["VK_GROUP_TOKEN"]
VK_GROUP_ID = int(os.environ["VK_GROUP_ID"])

# ---------- награды и лимиты ----------
LIST_SIZE_DEFAULT = 5            # размер активного списка
ROUND_BASE_REWARD = 100          # PP за 1-й раунд
ROUND_GROWTH = 0.60              # +60% за каждый следующий
REFERRAL_BONUS = 1000            # PP за друга
CHAT_ADD_BONUS = 10000           # PP за подключение беседы
CHAT_ADD_DAILY_LIMIT = 3         # бесед в день за бонус
SIGNUP_BONUS = 500               # PP за регистрацию
DAILY_BONUS = 100                # PP за ежедневный вход

# ---------- платное добавление в очередь ----------
SKIP_ADD_PRICE = 1500            # PP за пропуск проверки подписок
PENDING_ADD_TTL = 600            # сек, сколько хранится отложенная заявка

REFERRAL_MILESTONES = {5: 5000, 10: 15000, 25: 50000, 50: 150000}
CHAT_MILESTONES = {1: 10000, 3: 50000, 5: 100000, 10: 300000}

# ---------- таймауты ----------
WARN_DELETE_SECONDS = 7          # удаление предупреждений
GREETING_LIFETIME = 600          # приветствие новичка — 10 мин
PROMO_DELETE_SECONDS = 600       # удаление промо — 10 мин

# ---------- VK ----------
API_VERSION = "5.199"
BOT_LINK = f"https://vk.me/{(os.environ.get('VK_GROUP_SCREEN') or 'go_gigi')}"
