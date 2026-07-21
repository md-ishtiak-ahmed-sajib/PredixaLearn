"""Validated curriculum import/export and objective-coverage reporting."""

from __future__ import annotations

import csv
import io
import json
import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import Field
from sqlalchemy.orm import Session

from .api import db_session, service_for
from .models import Resource
from .schemas import LearningObjectiveCreate, StrictModel
from .security import Principal, require

router = APIRouter(prefix="/api/v2", tags=["curriculum"])
MAX_CURRICULUM_IMPORT_BYTES = 5 * 1024 * 1024
WORD = re.compile(r"[A-Za-z0-9]{3,}")


class MappingSuggestionRequest(StrictModel):
    source_resource_ids: list[str] = Field(min_length=1, max_length=100)
    limit_per_source: int = Field(default=5, ge=1, le=20)


def _tokens(value: str) -> set[str]:
    return {token.casefold() for token in WORD.findall(value)}


def _csv_items(data: bytes) -> list[dict[str, Any]]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=422, detail={"code": "invalid_encoding", "message": "Curriculum CSV must be UTF-8"}) from exc
    return [dict(row) for row in csv.DictReader(io.StringIO(text))]


def _json_items(data: bytes) -> list[dict[str, Any]]:
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail={"code": "invalid_json", "message": "Curriculum JSON is invalid"}) from exc
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict) and isinstance(value.get("CFItems"), list):
        items = []
        for raw in value["CFItems"]:
            if not isinstance(raw, dict):
                continue
            items.append(
                {
                    "stable_code": raw.get("humanCodingScheme") or raw.get("identifier"),
                    "title": raw.get("abbreviatedStatement") or raw.get("fullStatement"),
                    "description": raw.get("fullStatement"),
                    "parent_code": raw.get("parent_code"),
                    "prerequisites": raw.get("prerequisites", []),
                    "replaces": raw.get("replaces", []),
                    "case_identifier": raw.get("identifier"),
                    "case_uri": raw.get("uri"),
                }
            )
        return items
    raise HTTPException(status_code=422, detail={"code": "invalid_curriculum_shape", "message": "Expected an objective list or IMS CASE CFItems"})


def _list_value(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value or "").split("|") if item.strip()]


def validate_import(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    normalized: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    codes: set[str] = set()
    for index, raw in enumerate(items, start=1):
        code = str(raw.get("stable_code") or raw.get("identifier") or "").strip()
        title = str(raw.get("title") or raw.get("description") or raw.get("fullStatement") or "").strip()
        description = str(raw.get("description") or raw.get("fullStatement") or title).strip()
        if not code or not title or not description:
            errors.append({"row": index, "code": "required_field", "message": "stable_code, title, and description are required"})
            continue
        if code.casefold() in codes:
            errors.append({"row": index, "code": "duplicate_code", "message": f"Duplicate objective code: {code}"})
            continue
        codes.add(code.casefold())
        normalized.append(
            {
                "stable_code": code,
                "title": title,
                "description": description,
                "parent_code": str(raw.get("parent_code") or "").strip() or None,
                "prerequisites": _list_value(raw.get("prerequisites")),
                "replaces": _list_value(raw.get("replaces")),
                "case_identifier": raw.get("case_identifier"),
                "case_uri": raw.get("case_uri"),
            }
        )
    known = {item["stable_code"].casefold() for item in normalized}
    for index, item in enumerate(normalized, start=1):
        references = [item["parent_code"], *item["prerequisites"], *item["replaces"]]
        missing = sorted(
            str(reference) for reference in references if reference and str(reference).casefold() not in known
        )
        if missing:
            errors.append({"row": index, "code": "unresolved_reference", "message": f"Unknown objective reference(s): {', '.join(missing)}"})
    return normalized, errors


@router.post("/curricula/versions/{version_id}/import")
async def import_curriculum_objectives(
    version_id: str,
    request: Request,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("curriculum:write"))],
    file: Annotated[UploadFile, File()],
    commit: Annotated[bool, Query()] = False,
) -> dict[str, Any]:
    service = service_for(session, principal, request)
    version = service.get("curriculum_version", version_id)
    if version.status != "draft":
        raise HTTPException(status_code=409, detail={"code": "immutable_resource", "message": "Only draft curriculum versions can import objectives"})
    data = await file.read(MAX_CURRICULUM_IMPORT_BYTES + 1)
    if len(data) > MAX_CURRICULUM_IMPORT_BYTES:
        raise HTTPException(status_code=413, detail={"code": "curriculum_import_too_large", "message": "Curriculum import exceeds 5 MiB"})
    items = _csv_items(data) if (file.filename or "").casefold().endswith(".csv") else _json_items(data)
    normalized, errors = validate_import(items)
    if errors or not commit:
        return {
            "valid": not errors,
            "committed": False,
            "objective_count": len(normalized),
            "errors": errors,
        }
    created = []
    for item in normalized:
        body = LearningObjectiveCreate(
            title=item["title"],
            parent_id=version_id,
            academic_unit_id=version.academic_unit_id,
            course_id=version.course_id,
            access_classification=version.access_classification,
            stable_code=item["stable_code"],
            description=item["description"],
            prerequisites=item["prerequisites"],
            replaces=item["replaces"],
            payload={
                "parent_code": item["parent_code"],
                "case_identifier": item["case_identifier"],
                "case_uri": item["case_uri"],
            },
        )
        created.append(service.create("learning_objective", body).resource_id)
    return {"valid": True, "committed": True, "objective_count": len(created), "objective_ids": created, "errors": []}


