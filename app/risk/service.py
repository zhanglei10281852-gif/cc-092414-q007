"""供应商风险画像服务。

设计要点：
- 供应商只能停用或合并，不能删除；合并后历史事件保留在原供应商名下，
  评分沿合并链汇总，停用供应商的历史同样可查。
- 评分输入事件（超标、温控、逾期、投诉）全部落表，评分版本记录规则版本、
  输入指纹与逐事件因子明细；相同输入只产生同一个版本（幂等）。
- 高分等级走人工复核后发布，低中风险自动发布；抽检建议只取自已发布版本。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from app.core.clock import from_storage, to_storage, utc_now
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction
from app.risk.rules import (
    RISK_LEVELS,
    RULE_VERSION_1,
    default_config,
    evaluate,
    input_fingerprint,
    validate_config,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS risk_suppliers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    code TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','disabled','merged')),
    merged_into_id INTEGER REFERENCES risk_suppliers(id),
    disabled_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS risk_supplier_merges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_supplier_id INTEGER NOT NULL REFERENCES risk_suppliers(id),
    target_supplier_id INTEGER NOT NULL REFERENCES risk_suppliers(id),
    reason TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS risk_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id INTEGER NOT NULL REFERENCES risk_suppliers(id) ON DELETE RESTRICT,
    event_type TEXT NOT NULL CHECK(event_type IN ('test_fail','test_pass','temperature_anomaly','rectification_overdue','complaint')),
    occurred_at TEXT NOT NULL,
    severity TEXT,
    magnitude REAL,
    lot_id INTEGER,
    ref_table TEXT NOT NULL DEFAULT '',
    ref_id TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','void')),
    void_reason TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_risk_events_supplier ON risk_events(supplier_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_risk_events_ref ON risk_events(ref_table, ref_id);
CREATE TABLE IF NOT EXISTS risk_rectifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id INTEGER NOT NULL REFERENCES risk_suppliers(id) ON DELETE RESTRICT,
    lot_id INTEGER,
    reason_event_id INTEGER REFERENCES risk_events(id),
    title TEXT NOT NULL,
    due_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','resolved','cancelled')),
    resolved_at TEXT,
    resolution TEXT NOT NULL DEFAULT '',
    operator TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_risk_rect_status ON risk_rectifications(status, due_at);
CREATE TABLE IF NOT EXISTS risk_rule_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version TEXT NOT NULL UNIQUE,
    config_json TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    is_active INTEGER NOT NULL DEFAULT 0 CHECK(is_active IN (0,1)),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS risk_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id INTEGER NOT NULL REFERENCES risk_suppliers(id) ON DELETE RESTRICT,
    version INTEGER NOT NULL,
    rule_version TEXT NOT NULL,
    trigger_event_id INTEGER,
    trigger_source TEXT NOT NULL DEFAULT '',
    input_fingerprint TEXT NOT NULL,
    score REAL NOT NULL,
    computed_level TEXT NOT NULL,
    level TEXT NOT NULL,
    level_label TEXT NOT NULL DEFAULT '',
    sampling_ratio REAL NOT NULL,
    explanation_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','published','rejected','superseded')),
    is_current INTEGER NOT NULL DEFAULT 0 CHECK(is_current IN (0,1)),
    overridden INTEGER NOT NULL DEFAULT 0,
    published_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(supplier_id, version)
);
CREATE INDEX IF NOT EXISTS idx_risk_scores_current ON risk_scores(supplier_id, is_current);
CREATE TABLE IF NOT EXISTS risk_score_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    score_id INTEGER NOT NULL REFERENCES risk_scores(id) ON DELETE CASCADE,
    event_id INTEGER NOT NULL REFERENCES risk_events(id),
    event_type TEXT NOT NULL,
    contribution REAL NOT NULL,
    UNIQUE(score_id, event_id)
);
CREATE TABLE IF NOT EXISTS risk_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    score_id INTEGER NOT NULL REFERENCES risk_scores(id),
    decision TEXT NOT NULL CHECK(decision IN ('publish','publish_override','reject')),
    reason TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    override_level TEXT,
    override_sampling_ratio REAL,
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
"""

# 无评分版本时的基线抽检比例
BASELINE_RATIO = 0.05
LEVEL_LABELS = {"low": "低风险", "medium": "中风险", "high": "高风险", "critical": "极高风险"}
# 达到该等级（含）的新评分必须人工复核后才能发布
REVIEW_REQUIRED_LEVELS = {"high", "critical"}


def _now_iso() -> str:
    return to_storage(utc_now())


