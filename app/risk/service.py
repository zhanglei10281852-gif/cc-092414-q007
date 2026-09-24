"""供应商风险画像：事件归集、确定性评分、人工复核与等级发布。

设计要点：
- 所有风险事实（检测超标、温控异常、整改逾期、投诉）统一落入 risk_events，
  event_key 唯一约束保证同一来源记录只归集一次。
- 评分是纯函数（见 app.risk.scoring）：相同事件集合 + 规则版本 + 评估时点
  必然得到相同分数。risk_scores 以 (supplier_id, input_digest) 唯一约束去重，
  同一触发并发重放也只会产生一个确定的评分版本。
- 评分版本状态机：draft → reviewed → published（发布后旧版本转为 superseded），
  只有已发布版本决定后续批次的抽检比例。
- 供应商合并/停用只改注册表状态，事件与评分历史保留原始 supplier_id，
  合并后由存续供应商继承全部历史事件参与评分。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.core.clock import from_storage, to_storage, utc_now
from app.database import get_connection, transaction
from app.risk.scoring import DEFAULT_RULE_CONFIG, RULE_VERSION, compute_profile

SCHEMA = """
CREATE TABLE IF NOT EXISTS risk_suppliers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','suspended','merged')),
    merged_into_id INTEGER REFERENCES risk_suppliers(id),
    merged_at TEXT,
    merge_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS risk_rule_sets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version TEXT NOT NULL UNIQUE,
    config_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','retired')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS risk_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT NOT NULL UNIQUE,
    supplier_id INTEGER NOT NULL REFERENCES risk_suppliers(id),
    event_type TEXT NOT NULL CHECK(event_type IN ('exceedance','temperature_anomaly','rectification_overdue','complaint')),
    severity REAL NOT NULL,
    occurred_at TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_id INTEGER,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS risk_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id INTEGER NOT NULL REFERENCES risk_suppliers(id),
    version_no INTEGER NOT NULL,
    rule_version TEXT NOT NULL,
    trigger_event_id INTEGER REFERENCES risk_events(id),
    as_of TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    input_event_ids_json TEXT NOT NULL,
    score REAL NOT NULL,
    level TEXT NOT NULL CHECK(level IN ('low','medium','high','critical')),
    adjusted_level TEXT CHECK(adjusted_level IN ('low','medium','high','critical')),
    factors_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','reviewed','published','superseded')),
    published_ratio REAL,
    published_at TEXT,
    published_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(supplier_id, version_no),
    UNIQUE(supplier_id, input_digest)
);
CREATE TABLE IF NOT EXISTS risk_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    score_id INTEGER NOT NULL REFERENCES risk_scores(id) ON DELETE RESTRICT,
    reviewer TEXT NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('confirm','adjust')),
    adjusted_level TEXT CHECK(adjusted_level IN ('low','medium','high','critical')),
    comment TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS risk_complaints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id INTEGER NOT NULL REFERENCES risk_suppliers(id),
    complaint_no TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT 'hotline',
    severity TEXT NOT NULL DEFAULT 'general' CHECK(severity IN ('general','serious')),
    content TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'filed' CHECK(status IN ('filed','verified','dismissed')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS risk_rectifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id INTEGER NOT NULL REFERENCES risk_suppliers(id),
    lot_id INTEGER,
    order_no TEXT NOT NULL UNIQUE,
    requirement TEXT NOT NULL,
    deadline_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','completed','overdue')),
    completed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS risk_lot_sampling (
    lot_id INTEGER PRIMARY KEY,
    supplier_id INTEGER NOT NULL REFERENCES risk_suppliers(id),
    sampling_ratio REAL NOT NULL,
    score_id INTEGER REFERENCES risk_scores(id),
    rule_version TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS risk_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id INTEGER,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_risk_events_supplier ON risk_events(supplier_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_risk_scores_supplier ON risk_scores(supplier_id, status, version_no);
CREATE INDEX IF NOT EXISTS idx_risk_rectifications_due ON risk_rectifications(status, deadline_at);
"""


def _now() -> str:
    return to_storage(utc_now())


def ensure_schema(connection: sqlite3.Connection | None = None) -> None:
    """创建风险画像表结构并播种默认规则版本。

    逐条执行 DDL（而非 executescript），因此在已开启的事务内调用也是安全的；
    已建表时只做一次只读检查。播种默认规则前同样先查后写，避免每次请求都产生写操作。
    """
    conn = connection or get_connection()
    marker = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='risk_suppliers'").fetchone()
    if marker is None:
        for statement in SCHEMA.split(";"):
            sql = statement.strip()
            if sql:
                conn.execute(sql)
    seeded = conn.execute("SELECT 1 FROM risk_rule_sets WHERE version=?", (RULE_VERSION,)).fetchone()
    if seeded is None:
        conn.execute(
            "INSERT INTO risk_rule_sets(version,config_json,status,created_at) VALUES(?,?,?,?)",
            (RULE_VERSION, json.dumps(DEFAULT_RULE_CONFIG, ensure_ascii=False, sort_keys=True), "active", _now()),
        )


def _clamp(value: float, low: float, high: float) -> float:
    return round(max(low, min(high, value)), 4)


class RiskService:
    """供应商风险画像的事务边界与评分编排。"""

    def __init__(self, connection: sqlite3.Connection | None = None):
        self.connection = connection or get_connection()
        ensure_schema(self.connection)

    # ------------------------------------------------------------------
    # 基础查询
    # ------------------------------------------------------------------
    def _supplier_row(self, supplier_id: int) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM risk_suppliers WHERE id=?", (supplier_id,)).fetchone()
        if row is None:
            raise KeyError("supplier_not_found")
        return row

    def _audit(self, supplier_id: int | None, action: str, actor: str, payload: dict[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO risk_audit(supplier_id,action,actor,payload_json,created_at) VALUES(?,?,?,?,?)",
            (supplier_id, action, actor, json.dumps(payload, ensure_ascii=False, default=str), _now()),
        )

    def _active_rule(self) -> tuple[str, dict[str, Any]]:
        row = self.connection.execute(
            "SELECT version, config_json FROM risk_rule_sets WHERE status='active' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:  # 保底：播种逻辑异常时退回内置默认配置
            return RULE_VERSION, dict(DEFAULT_RULE_CONFIG)
        return row["version"], json.loads(row["config_json"])

    def _rule_by_version(self, version: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT config_json FROM risk_rule_sets WHERE version=?", (version,)).fetchone()
        if row is None:
            return dict(DEFAULT_RULE_CONFIG)
        return json.loads(row["config_json"])

    # ------------------------------------------------------------------
    # 供应商注册与生命周期
    # ------------------------------------------------------------------
    def register_supplier(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = _now()
        with transaction(immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO risk_suppliers(supplier_code,name,status,created_at,updated_at) VALUES(?,?,?,?,?)",
                (payload["supplier_code"], payload["name"], "active", now, now),
            )
            supplier_id = cursor.lastrowid
            self._audit(supplier_id, "supplier.register", actor, payload)
            return self.get_supplier(supplier_id)

    def _ensure_supplier_by_name(self, name: str, actor: str) -> dict[str, Any]:
        """按名称解析供应商；批次链路遇到的未登记名称自动登记，保持历史可挂接。"""
        row = self.connection.execute("SELECT * FROM risk_suppliers WHERE name=?", (name,)).fetchone()
        if row is not None:
            return dict(row)
        now = _now()
        code = name
        if self.connection.execute("SELECT 1 FROM risk_suppliers WHERE supplier_code=?", (code,)).fetchone():
            code = f"{name}#AUTO"
        cursor = self.connection.execute(
            "INSERT INTO risk_suppliers(supplier_code,name,status,created_at,updated_at) VALUES(?,?,?,?,?)",
            (code, name, "active", now, now),
        )
        self._audit(cursor.lastrowid, "supplier.auto_register", actor, {"name": name})
        return dict(self.connection.execute("SELECT * FROM risk_suppliers WHERE id=?", (cursor.lastrowid,)).fetchone())

    def get_supplier(self, supplier_id: int) -> dict[str, Any]:
        supplier = dict(self._supplier_row(supplier_id))
        if supplier["merged_into_id"]:
            target = self.connection.execute("SELECT id,name FROM risk_suppliers WHERE id=?", (supplier["merged_into_id"],)).fetchone()
            supplier["merged_into"] = dict(target) if target else None
        else:
            supplier["merged_into"] = None
        supplier["merged_sources"] = [
            {"id": row["id"], "name": row["name"], "merged_at": row["merged_at"]}
            for row in self.connection.execute(
                "SELECT id,name,merged_at FROM risk_suppliers WHERE merged_into_id=? ORDER BY id", (supplier_id,)
            ).fetchall()
        ]
        published = self._latest_published(self._merge_root(supplier_id)["id"])
        supplier["published_level"] = (published["adjusted_level"] or published["level"]) if published else None
        supplier["published_ratio"] = published["published_ratio"] if published else None
        return supplier

    def list_suppliers(self, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            rows = self.connection.execute("SELECT * FROM risk_suppliers WHERE status=? ORDER BY id", (status,)).fetchall()
        else:
            rows = self.connection.execute("SELECT * FROM risk_suppliers ORDER BY id").fetchall()
        return [self.get_supplier(row["id"]) for row in rows]

    def merge_supplier(self, source_id: int, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        with transaction(immediate=True):
            source = self._supplier_row(source_id)
            target_id = payload["target_supplier_id"]
            target = self._supplier_row(target_id)
            if source_id == target_id:
                raise ValueError("供应商不能合并到自身")
            if source["status"] == "merged":
                raise ValueError("供应商已合并，不能重复合并")
            if target["status"] != "active":
                raise ValueError("存续供应商必须处于正常状态")
            now = _now()
            self.connection.execute(
                "UPDATE risk_suppliers SET status='merged',merged_into_id=?,merged_at=?,merge_reason=?,updated_at=? WHERE id=?",
                (target_id, now, payload["reason"], now, source_id),
            )
            self._audit(source_id, "supplier.merge", actor, {"target_supplier_id": target_id, "reason": payload["reason"]})
            # 合并改变存续供应商的事件全集，立即形成一个待复核的新评分版本。
            score, _ = self._rescore(target_id, trigger_event_id=None, as_of=now)
            return {"source": self.get_supplier(source_id), "target": self.get_supplier(target_id), "score": score}

    def suspend_supplier(self, supplier_id: int, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        with transaction(immediate=True):
            supplier = self._supplier_row(supplier_id)
            if supplier["status"] == "merged":
                raise ValueError("已合并的供应商不能停用")
            if supplier["status"] == "suspended":
                raise ValueError("供应商已处于停用状态")
            now = _now()
            self.connection.execute("UPDATE risk_suppliers SET status='suspended',updated_at=? WHERE id=?", (now, supplier_id))
            self._audit(supplier_id, "supplier.suspend", actor, {"reason": payload["reason"]})
            return self.get_supplier(supplier_id)

    def activate_supplier(self, supplier_id: int, actor: str) -> dict[str, Any]:
        with transaction(immediate=True):
            supplier = self._supplier_row(supplier_id)
            if supplier["status"] != "suspended":
                raise ValueError("仅停用状态的供应商可以恢复")
            now = _now()
            self.connection.execute("UPDATE risk_suppliers SET status='active',updated_at=? WHERE id=?", (now, supplier_id))
            self._audit(supplier_id, "supplier.activate", actor, {})
            return self.get_supplier(supplier_id)

    # ------------------------------------------------------------------
    # 合并链与事件全集
    # ------------------------------------------------------------------
    def _merge_root(self, supplier_id: int) -> dict[str, Any]:
        """沿合并链找到当前承接历史的存续供应商。"""
        current = dict(self._supplier_row(supplier_id))
        seen = {current["id"]}
        while current["status"] == "merged" and current["merged_into_id"]:
            nxt = dict(self._supplier_row(current["merged_into_id"]))
            if nxt["id"] in seen:
                break
            seen.add(nxt["id"])
            current = nxt
        return current

    def _merged_source_ids(self, root_id: int) -> list[int]:
        """找到所有（传递地）合并进 root 的供应商 id。"""
        result: list[int] = []
        frontier = [root_id]
        while frontier:
            placeholders = ",".join("?" for _ in frontier)
            rows = self.connection.execute(
                f"SELECT id FROM risk_suppliers WHERE merged_into_id IN ({placeholders})", frontier
            ).fetchall()
            new_ids = [row["id"] for row in rows if row["id"] not in result]
            result.extend(new_ids)
            frontier = new_ids
        return result

    def _event_universe(self, root_id: int) -> list[dict[str, Any]]:
        """存续供应商的评分事件全集：自身事件 + 被合并供应商保留下来的历史事件。"""
        ids = [root_id] + self._merged_source_ids(root_id)
        placeholders = ",".join("?" for _ in ids)
        rows = self.connection.execute(
            f"SELECT e.*, s.name AS supplier_name FROM risk_events e JOIN risk_suppliers s ON s.id=e.supplier_id "
            f"WHERE e.supplier_id IN ({placeholders}) ORDER BY e.occurred_at, e.id",
            ids,
        ).fetchall()
        events = []
        for row in rows:
            events.append(
                {
                    "id": row["id"],
                    "event_type": row["event_type"],
                    "severity": row["severity"],
                    "occurred_at": row["occurred_at"],
                    "supplier_id": row["supplier_id"],
                    "supplier_name": row["supplier_name"],
                    "from_merged_supplier": row["supplier_id"] != root_id,
                    "source_type": row["source_type"],
                    "source_id": row["source_id"],
                    "detail": json.loads(row["detail_json"]),
                }
            )
        return events

    # ------------------------------------------------------------------
    # 事件归集与确定性评分
    # ------------------------------------------------------------------
    def _record_event(
        self,
        *,
        supplier_id: int,
        event_type: str,
        severity: float,
        occurred_at: str,
        source_type: str,
        source_id: int | None,
        event_key: str,
        detail: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any] | None, bool]:
        """归集一条风险事件并重评分；event_key 去重保证同一来源只归集一次。"""
        existing = self.connection.execute("SELECT * FROM risk_events WHERE event_key=?", (event_key,)).fetchone()
        if existing is not None:
            return dict(existing), None, False
        now = _now()
        cursor = self.connection.execute(
            "INSERT INTO risk_events(event_key,supplier_id,event_type,severity,occurred_at,source_type,source_id,detail_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (event_key, supplier_id, event_type, severity, occurred_at, source_type, source_id, json.dumps(detail, ensure_ascii=False), now),
        )
        event = dict(self.connection.execute("SELECT * FROM risk_events WHERE id=?", (cursor.lastrowid,)).fetchone())
        root = self._merge_root(supplier_id)
        score, _ = self._rescore(root["id"], trigger_event_id=event["id"], as_of=None)
        self._audit(supplier_id, f"event.{event_type}", "system", {"event_key": event_key, "severity": severity})
        return event, score, True

    def _rescore(self, supplier_id: int, trigger_event_id: int | None, as_of: str | None) -> tuple[dict[str, Any], bool]:
        """对供应商（合并链根）生成一个评分版本。

        评估时点 as_of：事件触发时取输入事件中最晚的 occurred_at（完全由输入决定），
        人工重算/合并触发时由调用方显式传入。input_digest = 规则版本 + as_of + 事件 id 列表
        的哈希，命中唯一约束即返回已存在版本——同一触发只产生一个确定版本。
        """
        root = self._merge_root(supplier_id)
        rule_version, config = self._active_rule()
        events = self._event_universe(root["id"])
        if as_of is None:
            as_of = max((event["occurred_at"] for event in events), default=None) or _now()
        included = [event for event in events if event["occurred_at"] <= as_of]
        event_ids = sorted(event["id"] for event in included)
        digest = self._input_digest(rule_version, as_of, event_ids)
        existing = self.connection.execute(
            "SELECT * FROM risk_scores WHERE supplier_id=? AND input_digest=?", (root["id"], digest)
        ).fetchone()
        if existing is not None:
            return dict(existing), False

        as_of_dt = from_storage(as_of)
        assert as_of_dt is not None
        profile = compute_profile(included, config, as_of_dt)
        version_no = self.connection.execute(
            "SELECT COALESCE(MAX(version_no),0)+1 FROM risk_scores WHERE supplier_id=?", (root["id"],)
        ).fetchone()[0]
        now = _now()
        cursor = self.connection.execute(
            "INSERT INTO risk_scores(supplier_id,version_no,rule_version,trigger_event_id,as_of,input_digest,input_event_ids_json,"
            "score,level,factors_json,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                root["id"],
                version_no,
                rule_version,
                trigger_event_id,
                as_of,
                digest,
                json.dumps(event_ids),
                profile["score"],
                profile["level"],
                json.dumps(profile, ensure_ascii=False),
                "draft",
                now,
            ),
        )
        score = dict(self.connection.execute("SELECT * FROM risk_scores WHERE id=?", (cursor.lastrowid,)).fetchone())
        self._audit(root["id"], "score.compute", "system", {"score_id": score["id"], "version_no": version_no, "trigger_event_id": trigger_event_id})
        return score, True

    @staticmethod
    def _input_digest(rule_version: str, as_of: str, event_ids: list[int]) -> str:
        import hashlib

        payload = json.dumps({"rule": rule_version, "as_of": as_of, "events": event_ids}, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()

    # ------------------------------------------------------------------
    # 食品追溯链路钩子（在 food 模块的事务内调用）
    # ------------------------------------------------------------------
    def on_lot_created(self, lot_id: int, supplier_name: str, actor: str) -> dict[str, Any]:
        """批次创建：按当前已发布画像落库抽检比例；合并/停用供应商拒绝新批次。"""
        supplier = self._ensure_supplier_by_name(supplier_name, actor)
        if supplier["status"] == "merged":
            raise ValueError(f"供应商已合并，请改用存续供应商（ID {supplier['merged_into_id']}）")
        if supplier["status"] == "suspended":
            raise ValueError("供应商已停用，不能创建新批次")
        ratio, score_id, rule_version = self._current_recommendation(supplier["id"])
        self.connection.execute(
            "INSERT OR IGNORE INTO risk_lot_sampling(lot_id,supplier_id,sampling_ratio,score_id,rule_version,created_at) VALUES(?,?,?,?,?,?)",
            (lot_id, supplier["id"], ratio, score_id, rule_version, _now()),
        )
        self._audit(supplier["id"], "lot.sampling", actor, {"lot_id": lot_id, "sampling_ratio": ratio, "score_id": score_id})
        return {"supplier_id": supplier["id"], "sampling_ratio": ratio, "risk_score_id": score_id, "rule_version": rule_version}

    def on_test_result(self, lot: dict[str, Any], result: dict[str, Any]) -> dict[str, Any] | None:
        """检测结果钩子：超标（fail）生成 exceedance 事件并触发一次确定性重评分。"""
        if result["verdict"] != "fail":
            return None
        supplier = self._ensure_supplier_by_name(lot["supplier"], "system")
        limit = result["limit_mg_kg"]
        value = result["value_mg_kg"]
        ratio = value / limit if limit > 0 else 5.0
        severity = _clamp(ratio, 1.0, 5.0)
        occurred_at = to_storage(from_storage(result["tested_at"]))
        event, score, created = self._record_event(
            supplier_id=supplier["id"],
            event_type="exceedance",
            severity=severity,
            occurred_at=occurred_at,
            source_type="food_test_result",
            source_id=result["id"],
            event_key=f"test_result:{result['id']}",
            detail={
                "lot_id": lot["id"],
                "lot_code": lot["lot_code"],
                "analyte": result["analyte"],
                "method": result["method"],
                "value_mg_kg": value,
                "limit_mg_kg": limit,
                "exceedance_ratio": round(ratio, 4),
            },
        )
        return {"event": event, "score": score, "created": created}

    def on_temperature(self, lot: dict[str, Any], shipment: dict[str, Any], temperature: dict[str, Any]) -> dict[str, Any] | None:
        """温控钩子：温度越限生成 temperature_anomaly 事件并触发重评分。"""
        if temperature["in_range"]:
            return None
        supplier = self._ensure_supplier_by_name(lot["supplier"], "system")
        low, high, actual = shipment["target_temp_min"], shipment["target_temp_max"], temperature["temperature_c"]
        deviation = (low - actual) if actual < low else (actual - high)
        severity = _clamp(1.0 + 0.2 * deviation, 1.0, 3.0)
        occurred_at = to_storage(from_storage(temperature["recorded_at"]))
        event, score, created = self._record_event(
            supplier_id=supplier["id"],
            event_type="temperature_anomaly",
            severity=severity,
            occurred_at=occurred_at,
            source_type="food_temperature",
            source_id=temperature["id"],
            event_key=f"temperature:{temperature['id']}",
            detail={
                "lot_id": lot["id"],
                "lot_code": lot["lot_code"],
                "shipment_id": shipment["id"],
                "shipment_code": shipment["shipment_code"],
                "temperature_c": actual,
                "target_temp_min": low,
                "target_temp_max": high,
                "deviation_c": round(deviation, 2),
            },
        )
        return {"event": event, "score": score, "created": created}

    # ------------------------------------------------------------------
    # 投诉与整改
    # ------------------------------------------------------------------
    def file_complaint(self, supplier_id: int, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        with transaction(immediate=True):
            self._supplier_row(supplier_id)
            now = _now()
            cursor = self.connection.execute(
                "INSERT INTO risk_complaints(supplier_id,complaint_no,category,channel,severity,content,occurred_at,status,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    supplier_id,
                    payload["complaint_no"],
                    payload["category"],
                    payload["channel"],
                    payload["severity"],
                    payload["content"],
                    payload["occurred_at"],
                    "filed",
                    now,
                ),
            )
            complaint_id = cursor.lastrowid
            severity = 2.0 if payload["severity"] == "serious" else 1.0
            event, score, _ = self._record_event(
                supplier_id=supplier_id,
                event_type="complaint",
                severity=severity,
                occurred_at=payload["occurred_at"],
                source_type="complaint",
                source_id=complaint_id,
                event_key=f"complaint:{complaint_id}",
                detail={"complaint_no": payload["complaint_no"], "category": payload["category"], "severity": payload["severity"]},
            )
            self._audit(supplier_id, "complaint.file", actor, {"complaint_no": payload["complaint_no"]})
            complaint = dict(self.connection.execute("SELECT * FROM risk_complaints WHERE id=?", (complaint_id,)).fetchone())
            return {"complaint": complaint, "event": event, "score": score}

    def list_complaints(self, supplier_id: int) -> list[dict[str, Any]]:
        self._supplier_row(supplier_id)
        rows = self.connection.execute(
            "SELECT * FROM risk_complaints WHERE supplier_id=? ORDER BY occurred_at, id", (supplier_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def create_rectification(self, supplier_id: int, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        with transaction(immediate=True):
            self._supplier_row(supplier_id)
            now = _now()
            cursor = self.connection.execute(
                "INSERT INTO risk_rectifications(supplier_id,lot_id,order_no,requirement,deadline_at,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (supplier_id, payload.get("lot_id"), payload["order_no"], payload["requirement"], payload["deadline_at"], "open", now, now),
            )
            self._audit(supplier_id, "rectification.create", actor, {"order_no": payload["order_no"]})
            return dict(self.connection.execute("SELECT * FROM risk_rectifications WHERE id=?", (cursor.lastrowid,)).fetchone())

    def list_rectifications(self, supplier_id: int) -> list[dict[str, Any]]:
        self._supplier_row(supplier_id)
        rows = self.connection.execute(
            "SELECT * FROM risk_rectifications WHERE supplier_id=? ORDER BY deadline_at, id", (supplier_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def complete_rectification(self, rectification_id: int, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        with transaction(immediate=True):
            row = self.connection.execute("SELECT * FROM risk_rectifications WHERE id=?", (rectification_id,)).fetchone()
            if row is None:
                raise KeyError("rectification_not_found")
            rect = dict(row)
            if rect["status"] == "completed":
                raise ValueError("整改单已完成")
            deadline = from_storage(rect["deadline_at"])
            completed = from_storage(payload["completed_at"])
            assert deadline is not None and completed is not None
            score = None
            if completed > deadline:
                # 逾期完成同样产生逾期事件（occurred_at 取截止时刻，只归集一次）。
                days = (completed - deadline).total_seconds() / 86400.0
                severity = _clamp(1.0 + 0.1 * days, 1.0, 4.0)
                _, score, _ = self._record_event(
                    supplier_id=rect["supplier_id"],
                    event_type="rectification_overdue",
                    severity=severity,
                    occurred_at=rect["deadline_at"],
                    source_type="rectification",
                    source_id=rect["id"],
                    event_key=f"rectification:{rect['id']}",
                    detail={"order_no": rect["order_no"], "days_overdue": round(days, 2), "detected_by": "late_completion"},
                )
            now = _now()
            self.connection.execute(
                "UPDATE risk_rectifications SET status='completed',completed_at=?,updated_at=? WHERE id=?",
                (payload["completed_at"], now, rectification_id),
            )
            self._audit(rect["supplier_id"], "rectification.complete", actor, {"order_no": rect["order_no"]})
            updated = dict(self.connection.execute("SELECT * FROM risk_rectifications WHERE id=?", (rectification_id,)).fetchone())
            return {"rectification": updated, "score": score}

    def sweep_overdue(self, as_of: str | None = None, actor: str = "system") -> dict[str, Any]:
        """扫描逾期整改单：标记 overdue 并为每单生成一次逾期事件（幂等）。"""
        if as_of:
            parsed = from_storage(as_of)
            if parsed is None:
                raise ValueError("as_of 时间格式无效")
            as_of = to_storage(parsed)
        else:
            as_of = _now()
        marked: list[int] = []
        with transaction(immediate=True):
            rows = self.connection.execute(
                "SELECT * FROM risk_rectifications WHERE status='open' AND deadline_at < ? ORDER BY id", (as_of,)
            ).fetchall()
            as_of_dt = from_storage(as_of)
            assert as_of_dt is not None
            for row in rows:
                rect = dict(row)
                deadline = from_storage(rect["deadline_at"])
                assert deadline is not None
                days = (as_of_dt - deadline).total_seconds() / 86400.0
                severity = _clamp(1.0 + 0.1 * days, 1.0, 4.0)
                self._record_event(
                    supplier_id=rect["supplier_id"],
                    event_type="rectification_overdue",
                    severity=severity,
                    occurred_at=rect["deadline_at"],
                    source_type="rectification",
                    source_id=rect["id"],
                    event_key=f"rectification:{rect['id']}",
                    detail={"order_no": rect["order_no"], "days_overdue": round(days, 2), "detected_by": "sweep"},
                )
                self.connection.execute(
                    "UPDATE risk_rectifications SET status='overdue',updated_at=? WHERE id=?", (_now(), rect["id"])
                )
                self._audit(rect["supplier_id"], "rectification.overdue", actor, {"order_no": rect["order_no"]})
                marked.append(rect["id"])
        return {"as_of": as_of, "marked_overdue": len(marked), "rectification_ids": marked}

    # ------------------------------------------------------------------
    # 人工复核与等级发布
    # ------------------------------------------------------------------
    def _score_row(self, score_id: int) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM risk_scores WHERE id=?", (score_id,)).fetchone()
        if row is None:
            raise KeyError("score_not_found")
        return row

    def review_score(self, score_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        with transaction(immediate=True):
            score = dict(self._score_row(score_id))
            if score["status"] != "draft":
                raise ValueError("仅待复核（draft）的评分版本可以复核")
            action = payload["action"]
            adjusted = payload.get("adjusted_level")
            if action == "adjust" and not adjusted:
                raise ValueError("调整等级时必须给出 adjusted_level")
            if action == "confirm":
                adjusted = None
            now = _now()
            self.connection.execute(
                "UPDATE risk_scores SET status='reviewed',adjusted_level=? WHERE id=?", (adjusted, score_id)
            )
            self.connection.execute(
                "INSERT INTO risk_reviews(score_id,reviewer,action,adjusted_level,comment,created_at) VALUES(?,?,?,?,?,?)",
                (score_id, payload["reviewer"], action, adjusted, payload["comment"], now),
            )
            self._audit(score["supplier_id"], "score.review", payload["reviewer"], {"score_id": score_id, "action": action, "adjusted_level": adjusted})
            return self.score_detail(score_id)

    def publish_score(self, score_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        with transaction(immediate=True):
            score = dict(self._score_row(score_id))
            if score["status"] != "reviewed":
                raise ValueError("仅已复核（reviewed）的评分版本可以发布")
            config = self._rule_by_version(score["rule_version"])
            effective_level = score["adjusted_level"] or score["level"]
            ratio = config["sampling_by_level"][effective_level]
            now = _now()
            self.connection.execute(
                "UPDATE risk_scores SET status='superseded' WHERE supplier_id=? AND status='published'", (score["supplier_id"],)
            )
            self.connection.execute(
                "UPDATE risk_scores SET status='published',published_ratio=?,published_at=?,published_by=? WHERE id=?",
                (ratio, now, payload["operator"], score_id),
            )
            self._audit(
                score["supplier_id"],
                "score.publish",
                payload["operator"],
                {"score_id": score_id, "effective_level": effective_level, "published_ratio": ratio, "note": payload.get("note", "")},
            )
            return self.score_detail(score_id)

    def recalculate(self, supplier_id: int, as_of: str | None, actor: str) -> dict[str, Any]:
        with transaction(immediate=True):
            root = self._merge_root(supplier_id)
            score, created = self._rescore(root["id"], trigger_event_id=None, as_of=as_of or _now())
            self._audit(root["id"], "score.recalculate", actor, {"score_id": score["id"], "created": created})
            return {"score": self.score_detail(score["id"]), "created": created}

    # ------------------------------------------------------------------
    # 查询与解释
    # ------------------------------------------------------------------
    def _latest_published(self, supplier_id: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM risk_scores WHERE supplier_id=? AND status='published' ORDER BY version_no DESC LIMIT 1", (supplier_id,)
        ).fetchone()
        return dict(row) if row else None

    def _current_recommendation(self, supplier_id: int) -> tuple[float, int | None, str]:
        """当前生效的抽检建议：最新已发布版本的比例，否则规则默认比例。"""
        published = self._latest_published(supplier_id)
        if published is not None:
            return published["published_ratio"], published["id"], published["rule_version"]
        rule_version, config = self._active_rule()
        return config["default_sampling_ratio"], None, rule_version

    def score_detail(self, score_id: int) -> dict[str, Any]:
        score = dict(self._score_row(score_id))
        score["input_event_ids"] = json.loads(score.pop("input_event_ids_json"))
        score["profile"] = json.loads(score.pop("factors_json"))
        score["effective_level"] = score["adjusted_level"] or score["level"]
        reviews = self.connection.execute(
            "SELECT * FROM risk_reviews WHERE score_id=? ORDER BY id", (score_id,)
        ).fetchall()
        score["reviews"] = [dict(row) for row in reviews]
        return score

    def list_scores(self, supplier_id: int) -> list[dict[str, Any]]:
        self._supplier_row(supplier_id)
        rows = self.connection.execute(
            "SELECT id,supplier_id,version_no,rule_version,trigger_event_id,as_of,score,level,adjusted_level,status,"
            "published_ratio,published_at,published_by,created_at FROM risk_scores WHERE supplier_id=? ORDER BY version_no DESC",
            (supplier_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_events(self, supplier_id: int) -> list[dict[str, Any]]:
        self._supplier_row(supplier_id)
        root = self._merge_root(supplier_id)
        return self._event_universe(root["id"])

    def profile(self, supplier_id: int) -> dict[str, Any]:
        """供应商风险画像：当前生效等级/抽检比例 + 逐因子解释（含反事实影响）。"""
        supplier = self.get_supplier(supplier_id)
        root = self._merge_root(supplier_id)
        published = self._latest_published(root["id"])
        pending_row = self.connection.execute(
            "SELECT id,version_no,score,level,adjusted_level,status,as_of,created_at FROM risk_scores "
            "WHERE supplier_id=? AND status IN ('draft','reviewed') ORDER BY version_no DESC LIMIT 1",
            (root["id"],),
        ).fetchone()
        events = self._event_universe(root["id"])
        event_counts: dict[str, int] = {}
        for event in events:
            event_counts[event["event_type"]] = event_counts.get(event["event_type"], 0) + 1

        if published is not None:
            detail = self.score_detail(published["id"])
            sampling = {
                "ratio": published["published_ratio"],
                "level": published["adjusted_level"] or published["level"],
                "computed_level": published["level"],
                "source": "published_score",
                "score_id": published["id"],
                "version_no": published["version_no"],
                "rule_version": published["rule_version"],
            }
            factors = detail["profile"]["factors"]
        else:
            rule_version, config = self._active_rule()
            detail = None
            sampling = {
                "ratio": config["default_sampling_ratio"],
                "level": None,
                "computed_level": None,
                "source": "default",
                "score_id": None,
                "version_no": None,
                "rule_version": rule_version,
            }
            factors = []
        return {
            "supplier": supplier,
            "scoring_supplier_id": root["id"],
            "sampling": sampling,
            "published": detail,
            "factor_explanations": factors,
            "pending": dict(pending_row) if pending_row else None,
            "event_counts": event_counts,
        }

    def lot_sampling(self, lot_id: int) -> dict[str, Any]:
        """批次抽检计划：创建时落库的比例 vs 当前生效比例（风险升高后差异可见）。"""
        row = self.connection.execute("SELECT * FROM risk_lot_sampling WHERE lot_id=?", (lot_id,)).fetchone()
        if row is None:
            raise KeyError("lot_sampling_not_found")
        recorded = dict(row)
        supplier = self._merge_root(recorded["supplier_id"])
        ratio, score_id, rule_version = self._current_recommendation(supplier["id"])
        return {
            "lot_id": lot_id,
            "supplier_id": supplier["id"],
            "supplier_name": supplier["name"],
            "recorded": {
                "sampling_ratio": recorded["sampling_ratio"],
                "risk_score_id": recorded["score_id"],
                "rule_version": recorded["rule_version"],
                "created_at": recorded["created_at"],
            },
            "current": {"sampling_ratio": ratio, "risk_score_id": score_id, "rule_version": rule_version},
            "ratio_changed": ratio != recorded["sampling_ratio"],
        }

    def lot_sampling_or_none(self, lot_id: int) -> dict[str, Any] | None:
        try:
            return self.lot_sampling(lot_id)
        except KeyError:
            return None

    def list_rules(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM risk_rule_sets ORDER BY id").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["config"] = json.loads(item.pop("config_json"))
            result.append(item)
        return result
