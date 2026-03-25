from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any


class LyubishchevStorage:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _initialize_sync(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode = WAL;
                PRAGMA foreign_keys = ON;

                CREATE TABLE IF NOT EXISTS records (
                    record_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    platform_id TEXT,
                    sender_id TEXT,
                    sender_name TEXT,
                    record_kind TEXT NOT NULL DEFAULT 'actual',
                    record_date TEXT NOT NULL,
                    raw_text TEXT NOT NULL,
                    normalized_text TEXT,
                    started_at TEXT,
                    ended_at TEXT,
                    duration_minutes INTEGER,
                    category TEXT,
                    project TEXT,
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    source TEXT NOT NULL,
                    parser_confidence REAL NOT NULL DEFAULT 0,
                    parser_notes TEXT,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    deleted_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_records_session_date
                    ON records(session_id, record_date, created_at);

                CREATE TABLE IF NOT EXISTS record_revisions (
                    revision_id TEXT PRIMARY KEY,
                    record_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(record_id) REFERENCES records(record_id)
                );

                CREATE TABLE IF NOT EXISTS summary_rules (
                    rule_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    platform_id TEXT,
                    rule_name TEXT NOT NULL,
                    cron_expression TEXT NOT NULL,
                    timezone TEXT NOT NULL,
                    period_type TEXT NOT NULL,
                    lookback_days INTEGER,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    send_empty INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_summary_rules_session
                    ON summary_rules(session_id, enabled);

                CREATE TABLE IF NOT EXISTS summaries (
                    summary_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    rule_id TEXT,
                    summary_type TEXT NOT NULL,
                    period_start TEXT NOT NULL,
                    period_end TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    stats_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_summaries_session_period
                    ON summaries(session_id, period_start, period_end, created_at);

                CREATE TABLE IF NOT EXISTS memory_chunks (
                    chunk_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    embedding_provider_id TEXT,
                    embedding_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(source_type, source_id)
                );

                CREATE INDEX IF NOT EXISTS idx_memory_chunks_session
                    ON memory_chunks(session_id, source_type, updated_at);

                CREATE INDEX IF NOT EXISTS idx_memory_chunks_session_provider
                    ON memory_chunks(session_id, embedding_provider_id, updated_at);
                """
            )

    def _json_dumps(self, value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    def _escape_like(self, value: str) -> str:
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    def _row_to_dict(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        data = dict(row)
        for key in ("tags_json", "stats_json", "metadata_json", "snapshot_json"):
            if key in data and data[key]:
                try:
                    data[key] = json.loads(data[key])
                except json.JSONDecodeError:
                    pass
        if "embedding_json" in data and data["embedding_json"]:
            try:
                data["embedding_json"] = json.loads(data["embedding_json"])
            except json.JSONDecodeError:
                pass
        if "tags_json" in data:
            data["tags"] = data.pop("tags_json")
        return data

    async def add_record(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(self._add_record_sync, payload)

    def _add_record_sync(self, payload: dict[str, Any]) -> dict[str, Any]:
        row = payload.copy()
        row["tags_json"] = self._json_dumps(row.get("tags", []))
        row.setdefault("record_id", uuid.uuid4().hex)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO records (
                    record_id, session_id, platform_id, sender_id, sender_name,
                    record_kind, record_date, raw_text, normalized_text,
                    started_at, ended_at, duration_minutes, category, project,
                    tags_json, source, parser_confidence, parser_notes, status,
                    created_at, updated_at, deleted_at
                ) VALUES (
                    :record_id, :session_id, :platform_id, :sender_id, :sender_name,
                    :record_kind, :record_date, :raw_text, :normalized_text,
                    :started_at, :ended_at, :duration_minutes, :category, :project,
                    :tags_json, :source, :parser_confidence, :parser_notes, :status,
                    :created_at, :updated_at, :deleted_at
                )
                """,
                row,
            )
            self._insert_revision_sync(conn, row["record_id"], "created", row)
            record = conn.execute(
                "SELECT * FROM records WHERE record_id = ?",
                (row["record_id"],),
            ).fetchone()
        return self._row_to_dict(record) or {}

    async def get_record(self, record_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_record_sync, record_id)

    def _get_record_sync(self, record_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM records WHERE record_id = ?",
                (record_id,),
            ).fetchone()
        return self._row_to_dict(row)

    async def resolve_record_id(
        self,
        session_id: str,
        record_id_prefix: str,
    ) -> str | None:
        return await asyncio.to_thread(
            self._resolve_record_id_sync, session_id, record_id_prefix
        )

    def _resolve_record_id_sync(self, session_id: str, record_id_prefix: str) -> str | None:
        like = f"{record_id_prefix}%"
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT record_id
                FROM records
                WHERE session_id = ? AND record_id LIKE ?
                ORDER BY created_at DESC
                """,
                (session_id, like),
            ).fetchall()
        if len(rows) == 1:
            return str(rows[0]["record_id"])
        return None

    async def list_records(
        self,
        session_id: str,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 20,
        include_deleted: bool = False,
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            self._list_records_sync,
            session_id,
            start_date,
            end_date,
            limit,
            include_deleted,
        )

    def _list_records_sync(
        self,
        session_id: str,
        start_date: str | None,
        end_date: str | None,
        limit: int,
        include_deleted: bool,
    ) -> list[dict[str, Any]]:
        clauses = ["session_id = ?"]
        params: list[Any] = [session_id]
        if not include_deleted:
            clauses.append("status = 'active'")
        if start_date:
            clauses.append("record_date >= ?")
            params.append(start_date)
        if end_date:
            clauses.append("record_date <= ?")
            params.append(end_date)
        params.append(limit)
        query = f"""
            SELECT *
            FROM records
            WHERE {' AND '.join(clauses)}
            ORDER BY record_date DESC, COALESCE(started_at, created_at) DESC
            LIMIT ?
        """
        with self._connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [self._row_to_dict(row) or {} for row in rows]

    async def amend_record(
        self,
        record_id: str,
        updates: dict[str, Any],
    ) -> dict[str, Any] | None:
        async with self._lock:
            return await asyncio.to_thread(self._amend_record_sync, record_id, updates)

    def _amend_record_sync(
        self,
        record_id: str,
        updates: dict[str, Any],
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            current = conn.execute(
                "SELECT * FROM records WHERE record_id = ?",
                (record_id,),
            ).fetchone()
            if current is None:
                return None
            row = dict(current)
            row.update(updates)
            row["tags_json"] = self._json_dumps(row.get("tags", row.get("tags_json", [])))
            conn.execute(
                """
                UPDATE records
                SET record_kind = :record_kind,
                    record_date = :record_date,
                    raw_text = :raw_text,
                    normalized_text = :normalized_text,
                    started_at = :started_at,
                    ended_at = :ended_at,
                    duration_minutes = :duration_minutes,
                    category = :category,
                    project = :project,
                    tags_json = :tags_json,
                    parser_confidence = :parser_confidence,
                    parser_notes = :parser_notes,
                    status = :status,
                    updated_at = :updated_at,
                    deleted_at = :deleted_at
                WHERE record_id = :record_id
                """,
                row,
            )
            self._insert_revision_sync(conn, record_id, "amended", row)
            updated = conn.execute(
                "SELECT * FROM records WHERE record_id = ?",
                (record_id,),
            ).fetchone()
        return self._row_to_dict(updated)

    async def soft_delete_record(
        self,
        record_id: str,
        *,
        deleted_at: str,
    ) -> dict[str, Any] | None:
        async with self._lock:
            return await asyncio.to_thread(
                self._soft_delete_record_sync, record_id, deleted_at
            )

    def _soft_delete_record_sync(
        self,
        record_id: str,
        deleted_at: str,
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM records WHERE record_id = ?",
                (record_id,),
            ).fetchone()
            if row is None:
                return None
            current = dict(row)
            current["status"] = "deleted"
            current["deleted_at"] = deleted_at
            current["updated_at"] = deleted_at
            conn.execute(
                """
                UPDATE records
                SET status = 'deleted', deleted_at = ?, updated_at = ?
                WHERE record_id = ?
                """,
                (deleted_at, deleted_at, record_id),
            )
            self._insert_revision_sync(conn, record_id, "deleted", current)
            updated = conn.execute(
                "SELECT * FROM records WHERE record_id = ?",
                (record_id,),
            ).fetchone()
        return self._row_to_dict(updated)

    def _insert_revision_sync(
        self,
        conn: sqlite3.Connection,
        record_id: str,
        action: str,
        snapshot: dict[str, Any],
    ) -> None:
        conn.execute(
            """
            INSERT INTO record_revisions (revision_id, record_id, action, snapshot_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                uuid.uuid4().hex,
                record_id,
                action,
                self._json_dumps(snapshot),
                snapshot["updated_at"],
            ),
        )

    async def list_revisions(self, record_id: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_revisions_sync, record_id)

    def _list_revisions_sync(self, record_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM record_revisions
                WHERE record_id = ?
                ORDER BY created_at DESC
                """,
                (record_id,),
            ).fetchall()
        return [self._row_to_dict(row) or {} for row in rows]

    async def upsert_summary_rule(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(self._upsert_summary_rule_sync, payload)

    def _upsert_summary_rule_sync(self, payload: dict[str, Any]) -> dict[str, Any]:
        row = payload.copy()
        row.setdefault("rule_id", uuid.uuid4().hex)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO summary_rules (
                    rule_id, session_id, platform_id, rule_name, cron_expression,
                    timezone, period_type, lookback_days, enabled, send_empty,
                    created_at, updated_at
                ) VALUES (
                    :rule_id, :session_id, :platform_id, :rule_name, :cron_expression,
                    :timezone, :period_type, :lookback_days, :enabled, :send_empty,
                    :created_at, :updated_at
                )
                ON CONFLICT(rule_id) DO UPDATE SET
                    rule_name = excluded.rule_name,
                    cron_expression = excluded.cron_expression,
                    timezone = excluded.timezone,
                    period_type = excluded.period_type,
                    lookback_days = excluded.lookback_days,
                    enabled = excluded.enabled,
                    send_empty = excluded.send_empty,
                    updated_at = excluded.updated_at
                """,
                row,
            )
            rule = conn.execute(
                "SELECT * FROM summary_rules WHERE rule_id = ?",
                (row["rule_id"],),
            ).fetchone()
        return self._row_to_dict(rule) or {}

    async def list_summary_rules(
        self,
        session_id: str | None = None,
        *,
        enabled_only: bool = False,
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            self._list_summary_rules_sync, session_id, enabled_only
        )

    def _list_summary_rules_sync(
        self,
        session_id: str | None,
        enabled_only: bool,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        if enabled_only:
            clauses.append("enabled = 1")
        query = "SELECT * FROM summary_rules"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at ASC"
        with self._connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [self._row_to_dict(row) or {} for row in rows]

    async def resolve_rule_id(self, session_id: str, rule_id_prefix: str) -> str | None:
        return await asyncio.to_thread(
            self._resolve_rule_id_sync, session_id, rule_id_prefix
        )

    def _resolve_rule_id_sync(self, session_id: str, rule_id_prefix: str) -> str | None:
        like = f"{rule_id_prefix}%"
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT rule_id
                FROM summary_rules
                WHERE session_id = ? AND rule_id LIKE ?
                ORDER BY created_at DESC
                """,
                (session_id, like),
            ).fetchall()
        if len(rows) == 1:
            return str(rows[0]["rule_id"])
        return None

    async def get_summary_rule(self, rule_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_summary_rule_sync, rule_id)

    def _get_summary_rule_sync(self, rule_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM summary_rules WHERE rule_id = ?",
                (rule_id,),
            ).fetchone()
        return self._row_to_dict(row)

    async def delete_summary_rule(self, rule_id: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._delete_summary_rule_sync, rule_id)

    def _delete_summary_rule_sync(self, rule_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM summary_rules WHERE rule_id = ?", (rule_id,))

    async def add_summary(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(self._add_summary_sync, payload)

    def _add_summary_sync(self, payload: dict[str, Any]) -> dict[str, Any]:
        row = payload.copy()
        row.setdefault("summary_id", uuid.uuid4().hex)
        row["stats_json"] = self._json_dumps(row.get("stats", {}))
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO summaries (
                    summary_id, session_id, rule_id, summary_type, period_start,
                    period_end, title, content, stats_json, created_at
                ) VALUES (
                    :summary_id, :session_id, :rule_id, :summary_type, :period_start,
                    :period_end, :title, :content, :stats_json, :created_at
                )
                """,
                row,
            )
            summary = conn.execute(
                "SELECT * FROM summaries WHERE summary_id = ?",
                (row["summary_id"],),
            ).fetchone()
        return self._row_to_dict(summary) or {}

    async def upsert_memory_chunk(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(self._upsert_memory_chunk_sync, payload)

    def _upsert_memory_chunk_sync(self, payload: dict[str, Any]) -> dict[str, Any]:
        row = payload.copy()
        row.setdefault("chunk_id", uuid.uuid4().hex)
        row["metadata_json"] = self._json_dumps(row.get("metadata", {}))
        row["embedding_json"] = (
            self._json_dumps(row["embedding"])
            if row.get("embedding") is not None
            else None
        )
        with self._connect() as conn:
            existing = conn.execute(
                """
                SELECT chunk_id
                FROM memory_chunks
                WHERE source_type = ? AND source_id = ?
                """,
                (row["source_type"], row["source_id"]),
            ).fetchone()
            if existing is not None:
                row["chunk_id"] = str(existing["chunk_id"])
            conn.execute(
                """
                INSERT INTO memory_chunks (
                    chunk_id, session_id, source_type, source_id, content,
                    metadata_json, embedding_provider_id, embedding_json,
                    created_at, updated_at
                ) VALUES (
                    :chunk_id, :session_id, :source_type, :source_id, :content,
                    :metadata_json, :embedding_provider_id, :embedding_json,
                    :created_at, :updated_at
                )
                ON CONFLICT(source_type, source_id) DO UPDATE SET
                    session_id = excluded.session_id,
                    content = excluded.content,
                    metadata_json = excluded.metadata_json,
                    embedding_provider_id = excluded.embedding_provider_id,
                    embedding_json = excluded.embedding_json,
                    updated_at = excluded.updated_at
                """,
                row,
            )
            chunk = conn.execute(
                "SELECT * FROM memory_chunks WHERE chunk_id = ?",
                (row["chunk_id"],),
            ).fetchone()
        return self._row_to_dict(chunk) or {}

    async def delete_memory_chunk(self, source_type: str, source_id: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._delete_memory_chunk_sync, source_type, source_id)

    def _delete_memory_chunk_sync(self, source_type: str, source_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM memory_chunks WHERE source_type = ? AND source_id = ?",
                (source_type, source_id),
            )

    async def search_memory_chunks_text(
        self,
        session_id: str,
        query_text: str,
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            self._search_memory_chunks_text_sync, session_id, query_text, limit
        )

    def _search_memory_chunks_text_sync(
        self,
        session_id: str,
        query_text: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        like = f"%{self._escape_like(query_text)}%"
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM memory_chunks
                WHERE session_id = ? AND content LIKE ? ESCAPE '\'
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (session_id, like, limit),
            ).fetchall()
        return [self._row_to_dict(row) or {} for row in rows]

    async def list_memory_chunks_with_embeddings(
        self,
        session_id: str,
        *,
        embedding_provider_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            self._list_memory_chunks_with_embeddings_sync,
            session_id,
            embedding_provider_id,
            limit,
        )

    def _list_memory_chunks_with_embeddings_sync(
        self,
        session_id: str,
        embedding_provider_id: str | None,
        limit: int | None,
    ) -> list[dict[str, Any]]:
        clauses = ["session_id = ?", "embedding_json IS NOT NULL"]
        params: list[Any] = [session_id]
        if embedding_provider_id:
            clauses.append("embedding_provider_id = ?")
            params.append(embedding_provider_id)
        query = """
                SELECT *
                FROM memory_chunks
                WHERE {where_clause}
                ORDER BY updated_at DESC
        """.format(where_clause=" AND ".join(clauses))
        if limit is not None:
            query += "\n                LIMIT ?"
            params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [self._row_to_dict(row) or {} for row in rows]

    async def count_records(self, session_id: str) -> int:
        return await asyncio.to_thread(self._count_records_sync, session_id)

    def _count_records_sync(self, session_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM records
                WHERE session_id = ? AND status = 'active'
                """,
                (session_id,),
            ).fetchone()
        return int(row["cnt"]) if row else 0