def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class RiskService:
    """供应商风险事件、评分版本与发布流程的事务边界。"""

    def __init__(self, connection: sqlite3.Connection | None = None, *, ensure: bool = True):
        self.connection = connection or get_connection()
        if ensure:
            self.ensure_schema(self.connection)

    # ---- 表结构与迁移 ---------------------------------------------------

    @staticmethod
    def ensure_schema(connection: sqlite3.Connection | None = None) -> None:
        conn = connection or get_connection()
        conn.executescript(SCHEMA)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(food_lots)").fetchall()}
        if "supplier_id" not in columns:
            conn.execute("ALTER TABLE food_lots ADD COLUMN supplier_id INTEGER")
        if "recommended_sampling_ratio" not in columns:
            conn.execute("ALTER TABLE food_lots ADD COLUMN recommended_sampling_ratio REAL NOT NULL DEFAULT 0.05")
        RiskService._backfill_suppliers(conn)
        now = _now_iso()
        seeded = conn.execute("SELECT 1 FROM risk_rule_versions WHERE version=?", (RULE_VERSION_1,)).fetchone()
        if seeded is None:
            conn.execute(
                "INSERT INTO risk_rule_versions(version,config_json,note,is_active,created_by,created_at) VALUES(?,?,?,1,?,?)",
                (RULE_VERSION_1, json.dumps(default_config(), ensure_ascii=False), "初始规则：四类因子、指数时间衰减与抽检比例映射", "system", now),
            )

    @staticmethod
    def _backfill_suppliers(conn: sqlite3.Connection) -> None:
        now = _now_iso()
        names = [row[0] for row in conn.execute("SELECT DISTINCT supplier FROM food_lots WHERE supplier_id IS NULL").fetchall()]
        for name in names:
            conn.execute(
                "INSERT OR IGNORE INTO risk_suppliers(name,status,created_at,updated_at) VALUES(?, 'active', ?, ?)",
                (name, now, now),
            )
            row = conn.execute("SELECT id FROM risk_suppliers WHERE name=?", (name,)).fetchone()
            conn.execute("UPDATE food_lots SET supplier_id=? WHERE supplier=? AND supplier_id IS NULL", (row[0], name))

    # ---- 规则版本 -------------------------------------------------------

    def list_rule_versions(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT id,version,note,is_active,created_by,created_at FROM risk_rule_versions ORDER BY id DESC").fetchall()
        return [dict(row) for row in rows]

    def get_active_rule(self, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        conn = conn or self.connection
        row = conn.execute("SELECT version,config_json FROM risk_rule_versions WHERE is_active=1 ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            raise RuntimeError("缺少生效中的风险评分规则版本")
        return {"version": row["version"], "config": json.loads(row["config_json"])}

    def publish_rule_version(self, config: dict[str, Any], note: str, actor: str) -> dict[str, Any]:
        validate_config(config)
        version = str(config.get("version") or "").strip()
        if not version:
            raise ValidationError("规则配置缺少 version")
        now = _now_iso()
        with transaction(immediate=True) as conn:
            existing = conn.execute("SELECT id FROM risk_rule_versions WHERE version=?", (version,)).fetchone()
            if existing:
                raise ConflictError("规则版本已存在", context={"version": version})
            conn.execute("UPDATE risk_rule_versions SET is_active=0")
            cursor = conn.execute(
                "INSERT INTO risk_rule_versions(version,config_json,note,is_active,created_by,created_at) VALUES(?,?,?,1,?,?)",
                (version, json.dumps(config, ensure_ascii=False), note, actor, now),
            )
            conn.execute("INSERT INTO risk_audit(action,actor,payload_json,created_at) VALUES(?,?,?,?)",
                         ("rule.publish", actor, json.dumps({"version": version, "note": note}, ensure_ascii=False), now))
            return _dict(conn.execute("SELECT id,version,note,is_active,created_by,created_at FROM risk_rule_versions WHERE id=?", (cursor.lastrowid,)).fetchone()) or {}

    # ---- 供应商合并/停用 ------------------------------------------------

    def register_supplier(self, name: str, code: str = "", actor: str = "system",
                          conn: sqlite3.Connection | None = None, *, create_only: bool = False) -> dict[str, Any]:
        own = conn is None
        conn = conn or self.connection
        now = _now_iso()
        if own:
            conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT * FROM risk_suppliers WHERE name=?", (name,)).fetchone()
            if row is None:
                cursor = conn.execute("INSERT INTO risk_suppliers(name,code,status,created_at,updated_at) VALUES(?,?,'active',?,?)", (name, code, now, now))
                row = conn.execute("SELECT * FROM risk_suppliers WHERE id=?", (cursor.lastrowid,)).fetchone()
                conn.execute("INSERT INTO risk_audit(supplier_id,action,actor,payload_json,created_at) VALUES(?,?,?,?,?)",
                             (row["id"], "supplier.register", actor, json.dumps({"name": name, "code": code}, ensure_ascii=False), now))
            elif create_only:
                if own:
                    conn.rollback()
                raise ConflictError("供应商已存在", context={"name": name})
            elif row["status"] == "merged":
                row = conn.execute("SELECT * FROM risk_suppliers WHERE id=?", (row["merged_into_id"],)).fetchone()
            result = dict(row)
            if own:
                conn.commit()
            return result
        except Exception:
            if own:
                conn.rollback()
            raise

    def list_suppliers(self, include_inactive: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM risk_suppliers"
        if not include_inactive:
            sql += " WHERE status='active'"
        return [dict(row) for row in self.connection.execute(sql + " ORDER BY id").fetchall()]

    def _require_supplier(self, conn: sqlite3.Connection, supplier_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM risk_suppliers WHERE id=?", (supplier_id,)).fetchone()
        if row is None:
            raise NotFoundError("供应商不存在")
        return row

    def merge_suppliers(self, source_id: int, target_id: int, reason: str, actor: str) -> dict[str, Any]:
        if source_id == target_id:
            raise ValidationError("不能将供应商合并到自身")
        now = _now_iso()
        with transaction(immediate=True) as conn:
            source = self._require_supplier(conn, source_id)
            target = self._require_supplier(conn, target_id)
            if target["status"] != "active":
                raise ValidationError("合并目标供应商必须处于启用状态")
            if source["status"] == "merged":
                raise ConflictError("来源供应商已被合并")
            conn.execute("UPDATE risk_suppliers SET status='merged',merged_into_id=?,updated_at=? WHERE id=?", (target_id, now, source_id))
            conn.execute("UPDATE food_lots SET supplier_id=? WHERE supplier_id=?", (target_id, source_id))
            conn.execute("INSERT INTO risk_supplier_merges(source_supplier_id,target_supplier_id,reason,actor,created_at) VALUES(?,?,?,?,?)",
                         (source_id, target_id, reason, actor, now))
            conn.execute("INSERT INTO risk_audit(supplier_id,action,actor,payload_json,created_at) VALUES(?,?,?,?,?)",
                         (source_id, "supplier.merge", actor, json.dumps({"target_id": target_id, "reason": reason}, ensure_ascii=False), now))
            # 合并后立即重算目标供应商画像，来源历史沿合并链全部纳入
            score = self.score_supplier(target_id, trigger_source="supplier_merge", actor=actor, force=True, conn=conn)
            ratio = self.current_sampling_ratio(target_id, conn=conn)
            conn.execute("UPDATE food_lots SET recommended_sampling_ratio=? WHERE supplier_id=?", (ratio, target_id))
            result = self.get_supplier(target_id, conn=conn)
            result["latest_score"] = {"version": score["version"], "status": score["status"], "level": score["level"]}
            return result

    def disable_supplier(self, supplier_id: int, reason: str, actor: str) -> dict[str, Any]:
        now = _now_iso()
        with transaction(immediate=True) as conn:
            supplier = self._require_supplier(conn, supplier_id)
            if supplier["status"] == "merged":
                raise ConflictError("已合并供应商不能停用")
            conn.execute("UPDATE risk_suppliers SET status='disabled',disabled_reason=?,updated_at=? WHERE id=?", (reason, now, supplier_id))
            conn.execute("INSERT INTO risk_audit(supplier_id,action,actor,payload_json,created_at) VALUES(?,?,?,?,?)",
                         (supplier_id, "supplier.disable", actor, json.dumps({"reason": reason}, ensure_ascii=False), now))
            return self.get_supplier(supplier_id, conn=conn)

    def enable_supplier(self, supplier_id: int, actor: str) -> dict[str, Any]:
        now = _now_iso()
        with transaction(immediate=True) as conn:
            supplier = self._require_supplier(conn, supplier_id)
            if supplier["status"] != "disabled":
                raise ConflictError("仅停用状态的供应商可重新启用")
            conn.execute("UPDATE risk_suppliers SET status='active',disabled_reason='',updated_at=? WHERE id=?", (now, supplier_id))
            conn.execute("INSERT INTO risk_audit(supplier_id,action,actor,payload_json,created_at) VALUES(?,?,?,?,?)",
                         (supplier_id, "supplier.enable", actor, "{}", now))
            return self.get_supplier(supplier_id, conn=conn)

    def _root_id(self, conn: sqlite3.Connection, supplier_id: int) -> int:
        """沿合并链找到当前承接的在营供应商。"""
        seen: set[int] = set()
        current = supplier_id
        while True:
            row = conn.execute("SELECT status,merged_into_id FROM risk_suppliers WHERE id=?", (current,)).fetchone()
            if row is None or row["status"] != "merged" or row["merged_into_id"] is None or current in seen:
                return current
            seen.add(current)
            current = row["merged_into_id"]

    def _merge_chain_ids(self, conn: sqlite3.Connection, supplier_id: int) -> list[int]:
        """评分汇总范围：供应商自身 + 所有沿合并链并入它的来源，历史不丢失。

        支持多级合并（A→B→C 时，C 的画像仍包含 A 的全部事件）。
        """
        ids = [supplier_id]
        frontier = [supplier_id]
        while frontier:
            placeholders = ",".join("?" for _ in frontier)
            rows = conn.execute(
                f"SELECT id FROM risk_suppliers WHERE merged_into_id IN ({placeholders}) AND status='merged'",
                frontier,
            ).fetchall()
            frontier = [row[0] for row in rows if row[0] not in ids]
            ids.extend(frontier)
        return ids

    def get_supplier(self, supplier_id: int, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        conn = conn or self.connection
        supplier = conn.execute("SELECT * FROM risk_suppliers WHERE id=?", (supplier_id,)).fetchone()
        if supplier is None:
            raise NotFoundError("供应商不存在")
        result = dict(supplier)
        result["merges_into"] = [dict(row) for row in conn.execute(
            "SELECT m.*, s.name AS source_name FROM risk_supplier_merges m JOIN risk_suppliers s ON s.id=m.source_supplier_id WHERE m.target_supplier_id=? ORDER BY m.id",
            (supplier_id,),
        ).fetchall()]
        result["merged_from"] = [dict(row) for row in conn.execute(
            "SELECT m.*, s.name AS target_name FROM risk_supplier_merges m JOIN risk_suppliers s ON s.id=m.target_supplier_id WHERE m.source_supplier_id=? ORDER BY m.id",
            (supplier_id,),
        ).fetchall()]
        result["event_count"] = conn.execute("SELECT COUNT(*) FROM risk_events WHERE supplier_id=?", (supplier_id,)).fetchone()[0]
        result["open_rectification_count"] = conn.execute("SELECT COUNT(*) FROM risk_rectifications WHERE supplier_id=? AND status='open'", (supplier_id,)).fetchone()[0]
        current = conn.execute("SELECT version,rule_version,score,level,level_label,sampling_ratio,published_at FROM risk_scores WHERE supplier_id=? AND is_current=1", (supplier_id,)).fetchone()
        result["current_profile"] = dict(current) if current else None
        return result

    # ---- 事件登记 -------------------------------------------------------

    def record_event(
        self,
        *,
        event_type: str,
        supplier_id: int,
        occurred_at: str,
        payload: dict[str, Any],
        severity: str | None = None,
        magnitude: float | None = None,
        lot_id: int | None = None,
        ref_table: str = "",
        ref_id: str = "",
        actor: str = "system",
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        own = conn is None
        conn = conn or self.connection
        now = _now_iso()
        if own:
            conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = conn.execute(
                "INSERT INTO risk_events(supplier_id,event_type,occurred_at,severity,magnitude,lot_id,ref_table,ref_id,payload_json,actor,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (supplier_id, event_type, to_storage(from_storage(occurred_at)), severity, magnitude, lot_id, ref_table, str(ref_id), json.dumps(payload, ensure_ascii=False), actor, now),
            )
            event_id = cursor.lastrowid
            conn.execute("INSERT INTO risk_audit(supplier_id,action,actor,payload_json,created_at) VALUES(?,?,?,?,?)",
                         (supplier_id, "event.record." + event_type, actor, json.dumps({"event_id": event_id, "ref_id": ref_id}, ensure_ascii=False), now))
            result = _dict(conn.execute("SELECT * FROM risk_events WHERE id=?", (event_id,)).fetchone()) or {}
            if own:
                conn.commit()
            return result
        except Exception:
            if own:
                conn.rollback()
            raise

    def list_events(self, supplier_id: int, *, include_void: bool = True) -> list[dict[str, Any]]:
        supplier = self._require_supplier(self.connection, supplier_id)
        root_id = self._root_id(self.connection, supplier_id)
        ids = self._merge_chain_ids(self.connection, root_id)
        placeholders = ",".join("?" for _ in ids)
        sql = f"SELECT * FROM risk_events WHERE supplier_id IN ({placeholders})"
        if not include_void:
            sql += " AND status='active'"
        sql += " ORDER BY occurred_at, id"
        return [dict(row) for row in self.connection.execute(sql, ids).fetchall()]

    # ---- 整改单 ---------------------------------------------------------

    def open_rectification(self, supplier_id: int, title: str, due_at: str, *, lot_id: int | None = None,
                           reason_event_id: int | None = None, operator: str = "system",
                           conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        own = conn is None
        conn = conn or self.connection
        now = _now_iso()
        if own:
            conn.execute("BEGIN IMMEDIATE")
        try:
            self._require_supplier(conn, supplier_id)
            cursor = conn.execute(
                "INSERT INTO risk_rectifications(supplier_id,lot_id,reason_event_id,title,due_at,operator,created_at) VALUES(?,?,?,?,?,?,?)",
                (supplier_id, lot_id, reason_event_id, title, to_storage(from_storage(due_at)), operator, now),
            )
            rect_id = cursor.lastrowid
            conn.execute("INSERT INTO risk_audit(supplier_id,action,actor,payload_json,created_at) VALUES(?,?,?,?,?)",
                         (supplier_id, "rectification.open", operator, json.dumps({"rectification_id": rect_id, "due_at": due_at}, ensure_ascii=False), now))
            result = _dict(conn.execute("SELECT * FROM risk_rectifications WHERE id=?", (rect_id,)).fetchone()) or {}
            if own:
                conn.commit()
            return result
        except Exception:
            if own:
                conn.rollback()
            raise

    def resolve_rectification(self, rectification_id: int, resolution: str, actor: str) -> dict[str, Any]:
        now = _now_iso()
        with transaction(immediate=True) as conn:
            row = conn.execute("SELECT * FROM risk_rectifications WHERE id=?", (rectification_id,)).fetchone()
            if row is None:
                raise NotFoundError("整改单不存在")
            if row["status"] != "open":
                raise ConflictError("整改单不是待办状态")
            conn.execute("UPDATE risk_rectifications SET status='resolved',resolved_at=?,resolution=? WHERE id=?", (now, resolution, rectification_id))
            # 已闭环的逾期事件不再计入后续评分，保留行用于历史追溯
            conn.execute(
                "UPDATE risk_events SET status='void',void_reason=? WHERE event_type='rectification_overdue' AND ref_table='risk_rectifications' AND ref_id=? AND status='active'",
                (f"整改单 #{rectification_id} 已闭环", rectification_id),
            )
            conn.execute("INSERT INTO risk_audit(supplier_id,action,actor,payload_json,created_at) VALUES(?,?,?,?,?)",
                         (row["supplier_id"], "rectification.resolve", actor, json.dumps({"rectification_id": rectification_id}, ensure_ascii=False), now))
            # 逾期事件作废后立即重算画像，让抽检建议及时回落
            score = self.score_supplier(row["supplier_id"], trigger_source="rectification_resolved", actor=actor, force=True, conn=conn)
            ratio = self.current_sampling_ratio(row["supplier_id"], conn=conn)
            conn.execute("UPDATE food_lots SET recommended_sampling_ratio=? WHERE supplier_id=?", (ratio, row["supplier_id"]))
            result = _dict(conn.execute("SELECT * FROM risk_rectifications WHERE id=?", (rectification_id,)).fetchone()) or {}
            result["score_version"] = score["version"]
            result["score_status"] = score["status"]
            return result

    def list_rectifications(self, supplier_id: int | None = None, status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT r.*, s.name AS supplier_name FROM risk_rectifications r JOIN risk_suppliers s ON s.id=r.supplier_id WHERE 1=1"
        params: list[Any] = []
        if supplier_id is not None:
            sql += " AND r.supplier_id=?"
            params.append(supplier_id)
        if status:
            sql += " AND r.status=?"
            params.append(status)
        sql += " ORDER BY r.due_at, r.id"
        return [dict(row) for row in self.connection.execute(sql, params).fetchall()]

    def record_complaint(self, supplier_id: int, payload: dict[str, Any], actor: str = "regulator", occurred_at: str | None = None) -> dict[str, Any]:
        if payload.get("severity") not in {"low", "medium", "high"}:
            raise ValidationError("投诉严重度必须是 low/medium/high")
        occurred_at = occurred_at or payload.get("occurred_at") or _now_iso()
        with transaction(immediate=True) as conn:
            self._require_supplier(conn, supplier_id)
            event = self.record_event(
                event_type="complaint", supplier_id=supplier_id, occurred_at=occurred_at,
                payload=payload, severity=payload["severity"], actor=actor, conn=conn,
            )
            score = self.score_supplier(supplier_id, trigger_source="complaint", trigger_event_id=event["id"], conn=conn)
            return {"event": event, "score": score}

    # ---- 评分 -----------------------------------------------------------

    def _load_scoring_events(self, conn: sqlite3.Connection, supplier_ids: list[int], as_of: datetime, window_days: int) -> list[dict[str, Any]]:
        start = to_storage(as_of - timedelta(days=window_days))
        placeholders = ",".join("?" for _ in supplier_ids)
        rows = conn.execute(
            f"SELECT * FROM risk_events WHERE supplier_id IN ({placeholders}) AND status='active' AND occurred_at>=? ORDER BY occurred_at, id",
            (*supplier_ids, start),
        ).fetchall()
        events: list[dict[str, Any]] = []
        for row in rows:
            occurred = from_storage(row["occurred_at"])
            item = dict(row)
            item["occurred_at"] = occurred
            item["occurred_at_iso"] = to_storage(occurred)
            item["payload"] = json.loads(row["payload_json"])
            events.append(item)
        return events

    def _materialize_overdue_events(self, conn: sqlite3.Connection, supplier_ids: list[int], as_of: datetime, actor: str) -> None:
        """为已到期限且未闭环、尚未登记逾期事件的整改单补齐输入事件。"""
        placeholders = ",".join("?" for _ in supplier_ids)
        as_of_iso = to_storage(as_of)
        rows = conn.execute(
            f"""SELECT r.* FROM risk_rectifications r
                WHERE r.supplier_id IN ({placeholders}) AND r.status='open' AND r.due_at < ?
                AND NOT EXISTS (
                    SELECT 1 FROM risk_events e
                    WHERE e.event_type='rectification_overdue' AND e.ref_table='risk_rectifications'
                      AND e.ref_id=CAST(r.id AS TEXT) AND e.status='active'
                )""",
            (*supplier_ids, as_of_iso),
        ).fetchall()
        for row in rows:
            self.record_event(
                event_type="rectification_overdue",
                supplier_id=row["supplier_id"],
                occurred_at=row["due_at"],
                payload={"due_at": row["due_at"], "title": row["title"]},
                lot_id=row["lot_id"],
                ref_table="risk_rectifications",
                ref_id=row["id"],
                actor=actor,
                conn=conn,
            )

    def score_supplier(self, supplier_id: int, *, trigger_source: str = "manual",
                       trigger_event_id: int | None = None, actor: str = "system",
                       as_of: datetime | None = None, force: bool = False,
                       conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        """计算（必要时新建）一个确定的评分版本；相同输入幂等返回最新版本。"""
        own = conn is None
        conn = conn or self.connection
        if own:
            conn.execute("BEGIN IMMEDIATE")
        try:
            supplier = self._require_supplier(conn, supplier_id)
            root_id = self._root_id(conn, supplier_id)
            as_of = as_of or utc_now()
            now = _now_iso()
            rule = self.get_active_rule(conn)
            config = rule["config"]
            supplier_ids = self._merge_chain_ids(conn, root_id)
            self._materialize_overdue_events(conn, supplier_ids, as_of, actor)
            events = self._load_scoring_events(conn, supplier_ids, as_of, int(config["decay"]["window_days"]))
            fingerprint = input_fingerprint(rule["version"], supplier_ids, events)
            latest = conn.execute(
                "SELECT * FROM risk_scores WHERE supplier_id=? ORDER BY version DESC LIMIT 1", (root_id,)
            ).fetchone()
            if latest is not None and latest["input_fingerprint"] == fingerprint and not force:
                result = self._version_payload(conn, latest)
                if own:
                    conn.commit()
                return result
            outcome = evaluate(events, config, as_of)
            next_version = (latest["version"] + 1) if latest else 1
            explanation = json.dumps(outcome, ensure_ascii=False)
            needs_review = outcome["level"] in REVIEW_REQUIRED_LEVELS
            cursor = conn.execute(
                """INSERT INTO risk_scores(supplier_id,version,rule_version,trigger_event_id,trigger_source,
                       input_fingerprint,score,computed_level,level,level_label,sampling_ratio,explanation_json,
                       status,is_current,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (root_id, next_version, rule["version"], trigger_event_id, trigger_source, fingerprint,
                 outcome["score"], outcome["level"], outcome["level"], outcome["level_label"],
                 outcome["sampling_ratio"], explanation,
                 "draft", 0, now),
            )
            score_id = cursor.lastrowid
            self._supersede_drafts(conn, root_id, score_id, now)
            for factor in outcome["factors"]:
                for item in factor["events"]:
                    conn.execute(
                        "INSERT OR IGNORE INTO risk_score_events(score_id,event_id,event_type,contribution) VALUES(?,?,?,?)",
                        (score_id, item["event_id"], factor["factor"], item["contribution"]),
                    )
            conn.execute("INSERT INTO risk_audit(supplier_id,action,actor,payload_json,created_at) VALUES(?,?,?,?,?)",
                         (root_id, "score.draft", actor, json.dumps({"score_id": score_id, "version": next_version, "level": outcome["level"], "score": outcome["score"], "trigger": trigger_source}, ensure_ascii=False), now))
            if not needs_review:
                self._publish(conn, score_id, actor, now, reason="低/中风险评分自动发布")
            row = conn.execute("SELECT * FROM risk_scores WHERE id=?", (score_id,)).fetchone()
            result = self._version_payload(conn, row)
            if own:
                conn.commit()
            return result
        except Exception:
            if own:
                conn.rollback()
            raise

    def _supersede_drafts(self, conn: sqlite3.Connection, supplier_id: int, new_score_id: int, now: str) -> None:
        """新版本产生后，同一供应商此前的草稿自动失效，保证待复核版本唯一。"""
        rows = conn.execute(
            "SELECT id FROM risk_scores WHERE supplier_id=? AND status='draft' AND id<>?",
            (supplier_id, new_score_id),
        ).fetchall()
        for row in rows:
            conn.execute("UPDATE risk_scores SET status='superseded' WHERE id=?", (row["id"],))
            conn.execute("INSERT INTO risk_audit(supplier_id,action,actor,payload_json,created_at) VALUES(?,?,?,?,?)",
                         (supplier_id, "score.supersede", "system", json.dumps({"score_id": row["id"], "by_score_id": new_score_id}, ensure_ascii=False), now))

    def _publish(self, conn: sqlite3.Connection, score_id: int, reviewer: str, now: str, *,
                 reason: str, override_level: str | None = None, override_ratio: float | None = None,
                 decision: str = "publish") -> None:
        score = conn.execute("SELECT * FROM risk_scores WHERE id=?", (score_id,)).fetchone()
        if score is None:
            raise NotFoundError("评分版本不存在")
        if score["status"] != "draft":
            raise ConflictError("仅草稿状态的评分可发布，该版本可能已被新版本取代")
        level = override_level or score["computed_level"]
        if level not in RISK_LEVELS:
            raise ValidationError("风险等级不合法")
        ratio = override_ratio if override_ratio is not None else score["sampling_ratio"]
        if not 0 < ratio <= 1:
            raise ValidationError("抽检比例必须在 (0,1] 内")
        label = LEVEL_LABELS[level]
        conn.execute("UPDATE risk_scores SET is_current=0 WHERE supplier_id=? AND is_current=1", (score["supplier_id"],))
        conn.execute(
            "UPDATE risk_scores SET status='published',is_current=1,published_at=?,level=?,level_label=?,sampling_ratio=?,overridden=? WHERE id=?",
            (now, level, label, ratio, int(override_level is not None), score_id),
        )
        conn.execute("INSERT INTO risk_reviews(score_id,decision,reason,reviewer,override_level,override_sampling_ratio,created_at) VALUES(?,?,?,?,?,?,?)",
                     (score_id, decision, reason, reviewer, override_level, override_ratio, now))
        conn.execute("INSERT INTO risk_audit(supplier_id,action,actor,payload_json,created_at) VALUES(?,?,?,?,?)",
                     (score["supplier_id"], "score.publish", reviewer, json.dumps({"score_id": score_id, "level": level, "sampling_ratio": ratio, "reason": reason}, ensure_ascii=False), now))

    def review_score(self, score_id: int, *, decision: str, reason: str, reviewer: str,
                     override_level: str | None = None, override_sampling_ratio: float | None = None) -> dict[str, Any]:
        if decision not in {"publish", "publish_override", "reject"}:
            raise ValidationError("复核决定不合法")
        if decision == "publish_override" and not override_level:
            raise ValidationError("调整发布必须给出 override_level")
        if decision == "reject" and not reason:
            raise ValidationError("驳回必须填写原因")
        now = _now_iso()
        with transaction(immediate=True) as conn:
            score = conn.execute("SELECT * FROM risk_scores WHERE id=?", (score_id,)).fetchone()
            if score is None:
                raise NotFoundError("评分版本不存在")
            if score["status"] != "draft":
                raise ConflictError("仅草稿状态的评分可复核")
            if decision == "reject":
                conn.execute("UPDATE risk_scores SET status='rejected' WHERE id=?", (score_id,))
                conn.execute("INSERT INTO risk_reviews(score_id,decision,reason,reviewer,created_at) VALUES(?,?,?,?,?)",
                             (score_id, decision, reason, reviewer, now))
                conn.execute("INSERT INTO risk_audit(supplier_id,action,actor,payload_json,created_at) VALUES(?,?,?,?,?)",
                             (score["supplier_id"], "score.reject", reviewer, json.dumps({"score_id": score_id, "reason": reason}, ensure_ascii=False), now))
            else:
                if decision == "publish":
                    self._publish(conn, score_id, reviewer, now, reason=reason)
                else:
                    self._publish(conn, score_id, reviewer, now, reason=reason,
                                  override_level=override_level, override_ratio=override_sampling_ratio,
                                  decision="publish_override")
            row = conn.execute("SELECT * FROM risk_scores WHERE id=?", (score_id,)).fetchone()
            return self._version_payload(conn, row)

    def pending_reviews(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT sc.*, s.name AS supplier_name FROM risk_scores sc JOIN risk_suppliers s ON s.id=sc.supplier_id
               WHERE sc.status='draft' ORDER BY sc.score DESC, sc.id"""
        ).fetchall()
        return [self._version_payload(self.connection, row) for row in rows]

    def list_versions(self, supplier_id: int) -> list[dict[str, Any]]:
        self._require_supplier(self.connection, supplier_id)
        root_id = self._root_id(self.connection, supplier_id)
        rows = self.connection.execute("SELECT * FROM risk_scores WHERE supplier_id=? ORDER BY version DESC", (root_id,)).fetchall()
        return [self._version_payload(self.connection, row, include_explanation=False) for row in rows]

    def get_version(self, supplier_id: int, version: int) -> dict[str, Any]:
        self._require_supplier(self.connection, supplier_id)
        root_id = self._root_id(self.connection, supplier_id)
        row = self.connection.execute("SELECT * FROM risk_scores WHERE supplier_id=? AND version=?", (root_id, version)).fetchone()
        if row is None:
            raise NotFoundError("评分版本不存在")
        return self._version_payload(self.connection, row)

    def _version_payload(self, conn: sqlite3.Connection, row: sqlite3.Row, *, include_explanation: bool = True) -> dict[str, Any]:
        result = {k: row[k] for k in row.keys()}
        result["explanation"] = json.loads(row["explanation_json"]) if include_explanation else None
        supplier = conn.execute("SELECT name FROM risk_suppliers WHERE id=?", (row["supplier_id"],)).fetchone()
        result["supplier_name"] = supplier["name"] if supplier else None
        result["reviews"] = [dict(item) for item in conn.execute("SELECT * FROM risk_reviews WHERE score_id=? ORDER BY id", (row["id"],)).fetchall()]
        result["input_events"] = [
            {
                "event_id": item["event_id"],
                "event_type": item["event_type"],
                "contribution": item["contribution"],
            }
            for item in conn.execute("SELECT event_id,event_type,contribution FROM risk_score_events WHERE score_id=? ORDER BY event_id", (row["id"],)).fetchall()
        ]
        return result

    # ---- 当前画像与抽检建议 ---------------------------------------------

    def current_sampling_ratio(self, supplier_id: int, conn: sqlite3.Connection | None = None) -> float:
        conn = conn or self.connection
        row = conn.execute("SELECT sampling_ratio FROM risk_scores WHERE supplier_id=? AND is_current=1", (supplier_id,)).fetchone()
        return float(row["sampling_ratio"]) if row else BASELINE_RATIO

    def current_profile(self, supplier_id: int) -> dict[str, Any]:
        self._require_supplier(self.connection, supplier_id)
        root_id = self._root_id(self.connection, supplier_id)
        supplier = self.get_supplier(root_id)
        row = self.connection.execute("SELECT * FROM risk_scores WHERE supplier_id=? AND is_current=1", (root_id,)).fetchone()
        advice: dict[str, Any]
        if row is None:
            advice = {
                "baseline": True,
                "level": "unknown",
                "level_label": "未评级",
                "sampling_ratio": BASELINE_RATIO,
                "advice": f"尚无已发布评分版本，按基线抽检比例 {BASELINE_RATIO:.0%} 执行",
                "factors": [],
            }
        else:
            version = self._version_payload(self.connection, row)
            factors = []
            for factor in version["explanation"]["factors"]:
                factors.append({
                    "factor": factor["factor"],
                    "label": factor["label"],
                    "weighted_score": factor["weighted_score"],
                    "event_count": factor["event_count"],
                    "rationale": factor["rationale"],
                    "sampling_effect": self._factor_effect(version["explanation"]["rule_version"], factor),
                    "events": factor["events"],
                })
            advice = {
                "baseline": False,
                "score_version": version["version"],
                "rule_version": version["rule_version"],
                "score": version["score"],
                "level": version["level"],
                "level_label": version["level_label"],
                "sampling_ratio": version["sampling_ratio"],
                "published_at": version["published_at"],
                "overridden": bool(version["overridden"]),
                "advice": self._sampling_advice_text(version),
                "factors": factors,
                "neutral_events": version["explanation"].get("neutral_events", {}),
            }
        pending = self.connection.execute(
            "SELECT version,score,computed_level,created_at FROM risk_scores WHERE supplier_id=? AND status='draft' ORDER BY version DESC",
            (root_id,),
        ).fetchall()
        advice["pending_versions"] = [dict(item) for item in pending]
        advice["supplier"] = {k: supplier[k] for k in ("id", "name", "status", "disabled_reason")}
        return advice

    @staticmethod
    def _sampling_advice_text(version: dict[str, Any]) -> str:
        ratio_pct = f"{version['sampling_ratio']:.0%}"
        if version["overridden"]:
            return f"复核后人工调整为{version['level_label']}，后续批次抽检比例 {ratio_pct}（评分 {version['score']}）"
        return f"{version['level_label']}（评分 {version['score']}），后续批次按 {ratio_pct} 比例抽检"

    @staticmethod
    def _factor_effect(rule_version: str, factor: dict[str, Any]) -> str:
        del rule_version
        if factor["weighted_score"] <= 0:
            return "不抬升抽检比例"
        return f"贡献 {factor['weighted_score']} 分，是提高抽检比例的依据之一"


def ensure_schema() -> None:
    RiskService.ensure_schema()
