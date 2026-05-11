"""Admin endpoints for experiments + metrics. Gated by ADMIN_API_KEY env
var (passed via X-Admin-Key header) so we don't expose them publicly.

Routes:
  GET    /admin/experiments               — list all
  POST   /admin/experiments               — create / update
  GET    /admin/experiments/{id}          — fetch one
  GET    /admin/experiments/{id}/metrics  — funnel by variant
"""
from __future__ import annotations

import os
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from db import (
    compute_experiment_metrics,
    get_experiment,
    list_active_experiments,
    upsert_experiment,
)

router = APIRouter(prefix="/admin", tags=["admin"])


def _require_admin(x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")):
    """Simple shared-secret gate. Set ADMIN_API_KEY in Render env vars."""
    expected = os.environ.get("ADMIN_API_KEY")
    if not expected:
        raise HTTPException(status_code=503, detail="Admin API disabled (set ADMIN_API_KEY)")
    if not x_admin_key or x_admin_key != expected:
        raise HTTPException(status_code=401, detail="Invalid admin key")


class ExperimentUpsert(BaseModel):
    id: str | None = None
    name: str
    description: str = ""
    variants: dict
    traffic_split: float = 0.5
    enabled: bool = True


@router.get("/experiments", dependencies=[Depends(_require_admin)])
def list_experiments():
    """All enabled experiments (disabled ones are visible via GET /admin/experiments/{id})."""
    return {"experiments": list_active_experiments()}


@router.get("/experiments/{experiment_id}", dependencies=[Depends(_require_admin)])
def get_one(experiment_id: str):
    exp = get_experiment(experiment_id)
    if not exp:
        raise HTTPException(status_code=404, detail="Experiment not found")
    return exp


@router.post("/experiments", dependencies=[Depends(_require_admin)])
def create_or_update(req: ExperimentUpsert):
    """Create a new experiment if id is missing, else update by id.
    variants is a dict like {"control": {...config...}, "treatment": {...config...}}.
    Currently the framework reads ranker_weights from variant config.
    """
    if not req.name.strip():
        raise HTTPException(status_code=400, detail="name required")
    if not req.variants or not isinstance(req.variants, dict):
        raise HTTPException(status_code=400, detail="variants must be a non-empty dict")
    if req.traffic_split < 0 or req.traffic_split > 1:
        raise HTTPException(status_code=400, detail="traffic_split must be 0..1")
    eid = req.id or str(uuid.uuid4())[:8]
    upsert_experiment(
        eid, req.name.strip(), req.description.strip(),
        req.variants, req.traffic_split, req.enabled,
    )
    return get_experiment(eid)


@router.get("/experiments/{experiment_id}/metrics", dependencies=[Depends(_require_admin)])
def metrics(experiment_id: str):
    """Per-variant funnel: turns, click_rate, save_rate, avg_first_click_rank."""
    exp = get_experiment(experiment_id)
    if not exp:
        raise HTTPException(status_code=404, detail="Experiment not found")
    return {
        "experiment": exp,
        "metrics_by_variant": compute_experiment_metrics(experiment_id),
    }
