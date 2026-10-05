# db_migrations.py — простые миграции схемы БД.
# Применяются при старте бота, до всего остального.
import logging
import db

log = logging.getLogger("migrations")

# При добавлении новой версии — НЕ менять существующие, только дописывать.
MIGRATIONS = {
    1: [
        """CREATE TABLE IF NOT EXISTS pending_deletes (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            peer_id    INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            delete_at  TIMESTAMP NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_pending_deletes_at ON pending_deletes(delete_at)",
        "CREATE INDEX IF NOT EXISTS idx_done_users_peer ON done_users(peer_id)",
        "CREATE INDEX IF NOT EXISTS idx_done_users_user ON done_users(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_channels_peer ON channels(peer_id)",
        "CREATE INDEX IF NOT EXISTS idx_channels_user ON channels(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_transfers_from ON transfers(from_id)",
        "CREATE INDEX IF NOT EXISTS idx_transfers_to ON transfers(to_id)",
        "CREATE INDEX IF NOT EXISTS idx_rewards_user ON rewards(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_id)",
    ],
    2: [
        "CREATE INDEX IF NOT EXISTS idx_users_balance ON users(balance DESC)",
    ],
    4: [
        """CREATE TABLE IF NOT EXISTS release_notes (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            title         TEXT NOT NULL,
            body          TEXT NOT NULL,
            created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            wall_post_id  INTEGER,
            wall_at       TIMESTAMP,
            chats_ok      INTEGER,
            chats_fail    INTEGER,
            chats_at      TIMESTAMP
        )""",
        "CREATE INDEX IF NOT EXISTS idx_release_notes_created ON release_notes(created_at DESC)",
    ],
    3: [
        """CREATE TABLE IF NOT EXISTS pending_adds (
            user_id       INTEGER NOT NULL,
            peer_id       INTEGER NOT NULL,
            resource_id   INTEGER NOT NULL,
            resource_type TEXT,
            screen_name   TEXT,
            title         TEXT,
            created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, peer_id)
        )""",
    ],
}


def _current_version() -> int:
    try:
        v = db.get_config("schema_version", "0")
        return int(v or 0)
    except (ValueError, TypeError):
        return 0


def apply() -> None:
    """Применить все недостающие миграции. Идемпотентно."""
    current = _current_version()
    latest = max(MIGRATIONS.keys()) if MIGRATIONS else 0

    if current >= latest:
        log.info("схема актуальна (v%s)", current)
        return

    log.info("миграции: v%s -> v%s", current, latest)
    for version in sorted(MIGRATIONS):
        if version <= current:
            continue
        log.info("применяю миграцию v%s", version)
        for sql in MIGRATIONS[version]:
            try:
                db.execute(sql)
            except Exception as e:
                log.error("миграция v%s не удалась: %s", version, e)
                raise
        db.set_config("schema_version", str(version))

    log.info("схема обновлена до v%s", latest)
