"""Durable claim-once routing metadata; never replays an existing job."""

from dataclasses import dataclass
import sqlite3


@dataclass(frozen=True)
class JobKey:
    source: str
    scope: str
    message_id: int
    actor_id: int


class VoiceCommandJournal:
    """Persist claims before external actions, including ambiguous sends.

    All existing keys remain claimed regardless of their state. The caller owns
    state ordering and must not retry sending/uncertain jobs automatically.
    """

    _STATES = frozenset({"claimed", "deleted", "sending", "sent", "uncertain"})

    def __init__(self, db_path: str):
        self._connection = sqlite3.connect(db_path)
        try:
            self._initialize_schema()
        except BaseException:
            self._connection.close()
            raise

    def _initialize_schema(self):
        with self._connection:
            self._connection.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    source TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    actor_id INTEGER NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN ('claimed', 'deleted', 'sending', 'sent', 'uncertain')
                    ),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (source, scope, message_id, actor_id)
                )
            """)

    def claim(self, key: JobKey) -> bool:
        with self._connection:
            result = self._connection.execute("""
                INSERT INTO jobs (source, scope, message_id, actor_id, state)
                VALUES (?, ?, ?, ?, 'claimed')
                ON CONFLICT (source, scope, message_id, actor_id) DO NOTHING
            """, (key.source, key.scope, key.message_id, key.actor_id))
        return result.rowcount == 1

    def is_claimed(self, key: JobKey) -> bool:
        """Skip repeated read-only business lookups for already claimed updates."""
        row = self._connection.execute("""
            SELECT 1 FROM jobs
            WHERE source = ? AND scope = ? AND message_id = ? AND actor_id = ?
        """, (key.source, key.scope, key.message_id, key.actor_id)).fetchone()
        return row is not None

    def set_state(self, key: JobKey, state: str) -> None:
        if state not in self._STATES:
            raise ValueError("invalid journal state")
        with self._connection:
            self._connection.execute("""
                UPDATE jobs SET state = ?, updated_at = CURRENT_TIMESTAMP
                WHERE source = ? AND scope = ? AND message_id = ? AND actor_id = ?
            """, (state, key.source, key.scope, key.message_id, key.actor_id))

    def close(self) -> None:
        self._connection.close()
