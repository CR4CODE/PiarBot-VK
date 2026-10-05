# db.py — работа с SQLite для PiarBot VK
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

DB_PATH = Path(__file__).parent / "data" / "piar.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

_local = threading.local()


def _conn() -> sqlite3.Connection:
    """Соединение на поток (thread-local)."""
    c = getattr(_local, "conn", None)
    if c is None:
        c = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        _local.conn = c
    return c


# ---------- низкоуровневые хелперы ----------

def execute(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    """Выполнить SQL. Внутри transaction() коммит откладывается."""
    cur = _conn().execute(sql, params)
    if not getattr(_local, "in_tx", False):
        _conn().commit()
    return cur


def query(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    return _conn().execute(sql, params).fetchall()


def query_one(sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
    return _conn().execute(sql, params).fetchone()


@contextmanager
def transaction():
    """Контекстный менеджер для атомарных операций.

    Внутри можно вызывать db.execute() — коммит произойдёт на выходе.
    Вложенные вызовы переиспользуют внешнюю транзакцию.
    """
    if getattr(_local, "in_tx", False):
        yield
        return
    conn = _conn()
    _local.in_tx = True
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _local.in_tx = False


# ---------- инициализация схемы ----------

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id       INTEGER PRIMARY KEY,
    first_name    TEXT,
    last_name     TEXT,
    balance       INTEGER DEFAULT 0,
    rounds        INTEGER DEFAULT 0,
    referral_code TEXT UNIQUE,
    referred_by   INTEGER,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_banned     INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS chats (
    peer_id       INTEGER PRIMARY KEY,
    chat_id       INTEGER,
    title         TEXT,
    admin_id      INTEGER,
    pending       INTEGER DEFAULT 0,
    list_size     INTEGER DEFAULT 5,
    invite_link   TEXT,
    registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_active     INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS channels (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    peer_id       INTEGER NOT NULL,
    user_id       INTEGER NOT NULL,
    resource_id   INTEGER NOT NULL,
    resource_type TEXT NOT NULL,
    screen_name   TEXT,
    title         TEXT,
    status        TEXT DEFAULT 'pending',
    position      INTEGER DEFAULT 0,
    added_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(peer_id, user_id)
);

CREATE TABLE IF NOT EXISTS done_users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    peer_id       INTEGER NOT NULL,
    user_id       INTEGER NOT NULL,
    resource_id   INTEGER NOT NULL,
    owner_id      INTEGER NOT NULL,
    round_number  INTEGER NOT NULL,
    completed_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(peer_id, user_id, resource_id, round_number)
);

CREATE TABLE IF NOT EXISTS referrals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    referrer_id INTEGER NOT NULL,
    referred_id INTEGER NOT NULL UNIQUE,
    bonus_paid  INTEGER DEFAULT 0,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS referral_milestones (
    referrer_id INTEGER NOT NULL,
    milestone   INTEGER NOT NULL,
    bonus       INTEGER NOT NULL,
    paid_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (referrer_id, milestone)
);

CREATE TABLE IF NOT EXISTS chat_add_rewards (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    peer_id    INTEGER NOT NULL,
    amount     INTEGER NOT NULL,
    awarded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS chat_milestones (
    user_id   INTEGER NOT NULL,
    milestone INTEGER NOT NULL,
    bonus     INTEGER NOT NULL,
    paid_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, milestone)
);

CREATE TABLE IF NOT EXISTS config (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS transfers (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    from_id    INTEGER NOT NULL,
    to_id      INTEGER NOT NULL,
    amount     INTEGER NOT NULL,
    comment    TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS rewards (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    peer_id      INTEGER,
    amount       INTEGER NOT NULL,
    round_number INTEGER,
    reason       TEXT,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS payments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    amount       INTEGER NOT NULL,
    price_rub    REAL,
    method       TEXT,
    status       TEXT DEFAULT 'pending',
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS promo_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    peer_id    INTEGER NOT NULL,
    message_id INTEGER,
    sent_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS chat_members (
    peer_id    INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (peer_id, user_id)
);

CREATE TABLE IF NOT EXISTS news_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    message_id INTEGER,
    status     TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS reminders_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    message_id INTEGER,
    sent_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_reminders_user ON reminders_log(user_id);

CREATE TABLE IF NOT EXISTS ad_bots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER UNIQUE,
    name          TEXT,
    token         TEXT NOT NULL,
    resource_id   INTEGER,
    screen_name   TEXT,
    title         TEXT,
    status        TEXT DEFAULT 'active',
    notes         TEXT,
    last_check    TIMESTAMP,
    last_post     TIMESTAMP,
    posts_ok      INTEGER DEFAULT 0,
    posts_fail    INTEGER DEFAULT 0,
    added_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ad_targets (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id      INTEGER NOT NULL,
    screen_name   TEXT,
    title         TEXT,
    method        TEXT DEFAULT 'wall',
    status        TEXT DEFAULT 'active',
    fails         INTEGER DEFAULT 0,
    last_post     TIMESTAMP,
    added_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(owner_id)
);

CREATE TABLE IF NOT EXISTS ad_posts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id        INTEGER NOT NULL,
    target_id     INTEGER,
    message       TEXT,
    status        TEXT,
    error         TEXT,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_ad_bots_status ON ad_bots(status);
CREATE INDEX IF NOT EXISTS idx_ad_targets_status ON ad_targets(status);
CREATE INDEX IF NOT EXISTS idx_ad_posts_bot ON ad_posts(bot_id);

CREATE TABLE IF NOT EXISTS imitation_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id      INTEGER NOT NULL,
    action      TEXT NOT NULL,
    target_id   INTEGER,
    details     TEXT,
    status      TEXT,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_imitation_bot ON imitation_log(bot_id);
CREATE INDEX IF NOT EXISTS idx_imitation_created ON imitation_log(created_at);

CREATE INDEX IF NOT EXISTS idx_channels_peer ON channels(peer_id);
CREATE INDEX IF NOT EXISTS idx_channels_user ON channels(user_id);
CREATE INDEX IF NOT EXISTS idx_done_peer ON done_users(peer_id, round_number);
CREATE INDEX IF NOT EXISTS idx_rewards_user ON rewards(user_id);
CREATE INDEX IF NOT EXISTS idx_promo_peer ON promo_log(peer_id);
CREATE INDEX IF NOT EXISTS idx_chat_members_peer ON chat_members(peer_id);
"""

DEFAULT_CONFIG = {
    "owner_id": "0",
    "promo_enabled": "0",
    "promo_interval": "60",
    "promo_text": "",
    "last_promo_sent": "",
    "bot_channel_url": "",
    "main_chat_url": "",
    "main_chat_invite": "https://vk.me/join/re1zQlmvrIVl9YvxoXkAoht_9fm52YA10_8=",
    "bot_name": "PiarBot",
    "signup_bonus": "500",
    "daily_bonus": "100",
    "wall_post_enabled": "0",
    "ad_enabled": "0",
    "imitation_enabled": "0",
    "imitation_subscribe_interval": "180",
    "imitation_round_interval": "600",
    "ad_interval_min": "45",
    "ad_interval_max": "90",
    "ad_message": "",
    "ad_targets_refresh": "0",
    "reminder_enabled": "1",
    "reminder_days": "7",
    "reminder_cooldown": "14",
    "wall_post_interval": "360",
    "wall_post_template": "",
    "last_wall_post": "",
}


def init_db() -> None:
    conn = _conn()
    conn.executescript(SCHEMA)
    for k, v in DEFAULT_CONFIG.items():
        conn.execute(
            "INSERT OR IGNORE INTO config(key, value) VALUES (?, ?)", (k, v)
        )
    conn.commit()


# ---------- высокоуровневые хелперы ----------

def get_config(key: str, default: str = "") -> str:
    row = query_one("SELECT value FROM config WHERE key=?", (key,))
    return row["value"] if row else default


def set_config(key: str, value: Any) -> None:
    execute(
        "INSERT INTO config(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def _migrate_ad_bots():
    """Добавляет поля имитации если их ещё нет."""
    cols = {
        "can_subscribe": "INTEGER DEFAULT 1",
        "can_join_round": "INTEGER DEFAULT 1",
        "active_hours_from": "INTEGER DEFAULT 9",
        "active_hours_to": "INTEGER DEFAULT 23",
        "actions_today": "INTEGER DEFAULT 0",
        "last_action_date": "TEXT",
    }
    existing = {r["name"] for r in query("PRAGMA table_info(ad_bots)")}
    for name, typedef in cols.items():
        if name not in existing:
            try:
                execute(f"ALTER TABLE ad_bots ADD COLUMN {name} {typedef}")
            except Exception:
                pass


def get_user(user_id: int) -> Optional[sqlite3.Row]:
    return query_one("SELECT * FROM users WHERE user_id=?", (user_id,))


def add_balance(user_id: int, amount: int, reason: str = "",
                peer_id: Optional[int] = None,
                round_number: Optional[int] = None) -> None:
    execute("UPDATE users SET balance = balance + ? WHERE user_id=?",
            (amount, user_id))
    execute(
        "INSERT INTO rewards(user_id, peer_id, amount, round_number, reason) "
        "VALUES (?, ?, ?, ?, ?)",
        (user_id, peer_id, amount, round_number, reason),
    )


# ---------- самопроверка ----------

if __name__ == "__main__":
    init_db()
    tables = query(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    print(f"✅ БД готова: {DB_PATH}")
    print(f"📋 Таблиц: {len(tables)}")
    for t in tables:
        print("  •", t["name"])
    print("\n⚙️  config:")
    for row in query("SELECT key, value FROM config ORDER BY key"):
        print(f"  {row['key']} = {row['value']}")
