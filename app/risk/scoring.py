"""供应商风险评分的纯函数引擎。

评分完全由 (输入事件集合, 规则配置, 评估时点) 决定，不包含任何隐藏状态，
因此相同输入必然得到相同分数——这是"一个触发只产生一个确定评分版本"的基础。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.core.clock import from_storage

RULE_VERSION = "v1"

EVENT_TYPES = ("exceedance", "temperature_anomaly", "rectification_overdue", "complaint")

EVENT_TYPE_LABELS = {
    "exceedance": "检测超标",
    "temperature_anomaly": "温控异常",
    "rectification_overdue": "整改逾期",
    "complaint": "投诉",
}

LEVELS = ("low", "medium", "high", "critical")

DEFAULT_RULE_CONFIG: dict[str, Any] = {
    # 事件影响按半衰期衰减：每经过 half_life_days 天，贡献减半。
    "half_life_days": 180.0,
    # 每类因子的权重：贡献 = 事件严重度 × 衰减系数 × 权重。
    "weights": {
        "exceedance": 12.0,
        "temperature_anomaly": 6.0,
        "rectification_overdue": 8.0,
        "complaint": 4.0,
    },
    # 评分到等级的阈值（下限含）。
    "thresholds": {"medium": 20.0, "high": 45.0, "critical": 70.0},
    # 每个等级对应的后续批次抽检比例。
    "sampling_by_level": {"low": 0.05, "medium": 0.15, "high": 0.35, "critical": 1.0},
    # 尚无已发布画像的供应商使用的默认抽检比例。
    "default_sampling_ratio": 0.05,
}


def decay_factor(age_days: float, half_life_days: float) -> float:
    """指数时间衰减：age_days 天前的事件贡献为 0.5 ** (age / half_life)。"""
    return 0.5 ** (max(0.0, age_days) / half_life_days)


def level_for(score: float, thresholds: dict[str, float]) -> str:
    if score >= thresholds["critical"]:
        return "critical"
    if score >= thresholds["high"]:
        return "high"
    if score >= thresholds["medium"]:
        return "medium"
    return "low"


def compute_profile(events: list[dict[str, Any]], config: dict[str, Any], as_of: datetime) -> dict[str, Any]:
    """对事件集合评分，返回分数、等级、抽检比例与逐因子解释。

    events 中每条需包含: id, event_type, severity, occurred_at(ISO 字符串),
    supplier_id, supplier_name, source_type, source_id, detail(dict)。
    """
    half_life = float(config["half_life_days"])
    weights = config["weights"]
    thresholds = config["thresholds"]
    sampling = config["sampling_by_level"]

    factors: list[dict[str, Any]] = []
    contributions: dict[str, float] = {}
    total = 0.0
    for event_type in EVENT_TYPES:
        weight = float(weights[event_type])
        bucket = sorted((e for e in events if e["event_type"] == event_type), key=lambda e: e["id"])
        raw_total = 0.0
        decayed_total = 0.0
        items: list[dict[str, Any]] = []
        for event in bucket:
            occurred = from_storage(event["occurred_at"])
            assert occurred is not None
            age_days = (as_of - occurred).total_seconds() / 86400.0
            decay = decay_factor(age_days, half_life)
            weighted = event["severity"] * decay * weight
            raw_total += event["severity"]
            decayed_total += event["severity"] * decay
            items.append(
                {
                    "event_id": event["id"],
                    "occurred_at": event["occurred_at"],
                    "age_days": round(max(0.0, age_days), 2),
                    "decay_factor": round(decay, 6),
                    "severity": round(event["severity"], 4),
                    "weighted_contribution": round(weighted, 4),
                    "supplier_id": event["supplier_id"],
                    "supplier_name": event["supplier_name"],
                    "from_merged_supplier": bool(event.get("from_merged_supplier")),
                    "source": f"{event['source_type']}:{event['source_id']}",
                    "detail": event.get("detail") or {},
                }
            )
        contribution = decayed_total * weight
        contributions[event_type] = contribution
        total += contribution
        factors.append(
            {
                "type": event_type,
                "label": EVENT_TYPE_LABELS[event_type],
                "weight": weight,
                "event_count": len(bucket),
                "raw_total": round(raw_total, 4),
                "decayed_total": round(decayed_total, 4),
                "contribution": round(contribution, 4),
                "events": items,
            }
        )

    score = round(min(100.0, total), 4)
    level = level_for(score, thresholds)
    # 反事实解释：移除该因子后分数/等级/抽检比例如何变化，说明该因子对抽检建议的影响。
    for factor in factors:
        without_total = total - contributions[factor["type"]]
        without_score = round(min(100.0, without_total), 4)
        without_level = level_for(without_score, thresholds)
        factor["share"] = round(contributions[factor["type"]] / total, 4) if total > 0 else 0.0
        factor["without_factor"] = {
            "score": without_score,
            "level": without_level,
            "sampling_ratio": sampling[without_level],
        }

    return {
        "score": score,
        "level": level,
        "sampling_ratio": sampling[level],
        "thresholds": dict(thresholds),
        "half_life_days": half_life,
        "factors": factors,
    }
