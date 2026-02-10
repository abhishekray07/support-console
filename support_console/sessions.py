"""Session persistence using SQLite (aiosqlite)."""

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiosqlite

logger = logging.getLogger(__name__)

_CREATE_TABLE_SQL = """\
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    title TEXT,
    chat_messages JSON,
    notebook_cells JSON,
    metadata JSON
);
"""

_CREATE_INDEX_SQL = """\
CREATE INDEX IF NOT EXISTS idx_sessions_created_at ON sessions (created_at DESC);
"""


def _serialize_json(value) -> Optional[str]:
    """Serialize a Python object to a JSON string for storage.

    Returns None if the input is None, allowing SQLite to store NULL.
    """
    if value is None:
        return None
    return json.dumps(value, default=str)


def _deserialize_json(text: Optional[str]):
    """Deserialize a JSON string from storage back to a Python object.

    Returns None if the input is None (SQLite NULL).
    """
    if text is None:
        return None
    return json.loads(text)


def _row_to_session(row: aiosqlite.Row) -> dict:
    """Convert a database row to a session dict with deserialized JSON fields."""
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "title": row["title"],
        "chat_messages": _deserialize_json(row["chat_messages"]),
        "notebook_cells": _deserialize_json(row["notebook_cells"]),
        "metadata": _deserialize_json(row["metadata"]),
    }


def _row_to_summary(row: aiosqlite.Row) -> dict:
    """Convert a database row to a session summary (no message/cell data)."""
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "title": row["title"],
        "metadata": _deserialize_json(row["metadata"]),
    }


class SessionStore:
    """Manages session persistence in SQLite.

    Usage::

        store = SessionStore("/data/sessions.db")
        await store.initialize()
        try:
            session = await store.create_session(title="Debug session")
            await store.save_session(session["id"], chat_messages=[...])
            loaded = await store.get_session(session["id"])
        finally:
            await store.close()

    Can also be used as an async context manager::

        async with SessionStore("/data/sessions.db") as store:
            session = await store.create_session(title="Debug session")
    """

    def __init__(self, db_path: str = "/data/sessions.db"):
        self._db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def __aenter__(self) -> "SessionStore":
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()

    async def initialize(self) -> None:
        """Open the database connection and ensure the schema exists.

        Creates the parent directory for the database file if it does not
        already exist.  This is safe to call multiple times; subsequent calls
        are no-ops if the connection is already open.
        """
        if self._db is not None:
            return

        # Ensure the directory containing the database file exists.
        db_dir = os.path.dirname(self._db_path)
        if db_dir:
            Path(db_dir).mkdir(parents=True, exist_ok=True)

        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row

        # Enable WAL mode for better concurrent read performance.
        await self._db.execute("PRAGMA journal_mode=WAL;")

        await self._db.execute(_CREATE_TABLE_SQL)
        await self._db.execute(_CREATE_INDEX_SQL)
        await self._db.commit()

        logger.info("Session store initialized at %s", self._db_path)

    async def close(self) -> None:
        """Close the database connection.

        Safe to call even if the connection is already closed or was never
        opened.
        """
        if self._db is not None:
            await self._db.close()
            self._db = None
            logger.info("Session store closed.")

    def _ensure_open(self) -> aiosqlite.Connection:
        """Return the active database connection or raise if not initialized."""
        if self._db is None:
            raise RuntimeError(
                "SessionStore is not initialized. Call initialize() or use "
                "the async context manager before performing operations."
            )
        return self._db

    # ------------------------------------------------------------------
    # CRUD operations
    # ------------------------------------------------------------------

    async def create_session(
        self, title: str = "", metadata: Optional[dict] = None
    ) -> dict:
        """Create a new session and return its full dict representation.

        Parameters
        ----------
        title:
            Human-readable session title.  Defaults to an empty string.
        metadata:
            Arbitrary metadata to attach to the session.

        Returns
        -------
        dict
            The newly created session, including ``id`` and ``created_at``.
        """
        db = self._ensure_open()
        session_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        await db.execute(
            """
            INSERT INTO sessions (id, created_at, title, chat_messages, notebook_cells, metadata)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                now,
                title,
                _serialize_json([]),
                _serialize_json([]),
                _serialize_json(metadata or {}),
            ),
        )
        await db.commit()

        logger.debug("Created session %s", session_id)

        return {
            "id": session_id,
            "created_at": now,
            "title": title,
            "chat_messages": [],
            "notebook_cells": [],
            "metadata": metadata or {},
        }

    async def save_session(
        self,
        session_id: str,
        chat_messages: Optional[list] = None,
        notebook_cells: Optional[list] = None,
        title: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> None:
        """Update one or more fields of an existing session.

        Only the fields that are explicitly provided (not ``None``) will be
        updated.  This allows callers to update messages without touching the
        title, and vice-versa.

        Parameters
        ----------
        session_id:
            The UUID of the session to update.
        chat_messages:
            Full list of chat messages to persist (replaces existing).
        notebook_cells:
            Full list of notebook cells to persist (replaces existing).
        title:
            New session title.
        metadata:
            New metadata dict (replaces existing).

        Raises
        ------
        ValueError
            If no fields are provided to update.
        KeyError
            If the session does not exist.
        """
        db = self._ensure_open()

        fields: list[str] = []
        values: list = []

        if chat_messages is not None:
            fields.append("chat_messages = ?")
            values.append(_serialize_json(chat_messages))
        if notebook_cells is not None:
            fields.append("notebook_cells = ?")
            values.append(_serialize_json(notebook_cells))
        if title is not None:
            fields.append("title = ?")
            values.append(title)
        if metadata is not None:
            fields.append("metadata = ?")
            values.append(_serialize_json(metadata))

        if not fields:
            raise ValueError("save_session called with no fields to update.")

        values.append(session_id)
        sql = f"UPDATE sessions SET {', '.join(fields)} WHERE id = ?"

        cursor = await db.execute(sql, values)
        if cursor.rowcount == 0:
            raise KeyError(f"Session not found: {session_id}")

        await db.commit()
        logger.debug("Saved session %s (fields: %s)", session_id, ", ".join(fields))

    async def get_session(self, session_id: str) -> Optional[dict]:
        """Load a session by ID.

        Parameters
        ----------
        session_id:
            The UUID of the session to load.

        Returns
        -------
        dict or None
            The full session dict, or ``None`` if no session with the given
            ID exists.
        """
        db = self._ensure_open()

        cursor = await db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
        row = await cursor.fetchone()

        if row is None:
            return None

        return _row_to_session(row)

    async def list_sessions(self, limit: int = 50) -> list[dict]:
        """List sessions ordered by creation time (most recent first).

        Returns lightweight summaries without the full ``chat_messages`` and
        ``notebook_cells`` payloads to keep the response fast even when
        individual sessions contain large amounts of data.

        Parameters
        ----------
        limit:
            Maximum number of sessions to return.  Defaults to 50.

        Returns
        -------
        list[dict]
            Session summaries containing ``id``, ``title``, ``created_at``,
            and ``metadata``.
        """
        db = self._ensure_open()

        cursor = await db.execute(
            "SELECT id, created_at, title, metadata FROM sessions ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()

        return [_row_to_summary(row) for row in rows]

    async def delete_session(self, session_id: str) -> None:
        """Delete a session by ID.

        This is a no-op if the session does not exist, matching common REST
        DELETE semantics (idempotent).

        Parameters
        ----------
        session_id:
            The UUID of the session to delete.
        """
        db = self._ensure_open()

        await db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        await db.commit()

        logger.debug("Deleted session %s", session_id)
