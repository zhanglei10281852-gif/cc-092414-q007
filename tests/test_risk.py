from __future__ import annotations

from app.core.clock import from_storage
from app.risk.rules import RULE_VERSION_1, default_config, evaluate, input_fingerprint
from app.risk.service import RiskService


def _supplier(client, name="安心农场"):
    response = client.post("/api/risk/suppliers", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()


def _lot(client, code="LOT-R1", supplier="安心农场"):
    response = client.post("/api/food/lots", json={"lot_code": code, "product_name": "菠菜", "category": "叶菜", "supplier": supplier, "origin": "山东寿光", "harvest_date": "2026-09-20", "quantity_kg": 500, "trace_code": code + "-TRACE"})
    assert response.status_code == 201, response.text
    return response.json()


def _fail_result(client, lot, sample_code="S-R1", value=0.4, limit=0.05, tested_at="2026-09-21T18:00:00+00:00"):
    sample = client.post(f"/api/food/lots/{lot['id']}/samples", json={"sample_code": sample_code, "collected_at": "2026-09-21T08:00:00+00:00", "collector": "监管员", "location": "批发市场", "sample_weight_g": 250})
    assert sample.status_code == 201
    result = client.post(f"/api/food/samples/{sample.json()['id']}/results", json={"analyte": "氯氰菊酯", "method": "GB/T 5009", "value_mg_kg": value, "limit_mg_kg": limit, "lab_operator": "实验员", "tested_at": tested_at})
    assert result.status_code == 201, result.text
    return result.json()


# ---------- 纯函数引擎：解释性与时间衰减 ----------

def _event(event_id, event_type, occurred_at, *, supplier_id=1, severity=None, magnitude=None, payload=None):
    dt = from_storage(occurred_at)
    return {
        "id": event_id,
        "event_type": event_type,
        "occurred_at": dt,
        "occurred_at_iso": occurred_at,
        "supplier_id": supplier_id,
        "severity": severity,
        "magnitude": magnitude,
        "payload": payload or {},
        "lot_id": None,
    }


def test_engine_explains_each_factor_and_applies_decay():
    config = default_config()
    as_of = from_storage("2026-09-24T00:00:00+00:00")
    recent = _event(1, "test_fail", "2026-09-24T00:00:00+00:00", magnitude=1.0,
                    payload={"analyte": "毒死蜱", "value_mg_kg": 0.1, "limit_mg_kg": 0.05})
    old = _event(2, "test_fail", "2026-03-26T00:00:00+00:00", magnitude=1.0,
                 payload={"analyte": "毒死蜱", "value_mg_kg": 0.1, "limit_mg_kg": 0.05})
    outcome = evaluate([recent], config, as_of)
    assert outcome["rule_version"] == RULE_VERSION_1
    factor = outcome["factors"][0]
    assert factor["factor"] == "test_fail"
    assert factor["event_count"] == 1
    assert factor["events"][0]["decay_factor"] == 1.0
    assert factor["events"][0]["contribution"] == 24.0
    assert "超标 1.0 倍" in factor["events"][0]["description"]
    outcome_old = evaluate([old], config, as_of)
    old_item = outcome_old["factors"][0]["events"][0]
    # 约 182 天 ≈ 一个半衰期，贡献约为新事件的一半
    assert old_item["age_days"] > 175
    assert abs(old_item["contribution"] - 12.0) < 1.0
    # 四因子齐全，零事件因子也给出解释
    assert [f["factor"] for f in outcome["factors"]] == ["test_fail", "temperature_anomaly", "rectification_overdue", "complaint"]
    assert all("贡献 0 分" in f["rationale"] for f in outcome["factors"][1:])


def test_engine_is_pure_and_fingerprint_stable():
    config = default_config()
    as_of = from_storage("2026-09-24T00:00:00+00:00")
    events = [_event(1, "complaint", "2026-09-20T00:00:00+00:00", severity="high", payload={"content": "x"})]
    first = evaluate(events, config, as_of)
    second = evaluate(events, config, as_of)
    assert first["score"] == second["score"]
    fp1 = input_fingerprint(config["version"], [1], events)
    fp2 = input_fingerprint(config["version"], [1], events)
    assert fp1 == fp2
    assert input_fingerprint("2.0.0", [1], events) != fp1


# ---------- 检测结果触发：唯一、确定的评分版本 ----------

def test_new_result_creates_single_deterministic_version(client):
    supplier = _supplier(client)
    lot = _lot(client)
    # 轻微超标 0.06/0.05，24 分以下，低风险自动发布
    result = _fail_result(client, lot, value=0.06, limit=0.05)
    assert result["risk_score"]["status"] == "published"
    versions = client.get(f"/api/risk/suppliers/{supplier['id']}/scores").json()
    assert len(versions) == 1
    v1 = versions[0]
    assert v1["rule_version"] == RULE_VERSION_1
    # 评分版本记录了输入事件
    detail = client.get(f"/api/risk/suppliers/{supplier['id']}/scores/{v1['version']}").json()
    assert len(detail["input_events"]) == 1
    assert detail["input_events"][0]["event_type"] == "test_fail"
    # 合格结果不产生新风险事件，但应生成一个新版本（输入指纹变化），且只生成一个
    sample = client.post(f"/api/food/lots/{lot['id']}/samples", json={"sample_code": "S-R2", "collected_at": "2026-09-22T08:00:00+00:00", "collector": "监管员", "location": "市场", "sample_weight_g": 250}).json()
    for _ in range(2):
        resp = client.post(f"/api/food/samples/{sample['id']}/results", json={"analyte": "毒死蜱", "method": "GB/T 5009", "value_mg_kg": 0.01, "limit_mg_kg": 0.05, "lab_operator": "实验员", "tested_at": "2026-09-22T18:00:00+00:00"})
        assert resp.status_code == 201
    versions = client.get(f"/api/risk/suppliers/{supplier['id']}/scores").json()
    assert [v["version"] for v in versions] == [2, 1]


def test_duplicate_result_delivery_does_not_duplicate_version(client):
    supplier = _supplier(client)
    lot = _lot(client, "LOT-R2")
    sample = client.post(f"/api/food/lots/{lot['id']}/samples", json={"sample_code": "S-DUP", "collected_at": "2026-09-21T08:00:00+00:00", "collector": "监管员", "location": "市场", "sample_weight_g": 250}).json()
    body = {"analyte": "毒死蜱", "method": "GB/T 5009", "value_mg_kg": 0.06, "limit_mg_kg": 0.05, "lab_operator": "实验员", "tested_at": "2026-09-21T18:00:00+00:00"}
    first = client.post(f"/api/food/samples/{sample['id']}/results", json=body)
    second = client.post(f"/api/food/samples/{sample['id']}/results", json=body)
    assert first.json()["id"] == second.json()["id"]
    versions = client.get(f"/api/risk/suppliers/{supplier['id']}/scores").json()
    assert len(versions) == 1


# ---------- 人工复核与发布流程 ----------

def test_high_score_requires_manual_review_and_publish(client):
    supplier = _supplier(client)
    lot = _lot(client, "LOT-R3")
    _fail_result(client, lot, value=0.4, limit=0.05)  # 超标 7 倍 → 严重档 60 分 → 高风险
    pending = client.get("/api/risk/reviews/pending").json()
    assert len(pending) == 1 and pending[0]["computed_level"] == "high"
    score_id = pending[0]["id"]
    # 未发布前只有基线抽检比例
    profile = client.get(f"/api/risk/suppliers/{supplier['id']}/profile").json()
    assert profile["level"] == "unknown" and profile["sampling_ratio"] == 0.05
    reviewed = client.post(f"/api/risk/scores/{score_id}/review", json={"decision": "publish", "reason": "核实属实，提高抽检"})
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["status"] == "published" and reviewed.json()["is_current"] == 1
    profile = client.get(f"/api/risk/suppliers/{supplier['id']}/profile").json()
    assert profile["level"] == "high"
    assert profile["sampling_ratio"] == 0.35
    factor = next(f for f in profile["factors"] if f["factor"] == "test_fail")
    assert factor["weighted_score"] > 0 and "提高抽检比例" in factor["sampling_effect"]


def test_reviewer_can_override_level_and_ratio(client):
    supplier = _supplier(client)
    lot = _lot(client, "LOT-R4")
    _fail_result(client, lot, value=0.4, limit=0.05)
    score_id = client.get("/api/risk/reviews/pending").json()[0]["id"]
    resp = client.post(f"/api/risk/scores/{score_id}/review", json={"decision": "publish_override", "reason": "供应商已整改，降为中风险观察", "override_level": "medium", "override_sampling_ratio": 0.2})
    assert resp.status_code == 200
    body = resp.json()
    assert body["level"] == "medium" and body["sampling_ratio"] == 0.2 and body["overridden"] == 1
    profile = client.get(f"/api/risk/suppliers/{supplier['id']}/profile").json()
    assert "人工调整" in profile["advice"]


def test_reject_draft_keeps_previous_profile(client):
    supplier = _supplier(client)
    lot = _lot(client, "LOT-R5")
    _fail_result(client, lot, value=0.06, limit=0.05)  # 低风险自动发布 v1
    _fail_result(client, lot, "S-RX", value=0.4, limit=0.05, tested_at="2026-09-23T18:00:00+00:00")  # v2 待复核
    drafts = client.get("/api/risk/reviews/pending").json()
    score_id = drafts[0]["id"]
    resp = client.post(f"/api/risk/scores/{score_id}/review", json={"decision": "reject", "reason": "样品污染，结果无效"})
    assert resp.status_code == 200 and resp.json()["status"] == "rejected"
    profile = client.get(f"/api/risk/suppliers/{supplier['id']}/profile").json()
    assert profile["score_version"] == 1


# ---------- 供应商合并/停用不丢历史 ----------

def test_merge_preserves_history_and_disable_keeps_profile(client):
    a = _supplier(client, "老农场")
    b = _supplier(client, "新合作社")
    lot = _lot(client, "LOT-R6", supplier="老农场")
    _fail_result(client, lot, value=0.06, limit=0.05)
    # 合并 A → B
    merged = client.post(f"/api/risk/suppliers/{a['id']}/merge", json={"target_supplier_id": b["id"], "reason": "主体更名合并"})
    assert merged.status_code == 200, merged.text
    # A 的历史事件仍在，并可通过 A 与 B 两个入口查到
    events_a = client.get(f"/api/risk/suppliers/{a['id']}/events").json()
    events_b = client.get(f"/api/risk/suppliers/{b['id']}/events").json()
    assert any(e["event_type"] == "test_fail" for e in events_a)
    assert any(e["event_type"] == "test_fail" for e in events_b)
    profile_b = client.get(f"/api/risk/suppliers/{b['id']}/profile").json()
    assert profile_b["score"] > 0
    profile_a = client.get(f"/api/risk/suppliers/{a['id']}/profile").json()
    assert profile_a["score"] == profile_b["score"]
    # 停用 B 后画像与历史仍可查询
    disabled = client.post(f"/api/risk/suppliers/{b['id']}/disable", json={"reason": "歇业"}).json()
    assert disabled["status"] == "disabled"
    profile = client.get(f"/api/risk/suppliers/{b['id']}/profile").json()
    assert profile["supplier"]["status"] == "disabled" and profile["score"] > 0
    assert client.get(f"/api/risk/suppliers/{b['id']}/events").status_code == 200


def test_multi_level_merge_keeps_deep_history(client):
    a = _supplier(client, "甲农场")
    b = _supplier(client, "乙合作社")
    c = _supplier(client, "丙集团")
    lot = _lot(client, "LOT-RM", supplier="甲农场")
    _fail_result(client, lot, value=0.06, limit=0.05)
    assert client.post(f"/api/risk/suppliers/{a['id']}/merge", json={"target_supplier_id": b['id'], "reason": "一级合并"}).status_code == 200
    assert client.post(f"/api/risk/suppliers/{b['id']}/merge", json={"target_supplier_id": c['id'], "reason": "二级合并"}).status_code == 200
    profile_c = client.get(f"/api/risk/suppliers/{c['id']}/profile").json()
    profile_a = client.get(f"/api/risk/suppliers/{a['id']}/profile").json()
    assert profile_c["score"] > 0 and profile_c["score"] == profile_a["score"]
    events = client.get(f"/api/risk/suppliers/{c['id']}/events").json()
    assert any(e["event_type"] == "test_fail" for e in events)


# ---------- 整改逾期 ----------

def test_overdue_rectification_enters_score_and_resolve_voids_it(client):
    from app.core.clock import to_storage, utc_now
    from datetime import timedelta
    supplier = _supplier(client, "绿源农场")
    risk = RiskService(ensure=False)
    due = to_storage(utc_now() - timedelta(days=3))
    # 登记一条已逾期整改单
    rect = risk.open_rectification(supplier["id"], "农残超标整改", due)
    scored = risk.score_supplier(supplier["id"], trigger_source="manual", force=True)
    factor = next(f for f in scored["explanation"]["factors"] if f["factor"] == "rectification_overdue")
    assert factor["event_count"] == 1 and factor["weighted_score"] > 0
    # 闭环后逾期事件作废，重算不再计分
    risk.resolve_rectification(rect["id"], "已提交整改报告", "regulator")
    rescored = risk.score_supplier(supplier["id"], trigger_source="manual", force=True)
    factor = next(f for f in rescored["explanation"]["factors"] if f["factor"] == "rectification_overdue")
    assert factor["event_count"] == 0 and factor["weighted_score"] == 0
    # 历史事件行仍保留（void 状态）
    events = risk.list_events(supplier["id"])
    assert any(e["event_type"] == "rectification_overdue" for e in events)


# ---------- 投诉因子与规则版本发布 ----------

def test_complaint_score_and_new_rule_version(client):
    supplier = _supplier(client, "投诉农场")
    resp = client.post(f"/api/risk/suppliers/{supplier['id']}/complaints", json={"severity": "high", "content": "疑似使用禁用药"})
    assert resp.status_code == 201, resp.text
    profile = client.get(f"/api/risk/suppliers/{supplier['id']}/profile").json()
    factor = next(f for f in profile["factors"] if f["factor"] == "complaint")
    assert factor["event_count"] == 1 and factor["weighted_score"] == 24.0
    # 发布新版本规则后，重算必须记录新规则版本
    config = default_config()
    config["version"] = "1.1.0"
    config["factors"]["complaint"]["base_weight"] = 4
    published = client.post("/api/risk/rules", json={"config": config, "note": "下调投诉权重"})
    assert published.status_code == 201, published.text
    rescored = client.post(f"/api/risk/suppliers/{supplier['id']}/scores?force=true").json()
    assert rescored["rule_version"] == "1.1.0"
    versions = client.get("/api/risk/rules").json()
    assert {v["version"] for v in versions} == {RULE_VERSION_1, "1.1.0"}
    assert [v["is_active"] for v in versions if v["version"] == "1.1.0"] == [1]


# ---------- 抽检建议写回批次 ----------

def test_new_draft_supersedes_previous_draft(client):
    supplier = _supplier(client, "草稿农场")
    lot = _lot(client, "LOT-RS", supplier="草稿农场")
    _fail_result(client, lot, value=0.4, limit=0.05, tested_at="2026-09-21T18:00:00+00:00")
    first = client.get("/api/risk/reviews/pending").json()
    assert len(first) == 1
    # 又一条严重超标到达，产生 v2 草稿，v1 自动失效
    _fail_result(client, lot, "S-RS2", value=0.5, limit=0.05, tested_at="2026-09-22T18:00:00+00:00")
    pending = client.get("/api/risk/reviews/pending").json()
    assert len(pending) == 1 and pending[0]["version"] == 2
    versions = client.get(f"/api/risk/suppliers/{supplier['id']}/scores").json()
    v1 = next(v for v in versions if v["version"] == 1)
    assert v1["status"] == "superseded"
    # 已失效版本不能再发布
    resp = client.post(f"/api/risk/scores/{v1['id']}/review", json={"decision": "publish", "reason": "x"})
    assert resp.status_code == 409


def test_lot_carries_supplier_sampling_ratio(client):
    supplier = _supplier(client)
    lot = _lot(client, "LOT-R7")
    assert lot["recommended_sampling_ratio"] == 0.05
    _fail_result(client, lot, value=0.4, limit=0.05)
    score_id = client.get("/api/risk/reviews/pending").json()[0]["id"]
    client.post(f"/api/risk/scores/{score_id}/review", json={"decision": "publish", "reason": "确认"})
    # 新批次建单时即携带已发布的抽检比例
    new_lot = _lot(client, "LOT-R8")
    assert new_lot["recommended_sampling_ratio"] == 0.35
