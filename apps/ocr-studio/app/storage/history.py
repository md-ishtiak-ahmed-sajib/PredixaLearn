"""Persistent, local-only OCR metadata and archived artifact storage."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.config import Settings, get_settings
from app.core.serialization import atomic_write_json, json_safe
from app.documents.naming import safe_output_stem

logger = logging.getLogger("predixalearn.history")
_JOB_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_ACTIVE_STATUSES = {"pending", "processing"}
_TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
_WORKFLOWS = {"text_recognition", "layout_parsing", "table_extraction", "vl_processing"}
_STATUSES = _ACTIVE_STATUSES | _TERMINAL_STATUSES
_HISTORY_SCHEMA_VERSION = 7


class HistoryNotFoundError(LookupError):
    pass


class HistoryConflictError(RuntimeError):
    pass


class HistoryResultError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class HistoryRecord:
    job_id: str
    input_name: str
    workflow: str
    input_kind: str
    status: str
    item_count: int | None
    error: str | None
    result_path: str | None
    artifact_manifest_path: str | None
    legacy: int
    pinned: int
    language: str | None
    document_profile: str | None
    remove_terms_json: str | None
    quality_score: float | None
    warning_count: int
    imported_from_job_id: str | None
    analysis_status: str | None
    analysis_updated_at: str | None
    analysis_question_count: int
    created_at: str
    started_at: str | None
    completed_at: str | None
    updated_at: str

    @property
    def duration_ms(self) -> int | None:
        if self.started_at is None or self.completed_at is None:
            return None
        started = datetime.fromisoformat(self.started_at)
        completed = datetime.fromisoformat(self.completed_at)
        return max(0, round((completed - started).total_seconds() * 1000))

    def summary(self) -> dict[str, Any]:
        try:
            remove_terms = json.loads(self.remove_terms_json or "[]")
        except (TypeError, json.JSONDecodeError):
            remove_terms = []
        return {
            "job_id": self.job_id,
            "input_name": self.input_name,
            "workflow": self.workflow,
            "input_kind": self.input_kind,
            "status": self.status,
            "item_count": self.item_count,
            "error": self.error,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "updated_at": self.updated_at,
            "duration_ms": self.duration_ms,
            "result_available": self.result_path is not None,
            "artifacts_available": self.artifact_manifest_path is not None,
            "legacy": bool(self.legacy),
            "pinned": bool(self.pinned),
            "language": self.language,
            "document_profile": self.document_profile,
            "remove_terms": remove_terms if isinstance(remove_terms, list) else [],
            "quality_score": self.quality_score,
            "warning_count": self.warning_count,
            "imported_from_job_id": self.imported_from_job_id,
            "analysis_status": self.analysis_status,
            "analysis_updated_at": self.analysis_updated_at,
            "analysis_question_count": self.analysis_question_count,
        }


@dataclass(frozen=True, slots=True)
class SyncOutboxRecord:
    event_id: str
    job_id: str
    tenant_id: str
    device_id: str
    status: str
    attempt_count: int
    idempotency_key: str
    payload_path: str
    payload_sha256: str
    error: str | None
    next_attempt_at: str | None
    created_at: str
    updated_at: str

    def public(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "job_id": self.job_id,
            "tenant_id": self.tenant_id,
            "device_id": self.device_id,
            "status": self.status,
            "attempt_count": self.attempt_count,
            "error": self.error,
            "next_attempt_at": self.next_attempt_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class HistoryStore:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.path = self.settings.history_db_path
        self._init_lock = threading.Lock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _backup_database(self, source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        source_connection = sqlite3.connect(source)
        destination_connection = sqlite3.connect(destination)
        try:
            source_connection.backup(destination_connection)
        finally:
            destination_connection.close()
            source_connection.close()

    def _migrate_legacy_database(self) -> bool:
        legacy = self.settings.output_dir / "history.sqlite3"
        if self.path.exists() or legacy.resolve() == self.path.resolve() or not legacy.is_file():
            return False
        backup = self.settings.history_dir / "migration-backups" / "history.sqlite3"
        self._backup_database(legacy, backup)
        self._backup_database(legacy, self.path)
        logger.info("Migrated OCR history database to %s", self.path)
        return True

    def _ensure_schema(self, connection: sqlite3.Connection) -> None:
        connection.execute("PRAGMA journal_mode = WAL")
        current_version = int(
            connection.execute("PRAGMA user_version").fetchone()[0]
        )
        if current_version > _HISTORY_SCHEMA_VERSION:
            raise HistoryResultError(
                "History database was created by a newer version of PredixaLearn"
            )

        migrations = {
            1: self._migrate_schema_v1,
            2: self._migrate_schema_v2,
            3: self._migrate_schema_v3,
            4: self._migrate_schema_v4,
            5: self._migrate_schema_v5,
            6: self._migrate_schema_v6,
            7: self._migrate_schema_v7,
        }
        for version in range(current_version + 1, _HISTORY_SCHEMA_VERSION + 1):
            migrations[version](connection)
            connection.execute(f"PRAGMA user_version = {version}")

    @staticmethod
    def _schema_columns(connection: sqlite3.Connection) -> set[str]:
        return {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(history_jobs)").fetchall()
        }

    @classmethod
    def _add_missing_columns(
        cls, connection: sqlite3.Connection, additions: dict[str, str]
    ) -> None:
        columns = cls._schema_columns(connection)
        for name, declaration in additions.items():
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE history_jobs ADD COLUMN {name} {declaration}"
                )

    @staticmethod
    def _migrate_schema_v1(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS history_jobs (
                job_id TEXT PRIMARY KEY,
                input_name TEXT NOT NULL,
                workflow TEXT NOT NULL,
                input_kind TEXT NOT NULL,
                status TEXT NOT NULL,
                item_count INTEGER,
                error TEXT,
                result_path TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )

    @classmethod
    def _migrate_schema_v2(cls, connection: sqlite3.Connection) -> None:
        cls._add_missing_columns(
            connection,
            {
                "artifact_manifest_path": "TEXT",
                "legacy": "INTEGER NOT NULL DEFAULT 0",
            },
        )

    @classmethod
    def _migrate_schema_v3(cls, connection: sqlite3.Connection) -> None:
        cls._add_missing_columns(
            connection,
            {
            "pinned": "INTEGER NOT NULL DEFAULT 0",
            "language": "TEXT",
            "document_profile": "TEXT",
            "remove_terms_json": "TEXT",
            "quality_score": "REAL",
            "warning_count": "INTEGER NOT NULL DEFAULT 0",
            "imported_from_job_id": "TEXT",
            },
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS history_created_idx "
            "ON history_jobs(created_at DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS history_pinned_created_idx "
            "ON history_jobs(pinned DESC, created_at DESC)"
        )

    @classmethod
    def _migrate_schema_v4(cls, connection: sqlite3.Connection) -> None:
        cls._add_missing_columns(
            connection,
            {
                "analysis_status": "TEXT",
                "analysis_updated_at": "TEXT",
                "analysis_question_count": "INTEGER NOT NULL DEFAULT 0",
            },
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS history_analysis_updated_idx "
            "ON history_jobs(analysis_updated_at DESC)"
        )

    @staticmethod
    def _migrate_schema_v5(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS institution_sync_outbox (
                event_id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL REFERENCES history_jobs(job_id) ON DELETE CASCADE,
                tenant_id TEXT NOT NULL,
                device_id TEXT NOT NULL,
                status TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                idempotency_key TEXT NOT NULL UNIQUE,
                payload_path TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                error TEXT,
                next_attempt_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS institution_sync_status_idx "
            "ON institution_sync_outbox(status, next_attempt_at, created_at)"
        )

    @staticmethod
    def _migrate_schema_v6(connection: sqlite3.Connection) -> None:
        """Create the evidence-linked teaching workspace and persistent batch queue."""

        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS teaching_audit_events (
                event_id TEXT PRIMARY KEY,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                job_id TEXT REFERENCES history_jobs(job_id) ON DELETE CASCADE,
                action TEXT NOT NULL,
                actor TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                previous_hash TEXT,
                event_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS teaching_audit_entity_idx
                ON teaching_audit_events(entity_type, entity_id, created_at);
            CREATE INDEX IF NOT EXISTS teaching_audit_job_idx
                ON teaching_audit_events(job_id, created_at);

            CREATE TABLE IF NOT EXISTS correction_sets (
                correction_id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL REFERENCES history_jobs(job_id) ON DELETE CASCADE,
                target_kind TEXT NOT NULL,
                target_id TEXT NOT NULL,
                original_json TEXT NOT NULL,
                replacement_json TEXT NOT NULL,
                reason TEXT NOT NULL,
                actor TEXT NOT NULL,
                status TEXT NOT NULL,
                source_hash TEXT NOT NULL,
                source_line_ids_json TEXT NOT NULL,
                geometry_json TEXT,
                version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS corrections_job_status_idx
                ON correction_sets(job_id, status, updated_at);
            CREATE UNIQUE INDEX IF NOT EXISTS corrections_target_active_idx
                ON correction_sets(job_id, target_kind, target_id)
                WHERE status IN ('draft', 'reviewed', 'approved');

            CREATE TABLE IF NOT EXISTS taxonomy_versions (
                taxonomy_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                name TEXT NOT NULL,
                subject TEXT,
                academic_level TEXT,
                status TEXT NOT NULL,
                source_hash TEXT,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (taxonomy_id, version)
            );
            CREATE TABLE IF NOT EXISTS taxonomy_nodes (
                taxonomy_id TEXT NOT NULL,
                taxonomy_version INTEGER NOT NULL,
                node_id TEXT NOT NULL,
                parent_id TEXT,
                label TEXT NOT NULL,
                aliases_json TEXT NOT NULL,
                description TEXT,
                sort_order INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (taxonomy_id, taxonomy_version, node_id),
                FOREIGN KEY (taxonomy_id, taxonomy_version)
                    REFERENCES taxonomy_versions(taxonomy_id, version) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS taxonomy_node_parent_idx
                ON taxonomy_nodes(taxonomy_id, taxonomy_version, parent_id, sort_order);

            CREATE TABLE IF NOT EXISTS syllabus_versions (
                syllabus_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                name TEXT NOT NULL,
                subject TEXT,
                academic_level TEXT,
                status TEXT NOT NULL,
                source_type TEXT NOT NULL,
                source_checksum TEXT NOT NULL,
                effective_from TEXT,
                effective_to TEXT,
                warnings_json TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (syllabus_id, version)
            );
            CREATE TABLE IF NOT EXISTS syllabus_objectives (
                syllabus_id TEXT NOT NULL,
                syllabus_version INTEGER NOT NULL,
                objective_id TEXT NOT NULL,
                parent_id TEXT,
                code TEXT,
                label TEXT NOT NULL,
                description TEXT,
                aliases_json TEXT NOT NULL,
                inferred INTEGER NOT NULL DEFAULT 0,
                confirmed INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (syllabus_id, syllabus_version, objective_id),
                FOREIGN KEY (syllabus_id, syllabus_version)
                    REFERENCES syllabus_versions(syllabus_id, version) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS syllabus_mappings (
                mapping_id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL REFERENCES history_jobs(job_id) ON DELETE CASCADE,
                question_id TEXT NOT NULL,
                syllabus_id TEXT NOT NULL,
                syllabus_version INTEGER NOT NULL,
                objective_id TEXT NOT NULL,
                topic_id TEXT,
                status TEXT NOT NULL,
                method TEXT NOT NULL,
                confidence REAL,
                actor TEXT NOT NULL,
                source_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (syllabus_id, syllabus_version, objective_id)
                    REFERENCES syllabus_objectives(syllabus_id, syllabus_version, objective_id)
            );
            CREATE INDEX IF NOT EXISTS syllabus_mapping_job_idx
                ON syllabus_mappings(job_id, question_id, status);

            CREATE TABLE IF NOT EXISTS duplicate_decisions (
                decision_id TEXT PRIMARY KEY,
                left_question_key TEXT NOT NULL,
                right_question_key TEXT NOT NULL,
                method TEXT NOT NULL,
                score REAL NOT NULL,
                disposition TEXT,
                explanation_json TEXT NOT NULL,
                actor TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS duplicate_pair_idx
                ON duplicate_decisions(left_question_key, right_question_key, method);

            CREATE TABLE IF NOT EXISTS question_bank_items (
                item_id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL REFERENCES history_jobs(job_id) ON DELETE RESTRICT,
                question_id TEXT NOT NULL,
                source_hash TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                approved_by TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(job_id, question_id)
            );
            CREATE INDEX IF NOT EXISTS question_bank_status_idx
                ON question_bank_items(status, updated_at);

            CREATE TABLE IF NOT EXISTS comparison_reports (
                comparison_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                job_ids_json TEXT NOT NULL,
                source_hashes_json TEXT NOT NULL,
                analysis_versions_json TEXT NOT NULL,
                taxonomy_ref TEXT,
                syllabus_ref TEXT,
                report_json TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS revision_packs (
                pack_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                status TEXT NOT NULL,
                course_ref TEXT,
                cohort_ref TEXT,
                language TEXT NOT NULL,
                source_items_json TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_by TEXT NOT NULL,
                approved_by TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS revision_pack_status_idx
                ON revision_packs(status, updated_at);

            CREATE TABLE IF NOT EXISTS batches (
                batch_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                status TEXT NOT NULL,
                workflow TEXT NOT NULL,
                language TEXT NOT NULL,
                document_profile TEXT,
                remove_terms_json TEXT NOT NULL,
                settings_json TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS batch_items (
                item_id TEXT PRIMARY KEY,
                batch_id TEXT NOT NULL REFERENCES batches(batch_id) ON DELETE CASCADE,
                position INTEGER NOT NULL,
                input_name TEXT NOT NULL,
                staged_path TEXT NOT NULL,
                source_sha256 TEXT NOT NULL,
                media_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                overrides_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL,
                job_id TEXT REFERENCES history_jobs(job_id) ON DELETE SET NULL,
                retry_pending INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(batch_id, position)
            );
            CREATE INDEX IF NOT EXISTS batch_items_next_idx
                ON batch_items(status, batch_id, position);
            """
        )

    @staticmethod
    def _migrate_schema_v7(connection: sqlite3.Connection) -> None:
        """Add per-file batch settings without rewriting queued source records."""

        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(batch_items)").fetchall()
        }
        if "overrides_json" not in columns:
            connection.execute(
                "ALTER TABLE batch_items ADD COLUMN overrides_json TEXT NOT NULL DEFAULT '{}'"
            )

    def _write_legacy_manifest(self, job_id: str, source_directory: Path) -> str:
        job_root = self.settings.history_dir / "jobs" / job_id
        stem = "legacy-output"
        archive = job_root / "artifacts" / stem
        archive.parent.mkdir(parents=True, exist_ok=True)
        if archive.exists():
            shutil.rmtree(archive)
        shutil.copytree(source_directory, archive)
        markdown = next((path.name for path in archive.glob("*.md")), None)
        docx = next((path.name for path in archive.glob("*.docx")), None)
        manifest = {
            "version": 1,
            "job_id": job_id,
            "output_stem": safe_output_stem(source_directory.name),
            "archive_root": archive.relative_to(self.settings.history_dir).as_posix(),
            "files": {
                "markdown": markdown,
                "docx": docx,
                "json": None,
                "images": [],
                "tables": [],
            },
            "legacy": True,
            "requires_reupload": docx is None,
        }
        manifest_path = job_root / "manifest.json"
        atomic_write_json(manifest_path, manifest)
        return manifest_path.relative_to(self.settings.history_dir).as_posix()

    def _migrate_legacy_artifacts(self, connection: sqlite3.Connection) -> None:
        marker = self.settings.history_dir / ".legacy-output-migrated"
        if marker.exists():
            return
        rows = connection.execute(
            "SELECT job_id, result_path FROM history_jobs"
        ).fetchall()
        matched: set[str] = set()
        for row in rows:
            job_id = str(row["job_id"])
            if not _JOB_ID_PATTERN.fullmatch(job_id):
                continue
            old_directory = self.settings.output_dir / job_id
            old_result = (
                self.settings.output_dir / str(row["result_path"])
                if row["result_path"]
                else old_directory / "result.json"
            )
            job_root = self.settings.history_dir / "jobs" / job_id
            new_result = job_root / "result.json"
            if old_result.is_file():
                job_root.mkdir(parents=True, exist_ok=True)
                shutil.copy2(old_result, new_result)
            manifest_path = None
            if old_directory.is_dir():
                manifest_path = self._write_legacy_manifest(job_id, old_directory)
                matched.add(job_id)
            result_relative = (
                new_result.relative_to(self.settings.history_dir).as_posix()
                if new_result.is_file()
                else None
            )
            connection.execute(
                """
                UPDATE history_jobs
                SET result_path = ?, artifact_manifest_path = ?, legacy = 1
                WHERE job_id = ?
                """,
                (result_relative, manifest_path, job_id),
            )

        legacy_root = self.settings.history_dir / "legacy"
        for directory in self.settings.output_dir.iterdir() if self.settings.output_dir.exists() else []:
            if not directory.is_dir() or not _JOB_ID_PATTERN.fullmatch(directory.name):
                continue
            if directory.name in matched:
                continue
            destination = legacy_root / directory.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                destination = legacy_root / f"{directory.name}-{uuid.uuid4().hex[:8]}"
            shutil.copytree(directory, destination)

        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("completed\n", encoding="utf-8")

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            self.settings.history_dir.mkdir(parents=True, exist_ok=True)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            migrated = self._migrate_legacy_database()
            with self._connect() as connection:
                self._ensure_schema(connection)
                if migrated:
                    self._migrate_legacy_artifacts(connection)
            if migrated:
                logger.info("Retained legacy history data after non-destructive migration")
            self._initialized = True

    @staticmethod
    def _validate_job_id(job_id: str) -> None:
        if not _JOB_ID_PATTERN.fullmatch(job_id):
            raise HistoryNotFoundError("History record not found")

    @staticmethod
    def _record_from_row(row: sqlite3.Row) -> HistoryRecord:
        return HistoryRecord(**dict(row))

    def upsert(
        self,
        *,
        job_id: str,
        input_name: str,
        workflow: str,
        input_kind: str,
        status: str,
        item_count: int | None,
        error: str | None,
        result_path: str | None,
        artifact_manifest_path: str | None = None,
        legacy: bool = False,
        created_at: datetime,
        started_at: datetime | None,
        completed_at: datetime | None,
        updated_at: datetime,
        pinned: bool = False,
        language: str | None = None,
        document_profile: str | None = None,
        remove_terms: list[str] | None = None,
        quality_score: float | None = None,
        warning_count: int = 0,
        imported_from_job_id: str | None = None,
    ) -> None:
        self.initialize()
        self._validate_job_id(job_id)
        if workflow not in _WORKFLOWS or status not in _STATUSES:
            raise ValueError("Unsupported history workflow or status")
        if input_kind not in {"pdf", "image"}:
            raise ValueError("Unsupported history input kind")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO history_jobs (
                    job_id, input_name, workflow, input_kind, status, item_count,
                    error, result_path, artifact_manifest_path, legacy,
                    pinned, language, document_profile, remove_terms_json,
                    quality_score, warning_count, imported_from_job_id,
                    created_at, started_at, completed_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    input_name = excluded.input_name,
                    workflow = excluded.workflow,
                    input_kind = excluded.input_kind,
                    status = excluded.status,
                    item_count = excluded.item_count,
                    error = excluded.error,
                    result_path = COALESCE(excluded.result_path, history_jobs.result_path),
                    artifact_manifest_path = COALESCE(
                        excluded.artifact_manifest_path,
                        history_jobs.artifact_manifest_path
                    ),
                    legacy = CASE WHEN excluded.legacy = 1 THEN 1 ELSE history_jobs.legacy END,
                    pinned = CASE WHEN excluded.pinned = 1 THEN 1 ELSE history_jobs.pinned END,
                    language = COALESCE(excluded.language, history_jobs.language),
                    document_profile = COALESCE(
                        excluded.document_profile, history_jobs.document_profile
                    ),
                    remove_terms_json = COALESCE(
                        excluded.remove_terms_json, history_jobs.remove_terms_json
                    ),
                    quality_score = COALESCE(excluded.quality_score, history_jobs.quality_score),
                    warning_count = excluded.warning_count,
                    imported_from_job_id = COALESCE(
                        excluded.imported_from_job_id, history_jobs.imported_from_job_id
                    ),
                    started_at = COALESCE(excluded.started_at, history_jobs.started_at),
                    completed_at = excluded.completed_at,
                    updated_at = excluded.updated_at
                """,
                (
                    job_id,
                    input_name,
                    workflow,
                    input_kind,
                    status,
                    item_count,
                    error,
                    result_path,
                    artifact_manifest_path,
                    int(legacy),
                    int(pinned),
                    language,
                    document_profile,
                    json.dumps(remove_terms, ensure_ascii=False) if remove_terms is not None else None,
                    quality_score,
                    max(0, warning_count),
                    imported_from_job_id,
                    created_at.isoformat(),
                    started_at.isoformat() if started_at else None,
                    completed_at.isoformat() if completed_at else None,
                    updated_at.isoformat(),
                ),
            )

    def save_result(self, job_id: str, result: Any) -> str:
        self._validate_job_id(job_id)
        relative = Path("jobs") / job_id / "result.json"
        target = self.settings.history_dir / relative
        atomic_write_json(target, json_safe(result))
        return relative.as_posix()

    def import_job_snapshot(self, snapshot: dict[str, Any]) -> bool:
        job_id = str(snapshot.get("job_id", ""))
        self._validate_job_id(job_id)
        workflow = str(snapshot.get("workflow", ""))
        status = str(snapshot.get("status", ""))
        if workflow not in _WORKFLOWS or status not in _STATUSES:
            return False
        input_name = str(snapshot.get("input_name") or "Recovered OCR job")
        input_kind = str(snapshot.get("input_kind") or "")
        if input_kind not in {"pdf", "image"}:
            input_kind = "pdf" if input_name.lower().endswith(".pdf") else "image"
        created_at = datetime.fromisoformat(str(snapshot["created_at"]))
        updated_at = datetime.fromisoformat(str(snapshot["updated_at"]))
        started_raw = snapshot.get("started_at")
        completed_raw = snapshot.get("completed_at")
        started_at = datetime.fromisoformat(str(started_raw)) if started_raw else created_at
        completed_at = (
            datetime.fromisoformat(str(completed_raw))
            if completed_raw
            else updated_at if status in _TERMINAL_STATUSES else None
        )
        progress = snapshot.get("progress")
        item_count = snapshot.get("item_count")
        if not isinstance(item_count, int) and isinstance(progress, dict):
            candidate = progress.get("total_pages")
            item_count = candidate if isinstance(candidate, int) else None
        result = snapshot.get("result")
        result_path = self.save_result(job_id, result) if result is not None else None
        self.upsert(
            job_id=job_id,
            input_name=input_name,
            workflow=workflow,
            input_kind=input_kind,
            status=status,
            item_count=item_count,
            error=str(snapshot["error"]) if snapshot.get("error") else None,
            result_path=result_path,
            legacy=True,
            created_at=created_at,
            started_at=started_at,
            completed_at=completed_at,
            updated_at=updated_at,
        )
        return True

    def _resolve_history_path(self, relative_path: str) -> Path:
        candidate = (self.settings.history_dir / relative_path).resolve()
        root = self.settings.history_dir.resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise HistoryResultError("Stored history path is invalid") from exc
        return candidate

    def get(self, job_id: str) -> HistoryRecord:
        self.initialize()
        self._validate_job_id(job_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM history_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise HistoryNotFoundError("History record not found")
        return self._record_from_row(row)

    def read_result(self, record: HistoryRecord) -> Any:
        if record.result_path is None:
            raise HistoryResultError("This job has no saved result")
        path = self._resolve_history_path(record.result_path)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise HistoryResultError("The saved OCR result is unavailable or corrupted") from exc

    def read_manifest(self, record: HistoryRecord) -> dict[str, Any] | None:
        if record.artifact_manifest_path is None:
            return None
        path = self._resolve_history_path(record.artifact_manifest_path)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise HistoryResultError("The artifact manifest is unavailable or corrupted") from exc
        if not isinstance(value, dict):
            raise HistoryResultError("The artifact manifest is malformed")
        return value

    def artifact_path(self, record: HistoryRecord, format_name: str) -> Path:
        manifest = self.read_manifest(record)
        if manifest is None:
            raise HistoryResultError(
                "This legacy result has no generated document; re-upload the source to create it"
            )
        files = manifest.get("files")
        archive_root = manifest.get("archive_root")
        if not isinstance(files, dict) or not isinstance(archive_root, str):
            raise HistoryResultError("The artifact manifest is malformed")
        filename = files.get(format_name)
        if not isinstance(filename, str) or not filename:
            raise HistoryResultError(
                f"The requested {format_name.upper()} artifact is unavailable; re-upload if needed"
            )
        root = self._resolve_history_path(archive_root)
        candidate = (root / filename).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError as exc:
            raise HistoryResultError("Stored artifact path is invalid") from exc
        if not candidate.is_file():
            raise HistoryResultError("The requested artifact is unavailable or corrupted")
        return candidate

    def table_artifact_path(
        self,
        record: HistoryRecord,
        *,
        table_index: int,
        extension: str,
    ) -> Path:
        if table_index < 1 or extension not in {"csv", "xlsx"}:
            raise HistoryResultError("The requested table artifact is invalid")
        manifest = self.read_manifest(record)
        if manifest is None:
            raise HistoryResultError(
                "This legacy result has no table exports; re-upload the source to create them"
            )
        files = manifest.get("files")
        archive_root = manifest.get("archive_root")
        if not isinstance(files, dict) or not isinstance(archive_root, str):
            raise HistoryResultError("The artifact manifest is malformed")
        table_files = files.get("tables")
        if not isinstance(table_files, list):
            raise HistoryResultError("The artifact manifest has no table exports")
        pattern = re.compile(rf"(?:^|/)table-{table_index:03d}\.{extension}$", re.IGNORECASE)
        filename = next(
            (
                item
                for item in table_files
                if isinstance(item, str) and pattern.search(item.replace("\\", "/"))
            ),
            None,
        )
        if filename is None:
            raise HistoryResultError(
                f"Table {table_index} has no {extension.upper()} export"
            )
        root = self._resolve_history_path(archive_root)
        candidate = (root / filename).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError as exc:
            raise HistoryResultError("Stored table artifact path is invalid") from exc
        if not candidate.is_file():
            raise HistoryResultError("The requested table artifact is unavailable or corrupted")
        return candidate

    def reveal_path(self, record: HistoryRecord) -> Path:
        manifest = self.read_manifest(record) if record.artifact_manifest_path else None
        if manifest is not None:
            archive_root = manifest.get("archive_root")
            if isinstance(archive_root, str):
                candidate = self._resolve_history_path(archive_root)
                if candidate.is_dir():
                    return candidate
        candidate = self.settings.history_dir / "jobs" / record.job_id
        if candidate.is_dir():
            return candidate.resolve()
        raise HistoryResultError("The saved output folder is unavailable")

    @staticmethod
    def _escape_like(value: str) -> str:
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    def list(
        self,
        *,
        query: str = "",
        workflow: str | None = None,
        status: str | None = None,
        limit: int = 20,
        offset: int = 0,
        created_from: str | None = None,
        created_to: str | None = None,
        pinned: bool | None = None,
        sort: str = "created_at",
        order: str = "desc",
    ) -> tuple[list[HistoryRecord], int]:
        self.initialize()
        clauses: list[str] = []
        parameters: list[Any] = []
        if query:
            clauses.append("input_name LIKE ? ESCAPE '\\'")
            parameters.append(f"%{self._escape_like(query)}%")
        if workflow:
            if workflow not in _WORKFLOWS:
                return [], 0
            clauses.append("workflow = ?")
            parameters.append(workflow)
        if status:
            if status not in _STATUSES:
                return [], 0
            clauses.append("status = ?")
            parameters.append(status)
        if created_from:
            clauses.append("created_at >= ?")
            parameters.append(created_from)
        if created_to:
            clauses.append("created_at <= ?")
            parameters.append(created_to)
        if pinned is not None:
            clauses.append("pinned = ?")
            parameters.append(int(pinned))
        order_columns = {
            "created_at": "created_at",
            "duration": "(julianday(completed_at) - julianday(started_at))",
            "input_name": "input_name COLLATE NOCASE",
            "quality": "quality_score",
        }
        order_column = order_columns.get(sort, "created_at")
        order_direction = "ASC" if order == "asc" else "DESC"
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) FROM history_jobs {where}",  # noqa: S608
                parameters,
            ).fetchone()[0]
            rows = connection.execute(
                f"SELECT * FROM history_jobs {where} "  # noqa: S608
                f"ORDER BY pinned DESC, {order_column} {order_direction} "  # noqa: S608
                "LIMIT ? OFFSET ?",
                [*parameters, limit, offset],
            ).fetchall()
        return [self._record_from_row(row) for row in rows], int(total)

    def set_pinned(self, job_id: str, pinned: bool) -> HistoryRecord:
        self.get(job_id)
        with self._connect() as connection:
            connection.execute(
                "UPDATE history_jobs SET pinned = ?, updated_at = ? WHERE job_id = ?",
                (int(pinned), datetime.now(timezone.utc).isoformat(), job_id),
            )
        return self.get(job_id)

    def set_analysis_summary(self, job_id: str, *, status: str, question_count: int) -> HistoryRecord:
        if status not in {"not_requested", "completed"}:
            raise ValueError("Unsupported Past Paper Intelligence status")
        self.get(job_id)
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE history_jobs
                SET analysis_status = ?, analysis_updated_at = ?, analysis_question_count = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (
                    status,
                    datetime.now(timezone.utc).isoformat(),
                    max(0, question_count),
                    datetime.now(timezone.utc).isoformat(),
                    job_id,
                ),
            )
        return self.get(job_id)

    def queue_institution_sync(
        self,
        job_id: str,
        *,
        tenant_id: str,
        device_id: str,
        payload: dict[str, Any],
    ) -> SyncOutboxRecord:
        """Queue one explicitly selected History result; never called automatically."""

        record = self.get(job_id)
        if record.status != "completed" or record.result_path is None:
            raise HistoryConflictError("Only completed OCR results can be selected for institution sync")
        if not tenant_id.strip() or len(tenant_id) > 128 or not device_id.strip() or len(device_id) > 128:
            raise ValueError("Institution tenant and device identifiers are invalid")
        canonical_payload = json_safe(payload)
        encoded = json.dumps(canonical_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        canonical_hash = hashlib.sha256(encoded).hexdigest()
        idempotency_key = hashlib.sha256(
            f"{tenant_id}\0{device_id}\0{job_id}\0{canonical_hash}".encode()
        ).hexdigest()
        existing: sqlite3.Row | None
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM institution_sync_outbox WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        if existing is not None:
            return SyncOutboxRecord(**dict(existing))
        event_id = uuid.uuid4().hex
        relative = Path("jobs") / job_id / "institution-sync" / f"{event_id}.json"
        payload_target = self.settings.history_dir / relative
        atomic_write_json(payload_target, canonical_payload)
        payload_sha256 = hashlib.sha256(payload_target.read_bytes()).hexdigest()
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO institution_sync_outbox (
                    event_id, job_id, tenant_id, device_id, status, attempt_count,
                    idempotency_key, payload_path, payload_sha256, error,
                    next_attempt_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'pending', 0, ?, ?, ?, NULL, NULL, ?, ?)
                """,
                (
                    event_id,
                    job_id,
                    tenant_id,
                    device_id,
                    idempotency_key,
                    relative.as_posix(),
                    payload_sha256,
                    now,
                    now,
                ),
            )
        return self.get_sync_event(event_id)

    def get_sync_event(self, event_id: str) -> SyncOutboxRecord:
        if not re.fullmatch(r"[0-9a-f]{32}", event_id):
            raise HistoryNotFoundError("Institution sync event not found")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM institution_sync_outbox WHERE event_id = ?", (event_id,)
            ).fetchone()
        if row is None:
            raise HistoryNotFoundError("Institution sync event not found")
        return SyncOutboxRecord(**dict(row))

    def list_sync_outbox(self, *, status: str | None = None) -> list[SyncOutboxRecord]:
        self.initialize()
        if status and status not in {"pending", "in_flight", "completed", "failed"}:
            return []
        with self._connect() as connection:
            if status:
                rows = connection.execute(
                    "SELECT * FROM institution_sync_outbox "
                    "WHERE status = ? ORDER BY created_at",
                    (status,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM institution_sync_outbox ORDER BY created_at"
                ).fetchall()
        return [SyncOutboxRecord(**dict(row)) for row in rows]

    def read_sync_payload(self, event: SyncOutboxRecord) -> dict[str, Any]:
        path = self._resolve_history_path(event.payload_path)
        try:
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != event.payload_sha256:
                raise HistoryResultError("Institution sync payload checksum failed")
            payload = json.loads(data)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise HistoryResultError("Institution sync payload is unavailable") from exc
        if not isinstance(payload, dict):
            raise HistoryResultError("Institution sync payload is malformed")
        return payload

    def update_sync_event(
        self,
        event_id: str,
        *,
        status: str,
        error: str | None = None,
        retry_after_seconds: int | None = None,
    ) -> SyncOutboxRecord:
        event = self.get_sync_event(event_id)
        if status not in {"pending", "in_flight", "completed", "failed"}:
            raise ValueError("Institution sync status is invalid")
        now = datetime.now(timezone.utc)
        next_attempt = (
            datetime.fromtimestamp(now.timestamp() + retry_after_seconds, timezone.utc).isoformat()
            if retry_after_seconds is not None
            else None
        )
        attempts = event.attempt_count + (1 if status == "in_flight" else 0)
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE institution_sync_outbox
                SET status = ?, attempt_count = ?, error = ?, next_attempt_at = ?, updated_at = ?
                WHERE event_id = ?
                """,
                (status, attempts, error, next_attempt, now.isoformat(), event_id),
            )
        return self.get_sync_event(event_id)

    def delete_many(self, job_ids: list[str]) -> int:
        unique_ids = list(dict.fromkeys(job_ids))
        for job_id in unique_ids:
            self._validate_job_id(job_id)
        records = [self.get(job_id) for job_id in unique_ids]
        if any(record.status in _ACTIVE_STATUSES for record in records):
            raise HistoryConflictError("Active OCR jobs cannot be deleted")
        with self._connect() as connection:
            connection.executemany(
                "DELETE FROM history_jobs WHERE job_id = ?",
                [(job_id,) for job_id in unique_ids],
            )
        for job_id in unique_ids:
            self._delete_job_directory(job_id)
        return len(unique_ids)

    def recover_interrupted(self) -> int:
        self.initialize()
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE history_jobs
                SET status = 'failed',
                    error = 'Processing was interrupted by a service restart',
                    completed_at = ?,
                    updated_at = ?
                WHERE status IN ('pending', 'processing')
                """,
                (now, now),
            )
        return cursor.rowcount

    def delete(self, job_id: str) -> None:
        record = self.get(job_id)
        if record.status in _ACTIVE_STATUSES:
            raise HistoryConflictError("An active OCR job cannot be deleted")
        with self._connect() as connection:
            connection.execute("DELETE FROM history_jobs WHERE job_id = ?", (job_id,))
        self._delete_job_directory(job_id)

    def clear_terminal(self, *, older_than_days: int | None = None) -> int:
        self.initialize()
        age_clause = ""
        parameters: list[Any] = []
        if older_than_days is not None:
            age_clause = " AND updated_at < datetime('now', ?)"
            parameters.append(f"-{older_than_days} days")
        with self._connect() as connection:
            if age_clause:
                rows = connection.execute(
                    "SELECT job_id FROM history_jobs "
                    "WHERE status IN ('completed', 'failed', 'cancelled') "
                    "AND updated_at < datetime('now', ?)",
                    parameters,
                ).fetchall()
                connection.execute(
                    "DELETE FROM history_jobs "
                    "WHERE status IN ('completed', 'failed', 'cancelled') "
                    "AND updated_at < datetime('now', ?)",
                    parameters,
                )
            else:
                rows = connection.execute(
                    "SELECT job_id FROM history_jobs "
                    "WHERE status IN ('completed', 'failed', 'cancelled')"
                ).fetchall()
                connection.execute(
                    "DELETE FROM history_jobs "
                    "WHERE status IN ('completed', 'failed', 'cancelled')"
                )
        for row in rows:
            self._delete_job_directory(str(row["job_id"]))
        return len(rows)

    def _delete_job_directory(self, job_id: str) -> None:
        self._validate_job_id(job_id)
        directory = (self.settings.history_dir / "jobs" / job_id).resolve()
        jobs_root = (self.settings.history_dir / "jobs").resolve()
        try:
            directory.relative_to(jobs_root)
        except ValueError as exc:
            raise HistoryResultError("History output path is invalid") from exc
        if directory.is_dir():
            shutil.rmtree(directory)


_store: HistoryStore | None = None
_store_lock = threading.Lock()


def get_history_store() -> HistoryStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = HistoryStore()
    return _store


def reset_history_store() -> None:
    global _store
    with _store_lock:
        _store = None
