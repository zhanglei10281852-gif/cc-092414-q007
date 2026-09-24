from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SupplierCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    code: str = Field(default="", max_length=64)


class SupplierMerge(BaseModel):
    target_supplier_id: int = Field(..., gt=0)
    reason: str = Field(..., min_length=1, max_length=300)


class SupplierDisable(BaseModel):
    reason: str = Field(..., min_length=1, max_length=300)


class ComplaintCreate(BaseModel):
    severity: str = Field(..., pattern="^(low|medium|high)$")
    content: str = Field(..., min_length=1, max_length=1000)
    channel: str = Field(default="现场", max_length=40)
    occurred_at: str | None = Field(default=None, min_length=10, max_length=40)


class RectificationCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    due_at: str = Field(..., min_length=10, max_length=40)
    lot_id: int | None = Field(default=None, gt=0)


class RectificationResolve(BaseModel):
    resolution: str = Field(..., min_length=1, max_length=500)


class RuleVersionPublish(BaseModel):
    config: dict[str, Any]
    note: str = Field(..., min_length=1, max_length=300)


class ScoreReview(BaseModel):
    decision: str = Field(..., pattern="^(publish|publish_override|reject)$")
    reason: str = Field(..., min_length=1, max_length=300)
    override_level: str | None = Field(default=None, pattern="^(low|medium|high|critical)$")
    override_sampling_ratio: float | None = Field(default=None, gt=0, le=1)
