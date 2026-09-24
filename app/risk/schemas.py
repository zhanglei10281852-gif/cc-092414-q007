from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from app.core.clock import from_storage, to_storage


def _normalize_dt(value: str) -> str:
    parsed = from_storage(value)
    if parsed is None:
        raise ValueError("时间不能为空")
    return to_storage(parsed)


class SupplierCreate(BaseModel):
    supplier_code: str = Field(..., min_length=3, max_length=64)
    name: str = Field(..., min_length=1, max_length=120)

    @field_validator("supplier_code")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        return value.strip().upper()


class SupplierMerge(BaseModel):
    target_supplier_id: int = Field(..., gt=0)
    reason: str = Field(..., min_length=1, max_length=300)
    operator: str = Field(..., min_length=1, max_length=80)


class SupplierSuspend(BaseModel):
    reason: str = Field(..., min_length=1, max_length=300)
    operator: str = Field(..., min_length=1, max_length=80)


class SupplierActivate(BaseModel):
    operator: str = Field(..., min_length=1, max_length=80)


class ComplaintCreate(BaseModel):
    complaint_no: str = Field(..., min_length=3, max_length=64)
    category: str = Field(..., min_length=1, max_length=40)
    channel: str = Field(default="hotline", min_length=1, max_length=40)
    severity: str = Field(default="general", pattern="^(general|serious)$")
    content: str = Field(..., min_length=1, max_length=1000)
    occurred_at: str = Field(..., min_length=10, max_length=40)
    reporter: str = Field(default="regulator", min_length=1, max_length=80)

    @field_validator("occurred_at")
    @classmethod
    def normalize_time(cls, value: str) -> str:
        return _normalize_dt(value)


class RectificationCreate(BaseModel):
    order_no: str = Field(..., min_length=3, max_length=64)
    lot_id: int | None = Field(default=None, gt=0)
    requirement: str = Field(..., min_length=1, max_length=500)
    deadline_at: str = Field(..., min_length=10, max_length=40)
    issued_by: str = Field(..., min_length=1, max_length=80)

    @field_validator("deadline_at")
    @classmethod
    def normalize_deadline(cls, value: str) -> str:
        return _normalize_dt(value)


class RectificationComplete(BaseModel):
    completed_at: str = Field(..., min_length=10, max_length=40)
    operator: str = Field(..., min_length=1, max_length=80)
    note: str = Field(default="", max_length=300)

    @field_validator("completed_at")
    @classmethod
    def normalize_completed(cls, value: str) -> str:
        return _normalize_dt(value)


class ReviewCreate(BaseModel):
    reviewer: str = Field(..., min_length=1, max_length=80)
    action: str = Field(..., pattern="^(confirm|adjust)$")
    adjusted_level: str | None = Field(default=None, pattern="^(low|medium|high|critical)$")
    comment: str = Field(..., min_length=1, max_length=500)


class PublishCreate(BaseModel):
    operator: str = Field(..., min_length=1, max_length=80)
    note: str = Field(default="", max_length=300)


class RecalculateCreate(BaseModel):
    as_of: str | None = Field(default=None, min_length=10, max_length=40)
    operator: str = Field(default="system", min_length=1, max_length=80)

    @field_validator("as_of")
    @classmethod
    def normalize_as_of(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _normalize_dt(value)
