"""Automatic draft figure descriptions with mandatory human verification."""

from __future__ import annotations

from typing import Annotated, Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import Field
from sqlalchemy.orm import Session

from .api import db_session, service_for
from .config import Settings, get_settings
from .schemas import AccessibilityDescriptionCreate, StrictModel
from .security import Principal, require
from .service import serialize_resource

router = APIRouter(prefix="/api/v2", tags=["accessibility"])


class GenerateDescriptionRequest(StrictModel):
    generation_mode: Literal["local_model", "institution_ai"] = "local_model"
    figure_kind: Literal["figure", "diagram", "chart", "table", "equation", "decorative"]
    extracted_text: str = Field(default="", max_length=20_000)
    surrounding_context: str = Field(default="", max_length=20_000)
    structured_data: dict[str, Any] = Field(default_factory=dict)


def _bounded_words(text: str, limit: int = 50) -> str:
    words = " ".join(text.split()).split(" ")
    return " ".join(words[:limit]).strip()


def local_description(body: GenerateDescriptionRequest) -> tuple[str, str, float]:
    if body.figure_kind == "decorative":
        return "", "decorative", 0.95
    visible = _bounded_words(body.extracted_text)
    context = _bounded_words(body.surrounding_context, 25)
    structured = body.structured_data
    if body.figure_kind == "chart":
        chart_type = str(structured.get("chart_type") or "chart")
        axes = ", ".join(str(value) for value in structured.get("axes", [])[:4])
        trend = str(structured.get("trend") or "").strip()
        parts = [f"A {chart_type}."]
        if axes:
            parts.append(f"Axes or measures: {axes}.")
        if trend:
            parts.append(f"Reported pattern: {trend}.")
        if visible:
            parts.append(f"Visible text includes: {visible}.")
        return " ".join(parts), "short", 0.62
    if body.figure_kind == "table":
        rows = structured.get("rows")
        columns = structured.get("columns")
        size = f" with {rows} rows and {columns} columns" if isinstance(rows, int) and isinstance(columns, int) else ""
        return f"A table{size}. Visible text includes: {visible or 'no reliable OCR text'}.", "short", 0.68
    if body.figure_kind == "equation":
        return f"An equation: {visible or 'the mathematical expression needs specialist review'}.", "short", 0.55
    prefix = "A diagram" if body.figure_kind == "diagram" else "A figure"
    description = f"{prefix}."
    if visible:
        description += f" Visible text includes: {visible}."
    if context:
        description += f" It appears near content about: {context}."
    return description, "short", 0.5


async def institution_ai_description(
    body: GenerateDescriptionRequest, settings: Settings
) -> tuple[str, str, float]:
    if not settings.external_ai_enabled:
        raise HTTPException(status_code=409, detail={"code": "external_ai_disabled", "message": "Institution AI is disabled by policy"})
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                str(settings.accessibility_ai_endpoint),
                headers={"Authorization": f"Bearer {settings.accessibility_ai_token}"},
                json={
                    "task": "draft_accessibility_description",
                    "figure_kind": body.figure_kind,
                    "extracted_text": body.extracted_text,
                    "surrounding_context": body.surrounding_context,
                    "structured_data": body.structured_data,
                    "rules": {
                        "must_not_invent": True,
                        "human_verification_required": True,
                    },
                },
            )
            response.raise_for_status()
            result = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=503, detail={"code": "accessibility_ai_unavailable", "message": "Institution accessibility AI is temporarily unavailable"}) from exc
    description = " ".join(str(result.get("description", "")).split())
    confidence = result.get("confidence")
    if not description or len(description) > 10_000 or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise HTTPException(status_code=422, detail={"code": "invalid_ai_description", "message": "Institution AI returned an invalid accessibility draft"})
    description_kind = str(result.get("description_kind") or "short")
    if description_kind not in {"short", "long", "decorative", "unreadable"}:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_ai_description",
                "message": "Institution AI returned an unsupported description kind",
            },
        )
    return description, description_kind, float(confidence)


@router.post("/figures/{figure_id}/accessibility-description", status_code=201)
async def generate_figure_description(
    figure_id: str,
    body: GenerateDescriptionRequest,
    request: Request,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("accessibility:read"))],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    service = service_for(session, principal, request)
    figure = service.get("figure", figure_id)
    if body.generation_mode == "institution_ai":
        description, description_kind, confidence = await institution_ai_description(body, settings)
        model_name = "institution-approved-accessibility-service"
    else:
        description, description_kind, confidence = local_description(body)
        model_name = "predixalearn-local-structured-description"
    create = AccessibilityDescriptionCreate(
        title=f"Accessibility description for {figure.title}",
        parent_id=figure_id,
        academic_unit_id=figure.academic_unit_id,
        course_id=figure.course_id,
        access_classification=figure.access_classification,
        figure_id=figure_id,
        description=description or "Decorative figure",
        description_kind=description_kind,
        generation_mode=body.generation_mode,
        model_name=model_name,
        model_version="1",
        payload={
            "draft_confidence": confidence,
            "evidence": {
                "extracted_text": body.extracted_text,
                "structured_data": body.structured_data,
                "figure_sha256": figure.content_sha256,
            },
            "human_verification_required": True,
        },
    )
    resource = service.create("accessibility_description", create)
    return serialize_resource(resource)


@router.get("/accessibility/descriptions/{description_id}/export")
async def export_approved_description(
    description_id: str,
    request: Request,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("accessibility:read"))],
) -> PlainTextResponse:
    description = service_for(session, principal, request).get(
        "accessibility_description", description_id
    )
    if description.status != "approved":
        raise HTTPException(status_code=409, detail={"code": "human_verification_required", "message": "Only human-approved accessibility descriptions can be exported"})
    text = str(description.payload.get("description", ""))
    if description.payload.get("description_kind") == "decorative":
        text = ""
    return PlainTextResponse(text, headers={"Content-Disposition": f'attachment; filename="alt-{description_id}.txt"'})
