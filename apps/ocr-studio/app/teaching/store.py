"""SQLite persistence for immutable-evidence teaching overlays."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from app.core.config import Settings, get_settings
from app.storage.history import HistoryStore

_CORRECTION_STATUSES = {"draft", "reviewed", "approved", "rejected", "reverted"}
_VERSION_STATUSES = {"draft", "published", "superseded", "retired"}
_BANK_STATUSES = {"draft", "reviewed", "approved", "rejected"}
_BATCH_STATUSES = {"draft", "queued", "processing", "paused", "completed", "partial", "cancelled"}


class TeachingValidationError(ValueError):
    """The requested teaching overlay is structurally invalid."""


class TeachingConflictError(RuntimeError):
    """The requested mutation conflicts with immutable or concurrent state."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id() -> str:
    return uuid.uuid4().hex


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback


def result_hash(result: Any) -> str:
    return hashlib.sha256(_json(result).encode("utf-8")).hexdigest()


class TeachingStore:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.history = HistoryStore(self.settings)
        self.history.initialize()
        self._migrate_legacy_topics()

    def _migrate_legacy_topics(self) -> None:
        """Copy pre-editor topic strings into a new taxonomy without touching analyses."""

        marker = self.settings.history_dir / ".legacy-topics-taxonomy-v1"
        if marker.exists() or self.list_taxonomies():
            return
        topics: set[str] = set()
        for path in (self.settings.history_dir / "jobs").glob("*/education/analysis.json"):
            try:
                analysis = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            for value in analysis.get("topic_taxonomy", []):
                if isinstance(value, str) and value.strip():
                    topics.add(value.strip())
            for question in analysis.get("questions", []):
                if not isinstance(question, Mapping):
                    continue
                teacher = question.get("teacher_override")
                ai = question.get("ai")
                for source in (teacher, ai):
                    value = source.get("topic") if isinstance(source, Mapping) else None
                    if isinstance(value, str) and value.strip():
                        topics.add(value.strip())
        if not topics:
            return
        root = "unmapped-legacy"
        nodes: list[dict[str, Any]] = [
            {
                "node_id": root,
                "label": "Unmapped legacy topics",
                "parent_id": None,
                "aliases": [],
                "description": "Copied from earlier free-text analyses; the original analyses remain unchanged.",
            }
        ]
        for topic in sorted(topics, key=str.casefold):
            identifier = hashlib.sha256(topic.casefold().encode()).hexdigest()[:16]
            nodes.append(
                {
                    "node_id": f"legacy-{identifier}",
                    "label": topic,
                    "parent_id": root,
                    "aliases": [],
                    "description": "Legacy free-text topic awaiting teacher mapping.",
                }
            )
        taxonomy = self.create_taxonomy(
            {
                "name": "Migrated legacy topics",
                "status": "published",
                "nodes": nodes,
                "actor": "local-migration",
            },
            taxonomy_id="legacy-topics",
        )
        marker.write_text(
            _json({"taxonomy_id": taxonomy["taxonomy_id"], "version": taxonomy["version"]}),
            encoding="utf-8",
        )

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.settings.history_db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def source_hash(self, job_id: str) -> str:
        record = self.history.get(job_id)
        return result_hash(self.history.read_result(record))

    def _audit(
        self,
        connection: sqlite3.Connection,
        *,
        entity_type: str,
        entity_id: str,
        action: str,
        actor: str,
        payload: Mapping[str, Any],
        job_id: str | None = None,
    ) -> dict[str, Any]:
        prior = connection.execute(
            "SELECT event_hash FROM teaching_audit_events "
            "WHERE entity_type = ? AND entity_id = ? ORDER BY created_at DESC, event_id DESC LIMIT 1",
            (entity_type, entity_id),
        ).fetchone()
        previous_hash = str(prior["event_hash"]) if prior else None
        event_id = _id()
        created_at = _now()
        material = {
            "event_id": event_id,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "job_id": job_id,
            "action": action,
            "actor": actor,
            "payload": payload,
            "previous_hash": previous_hash,
            "created_at": created_at,
        }
        event_hash = hashlib.sha256(_json(material).encode("utf-8")).hexdigest()
        connection.execute(
            "INSERT INTO teaching_audit_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                entity_type,
                entity_id,
                job_id,
                action,
                actor,
                _json(payload),
                previous_hash,
                event_hash,
                created_at,
            ),
        )
        return {**material, "event_hash": event_hash}

    def audit(self, *, job_id: str | None = None, entity_type: str | None = None, entity_id: str | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        for column, value in (("job_id", job_id), ("entity_type", entity_type), ("entity_id", entity_id)):
            if value is not None:
                clauses.append(f"{column} = ?")
                values.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM teaching_audit_events {where} ORDER BY created_at, event_id",  # noqa: S608
                values,
            ).fetchall()
        return [{**dict(row), "payload": _load(row["payload_json"], {})} for row in rows]

    @staticmethod
    def _correction(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        for source, target, fallback in (
            ("original_json", "original", None),
            ("replacement_json", "replacement", None),
            ("source_line_ids_json", "source_line_ids", []),
            ("geometry_json", "geometry", None),
        ):
            item[target] = _load(item.pop(source), fallback)
        item["etag"] = f'"correction-{item["correction_id"]}-v{item["version"]}"'
        return item

    def list_corrections(self, job_id: str) -> dict[str, Any]:
        current_hash = self.source_hash(job_id)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM correction_sets WHERE job_id = ? ORDER BY updated_at DESC",
                (job_id,),
            ).fetchall()
        items = [self._correction(row) for row in rows]
        for item in items:
            item["stale"] = item["source_hash"] != current_hash
        return {"job_id": job_id, "source_hash": current_hash, "items": items}

    def create_correction(self, job_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        target_kind = str(payload.get("target_kind", "")).strip()
        target_id = str(payload.get("target_id", "")).strip()
        reason = str(payload.get("reason", "")).strip()
        actor = str(payload.get("actor", "local-teacher")).strip()[:128]
        status = str(payload.get("status", "draft")).strip().casefold()
        if target_kind not in {"line", "block", "question", "marks", "table_cell", "reconstructed_text"}:
            raise TeachingValidationError("Correction target kind is invalid")
        if not target_id or len(target_id) > 256 or not reason or len(reason) > 1000:
            raise TeachingValidationError("Correction target and reason are required")
        if status not in _CORRECTION_STATUSES:
            raise TeachingValidationError("Correction status is invalid")
        correction_id = _id()
        source_hash = self.source_hash(job_id)
        record = self.history.get(job_id)
        evidence = self.history.read_result(record)
        original = payload.get("original")
        source_line_ids = [str(value) for value in payload.get("source_line_ids", [])][:500]
        geometry = payload.get("geometry")
        if target_kind in {"line", "block"}:
            raw_lines = evidence.get("lines", []) if isinstance(evidence, Mapping) else []
            source_line = next(
                (
                    value
                    for value in raw_lines
                    if isinstance(value, Mapping) and str(value.get("line_id")) == target_id
                ),
                None,
            )
            if source_line is None:
                raise TeachingValidationError("Correction references an unknown OCR source line")
            original = {"text": str(source_line.get("text", ""))}
            source_line_ids = [target_id]
            geometry = source_line.get("normalized_bbox")
        elif target_kind in {"question", "marks"}:
            from app.education.questions import segment_questions

            questions = segment_questions(evidence)["questions"] if isinstance(evidence, Mapping) else []
            question = next(
                (value for value in questions if str(value.get("question_id")) == target_id),
                None,
            )
            if question is None:
                raise TeachingValidationError("Correction references an unknown source question")
            original = (
                {"marks": question.get("marks"), "marks_status": question.get("marks_status")}
                if target_kind == "marks"
                else {"text": question.get("text"), "display_number": question.get("display_number")}
            )
            source_line_ids = list(question.get("source_line_ids", []))
            geometry = question.get("source_bbox")
        elif target_kind == "reconstructed_text" and target_id != "document":
            raise TeachingValidationError("The reconstructed document correction target is invalid")
        elif target_kind == "table_cell":
            known_lines = {
                str(value.get("line_id"))
                for value in (evidence.get("lines", []) if isinstance(evidence, Mapping) else [])
                if isinstance(value, Mapping) and value.get("line_id")
            }
            if not source_line_ids or not set(source_line_ids).issubset(known_lines):
                raise TeachingValidationError("A table-cell correction requires known source line IDs")
        if geometry is not None:
            if (
                not isinstance(geometry, Sequence)
                or isinstance(geometry, (str, bytes))
                or len(geometry) != 4
            ):
                raise TeachingValidationError("Correction geometry must contain four coordinates")
            try:
                left, top, right, bottom = (float(value) for value in geometry)
            except (TypeError, ValueError) as exc:
                raise TeachingValidationError("Correction geometry is invalid") from exc
            if not (0 <= left <= right <= 1 and 0 <= top <= bottom <= 1):
                raise TeachingValidationError("Correction geometry must be normalized to the source page")
            geometry = [left, top, right, bottom]
        now = _now()
        values = (
            correction_id,
            job_id,
            target_kind,
            target_id,
            _json(original),
            _json(payload.get("replacement")),
            reason,
            actor,
            status,
            source_hash,
            _json(source_line_ids),
            _json(geometry) if geometry is not None else None,
            now,
            now,
        )
        try:
            with self.connect() as connection:
                connection.execute(
                    """INSERT INTO correction_sets (
                    correction_id, job_id, target_kind, target_id, original_json,
                    replacement_json, reason, actor, status, source_hash,
                    source_line_ids_json, geometry_json, version, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                    values,
                )
                self._audit(connection, entity_type="correction", entity_id=correction_id, job_id=job_id, action="created", actor=actor, payload={"status": status, "target_kind": target_kind, "target_id": target_id})
        except sqlite3.IntegrityError as exc:
            raise TeachingConflictError("An active correction already exists for this target") from exc
        return self.get_correction(correction_id)

    def get_correction(self, correction_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM correction_sets WHERE correction_id = ?", (correction_id,)).fetchone()
        if row is None:
            raise KeyError("Correction not found")
        item = self._correction(row)
        item["stale"] = item["source_hash"] != self.source_hash(item["job_id"])
        return item

    @staticmethod
    def _etag_version(etag: str | None, correction_id: str) -> int:
        prefix = f'"correction-{correction_id}-v'
        if not etag or not etag.startswith(prefix) or not etag.endswith('"'):
            raise TeachingConflictError("A current If-Match ETag is required")
        try:
            return int(etag[len(prefix) : -1])
        except ValueError as exc:
            raise TeachingConflictError("The supplied ETag is invalid") from exc

    def update_correction(
        self,
        correction_id: str,
        payload: Mapping[str, Any],
        if_match: str | None,
        *,
        _allow_revert: bool = False,
    ) -> dict[str, Any]:
        existing = self.get_correction(correction_id)
        expected = self._etag_version(if_match, correction_id)
        if expected != existing["version"]:
            raise TeachingConflictError("The correction changed; reload before saving")
        if existing["stale"]:
            raise TeachingConflictError("The OCR evidence changed; rebuild this correction first")
        status = str(payload.get("status", existing["status"])).casefold()
        allowed_statuses = _CORRECTION_STATUSES if _allow_revert else _CORRECTION_STATUSES - {"reverted"}
        if status not in allowed_statuses:
            raise TeachingValidationError("Correction status is invalid")
        actor = str(payload.get("actor", "local-teacher"))[:128]
        replacement = payload.get("replacement", existing["replacement"])
        reason = str(payload.get("reason", existing["reason"]))[:1000]
        now = _now()
        with self.connect() as connection:
            changed = connection.execute(
                """UPDATE correction_sets SET replacement_json = ?, reason = ?, actor = ?,
                status = ?, version = version + 1, updated_at = ?
                WHERE correction_id = ? AND version = ?""",
                (_json(replacement), reason, actor, status, now, correction_id, expected),
            ).rowcount
            if changed != 1:
                raise TeachingConflictError("The correction changed; reload before saving")
            self._audit(connection, entity_type="correction", entity_id=correction_id, job_id=existing["job_id"], action="updated", actor=actor, payload={"from_status": existing["status"], "to_status": status, "replacement": replacement})
        return self.get_correction(correction_id)

    def revert_correction(self, correction_id: str, *, actor: str, if_match: str | None) -> dict[str, Any]:
        return self.update_correction(
            correction_id,
            {"status": "reverted", "actor": actor},
            if_match,
            _allow_revert=True,
        )

    @staticmethod
    def _validate_tree(nodes: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        clean: list[dict[str, Any]] = []
        identifiers: set[str] = set()
        aliases: set[str] = set()
        parents: dict[str, str | None] = {}
        for index, raw in enumerate(nodes):
            node_id = str(raw.get("node_id") or _id()).strip()
            label = str(raw.get("label", "")).strip()
            raw_parent = raw.get("parent_id")
            parent_id = str(raw_parent).strip() if raw_parent is not None else None
            parent_id = parent_id or None
            node_aliases = [str(value).strip() for value in raw.get("aliases", []) if str(value).strip()]
            if not node_id or node_id in identifiers or not label:
                raise TeachingValidationError("Taxonomy node IDs must be unique and labels are required")
            identifiers.add(node_id)
            for alias in [label, *node_aliases]:
                folded = alias.casefold()
                if folded in aliases:
                    raise TeachingValidationError(f"Duplicate taxonomy label or alias: {alias}")
                aliases.add(folded)
            parents[node_id] = parent_id
            clean.append({"node_id": node_id, "parent_id": parent_id, "label": label[:256], "aliases": node_aliases[:50], "description": str(raw.get("description", ""))[:2000], "sort_order": int(raw.get("sort_order", index))})
        for node_id, parent_id in parents.items():
            if parent_id and parent_id not in identifiers:
                raise TeachingValidationError(f"Unknown taxonomy parent: {parent_id}")
            visited = {node_id}
            current = parent_id
            while current:
                if current in visited:
                    raise TeachingValidationError("Taxonomy hierarchy contains a cycle")
                visited.add(current)
                current = parents.get(current)
        return clean

    def create_taxonomy(self, payload: Mapping[str, Any], *, taxonomy_id: str | None = None) -> dict[str, Any]:
        taxonomy_id = taxonomy_id or _id()
        nodes = self._validate_tree(payload.get("nodes", []))
        name = str(payload.get("name", "Untitled taxonomy")).strip()[:256]
        actor = str(payload.get("actor", "local-teacher"))[:128]
        status = str(payload.get("status", "draft")).casefold()
        if status not in _VERSION_STATUSES:
            raise TeachingValidationError("Taxonomy status is invalid")
        with self.connect() as connection:
            row = connection.execute("SELECT COALESCE(MAX(version), 0) FROM taxonomy_versions WHERE taxonomy_id = ?", (taxonomy_id,)).fetchone()
            version = int(row[0]) + 1
            if status == "published":
                connection.execute("UPDATE taxonomy_versions SET status = 'superseded' WHERE taxonomy_id = ? AND status = 'published'", (taxonomy_id,))
            now = _now()
            connection.execute("INSERT INTO taxonomy_versions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (taxonomy_id, version, name, str(payload.get("subject", ""))[:128] or None, str(payload.get("academic_level", ""))[:128] or None, status, payload.get("source_hash"), actor, now))
            connection.executemany(
                "INSERT INTO taxonomy_nodes VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(taxonomy_id, version, node["node_id"], node["parent_id"], node["label"], _json(node["aliases"]), node["description"], node["sort_order"]) for node in nodes],
            )
            self._audit(connection, entity_type="taxonomy", entity_id=taxonomy_id, action="version_created", actor=actor, payload={"version": version, "status": status, "node_count": len(nodes)})
        return self.get_taxonomy(taxonomy_id, version)

    def get_taxonomy(self, taxonomy_id: str, version: int | None = None) -> dict[str, Any]:
        with self.connect() as connection:
            if version is None:
                header = connection.execute("SELECT * FROM taxonomy_versions WHERE taxonomy_id = ? ORDER BY version DESC LIMIT 1", (taxonomy_id,)).fetchone()
            else:
                header = connection.execute("SELECT * FROM taxonomy_versions WHERE taxonomy_id = ? AND version = ?", (taxonomy_id, version)).fetchone()
            if header is None:
                raise KeyError("Taxonomy not found")
            nodes = connection.execute("SELECT * FROM taxonomy_nodes WHERE taxonomy_id = ? AND taxonomy_version = ? ORDER BY sort_order, label", (taxonomy_id, header["version"])).fetchall()
        return {**dict(header), "nodes": [{**dict(row), "aliases": _load(row["aliases_json"], [])} for row in nodes]}

    def list_taxonomies(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute("""SELECT t.* FROM taxonomy_versions t JOIN (
                SELECT taxonomy_id, MAX(version) version FROM taxonomy_versions GROUP BY taxonomy_id
            ) latest USING (taxonomy_id, version) ORDER BY name""").fetchall()
        return [dict(row) for row in rows]

    def save_syllabus(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        syllabus_id = str(payload.get("syllabus_id") or _id())
        objectives = self._validate_objectives(payload.get("objectives", []))
        actor = str(payload.get("actor", "local-teacher"))[:128]
        status = str(payload.get("status", "draft")).casefold()
        if status not in _VERSION_STATUSES:
            raise TeachingValidationError("Syllabus status is invalid")
        if status == "published" and any(
            item["inferred"] and not item["confirmed"] for item in objectives
        ):
            raise TeachingValidationError(
                "Every inferred objective must be teacher-confirmed before publication"
            )
        with self.connect() as connection:
            next_version = int(connection.execute("SELECT COALESCE(MAX(version), 0) FROM syllabus_versions WHERE syllabus_id = ?", (syllabus_id,)).fetchone()[0]) + 1
            if status == "published":
                connection.execute("UPDATE syllabus_versions SET status = 'superseded' WHERE syllabus_id = ? AND status = 'published'", (syllabus_id,))
            now = _now()
            connection.execute("INSERT INTO syllabus_versions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (syllabus_id, next_version, str(payload.get("name", "Imported syllabus"))[:256], str(payload.get("subject", ""))[:128] or None, str(payload.get("academic_level", ""))[:128] or None, status, str(payload.get("source_type", "json")), str(payload["source_checksum"]), payload.get("effective_from"), payload.get("effective_to"), _json(payload.get("warnings", [])), actor, now))
            connection.executemany("INSERT INTO syllabus_objectives VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [(syllabus_id, next_version, item["objective_id"], item["parent_id"], item["code"], item["label"], item["description"], _json(item["aliases"]), int(item["inferred"]), int(item["confirmed"])) for item in objectives])
            self._audit(connection, entity_type="syllabus", entity_id=syllabus_id, action="version_created", actor=actor, payload={"version": next_version, "status": status, "objective_count": len(objectives)})
        return self.get_syllabus(syllabus_id, next_version)

    @staticmethod
    def _validate_objectives(values: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        converted = [{"node_id": value.get("objective_id") or value.get("code") or _id(), "parent_id": value.get("parent_id"), "label": value.get("label") or value.get("description"), "aliases": value.get("aliases", []), "description": value.get("description", ""), "sort_order": index} for index, value in enumerate(values)]
        checked = TeachingStore._validate_tree(converted)
        original = list(values)
        return [{"objective_id": item["node_id"], "parent_id": item["parent_id"], "code": str(original[index].get("code", ""))[:128] or None, "label": item["label"], "description": item["description"], "aliases": item["aliases"], "inferred": bool(original[index].get("inferred")), "confirmed": bool(original[index].get("confirmed", not original[index].get("inferred")))} for index, item in enumerate(checked)]

    def get_syllabus(self, syllabus_id: str, version: int | None = None) -> dict[str, Any]:
        with self.connect() as connection:
            if version is None:
                header = connection.execute("SELECT * FROM syllabus_versions WHERE syllabus_id = ? ORDER BY version DESC LIMIT 1", (syllabus_id,)).fetchone()
            else:
                header = connection.execute("SELECT * FROM syllabus_versions WHERE syllabus_id = ? AND version = ?", (syllabus_id, version)).fetchone()
            if header is None:
                raise KeyError("Syllabus not found")
            rows = connection.execute("SELECT * FROM syllabus_objectives WHERE syllabus_id = ? AND syllabus_version = ? ORDER BY code, label", (syllabus_id, header["version"])).fetchall()
        return {**dict(header), "warnings": _load(header["warnings_json"], []), "objectives": [{**dict(row), "aliases": _load(row["aliases_json"], []), "inferred": bool(row["inferred"]), "confirmed": bool(row["confirmed"])} for row in rows]}

    def list_syllabi(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT s.syllabus_id, s.version FROM syllabus_versions s JOIN (
                SELECT syllabus_id, MAX(version) version FROM syllabus_versions GROUP BY syllabus_id
                ) latest USING (syllabus_id, version) ORDER BY s.name"""
            ).fetchall()
        return [self.get_syllabus(str(row["syllabus_id"]), int(row["version"])) for row in rows]

    def save_mapping(self, syllabus_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        mapping_id = _id()
        job_id = str(payload.get("job_id", ""))
        source_hash = self.source_hash(job_id)
        version = int(payload.get("syllabus_version") or self.get_syllabus(syllabus_id)["version"])
        status = str(payload.get("status", "draft")).casefold()
        if status not in {"draft", "approved", "rejected"}:
            raise TeachingValidationError("Mapping status is invalid")
        method = str(payload.get("method", "teacher"))
        if method != "teacher" and status != "draft":
            raise TeachingValidationError(
                "Deterministic and AI-assisted mapping suggestions must remain drafts"
            )
        now = _now()
        actor = str(payload.get("actor", "local-teacher"))[:128]
        with self.connect() as connection:
            connection.execute("INSERT INTO syllabus_mappings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (mapping_id, job_id, str(payload["question_id"]), syllabus_id, version, str(payload["objective_id"]), payload.get("topic_id"), status, method, payload.get("confidence"), actor, source_hash, now, now))
            self._audit(connection, entity_type="syllabus_mapping", entity_id=mapping_id, job_id=job_id, action="created", actor=actor, payload={"status": status, "objective_id": payload["objective_id"]})
        return self.get_mapping(mapping_id)

    def get_mapping(self, mapping_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM syllabus_mappings WHERE mapping_id = ?", (mapping_id,)).fetchone()
        if row is None:
            raise KeyError("Mapping not found")
        item = dict(row)
        item["stale"] = item["source_hash"] != self.source_hash(item["job_id"])
        return item

    def list_mappings(self, syllabus_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM syllabus_mappings WHERE syllabus_id = ? ORDER BY updated_at DESC", (syllabus_id,)).fetchall()
        return [dict(row) for row in rows]

    def upsert_bank_item(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        job_id = str(payload.get("job_id", ""))
        question_id = str(payload.get("question_id", ""))
        if not job_id or not question_id:
            raise TeachingValidationError("Job and question IDs are required")
        source_hash = self.source_hash(job_id)
        record = self.history.get(job_id)
        evidence = self.history.read_result(record)
        from app.education.questions import segment_questions

        questions = segment_questions(evidence)["questions"] if isinstance(evidence, Mapping) else []
        source_question = next(
            (item for item in questions if str(item.get("question_id")) == question_id),
            None,
        )
        if source_question is None:
            raise TeachingValidationError(
                "Question-bank item references an unknown source question"
            )
        bank_payload = dict(payload.get("payload", {}))
        requested_language = str(bank_payload.get("language", "en")).strip().casefold()
        if requested_language != "en" or str(record.language or "en").strip().casefold() != "en":
            raise TeachingValidationError(
                "This PredixaLearn release accepts English question-bank items only"
            )
        bank_payload.setdefault("text", source_question.get("text"))
        bank_payload.update(
            {
                "source_job_id": job_id,
                "source_page": source_question.get("source_page"),
                "source_line_ids": source_question.get("source_line_ids", []),
                "source_geometry": source_question.get("source_bbox"),
                "raw_ocr_text": source_question.get("text"),
                "ocr_confidence": source_question.get("ocr_confidence"),
                "alignment_confidence": bank_payload.get("alignment_confidence"),
                "ai_confidence": bank_payload.get("ai_confidence"),
                "language": "en",
                "provenance": "immutable OCR evidence plus approved teacher overlays",
            }
        )
        if not str(bank_payload.get("text", "")).strip():
            raise TeachingValidationError("Question-bank text is required")
        status = str(payload.get("status", "draft")).casefold()
        if status not in _BANK_STATUSES:
            raise TeachingValidationError("Question-bank status is invalid")
        now = _now()
        actor = str(payload.get("actor", "local-teacher"))[:128]
        with self.connect() as connection:
            prior = connection.execute("SELECT * FROM question_bank_items WHERE job_id = ? AND question_id = ?", (job_id, question_id)).fetchone()
            if prior:
                item_id = str(prior["item_id"])
                version = int(prior["version"]) + 1
                connection.execute("UPDATE question_bank_items SET source_hash = ?, payload_json = ?, status = ?, version = ?, approved_by = ?, updated_at = ? WHERE item_id = ?", (source_hash, _json(bank_payload), status, version, actor if status == "approved" else None, now, item_id))
                action = "updated"
            else:
                item_id = _id()
                connection.execute("INSERT INTO question_bank_items VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)", (item_id, job_id, question_id, source_hash, _json(bank_payload), status, actor if status == "approved" else None, now, now))
                action = "created"
            self._audit(connection, entity_type="question_bank_item", entity_id=item_id, job_id=job_id, action=action, actor=actor, payload={"status": status, "question_id": question_id})
        return self.get_bank_item(item_id)

    def get_bank_item(self, item_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM question_bank_items WHERE item_id = ?", (item_id,)).fetchone()
        if row is None:
            raise KeyError("Question-bank item not found")
        item = dict(row)
        item["payload"] = _load(item.pop("payload_json"), {})
        item["stale"] = item["source_hash"] != self.source_hash(item["job_id"])
        item["etag"] = f'"question-bank-{item_id}-v{item["version"]}"'
        return item

    def list_bank(self, *, status: str | None = None, query: str = "", limit: int = 200) -> list[dict[str, Any]]:
        clauses, values = [], []
        if status:
            clauses.append("status = ?")
            values.append(status)
        if query:
            clauses.append("payload_json LIKE ?")
            values.append(f"%{query.replace('%', '')}%")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect() as connection:
            rows = connection.execute(f"SELECT * FROM question_bank_items {where} ORDER BY updated_at DESC LIMIT ?", [*values, min(500, max(1, limit))]).fetchall()  # noqa: S608
        return [self.get_bank_item(str(row["item_id"])) for row in rows]

    def save_duplicate_decision(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        left, right = sorted((str(payload["left_question_key"]), str(payload["right_question_key"])))
        method = str(payload.get("method", "token_shingle"))
        now = _now()
        with self.connect() as connection:
            row = connection.execute("SELECT decision_id FROM duplicate_decisions WHERE left_question_key = ? AND right_question_key = ? AND method = ?", (left, right, method)).fetchone()
            if row:
                decision_id = str(row[0])
                connection.execute("UPDATE duplicate_decisions SET score = ?, disposition = ?, explanation_json = ?, actor = ?, updated_at = ? WHERE decision_id = ?", (float(payload["score"]), payload.get("disposition"), _json(payload.get("explanation", {})), payload.get("actor"), now, decision_id))
            else:
                decision_id = _id()
                connection.execute("INSERT INTO duplicate_decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (decision_id, left, right, method, float(payload["score"]), payload.get("disposition"), _json(payload.get("explanation", {})), payload.get("actor"), now, now))
        return {"decision_id": decision_id, **payload, "left_question_key": left, "right_question_key": right, "updated_at": now}

    def save_comparison(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        comparison_id = _id()
        now = _now()
        with self.connect() as connection:
            connection.execute("INSERT INTO comparison_reports VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (comparison_id, str(payload.get("name", "Paper comparison"))[:256], _json(payload["job_ids"]), _json(payload["source_hashes"]), _json(payload.get("analysis_versions", {})), payload.get("taxonomy_ref"), payload.get("syllabus_ref"), _json(payload["report"]), str(payload.get("actor", "local-teacher"))[:128], now))
            self._audit(connection, entity_type="comparison", entity_id=comparison_id, action="created", actor=str(payload.get("actor", "local-teacher")), payload={"job_ids": payload["job_ids"]})
        return {"comparison_id": comparison_id, **payload, "created_at": now}

    def list_comparisons(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM comparison_reports ORDER BY created_at DESC").fetchall()
        return [{**dict(row), "job_ids": _load(row["job_ids_json"], []), "source_hashes": _load(row["source_hashes_json"], {}), "analysis_versions": _load(row["analysis_versions_json"], {}), "report": _load(row["report_json"], {})} for row in rows]

    def save_revision_pack(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        pack_id = str(payload.get("pack_id") or _id())
        now = _now()
        status = str(payload.get("status", "draft")).casefold()
        if status not in {"draft", "reviewed", "approved", "retired"}:
            raise TeachingValidationError("Revision-pack status is invalid")
        language = str(payload.get("language", "en")).strip().casefold()
        if language != "en":
            raise TeachingValidationError(
                "This PredixaLearn release accepts English revision packs only"
            )
        actor = str(payload.get("actor", "local-teacher"))[:128]
        with self.connect() as connection:
            connection.execute("INSERT INTO revision_packs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(pack_id) DO UPDATE SET title = excluded.title, status = excluded.status, course_ref = excluded.course_ref, cohort_ref = excluded.cohort_ref, language = excluded.language, source_items_json = excluded.source_items_json, payload_json = excluded.payload_json, approved_by = excluded.approved_by, updated_at = excluded.updated_at", (pack_id, str(payload.get("title", "Revision pack"))[:256], status, payload.get("course_ref"), payload.get("cohort_ref"), language, _json(payload.get("source_item_ids", [])), _json(payload.get("payload", {})), actor, actor if status == "approved" else None, now, now))
            self._audit(connection, entity_type="revision_pack", entity_id=pack_id, action="saved", actor=actor, payload={"status": status})
        return self.get_revision_pack(pack_id)

    def get_revision_pack(self, pack_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM revision_packs WHERE pack_id = ?", (pack_id,)).fetchone()
        if row is None:
            raise KeyError("Revision pack not found")
        item = dict(row)
        item["source_item_ids"] = _load(item.pop("source_items_json"), [])
        item["payload"] = _load(item.pop("payload_json"), {})
        return item

    def list_revision_packs(self, *, approved_only: bool = False) -> list[dict[str, Any]]:
        with self.connect() as connection:
            if approved_only:
                rows = connection.execute(
                    "SELECT pack_id FROM revision_packs WHERE status = 'approved' "
                    "ORDER BY updated_at DESC"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT pack_id FROM revision_packs ORDER BY updated_at DESC"
                ).fetchall()
        return [self.get_revision_pack(str(row[0])) for row in rows]

    def create_batch(self, payload: Mapping[str, Any], items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        if not items or len(items) > 100:
            raise TeachingValidationError("A batch must contain between 1 and 100 files")
        batch_id = _id()
        now = _now()
        status = str(payload.get("status", "queued"))
        if status not in _BATCH_STATUSES:
            raise TeachingValidationError("Batch status is invalid")
        language = str(payload.get("language", "en")).strip().casefold()
        if language != "en":
            raise TeachingValidationError(
                "This PredixaLearn release accepts English batch documents only"
            )
        for item in items:
            overrides = item.get("overrides", {})
            if isinstance(overrides, Mapping) and str(
                overrides.get("language", "en")
            ).strip().casefold() != "en":
                raise TeachingValidationError(
                    "Per-file batch language overrides must use English ('en')"
                )
        with self.connect() as connection:
            connection.execute("INSERT INTO batches VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (batch_id, str(payload.get("name", "OCR batch"))[:256], status, str(payload.get("workflow", "text_recognition")), language, payload.get("document_profile"), _json(payload.get("remove_terms", [])), _json(payload.get("settings", {})), str(payload.get("actor", "local-teacher"))[:128], now, now))
            connection.executemany(
                """INSERT INTO batch_items (
                item_id, batch_id, position, input_name, staged_path, source_sha256,
                media_type, size_bytes, overrides_json, status, job_id, retry_pending,
                error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', NULL, 0, NULL, ?, ?)""",
                [
                    (
                        _id(),
                        batch_id,
                        index,
                        item["input_name"],
                        item["staged_path"],
                        item["source_sha256"],
                        item["media_type"],
                        int(item["size_bytes"]),
                        _json(item.get("overrides", {})),
                        now,
                        now,
                    )
                    for index, item in enumerate(items)
                ],
            )
            self._audit(connection, entity_type="batch", entity_id=batch_id, action="submitted", actor=str(payload.get("actor", "local-teacher")), payload={"item_count": len(items)})
        return self.get_batch(batch_id)

    def get_batch(self, batch_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            header = connection.execute("SELECT * FROM batches WHERE batch_id = ?", (batch_id,)).fetchone()
            if header is None:
                raise KeyError("Batch not found")
            rows = connection.execute("SELECT * FROM batch_items WHERE batch_id = ? ORDER BY position", (batch_id,)).fetchall()
        item = dict(header)
        item["remove_terms"] = _load(item.pop("remove_terms_json"), [])
        item["settings"] = _load(item.pop("settings_json"), {})
        item["items"] = []
        for row in rows:
            batch_item = dict(row)
            batch_item["overrides"] = _load(batch_item.pop("overrides_json"), {})
            item["items"].append(batch_item)
        return item

    def list_batches(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute("SELECT batch_id FROM batches ORDER BY created_at DESC LIMIT 100").fetchall()
        return [self.get_batch(str(row[0])) for row in rows]

    def patch_batch(self, batch_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        existing = self.get_batch(batch_id)
        status = str(payload.get("status", existing["status"]))
        if status not in _BATCH_STATUSES:
            raise TeachingValidationError("Batch status is invalid")
        with self.connect() as connection:
            connection.execute("UPDATE batches SET status = ?, updated_at = ? WHERE batch_id = ?", (status, _now(), batch_id))
            self._audit(connection, entity_type="batch", entity_id=batch_id, action="status_changed", actor=str(payload.get("actor", "local-teacher")), payload={"from": existing["status"], "to": status})
        return self.get_batch(batch_id)

    def patch_batch_item(self, batch_id: str, item_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {"queued", "processing", "completed", "failed", "cancelled", "interrupted"}
        status = str(payload.get("status", ""))
        if status not in allowed:
            raise TeachingValidationError("Batch item status is invalid")
        with self.connect() as connection:
            changed = connection.execute("UPDATE batch_items SET status = ?, job_id = COALESCE(?, job_id), retry_pending = ?, error = ?, updated_at = ? WHERE batch_id = ? AND item_id = ?", (status, payload.get("job_id"), int(bool(payload.get("retry_pending"))), payload.get("error"), _now(), batch_id, item_id)).rowcount
            if changed != 1:
                raise KeyError("Batch item not found")
        return next(item for item in self.get_batch(batch_id)["items"] if item["item_id"] == item_id)

    def reorder_batch_item(self, batch_id: str, item_id: str, *, direction: int) -> dict[str, Any]:
        if direction not in {-1, 1}:
            raise TeachingValidationError("Batch reorder direction is invalid")
        batch = self.get_batch(batch_id)
        if batch["status"] not in {"draft", "queued", "paused"}:
            raise TeachingConflictError("Only queued or paused batches can be reordered")
        items = batch["items"]
        index = next((offset for offset, item in enumerate(items) if item["item_id"] == item_id), None)
        if index is None:
            raise KeyError("Batch item not found")
        target = index + direction
        if target < 0 or target >= len(items):
            return items[index]
        if items[index]["status"] == "processing" or items[target]["status"] == "processing":
            raise TeachingConflictError("A processing item cannot be reordered")
        now = _now()
        with self.connect() as connection:
            connection.execute("UPDATE batch_items SET position = -1 WHERE item_id = ?", (items[index]["item_id"],))
            connection.execute("UPDATE batch_items SET position = ?, updated_at = ? WHERE item_id = ?", (items[index]["position"], now, items[target]["item_id"]))
            connection.execute("UPDATE batch_items SET position = ?, updated_at = ? WHERE item_id = ?", (items[target]["position"], now, items[index]["item_id"]))
            self._audit(connection, entity_type="batch", entity_id=batch_id, action="item_reordered", actor="local-teacher", payload={"item_id": item_id, "direction": direction})
        return next(item for item in self.get_batch(batch_id)["items"] if item["item_id"] == item_id)

    def prioritize_batch_item(self, batch_id: str, item_id: str) -> dict[str, Any]:
        batch = self.get_batch(batch_id)
        if batch["status"] not in {"draft", "queued", "paused"}:
            raise TeachingConflictError("Only queued or paused batches can be prioritized")
        selected = next(
            (item for item in batch["items"] if item["item_id"] == item_id),
            None,
        )
        if selected is None:
            raise KeyError("Batch item not found")
        if selected["status"] != "queued":
            raise TeachingConflictError("Only a queued item can be processed next")
        ordered = [selected, *(item for item in batch["items"] if item["item_id"] != item_id)]
        now = _now()
        with self.connect() as connection:
            connection.execute(
                "UPDATE batch_items SET position = position + 1000 WHERE batch_id = ?",
                (batch_id,),
            )
            connection.executemany(
                "UPDATE batch_items SET position = ?, updated_at = ? WHERE item_id = ?",
                [(position, now, item["item_id"]) for position, item in enumerate(ordered)],
            )
            self._audit(
                connection,
                entity_type="batch",
                entity_id=batch_id,
                action="item_prioritized",
                actor="local-teacher",
                payload={"item_id": item_id},
            )
        return next(
            item
            for item in self.get_batch(batch_id)["items"]
            if item["item_id"] == item_id
        )

    def recover_batches(self) -> int:
        now = _now()
        with self.connect() as connection:
            count = connection.execute("UPDATE batch_items SET status = 'interrupted', error = 'Service stopped during processing', updated_at = ? WHERE status = 'processing'", (now,)).rowcount
            connection.execute("UPDATE batch_items SET status = 'queued', retry_pending = 1, updated_at = ? WHERE status = 'interrupted'", (now,))
            connection.execute("UPDATE batches SET status = 'queued', updated_at = ? WHERE status = 'processing'", (now,))
        return int(count)

    def delete_batch(self, batch_id: str) -> list[str]:
        batch = self.get_batch(batch_id)
        if any(item["status"] == "processing" for item in batch["items"]):
            raise TeachingConflictError("A processing batch cannot be deleted")
        with self.connect() as connection:
            connection.execute("DELETE FROM batches WHERE batch_id = ?", (batch_id,))
        return [str(item["staged_path"]) for item in batch["items"]]

    def dashboard_counts(self) -> dict[str, Any]:
        with self.connect() as connection:
            def scalar(sql: str) -> int:
                return int(connection.execute(sql).fetchone()[0])

            return {
                "pending_corrections": scalar("SELECT COUNT(*) FROM correction_sets WHERE status IN ('draft', 'reviewed')"),
                "low_confidence_jobs": scalar("SELECT COUNT(*) FROM history_jobs WHERE status = 'completed' AND quality_score IS NOT NULL AND quality_score < 0.70"),
                "question_bank_pending": scalar("SELECT COUNT(*) FROM question_bank_items WHERE status != 'approved'"),
                "approved_questions": scalar("SELECT COUNT(*) FROM question_bank_items WHERE status = 'approved'"),
                "active_batches": scalar("SELECT COUNT(*) FROM batches WHERE status IN ('queued', 'processing', 'paused')"),
                "unmapped_questions": scalar("SELECT MAX(0, (SELECT COALESCE(SUM(analysis_question_count), 0) FROM history_jobs) - (SELECT COUNT(DISTINCT job_id || ':' || question_id) FROM syllabus_mappings WHERE status = 'approved'))"),
                "recent_audit": self.audit()[-20:],
            }
