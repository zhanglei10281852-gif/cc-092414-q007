"""供应商风险评分规则引擎。

规则以带版本号的配置对象表达，评分过程是纯函数：相同的输入事件、规则版本与
评估时点必然得到相同的分数与因子明细，便于复核、回放与审计。
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime
from typing import Any

from app.core.clock import from_storage

RULE_VERSION_1 = "1.0.0"

# 因子展示顺序固定，保证解释输出稳定
FACTOR_ORDER = ("test_fail", "temperature_anomaly", "rectification_overdue", "complaint")
FACTOR_LABELS = {
    "test_fail": "历史超标",
    "temperature_anomaly": "温控异常",
    "rectification_overdue": "整改逾期",
    "complaint": "投诉情况",
}
RISK_LEVELS = ("low", "medium", "high", "critical")

DEFAULT_CONFIG: dict[str, Any] = {
    "version": RULE_VERSION_1,
    "decay": {
        # 指数时间衰减：weight * 0.5 ** (事件年龄天数 / half_life_days)
        "half_life_days": 180.0,
        "window_days": 720,
    },
    # 检测超标后自动开立的整改单期限（天）
    "rectification_window_days": 7,
    "score_cap": 100,
    "factors": {
        "test_fail": {
            "label": "历史超标",
            "base_weight": 24,
            "severity_bands": [
                {"max": 2.0, "severity": "minor", "label": "轻微超标", "multiplier": 1.0},
                {"max": 5.0, "severity": "major", "label": "明显超标", "multiplier": 1.6},
                {"max": None, "severity": "severe", "label": "严重超标", "multiplier": 2.5},
            ],
        },
        "temperature_anomaly": {
            "label": "温控异常",
            "base_weight": 10,
            "severity_bands": [
                {"max": 3.0, "severity": "minor", "label": "轻微偏差", "multiplier": 1.0},
                {"max": 8.0, "severity": "major", "label": "明显偏差", "multiplier": 1.6},
                {"max": None, "severity": "severe", "label": "严重偏差", "multiplier": 2.2},
            ],
        },
        "rectification_overdue": {
            "label": "整改逾期",
            "base_weight": 18,
            "severity_bands": [
                {"max": 7.0, "severity": "minor", "label": "逾期一周内", "multiplier": 1.0},
                {"max": 30.0, "severity": "major", "label": "逾期一月内", "multiplier": 1.5},
                {"max": None, "severity": "severe", "label": "长期逾期", "multiplier": 2.2},
            ],
        },
        "complaint": {
            "label": "投诉情况",
            "base_weight": 8,
            "severity_multiplier": {"low": 1.0, "medium": 1.8, "high": 3.0},
            "severity_labels": {"low": "一般投诉", "medium": "较重投诉", "high": "严重投诉"},
        },
    },
    "thresholds": [
        {"min": 0, "level": "low", "label": "低风险", "sampling_ratio": 0.05},
        {"min": 25, "level": "medium", "label": "中风险", "sampling_ratio": 0.15},
        {"min": 50, "level": "high", "label": "高风险", "sampling_ratio": 0.35},
        {"min": 75, "level": "critical", "label": "极高风险", "sampling_ratio": 0.6},
    ],
}


def default_config() -> dict[str, Any]:
    return copy.deepcopy(DEFAULT_CONFIG)


def validate_config(config: dict[str, Any]) -> None:
    """发布前校验规则配置，不合法直接抛 ValueError。"""
    if not isinstance(config, dict):
        raise ValueError("规则配置必须是对象")
    decay = config.get("decay", {})
    if float(decay.get("half_life_days", 0)) <= 0:
        raise ValueError("half_life_days 必须为正数")
    if int(decay.get("window_days", 0)) <= 0:
        raise ValueError("window_days 必须为正整数")
    if float(config.get("score_cap", 0)) <= 0:
        raise ValueError("score_cap 必须为正数")
    factors = config.get("factors", {})
    for key in FACTOR_ORDER:
        factor = factors.get(key)
        if not isinstance(factor, dict) or float(factor.get("base_weight", 0)) < 0:
            raise ValueError(f"因子 {key} 缺少 base_weight")
        if key != "complaint":
            bands = factor.get("severity_bands")
            if not bands or not isinstance(bands, list):
                raise ValueError(f"因子 {key} 缺少 severity_bands")
            last_max = -1.0
            for band in bands:
                if band.get("max") is not None:
                    if float(band["max"]) <= last_max:
                        raise ValueError(f"因子 {key} 分段上界必须递增")
                    last_max = float(band["max"])
                if float(band.get("multiplier", 0)) <= 0:
                    raise ValueError(f"因子 {key} 系数必须为正")
        else:
            multipliers = factor.get("severity_multiplier", {})
            for level in ("low", "medium", "high"):
                if float(multipliers.get(level, 0)) <= 0:
                    raise ValueError("投诉因子缺少严重度系数")
    thresholds = config.get("thresholds", [])
    levels = [item.get("level") for item in thresholds]
    if levels != list(RISK_LEVELS):
        raise ValueError("thresholds 必须依次包含 low/medium/high/critical")
    last_min = -1.0
    last_ratio = -1.0
    for item in thresholds:
        if float(item["min"]) <= last_min:
            raise ValueError("风险等级阈值必须递增")
        last_min = float(item["min"])
        ratio = float(item["sampling_ratio"])
        if not 0 < ratio <= 1 or ratio <= last_ratio:
            raise ValueError("抽检比例必须随风险等级严格递增且在 (0,1] 内")
        last_ratio = ratio


def level_for(score: float, config: dict[str, Any]) -> dict[str, Any]:
    chosen = config["thresholds"][0]
    for item in config["thresholds"]:
        if score >= float(item["min"]):
            chosen = item
    return chosen


def _band_for(factor_cfg: dict[str, Any], magnitude: float) -> dict[str, Any]:
    bands = factor_cfg["severity_bands"]
    for band in bands:
        if band["max"] is None or magnitude <= float(band["max"]):
            return band
    return bands[-1]


def _severity_info(factor_key: str, factor_cfg: dict[str, Any], event: dict[str, Any], as_of: datetime) -> dict[str, Any]:
    if factor_key == "complaint":
        severity = event.get("severity") or "low"
        return {
            "severity": severity,
            "label": factor_cfg["severity_labels"][severity],
            "multiplier": float(factor_cfg["severity_multiplier"][severity]),
            "magnitude": None,
        }
    magnitude = event.get("magnitude")
    if factor_key == "rectification_overdue":
        due_at = from_storage(event.get("payload", {}).get("due_at"))
        magnitude = max((as_of - due_at).total_seconds() / 86400.0, 0.0) if due_at else 0.0
    magnitude = float(magnitude or 0.0)
    band = _band_for(factor_cfg, magnitude)
    return {
        "severity": band["severity"],
        "label": band["label"],
        "multiplier": float(band["multiplier"]),
        "magnitude": round(magnitude, 2),
    }


def _describe(factor_key: str, event: dict[str, Any], info: dict[str, Any]) -> str:
    payload = event.get("payload", {})
    if factor_key == "test_pass":
        return f"{payload.get('analyte','检测项')} 检出 {payload.get('value_mg_kg')}，低于限值 {payload.get('limit_mg_kg')}，检测合格"
    if factor_key == "test_fail":
        return (
            f"{payload.get('analyte','检测项')} 检出 {payload.get('value_mg_kg')}，"
            f"限值 {payload.get('limit_mg_kg')}，超标 {info['magnitude']} 倍（{info['label']}）"
        )
    if factor_key == "temperature_anomaly":
        return (
            f"{payload.get('recorded_at','')} 温度 {payload.get('temperature_c')}℃，"
            f"偏离目标区间 {info['magnitude']}℃（{info['label']}）"
        )
    if factor_key == "rectification_overdue":
        return f"整改单 #{event.get('ref_id')} 逾期 {info['magnitude']} 天（{info['label']}）"
    return f"{info['label']}：{str(payload.get('content',''))[:60]}"


def evaluate(events: list[dict[str, Any]], config: dict[str, Any], as_of: datetime) -> dict[str, Any]:
    """纯函数评分。

    events 为供应商（含合并来源）时间窗口内的事件字典，occurred_at 为 UTC datetime。
    返回总分、等级、抽检比例以及逐因子、逐事件的解释明细。
    """
    half_life = float(config["decay"]["half_life_days"])
    cap = float(config["score_cap"])
    factor_results: list[dict[str, Any]] = []
    total = 0.0
    for factor_key in FACTOR_ORDER:
        factor_cfg = config["factors"][factor_key]
        items: list[dict[str, Any]] = []
        subtotal = 0.0
        for event in events:
            if event["event_type"] != factor_key:
                continue
            age_days = max((as_of - event["occurred_at"]).total_seconds() / 86400.0, 0.0)
            decay = 0.5 ** (age_days / half_life)
            info = _severity_info(factor_key, factor_cfg, event, as_of)
            contribution = float(factor_cfg["base_weight"]) * info["multiplier"] * decay
            subtotal += contribution
            items.append(
                {
                    "event_id": event["id"],
                    "supplier_id": event.get("supplier_id"),
                    "lot_id": event.get("lot_id"),
                    "occurred_at": event["occurred_at_iso"],
                    "age_days": round(age_days, 1),
                    "base_weight": float(factor_cfg["base_weight"]),
                    "severity": info["severity"],
                    "severity_label": info["label"],
                    "magnitude": info["magnitude"],
                    "multiplier": info["multiplier"],
                    "decay_factor": round(decay, 4),
                    "contribution": round(contribution, 2),
                    "description": _describe(factor_key, event, info),
                }
            )
        items.sort(key=lambda item: (item["occurred_at"], item["event_id"]))
        subtotal = round(subtotal, 2)
        total += subtotal
        if items:
            rationale = (
                f"{FACTOR_LABELS[factor_key]}：窗口内 {len(items)} 次记录，"
                f"按 {half_life:g} 天半衰期做时间衰减后合计贡献 {subtotal} 分"
            )
        else:
            rationale = f"{FACTOR_LABELS[factor_key]}：窗口内无记录，贡献 0 分"
        factor_results.append(
            {
                "factor": factor_key,
                "label": factor_cfg["label"],
                "event_count": len(items),
                "base_weight": float(factor_cfg["base_weight"]),
                "weighted_score": subtotal,
                "rationale": rationale,
                "events": items,
            }
        )
    score = round(min(total, cap), 2)
    level_info = level_for(score, config)
    pass_events = [
        {
            "event_id": event["id"],
            "occurred_at": event["occurred_at_iso"],
            "description": _describe("test_pass", event, {}),
        }
        for event in events
        if event["event_type"] == "test_pass"
    ]
    return {
        "rule_version": config["version"],
        "score": score,
        "level": level_info["level"],
        "level_label": level_info["label"],
        "sampling_ratio": float(level_info["sampling_ratio"]),
        "half_life_days": half_life,
        "factors": factor_results,
        "neutral_events": {"test_pass_count": len(pass_events), "test_pass": pass_events},
    }


def input_fingerprint(rule_version: str, supplier_ids: list[int], events: list[dict[str, Any]]) -> str:
    """对评分输入（规则版本 + 供应商集合 + 事件快照）生成确定性指纹。"""
    payload = {
        "rule_version": rule_version,
        "supplier_ids": sorted(set(supplier_ids)),
        "events": sorted(
            (
                event["id"],
                event["event_type"],
                event["occurred_at_iso"],
                event.get("severity") or "",
                None if event.get("magnitude") is None else round(float(event["magnitude"]), 4),
                hashlib.sha256(
                    json.dumps(event.get("payload", {}), sort_keys=True, ensure_ascii=False).encode()
                ).hexdigest(),
            )
            for event in events
        ),
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()
