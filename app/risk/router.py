from __future__ import annotations

import sqlite3

from fastapi import APIRouter, HTTPException, Query

from app.risk.schemas import (
    ComplaintCreate,
    PublishCreate,
    RecalculateCreate,
    RectificationComplete,
    RectificationCreate,
    ReviewCreate,
    SupplierActivate,
    SupplierCreate,
    SupplierMerge,
    SupplierSuspend,
)
from app.risk.service import RiskService

router = APIRouter(prefix="/api/risk", tags=["供应商风险画像"])


def service() -> RiskService:
    return RiskService()


def _handle(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail="资源不存在")
    if isinstance(exc, ValueError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, sqlite3.IntegrityError):
        return HTTPException(status_code=409, detail="唯一性冲突：编码或单号已存在")
    return HTTPException(status_code=500, detail="内部错误")


@router.post("/suppliers", status_code=201)
def create_supplier(payload: SupplierCreate):
    try:
        return service().register_supplier(payload.model_dump(), actor="regulator")
    except Exception as exc:
        raise _handle(exc) from exc


@router.get("/suppliers")
def list_suppliers(status: str | None = Query(None, pattern="^(active|suspended|merged)$")):
    return {"items": service().list_suppliers(status)}


@router.get("/suppliers/{supplier_id}")
def get_supplier(supplier_id: int):
    try:
        return service().get_supplier(supplier_id)
    except Exception as exc:
        raise _handle(exc) from exc


@router.post("/suppliers/{supplier_id}/merge")
def merge_supplier(supplier_id: int, payload: SupplierMerge):
    try:
        return service().merge_supplier(supplier_id, payload.model_dump(), actor=payload.operator)
    except Exception as exc:
        raise _handle(exc) from exc


@router.post("/suppliers/{supplier_id}/suspend")
def suspend_supplier(supplier_id: int, payload: SupplierSuspend):
    try:
        return service().suspend_supplier(supplier_id, payload.model_dump(), actor=payload.operator)
    except Exception as exc:
        raise _handle(exc) from exc


@router.post("/suppliers/{supplier_id}/activate")
def activate_supplier(supplier_id: int, payload: SupplierActivate):
    try:
        return service().activate_supplier(supplier_id, actor=payload.operator)
    except Exception as exc:
        raise _handle(exc) from exc


@router.get("/suppliers/{supplier_id}/events")
def list_events(supplier_id: int):
    try:
        return {"items": service().list_events(supplier_id)}
    except Exception as exc:
        raise _handle(exc) from exc


@router.get("/suppliers/{supplier_id}/scores")
def list_scores(supplier_id: int):
    try:
        return {"items": service().list_scores(supplier_id)}
    except Exception as exc:
        raise _handle(exc) from exc


@router.get("/suppliers/{supplier_id}/profile")
def profile(supplier_id: int):
    try:
        return service().profile(supplier_id)
    except Exception as exc:
        raise _handle(exc) from exc


@router.post("/suppliers/{supplier_id}/recalculate")
def recalculate(supplier_id: int, payload: RecalculateCreate):
    try:
        return service().recalculate(supplier_id, payload.as_of, actor=payload.operator)
    except Exception as exc:
        raise _handle(exc) from exc


@router.post("/suppliers/{supplier_id}/complaints", status_code=201)
def file_complaint(supplier_id: int, payload: ComplaintCreate):
    try:
        return service().file_complaint(supplier_id, payload.model_dump(), actor=payload.reporter)
    except Exception as exc:
        raise _handle(exc) from exc


@router.get("/suppliers/{supplier_id}/complaints")
def list_complaints(supplier_id: int):
    try:
        return {"items": service().list_complaints(supplier_id)}
    except Exception as exc:
        raise _handle(exc) from exc


@router.post("/suppliers/{supplier_id}/rectifications", status_code=201)
def create_rectification(supplier_id: int, payload: RectificationCreate):
    try:
        return service().create_rectification(supplier_id, payload.model_dump(), actor=payload.issued_by)
    except Exception as exc:
        raise _handle(exc) from exc


@router.get("/suppliers/{supplier_id}/rectifications")
def list_rectifications(supplier_id: int):
    try:
        return {"items": service().list_rectifications(supplier_id)}
    except Exception as exc:
        raise _handle(exc) from exc


@router.post("/rectifications/sweep")
def sweep_rectifications(as_of: str | None = Query(None), operator: str = Query("system")):
    return service().sweep_overdue(as_of=as_of, actor=operator)


@router.post("/rectifications/{rectification_id}/complete")
def complete_rectification(rectification_id: int, payload: RectificationComplete):
    try:
        return service().complete_rectification(rectification_id, payload.model_dump(), actor=payload.operator)
    except Exception as exc:
        raise _handle(exc) from exc


@router.get("/scores/{score_id}")
def score_detail(score_id: int):
    try:
        return service().score_detail(score_id)
    except Exception as exc:
        raise _handle(exc) from exc


@router.post("/scores/{score_id}/review")
def review_score(score_id: int, payload: ReviewCreate):
    try:
        return service().review_score(score_id, payload.model_dump())
    except Exception as exc:
        raise _handle(exc) from exc


@router.post("/scores/{score_id}/publish")
def publish_score(score_id: int, payload: PublishCreate):
    try:
        return service().publish_score(score_id, payload.model_dump())
    except Exception as exc:
        raise _handle(exc) from exc


@router.get("/lots/{lot_id}/sampling")
def lot_sampling(lot_id: int):
    try:
        return service().lot_sampling(lot_id)
    except Exception as exc:
        raise _handle(exc) from exc


@router.get("/rules")
def list_rules():
    return {"items": service().list_rules()}
