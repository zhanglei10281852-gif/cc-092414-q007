from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.clock import from_storage, to_storage
from app.database import get_connection, transaction
from app.risk.service import RiskService


SCHEMA = """
CREATE TABLE IF NOT EXISTS food_lots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_code TEXT NOT NULL UNIQUE,
    product_name TEXT NOT NULL,
    category TEXT NOT NULL,
    supplier TEXT NOT NULL,
    origin TEXT NOT NULL,
    harvest_date TEXT NOT NULL,
    quantity_kg REAL NOT NULL,
    trace_code TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','testing','released','held','recalled','destroyed')),
    risk_level TEXT NOT NULL DEFAULT 'unknown' CHECK(risk_level IN ('unknown','low','medium','high','critical')),
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS food_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id INTEGER NOT NULL REFERENCES food_lots(id) ON DELETE RESTRICT,
    sample_code TEXT NOT NULL UNIQUE,
    collected_at TEXT NOT NULL,
    collector TEXT NOT NULL,
    location TEXT NOT NULL,
    sample_weight_g REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'collected' CHECK(status IN ('collected','in_lab','complete','void')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS food_test_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id INTEGER NOT NULL REFERENCES food_samples(id) ON DELETE RESTRICT,
    analyte TEXT NOT NULL,
    method TEXT NOT NULL,
    value_mg_kg REAL NOT NULL,
    limit_mg_kg REAL NOT NULL,
    unit TEXT NOT NULL,
    lab_operator TEXT NOT NULL,
    tested_at TEXT NOT NULL,
    certificate_no TEXT NOT NULL DEFAULT '',
    verdict TEXT NOT NULL CHECK(verdict IN ('pass','fail')),
    result_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(sample_id, analyte, method, tested_at)
);
CREATE TABLE IF NOT EXISTS food_shipments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id INTEGER NOT NULL REFERENCES food_lots(id) ON DELETE RESTRICT,
    shipment_code TEXT NOT NULL UNIQUE,
    carrier TEXT NOT NULL,
    vehicle_no TEXT NOT NULL,
    departure_at TEXT NOT NULL,
    arrival_due_at TEXT NOT NULL,
    destination TEXT NOT NULL,
    target_temp_min REAL NOT NULL,
    target_temp_max REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'planned' CHECK(status IN ('planned','in_transit','arrived','delayed','cancelled')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS food_temperatures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shipment_id INTEGER NOT NULL REFERENCES food_shipments(id) ON DELETE CASCADE,
    recorded_at TEXT NOT NULL,
    temperature_c REAL NOT NULL,
    source TEXT NOT NULL,
    in_range INTEGER NOT NULL CHECK(in_range IN (0,1)),
    created_at TEXT NOT NULL,
    UNIQUE(shipment_id, recorded_at)
);
CREATE TABLE IF NOT EXISTS food_risk_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id INTEGER NOT NULL REFERENCES food_lots(id) ON DELETE RESTRICT,
    decision TEXT NOT NULL CHECK(decision IN ('release','hold','recall','destroy')),
    reason TEXT NOT NULL,
    operator TEXT NOT NULL,
    previous_status TEXT NOT NULL,
    new_status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS food_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id INTEGER,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_food_samples_lot ON food_samples(lot_id, collected_at);
CREATE INDEX IF NOT EXISTS idx_food_results_sample ON food_test_results(sample_id, tested_at);
CREATE INDEX IF NOT EXISTS idx_food_shipments_lot ON food_shipments(lot_id, departure_at);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_schema() -> None:
    get_connection().executescript(SCHEMA)
    # 食品流程会写入风险事件，确保风险画像表结构同步就绪
    from app.risk.service import ensure_schema as ensure_risk_schema
    ensure_risk_schema()


def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


def _result_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _lot_code(connection: sqlite3.Connection, lot_id: int) -> str:
    row = connection.execute("SELECT lot_code FROM food_lots WHERE id=?", (lot_id,)).fetchone()
    return row["lot_code"] if row else ""


class FoodService:
    """食品批次、检测与运输流程的事务边界。"""

    def __init__(self, connection: sqlite3.Connection | None = None):
        self.connection = connection or get_connection()
        ensure_schema()

    def create_lot(self, payload: dict[str, Any], actor: str = "system") -> dict[str, Any]:
        now = _now()
        with transaction(immediate=True) as connection:
            risk = RiskService(connection, ensure=False)
            supplier = risk.register_supplier(payload["supplier"], actor=actor, conn=connection)
            ratio = risk.current_sampling_ratio(supplier["id"], conn=connection)
            cursor = connection.execute(
                "INSERT INTO food_lots(lot_code,product_name,category,supplier,origin,harvest_date,quantity_kg,trace_code,supplier_id,recommended_sampling_ratio,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (payload["lot_code"], payload["product_name"], payload["category"], payload["supplier"], payload["origin"], payload["harvest_date"], payload["quantity_kg"], payload["trace_code"], supplier["id"], ratio, now, now),
            )
            lot_id = cursor.lastrowid
            connection.execute("INSERT INTO food_audit(lot_id,action,actor,payload_json,created_at) VALUES(?,?,?,?,?)", (lot_id, "lot.create", actor, json.dumps(payload, ensure_ascii=False), now))
            return _dict(connection.execute("SELECT * FROM food_lots WHERE id=?", (lot_id,)).fetchone()) or {}

    def get_lot(self, lot_id: int, details: bool = True) -> dict[str, Any] | None:
        lot = self.connection.execute("SELECT * FROM food_lots WHERE id=?", (lot_id,)).fetchone()
        if lot is None:
            return None
        result = dict(lot)
        if details:
            samples = self.connection.execute("SELECT * FROM food_samples WHERE lot_id=? ORDER BY collected_at,id", (lot_id,)).fetchall()
            shipments = self.connection.execute("SELECT * FROM food_shipments WHERE lot_id=? ORDER BY departure_at,id", (lot_id,)).fetchall()
            result["samples"] = []
            for sample in samples:
                item = dict(sample)
                item["results"] = [dict(row) for row in self.connection.execute("SELECT * FROM food_test_results WHERE sample_id=? ORDER BY tested_at,id", (sample["id"],)).fetchall()]
                result["samples"].append(item)
            result["shipments"] = [dict(row) for row in shipments]
        return result

    def add_sample(self, lot_id: int, payload: dict[str, Any], actor: str = "inspector") -> dict[str, Any]:
        if self.connection.execute("SELECT id FROM food_lots WHERE id=?", (lot_id,)).fetchone() is None:
            raise KeyError("lot_not_found")
        now = _now()
        with transaction(immediate=True) as connection:
            cursor = connection.execute("INSERT INTO food_samples(lot_id,sample_code,collected_at,collector,location,sample_weight_g,status,created_at) VALUES(?,?,?,?,?,?,?,?)", (lot_id, payload["sample_code"], payload["collected_at"], payload["collector"], payload["location"], payload["sample_weight_g"], "collected", now))
            connection.execute("UPDATE food_lots SET status='testing',version=version+1,updated_at=? WHERE id=? AND status='pending'", (now, lot_id))
            connection.execute("INSERT INTO food_audit(lot_id,action,actor,payload_json,created_at) VALUES(?,?,?,?,?)", (lot_id, "sample.collect", actor, json.dumps(payload, ensure_ascii=False), now))
            return _dict(connection.execute("SELECT * FROM food_samples WHERE id=?", (cursor.lastrowid,)).fetchone()) or {}

    def add_result(self, sample_id: int, payload: dict[str, Any], actor: str = "lab") -> dict[str, Any]:
        sample = self.connection.execute("SELECT * FROM food_samples WHERE id=?", (sample_id,)).fetchone()
        if sample is None:
            raise KeyError("sample_not_found")
        verdict = "pass" if payload["value_mg_kg"] <= payload["limit_mg_kg"] else "fail"
        result_hash = _result_hash({**payload, "verdict": verdict, "sample_id": sample_id})
        now = _now()
        with transaction(immediate=True) as connection:
            existing = connection.execute("SELECT * FROM food_test_results WHERE sample_id=? AND analyte=? AND method=? AND tested_at=?", (sample_id, payload["analyte"], payload["method"], payload["tested_at"])).fetchone()
            if existing:
                return dict(existing)
            cursor = connection.execute("INSERT INTO food_test_results(sample_id,analyte,method,value_mg_kg,limit_mg_kg,unit,lab_operator,tested_at,certificate_no,verdict,result_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (sample_id, payload["analyte"], payload["method"], payload["value_mg_kg"], payload["limit_mg_kg"], payload["unit"], payload["lab_operator"], payload["tested_at"], payload["certificate_no"], verdict, result_hash, now))
            result_id = cursor.lastrowid
            connection.execute("UPDATE food_samples SET status='complete' WHERE id=?", (sample_id,))
            lot_id = sample["lot_id"]
            failed = connection.execute("SELECT COUNT(*) FROM food_test_results WHERE sample_id=? AND verdict='fail'", (sample_id,)).fetchone()[0]
            if failed:
                connection.execute("UPDATE food_lots SET risk_level='high',status='held',version=version+1,updated_at=? WHERE id=?", (now, lot_id))
            lot = connection.execute("SELECT supplier_id FROM food_lots WHERE id=?", (lot_id,)).fetchone()
            supplier_id = lot["supplier_id"]
            risk = RiskService(connection, ensure=False)
            if supplier_id is not None:
                risk_payload = {
                    "analyte": payload["analyte"],
                    "method": payload["method"],
                    "value_mg_kg": payload["value_mg_kg"],
                    "limit_mg_kg": payload["limit_mg_kg"],
                    "unit": payload["unit"],
                    "sample_code": sample["sample_code"],
                    "lot_code": _lot_code(connection, lot_id),
                }
                if verdict == "fail":
                    magnitude = max(payload["value_mg_kg"] / payload["limit_mg_kg"] - 1.0, 0.0) if payload["limit_mg_kg"] else 0.0
                    event = risk.record_event(
                        event_type="test_fail", supplier_id=supplier_id,
                        occurred_at=payload["tested_at"], payload=risk_payload,
                        magnitude=round(magnitude, 4), lot_id=lot_id,
                        ref_table="food_test_results", ref_id=result_id,
                        actor=payload["lab_operator"], conn=connection,
                    )
                    rule = risk.get_active_rule(connection)
                    due_at = to_storage(from_storage(payload["tested_at"]) + timedelta(days=int(rule["config"]["rectification_window_days"])))
                    risk.open_rectification(
                        supplier_id, f"{payload['analyte']} 超标整改（{risk_payload['lot_code']}）", due_at,
                        lot_id=lot_id, reason_event_id=event["id"], operator=payload["lab_operator"], conn=connection,
                    )
                else:
                    event = risk.record_event(
                        event_type="test_pass", supplier_id=supplier_id,
                        occurred_at=payload["tested_at"], payload=risk_payload,
                        lot_id=lot_id, ref_table="food_test_results", ref_id=result_id,
                        actor=payload["lab_operator"], conn=connection,
                    )
                score = risk.score_supplier(
                    supplier_id, trigger_source="test_result",
                    trigger_event_id=event["id"], actor=payload["lab_operator"], conn=connection,
                )
                ratio = risk.current_sampling_ratio(supplier_id, conn=connection)
                connection.execute("UPDATE food_lots SET recommended_sampling_ratio=? WHERE id=?", (ratio, lot_id))
                result = _dict(connection.execute("SELECT * FROM food_test_results WHERE id=?", (result_id,)).fetchone()) or {}
                result["risk_score"] = {"version": score["version"], "status": score["status"], "level": score["level"], "sampling_ratio": ratio}
            else:
                result = _dict(connection.execute("SELECT * FROM food_test_results WHERE id=?", (result_id,)).fetchone()) or {}
            connection.execute("INSERT INTO food_audit(lot_id,action,actor,payload_json,created_at) VALUES(?,?,?,?,?)", (lot_id, "test.result", actor, json.dumps({**payload, "verdict": verdict}, ensure_ascii=False), now))
            return result

    def create_shipment(self, lot_id: int, payload: dict[str, Any], actor: str = "dispatcher") -> dict[str, Any]:
        lot = self.connection.execute("SELECT * FROM food_lots WHERE id=?", (lot_id,)).fetchone()
        if lot is None:
            raise KeyError("lot_not_found")
        if lot["status"] in {"held", "recalled", "destroyed"}:
            raise ValueError("lot_not_releasable")
        if payload["target_temp_min"] > payload["target_temp_max"]:
            raise ValueError("temperature_range_invalid")
        now = _now()
        with transaction(immediate=True) as connection:
            cursor = connection.execute("INSERT INTO food_shipments(lot_id,shipment_code,carrier,vehicle_no,departure_at,arrival_due_at,destination,target_temp_min,target_temp_max,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (lot_id, payload["shipment_code"], payload["carrier"], payload["vehicle_no"], payload["departure_at"], payload["arrival_due_at"], payload["destination"], payload["target_temp_min"], payload["target_temp_max"], now, now))
            connection.execute("INSERT INTO food_audit(lot_id,action,actor,payload_json,created_at) VALUES(?,?,?,?,?)", (lot_id, "shipment.plan", actor, json.dumps(payload, ensure_ascii=False), now))
            return _dict(connection.execute("SELECT * FROM food_shipments WHERE id=?", (cursor.lastrowid,)).fetchone()) or {}

    def add_temperature(self, shipment_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        shipment = self.connection.execute("SELECT * FROM food_shipments WHERE id=?", (shipment_id,)).fetchone()
        if shipment is None:
            raise KeyError("shipment_not_found")
        in_range = int(shipment["target_temp_min"] <= payload["temperature_c"] <= shipment["target_temp_max"])
        now = _now()
        with transaction(immediate=True) as connection:
            existing = connection.execute("SELECT * FROM food_temperatures WHERE shipment_id=? AND recorded_at=?", (shipment_id, payload["recorded_at"])).fetchone()
            if existing:
                return dict(existing)
            cursor = connection.execute("INSERT INTO food_temperatures(shipment_id,recorded_at,temperature_c,source,in_range,created_at) VALUES(?,?,?,?,?,?)", (shipment_id, payload["recorded_at"], payload["temperature_c"], payload["source"], in_range, now))
            temp_id = cursor.lastrowid
            if not in_range:
                connection.execute("UPDATE food_shipments SET status='delayed',updated_at=? WHERE id=? AND status IN ('planned','in_transit')", (now, shipment_id))
                lot = connection.execute("SELECT l.id AS lot_id,l.supplier_id FROM food_shipments s JOIN food_lots l ON l.id=s.lot_id WHERE s.id=?", (shipment_id,)).fetchone()
                lot_id, supplier_id = lot["lot_id"], lot["supplier_id"]
                if supplier_id is not None:
                    risk = RiskService(connection, ensure=False)
                    magnitude = max(shipment["target_temp_min"] - payload["temperature_c"], payload["temperature_c"] - shipment["target_temp_max"], 0.0)
                    event = risk.record_event(
                        event_type="temperature_anomaly", supplier_id=supplier_id,
                        occurred_at=payload["recorded_at"],
                        payload={
                            "shipment_code": shipment["shipment_code"],
                            "recorded_at": payload["recorded_at"],
                            "temperature_c": payload["temperature_c"],
                            "target_temp_min": shipment["target_temp_min"],
                            "target_temp_max": shipment["target_temp_max"],
                            "lot_code": _lot_code(connection, lot_id),
                        },
                        magnitude=round(magnitude, 2), lot_id=lot_id,
                        ref_table="food_temperatures", ref_id=temp_id,
                        actor=payload["source"], conn=connection,
                    )
                    risk.score_supplier(
                        supplier_id, trigger_source="temperature",
                        trigger_event_id=event["id"], actor=payload["source"], conn=connection,
                    )
                    ratio = risk.current_sampling_ratio(supplier_id, conn=connection)
                    connection.execute("UPDATE food_lots SET recommended_sampling_ratio=? WHERE id=?", (ratio, lot_id))
            return _dict(connection.execute("SELECT * FROM food_temperatures WHERE id=?", (temp_id,)).fetchone()) or {}

    def decide_risk(self, lot_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            lot = connection.execute("SELECT * FROM food_lots WHERE id=?", (lot_id,)).fetchone()
            if lot is None:
                raise KeyError("lot_not_found")
            mapping = {"release": "released", "hold": "held", "recall": "recalled", "destroy": "destroyed"}
            new_status = mapping[payload["decision"]]
            now = _now()
            connection.execute("UPDATE food_lots SET status=?,version=version+1,updated_at=? WHERE id=?", (new_status, now, lot_id))
            connection.execute("INSERT INTO food_risk_actions(lot_id,decision,reason,operator,previous_status,new_status,created_at) VALUES(?,?,?,?,?,?,?)", (lot_id, payload["decision"], payload["reason"], payload["operator"], lot["status"], new_status, now))
            connection.execute("INSERT INTO food_audit(lot_id,action,actor,payload_json,created_at) VALUES(?,?,?,?,?)", (lot_id, "risk." + payload["decision"], payload["operator"], json.dumps(payload, ensure_ascii=False), now))
            return _dict(connection.execute("SELECT * FROM food_lots WHERE id=?", (lot_id,)).fetchone()) or {}

    def summary(self, lot_id: int) -> dict[str, Any]:
        lot = self.get_lot(lot_id, details=False)
        if lot is None:
            raise KeyError("lot_not_found")
        sample_count = self.connection.execute("SELECT COUNT(*) FROM food_samples WHERE lot_id=?", (lot_id,)).fetchone()[0]
        result_count = self.connection.execute("SELECT COUNT(*) FROM food_test_results r JOIN food_samples s ON s.id=r.sample_id WHERE s.lot_id=?", (lot_id,)).fetchone()[0]
        failed_count = self.connection.execute("SELECT COUNT(*) FROM food_test_results r JOIN food_samples s ON s.id=r.sample_id WHERE s.lot_id=? AND r.verdict='fail'", (lot_id,)).fetchone()[0]
        temperature_count = self.connection.execute("SELECT COUNT(*) FROM food_temperatures t JOIN food_shipments s ON s.id=t.shipment_id WHERE s.lot_id=?", (lot_id,)).fetchone()[0]
        return {"lot": lot, "sample_count": sample_count, "result_count": result_count, "failed_count": failed_count, "temperature_count": temperature_count}

    def delete_lot(self, lot_id: int) -> bool:
        """移除尚未关联记录的批次；关联记录的错误映射由上层负责。"""
        with transaction(immediate=True) as connection:
            cursor = connection.execute("DELETE FROM food_lots WHERE id=?", (lot_id,))
            if cursor.rowcount == 0:
                raise KeyError("lot_not_found")
            return True
