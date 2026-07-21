"""Loopback-only API routes for signed runtime maintenance."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, status
from pydantic import BaseModel, Field

from app.maintenance.service import MaintenanceError, get_maintenance_service

router = APIRouter(prefix="/api/v1/maintenance", tags=["maintenance"])


class ApplyMaintenanceRequest(BaseModel):
    manifest_version: Annotated[str, Field(min_length=1, max_length=128)]
    review_token: Annotated[str, Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]+$")]


@router.post("/check")
async def check_maintenance() -> dict[str, Any]:
    """Verify the remote signature before returning any update candidates."""
    return get_maintenance_service().check()


@router.post("/apply", status_code=status.HTTP_202_ACCEPTED)
async def apply_maintenance(request: ApplyMaintenanceRequest) -> dict[str, Any]:
    try:
        return get_maintenance_service().apply(request.manifest_version, request.review_token)
    except MaintenanceError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("/jobs/{job_id}")
async def get_maintenance_job(job_id: Annotated[str, Path(pattern=r"^[a-f0-9]{32}$")]) -> dict[str, Any]:
    job = get_maintenance_service().get_job(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Maintenance job not found")
    return job


@router.delete("/jobs/{job_id}")
async def cancel_maintenance_job(
    job_id: Annotated[str, Path(pattern=r"^[a-f0-9]{32}$")],
) -> dict[str, Any]:
    job = get_maintenance_service().cancel(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Maintenance job not found")
    return job
