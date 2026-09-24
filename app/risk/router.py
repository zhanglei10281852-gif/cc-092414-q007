from __future__ import annotations

from fastapi import APIRouter

from app.risk.schemas import (
    ComplaintCreate,
    RectificationCreate,
    RectificationResolve,
    RuleVersionPublish,
    ScoreReview,
    SupplierCreate,
    SupplierDisable,
    SupplierMerge,
)
from app.risk.service import RiskService

router = APIRouter(prefix="/api/risk", tags=["供应商风险画像"])


def service() -> RiskService:
    return RiskService()


@router.post("/suppliers", status_code=201)
def create_supplier(payload: SupplierCreate):
    return service().register_supplier(payload.name, payload.code, create_only=True)


@router.get("/suppliers")
def list_suppliers(include_inactive: bool = True):
    return service().list_suppliers(include_inactive=include_inactive)


@router.get("/suppliers/{supplier_id}")
def get_supplier(supplier_id: int):
    return service().get_supplier(supplier_id)


@router.post("/suppliers/{supplier_id}/merge")
def merge_suppliers(supplier_id: int, payload: SupplierMerge):
    return service().merge_suppliers(supplier_id, payload.target_supplier_id, payload.reason, actor="regulator")


@router.post("/suppliers/{supplier_id}/disable")
def disable_supplier(supplier_id: int, payload: SupplierDisable):
    return service().disable_supplier(supplier_id, payload.reason, actor="regulator")


@router.post("/suppliers/{supplier_id}/enable")
def enable_supplier(supplier_id: int):
    return service().enable_supplier(supplier_id, actor="regulator")


@router.get("/suppliers/{supplier_id}/events")
def list_events(supplier_id: int, include_void: bool = True):
    return service().list_events(supplier_id, include_void=include_void)


@router.post("/suppliers/{supplier_id}/complaints", status_code=201)
def record_complaint(supplier_id: int, payload: ComplaintCreate):
    return service().record_complaint(
        supplier_id,
        payload.model_dump(),
        occurred_at=payload.occurred_at,
    )


@router.get("/rectifications")
def list_rectifications(supplier_id: int | None = None, status: str | None = None):
    return service().list_rectifications(supplier_id=supplier_id, status=status)


@router.post("/suppliers/{supplier_id}/rectifications", status_code=201)
def open_rectification(supplier_id: int, payload: RectificationCreate):
    return service().open_rectification(
        supplier_id, payload.title, payload.due_at, lot_id=payload.lot_id, operator="regulator"
    )


@router.post("/rectifications/{rectification_id}/resolve")
def resolve_rectification(rectification_id: int, payload: RectificationResolve):
    return service().resolve_rectification(rectification_id, payload.resolution, actor="regulator")


@router.post("/suppliers/{supplier_id}/scores", status_code=201)
def recompute_score(supplier_id: int, force: bool = True):
    return service().score_supplier(supplier_id, trigger_source="manual", force=force)


@router.get("/suppliers/{supplier_id}/scores")
def list_versions(supplier_id: int):
    return service().list_versions(supplier_id)


@router.get("/suppliers/{supplier_id}/scores/{version}")
def get_version(supplier_id: int, version: int):
    return service().get_version(supplier_id, version)


@router.get("/suppliers/{supplier_id}/profile")
def current_profile(supplier_id: int):
    return service().current_profile(supplier_id)


@router.get("/reviews/pending")
def pending_reviews():
    return service().pending_reviews()


@router.post("/scores/{score_id}/review")
def review_score(score_id: int, payload: ScoreReview):
    return service().review_score(
        score_id,
        decision=payload.decision,
        reason=payload.reason,
        reviewer="regulator",
        override_level=payload.override_level,
        override_sampling_ratio=payload.override_sampling_ratio,
    )


@router.get("/rules")
def list_rules():
    return service().list_rule_versions()


@router.post("/rules", status_code=201)
def publish_rule(payload: RuleVersionPublish):
    return service().publish_rule_version(payload.config, payload.note, actor="regulator")