@router.get("/curricula/versions/{version_id}/case")
async def export_curriculum_case(
    version_id: str,
    request: Request,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("curriculum:read"))],
) -> JSONResponse:
    service = service_for(session, principal, request)
    version = service.get("curriculum_version", version_id)
    objectives = list(
        session.scalars(
            service._query("learning_objective").where(Resource.parent_id == version_id)  # noqa: SLF001
        )
    )
    payload = {
        "CFDocument": {
            "identifier": version.resource_id,
            "title": version.title,
            "lastChangeDateTime": version.updated_at.isoformat(),
            "language": "en",
            "version": version.payload.get("version_label"),
        },
        "CFItems": [
            {
                "identifier": item.payload.get("case_identifier") or item.resource_id,
                "uri": item.payload.get("case_uri"),
                "fullStatement": item.payload.get("description"),
                "humanCodingScheme": item.payload.get("stable_code"),
                "abbreviatedStatement": item.title,
                "parent_code": item.payload.get("parent_code"),
                "prerequisites": item.payload.get("prerequisites", []),
                "replaces": item.payload.get("replaces", []),
            }
            for item in objectives
        ],
    }
    return JSONResponse(payload, headers={"Content-Disposition": f'attachment; filename="curriculum-{version_id}.json"'})


@router.get("/curricula/versions/{version_id}/coverage")
async def curriculum_coverage(
    version_id: str,
    request: Request,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("curriculum:read"))],
) -> dict[str, Any]:
    service = service_for(session, principal, request)
    service.get("curriculum_version", version_id)
    objectives = list(
        session.scalars(service._query("learning_objective").where(Resource.parent_id == version_id))  # noqa: SLF001
    )
    mappings = list(
        session.scalars(
            service._query("objective_mapping").where(  # noqa: SLF001
                Resource.payload["curriculum_version_id"].as_string() == version_id
            )
        )
    )
    counts: dict[str, int] = {}
    for mapping in mappings:
        objective_id = str(mapping.payload.get("objective_id", ""))
        counts[objective_id] = counts.get(objective_id, 0) + 1
    coverage = [
        {
            "objective_id": item.resource_id,
            "stable_code": item.payload.get("stable_code"),
            "title": item.title,
            "mapping_count": counts.get(item.resource_id, 0),
            "coverage": "unmapped" if counts.get(item.resource_id, 0) == 0 else "mapped",
        }
        for item in objectives
    ]
    return {
        "curriculum_version_id": version_id,
        "objective_count": len(objectives),
        "mapped_count": sum(item["mapping_count"] > 0 for item in coverage),
        "unmapped_count": sum(item["mapping_count"] == 0 for item in coverage),
        "objectives": coverage,
    }


@router.post("/curricula/versions/{version_id}/mapping-suggestions")
async def suggest_objective_mappings(
    version_id: str,
    body: MappingSuggestionRequest,
    request: Request,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("curriculum:read"))],
) -> dict[str, Any]:
    """Return transparent deterministic drafts; never approve mappings automatically."""

    service = service_for(session, principal, request)
    service.get("curriculum_version", version_id)
    objectives = list(
        session.scalars(
            service._query("learning_objective").where(Resource.parent_id == version_id)  # noqa: SLF001
        )
    )
    suggestions: list[dict[str, Any]] = []
    for source_id in body.source_resource_ids:
        source = session.scalar(
            service._query().where(Resource.resource_id == source_id)  # noqa: SLF001
        )
        if source is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "mapping_source_not_found",
                    "message": f"Mapping source was not found: {source_id}",
                },
            )
        source_tokens = _tokens(f"{source.title} {source.search_text}")
        ranked = []
        for objective in objectives:
            objective_tokens = _tokens(
                f"{objective.title} {objective.payload.get('stable_code', '')} "
                f"{objective.payload.get('description', '')}"
            )
            overlap = source_tokens & objective_tokens
            union = source_tokens | objective_tokens
            score = len(overlap) / len(union) if union else 0.0
            if overlap:
                ranked.append((score, sorted(overlap), objective))
        for score, overlap, objective in sorted(
            ranked, key=lambda value: (-value[0], value[2].resource_id)
        )[: body.limit_per_source]:
            suggestions.append(
                {
                    "source_resource_id": source.resource_id,
                    "objective_id": objective.resource_id,
                    "curriculum_version_id": version_id,
                    "mapping_status": "draft",
                    "method": "deterministic_token_overlap",
                    "confidence": round(score, 4),
                    "rationale": f"Shared terms: {', '.join(overlap[:12])}",
                    "human_approval_required": True,
                }
            )
    return {"items": suggestions, "external_ai_used": False}
