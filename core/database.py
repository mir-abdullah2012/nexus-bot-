"""Async SQLite access layer.

One connection, WAL mode, serialised through aiosqlite's worker thread. That is
plenty for a Discord bot and it removes the read-modify-write race that the old
JSON storage had (two concurrent !give calls could silently lose one transfer).

Cogs never touch this directly -- they go through core.repository.
"""

import sqlite3
import time
from contextlib import asynccontextmanager

import aiosqlite

from core import migrations

_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA busy_timeout=5000",
)


class Database:
    def __init__(self, path):
        self.path = str(path)
        self._conn: aiosqlite.Connection | None = None

    # ---------- lifecycle ----------
    async def connect(self):
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        for pragma in _PRAGMAS:
            await self._conn.execute(pragma)
        await self._conn.commit()
        version = await self._migrate()
        print(f"[db] connected: {self.path} (schema v{version})")

    async def close(self):
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def _migrate(self) -> int:
        await self._conn.execute(migrations.BOOTSTRAP)
        async with self._conn.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_version"
        ) as cur:
            row = await cur.fetchone()
        current = row[0] if row else 0

        for version, name, statements in migrations.pending(current):
            for statement in statements:
                try:
                    await self._conn.execute(statement)
                except sqlite3.OperationalError as e:
                    # ALTER TABLE has no IF NOT EXISTS; a duplicate column means
                    # already applied, not broken.
                    if not migrations.is_benign_ddl_error(e):
                        raise
            for sql, rows in migrations.SEEDS.get(version, []):
                await self._conn.executemany(sql, rows)
            await self._conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (version, int(time.time())),
            )
            await self._conn.commit()
            print(f"[db] applied migration v{version} ({name})")
            current = version

        return current

    # ---------- queries ----------
    async def execute(self, sql, params=()):
        """Run a write. Returns the cursor's lastrowid."""
        cur = await self._conn.execute(sql, params)
        await self._conn.commit()
        return cur.lastrowid

    async def executemany(self, sql, rows):
        await self._conn.executemany(sql, rows)
        await self._conn.commit()

    async def fetchone(self, sql, params=()):
        async with self._conn.execute(sql, params) as cur:
            return await cur.fetchone()

    async def fetchall(self, sql, params=()):
        async with self._conn.execute(sql, params) as cur:
            return await cur.fetchall()

    async def fetchval(self, sql, params=(), default=None):
        row = await self.fetchone(sql, params)
        return row[0] if row is not None else default

    @asynccontextmanager
    async def transaction(self):
        """Group several writes so they commit or roll back together.

        Used by !give and !rob, where two balances must move as one unit.
        """
        try:
            yield self._conn
            await self._conn.commit()
        except Exception:
            await self._conn.rollback()
            raise
