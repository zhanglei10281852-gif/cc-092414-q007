from __future__ import annotations

from datetime import UTC, datetime

from app.risk.scoring import DEFAULT_RULE_CONFIG, compute_profile, decay_factor, level_for


def make_lot(client, code="LOT-R001", supplier="甲公司"):
    response = client.post(
        "/api/food/lots",
        json={
            "lot_code": code,
            "product_name": "菠菜",
            "category": "叶菜",
            "supplier": supplier,
            "origin": "山东寿光",
            "harvest_date": "2026-09-20",
            "quantity_kg": 500,
            "trace_code": code + "-TRACE",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def make_fail_result(client, lot_id, sample_code="S-R001", value=0.3, limit=0.05, tested_at="2026-09-21T18:00:00+00:00"):
    sample = client.post(
        f"/api/food/lots/{lot_id}/samples",
        json={"sample_code": sample_code, "collected_at": "2026-09-21T08:00:00+00:00", "collector": "监管员", "location": "批发市场", "sample_weight_g": 250},
    )
    assert sample.status_code == 201, sample.text
    result = client.post(
        f"/api/food/samples/{sample.json()['id']}/results",
        json={"analyte": "氯氰菊酯", "method": "GB/T 5009", "value_mg_kg": value, "limit_mg_kg": limit, "lab_operator": "实验员", "tested_at": tested_at},
    )
    assert result.status_code == 201, result.text
    return result.json()


def supplier_id_of(client, name):
    items = client.get("/api/risk/suppliers").json()["items"]
    matches = [item for item in items if item["name"] == name]
    assert matches, f"供应商未登记: {name}"
    return matches[0]["id"]


def test_exceedance_generates_single_deterministic_score(client):
    lot = make_lot(client)
    result = make_fail_result(client, lot["id"])
    supplier_id = supplier_id_of(client, "甲公司")

    scores = client.get(f"/api/risk/suppliers/{supplier_id}/scores").json()["items"]
    assert len(scores) == 1
    assert scores[0]["status"] == "draft"
    assert scores[0]["version_no"] == 1
    assert scores[0]["rule_version"] == "v1"
    # 超标 6 倍被截断为严重度 5，贡献 5×12=60 → high
    assert scores[0]["score"] == 60.0
    assert scores[0]["level"] == "high"

    # 同一检测结果重复提交：批次链路幂等返回，评分版本不增加
    again = client.post(
        f"/api/food/samples/{result['sample_id']}/results",
        json={"analyte": "氯氰菊酯", "method": "GB/T 5009", "value_mg_kg": 0.3, "limit_mg_kg": 0.05, "lab_operator": "实验员", "tested_at": "2026-09-21T18:00:00+00:00"},
    )
    assert again.status_code == 201
    assert again.json()["id"] == result["id"]
    scores_after = client.get(f"/api/risk/suppliers/{supplier_id}/scores").json()["items"]
    assert len(scores_after) == 1

    # 评分版本记录了输入事件与规则版本，可回放
    detail = client.get(f"/api/risk/scores/{scores[0]['id']}").json()
    assert detail["rule_version"] == "v1"
    assert len(detail["input_event_ids"]) == 1
    assert detail["trigger_event_id"] is not None

    # 人工重算同一时点：命中输入摘要，仍是同一确定版本
    as_of = detail["as_of"]
    first = client.post(f"/api/risk/suppliers/{supplier_id}/recalculate", json={"as_of": as_of, "operator": "监管员"})
    assert first.status_code == 200
    assert first.json()["created"] is False
    assert first.json()["score"]["id"] == detail["id"]


def test_factor_explanation_and_publish_raise_sampling(client):
    lot = make_lot(client, "LOT-R002")
    assert lot["sampling"]["sampling_ratio"] == 0.05  # 无画像时的默认比例
    make_fail_result(client, lot["id"], sample_code="S-R002")
    supplier_id = supplier_id_of(client, "甲公司")

    profile = client.get(f"/api/risk/suppliers/{supplier_id}/profile").json()
    assert profile["sampling"]["source"] == "default"  # 未发布前不生效
    factors = {f["type"]: f for f in profile["factor_explanations"]}
    assert factors == {}  # draft 未发布，解释挂在 pending 上
    pending = profile["pending"]
    assert pending["status"] == "draft"

    # 待复核版本的因子解释：每个因子如何影响抽检建议
    draft_detail = client.get(f"/api/risk/scores/{pending['id']}").json()
    factors = {f["type"]: f for f in draft_detail["profile"]["factors"]}
    exceedance = factors["exceedance"]
    assert exceedance["event_count"] == 1
    assert exceedance["contribution"] == 60.0
    assert exceedance["share"] == 1.0
    assert exceedance["events"][0]["detail"]["analyte"] == "氯氰菊酯"
    # 反事实：去掉超标因子后回到 low / 5%
    assert exceedance["without_factor"] == {"score": 0.0, "level": "low", "sampling_ratio": 0.05}

    # 未复核不能发布
    blocked = client.post(f"/api/risk/scores/{pending['id']}/publish", json={"operator": "监管科长"})
    assert blocked.status_code == 409

    # 复核确认 → 发布 → 后续批次抽检比例自动提高
    reviewed = client.post(f"/api/risk/scores/{pending['id']}/review", json={"reviewer": "监管员A", "action": "confirm", "comment": "因子与证据一致"})
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["status"] == "reviewed"
    published = client.post(f"/api/risk/scores/{pending['id']}/publish", json={"operator": "监管科长", "note": "周会确认"})
    assert published.status_code == 200, published.text
    assert published.json()["status"] == "published"
    assert published.json()["published_ratio"] == 0.35

    profile = client.get(f"/api/risk/suppliers/{supplier_id}/profile").json()
    assert profile["sampling"]["ratio"] == 0.35
    assert profile["sampling"]["level"] == "high"
    factors = {f["type"]: f for f in profile["factor_explanations"]}
    assert factors["exceedance"]["contribution"] == 60.0

    follow_up = make_lot(client, "LOT-R003")
    assert follow_up["sampling"]["sampling_ratio"] == 0.35
    assert follow_up["sampling"]["risk_score_id"] == pending["id"]
    plan = client.get(f"/api/risk/lots/{follow_up['id']}/sampling").json()
    assert plan["recorded"]["sampling_ratio"] == 0.35
    assert plan["ratio_changed"] is False


def test_review_adjust_level_changes_published_ratio(client):
    lot = make_lot(client, "LOT-R004")
    # 超标 1.2 倍 → 严重度 1.2，贡献 14.4 → low
    make_fail_result(client, lot["id"], sample_code="S-R004", value=0.06, limit=0.05)
    supplier_id = supplier_id_of(client, "甲公司")
    pending = client.get(f"/api/risk/suppliers/{supplier_id}/profile").json()["pending"]
    assert pending["level"] == "low"

    reviewed = client.post(
        f"/api/risk/scores/{pending['id']}/review",
        json={"reviewer": "监管员B", "action": "adjust", "adjusted_level": "critical", "comment": "该农药属禁用清单，直接提级"},
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["adjusted_level"] == "critical"
    assert reviewed.json()["level"] == "low"  # 计算分不可改，只覆盖等级
    published = client.post(f"/api/risk/scores/{pending['id']}/publish", json={"operator": "监管科长"})
    assert published.json()["published_ratio"] == 1.0
    assert published.json()["effective_level"] == "critical"

    follow_up = make_lot(client, "LOT-R005")
    assert follow_up["sampling"]["sampling_ratio"] == 1.0


def test_new_score_supersedes_previous_publish(client):
    lot = make_lot(client, "LOT-R006")
    make_fail_result(client, lot["id"], sample_code="S-R006", value=0.06, limit=0.05, tested_at="2026-09-21T18:00:00+00:00")
    supplier_id = supplier_id_of(client, "甲公司")
    first = client.get(f"/api/risk/suppliers/{supplier_id}/profile").json()["pending"]
    client.post(f"/api/risk/scores/{first['id']}/review", json={"reviewer": "A", "action": "confirm", "comment": "ok"})
    client.post(f"/api/risk/scores/{first['id']}/publish", json={"operator": "B"})

    # 第二次超标（6 倍）→ 新版本累积两次事件 → 发布后旧版本转为 superseded
    make_fail_result(client, lot["id"], sample_code="S-R007", value=0.3, limit=0.05, tested_at="2026-09-22T18:00:00+00:00")
    scores = client.get(f"/api/risk/suppliers/{supplier_id}/scores").json()["items"]
    assert len(scores) == 2
    second = [s for s in scores if s["status"] == "draft"][0]
    assert second["version_no"] == 2
    # 累积评分：14.4×衰减 + 60 ≈ 74 → critical
    assert second["score"] > 70
    assert second["level"] == "critical"
    client.post(f"/api/risk/scores/{second['id']}/review", json={"reviewer": "A", "action": "confirm", "comment": "ok"})
    client.post(f"/api/risk/scores/{second['id']}/publish", json={"operator": "B"})
    scores = client.get(f"/api/risk/suppliers/{supplier_id}/scores").json()["items"]
    by_id = {s["id"]: s for s in scores}
    assert by_id[first["id"]]["status"] == "superseded"
    assert by_id[second["id"]]["status"] == "published"
    profile = client.get(f"/api/risk/suppliers/{supplier_id}/profile").json()
    assert profile["sampling"]["ratio"] == 1.0


def test_temperature_anomaly_feeds_profile(client):
    lot = make_lot(client, "LOT-R008")
    shipment = client.post(
        f"/api/food/lots/{lot['id']}/shipments",
        json={"shipment_code": "SHIP-R008", "carrier": "冷链物流", "vehicle_no": "鲁A001", "departure_at": "2026-09-22T01:00:00+00:00", "arrival_due_at": "2026-09-22T10:00:00+00:00", "destination": "市民餐桌", "target_temp_min": 0, "target_temp_max": 8},
    ).json()
    temp = client.post(f"/api/food/shipments/{shipment['id']}/temperatures", json={"recorded_at": "2026-09-22T04:00:00+00:00", "temperature_c": 12, "source": "sensor-A"})
    assert temp.status_code == 201

    supplier_id = supplier_id_of(client, "甲公司")
    pending = client.get(f"/api/risk/suppliers/{supplier_id}/profile").json()["pending"]
    detail = client.get(f"/api/risk/scores/{pending['id']}").json()
    factors = {f["type"]: f for f in detail["profile"]["factors"]}
    anomaly = factors["temperature_anomaly"]
    assert anomaly["event_count"] == 1
    # 越限 4℃ → 严重度 1+0.2×4=1.8，贡献 1.8×6=10.8
    assert anomaly["contribution"] == 10.8
    assert anomaly["events"][0]["detail"]["deviation_c"] == 4.0


def test_rectification_overdue_sweep_is_idempotent(client):
    lot = make_lot(client, "LOT-R009")
    supplier_id = supplier_id_of(client, "甲公司")
    rect = client.post(
        f"/api/risk/suppliers/{supplier_id}/rectifications",
        json={"order_no": "ZG-001", "lot_id": lot["id"], "requirement": "更换冷链车厢并复检", "deadline_at": "2026-09-01T00:00:00+00:00", "issued_by": "监管员"},
    )
    assert rect.status_code == 201, rect.text

    sweep = client.post("/api/risk/rectifications/sweep", params={"as_of": "2026-09-24T00:00:00+00:00"})
    assert sweep.status_code == 200
    assert sweep.json()["marked_overdue"] == 1

    # 再次扫描不重复生成事件
    again = client.post("/api/risk/rectifications/sweep", params={"as_of": "2026-09-24T00:00:00+00:00"})
    assert again.json()["marked_overdue"] == 0
    events = client.get(f"/api/risk/suppliers/{supplier_id}/events").json()["items"]
    overdue_events = [e for e in events if e["event_type"] == "rectification_overdue"]
    assert len(overdue_events) == 1
    # 逾期 23 天 → 严重度 1+0.1×23=3.3，贡献 3.3×8=26.4 → medium
    assert overdue_events[0]["severity"] == 3.3
    pending = client.get(f"/api/risk/suppliers/{supplier_id}/profile").json()["pending"]
    detail = client.get(f"/api/risk/scores/{pending['id']}").json()
    factors = {f["type"]: f for f in detail["profile"]["factors"]}
    assert factors["rectification_overdue"]["contribution"] == 26.4
    assert detail["level"] == "medium"


def test_late_completion_also_generates_overdue_event(client):
    lot = make_lot(client, "LOT-R010")
    supplier_id = supplier_id_of(client, "甲公司")
    rect = client.post(
        f"/api/risk/suppliers/{supplier_id}/rectifications",
        json={"order_no": "ZG-002", "lot_id": lot["id"], "requirement": "补交检测报告", "deadline_at": "2026-09-10T00:00:00+00:00", "issued_by": "监管员"},
    ).json()
    done = client.post(
        f"/api/risk/rectifications/{rect['id']}/complete",
        json={"completed_at": "2026-09-20T00:00:00+00:00", "operator": "监管员", "note": "逾期 10 天完成"},
    )
    assert done.status_code == 200
    events = client.get(f"/api/risk/suppliers/{supplier_id}/events").json()["items"]
    assert [e["event_type"] for e in events] == ["rectification_overdue"]
    assert events[0]["occurred_at"] == "2026-09-10T00:00:00+00:00"  # 事件时点为截止时刻


def test_complaint_feeds_profile_and_dedupes(client):
    make_lot(client, "LOT-R011")
    supplier_id = supplier_id_of(client, "甲公司")
    payload = {"complaint_no": "TS-001", "category": "质量", "severity": "serious", "content": "蔬菜有异味", "occurred_at": "2026-09-15T10:00:00+00:00", "reporter": "市民热线"}
    filed = client.post(f"/api/risk/suppliers/{supplier_id}/complaints", json=payload)
    assert filed.status_code == 201, filed.text
    duplicate = client.post(f"/api/risk/suppliers/{supplier_id}/complaints", json=payload)
    assert duplicate.status_code == 409

    events = client.get(f"/api/risk/suppliers/{supplier_id}/events").json()["items"]
    assert len([e for e in events if e["event_type"] == "complaint"]) == 1
    pending = client.get(f"/api/risk/suppliers/{supplier_id}/profile").json()["pending"]
    detail = client.get(f"/api/risk/scores/{pending['id']}").json()
    factors = {f["type"]: f for f in detail["profile"]["factors"]}
    # 严重投诉严重度 2，贡献 2×4=8
    assert factors["complaint"]["contribution"] == 8.0


def test_merge_preserves_history_and_blocks_new_lots(client):
    lot = make_lot(client, "LOT-R012", supplier="甲公司")
    make_fail_result(client, lot["id"], sample_code="S-R012")
    source_id = supplier_id_of(client, "甲公司")

    target = client.post("/api/risk/suppliers", json={"supplier_code": "SUP-YI", "name": "乙公司"})
    assert target.status_code == 201, target.text
    target_id = target.json()["id"]

    merged = client.post(
        f"/api/risk/suppliers/{source_id}/merge",
        json={"target_supplier_id": target_id, "reason": "甲公司被乙公司收购", "operator": "监管科长"},
    )
    assert merged.status_code == 200, merged.text
    assert merged.json()["source"]["status"] == "merged"
    assert merged.json()["source"]["merged_into"]["id"] == target_id

    # 存续供应商继承了被合并方的历史事件，并生成新的待复核版本
    events = client.get(f"/api/risk/suppliers/{target_id}/events").json()["items"]
    assert len(events) == 1
    assert events[0]["supplier_id"] == source_id  # 事件仍挂在原供应商，历史不丢
    assert events[0]["from_merged_supplier"] is True
    profile = client.get(f"/api/risk/suppliers/{target_id}/profile").json()
    assert profile["event_counts"]["exceedance"] == 1
    assert profile["pending"] is not None

    # 被合并供应商的历史评分与事件仍可查询
    source_scores = client.get(f"/api/risk/suppliers/{source_id}/scores").json()["items"]
    assert len(source_scores) == 1
    source_events = client.get(f"/api/risk/suppliers/{source_id}/events").json()["items"]
    assert len(source_events) == 1

    # 合并后不能再用原供应商名义开新批次
    blocked = client.post(
        "/api/food/lots",
        json={"lot_code": "LOT-R013", "product_name": "菠菜", "category": "叶菜", "supplier": "甲公司", "origin": "山东寿光", "harvest_date": "2026-09-20", "quantity_kg": 500, "trace_code": "LOT-R013-TRACE"},
    )
    assert blocked.status_code == 409
    # 存续供应商可以正常开批次
    ok = make_lot(client, "LOT-R014", supplier="乙公司")
    assert ok["sampling"]["supplier_id"] == target_id


def test_suspend_blocks_new_lots_but_keeps_history(client):
    lot = make_lot(client, "LOT-R015")
    make_fail_result(client, lot["id"], sample_code="S-R015")
    supplier_id = supplier_id_of(client, "甲公司")

    suspended = client.post(f"/api/risk/suppliers/{supplier_id}/suspend", json={"reason": "许可证暂扣", "operator": "监管科长"})
    assert suspended.status_code == 200
    assert suspended.json()["status"] == "suspended"

    blocked = client.post(
        "/api/food/lots",
        json={"lot_code": "LOT-R016", "product_name": "菠菜", "category": "叶菜", "supplier": "甲公司", "origin": "山东寿光", "harvest_date": "2026-09-20", "quantity_kg": 500, "trace_code": "LOT-R016-TRACE"},
    )
    assert blocked.status_code == 409

    # 历史画像、事件、评分版本完整可查
    profile = client.get(f"/api/risk/suppliers/{supplier_id}/profile").json()
    assert profile["event_counts"]["exceedance"] == 1
    assert client.get(f"/api/risk/suppliers/{supplier_id}/scores").json()["items"]

    restored = client.post(f"/api/risk/suppliers/{supplier_id}/activate", json={"operator": "监管科长"})
    assert restored.status_code == 200
    assert restored.json()["status"] == "active"
    assert make_lot(client, "LOT-R017")["sampling"]["supplier_id"] == supplier_id


def test_scoring_engine_time_decay():
    # 半衰期 180 天：180 天前的事件贡献减半，360 天前为四分之一
    assert decay_factor(0, 180) == 1.0
    assert abs(decay_factor(180, 180) - 0.5) < 1e-9
    assert abs(decay_factor(360, 180) - 0.25) < 1e-9
    assert decay_factor(-5, 180) == 1.0  # 未来日期不产生大于 1 的权重

    as_of = datetime(2026, 9, 24, tzinfo=UTC)

    def event(event_id, occurred_at, severity=2.0, event_type="exceedance"):
        return {
            "id": event_id,
            "event_type": event_type,
            "severity": severity,
            "occurred_at": occurred_at,
            "supplier_id": 1,
            "supplier_name": "甲公司",
            "source_type": "food_test_result",
            "source_id": event_id,
            "detail": {},
        }

    recent = compute_profile([event(1, "2026-09-24T00:00:00+00:00")], DEFAULT_RULE_CONFIG, as_of)
    aged = compute_profile([event(1, "2026-03-28T00:00:00+00:00")], DEFAULT_RULE_CONFIG, as_of)  # 180 天前
    assert recent["score"] == 24.0  # 2×12
    assert aged["score"] == 12.0  # 衰减一半
    assert recent["level"] == "medium"
    assert aged["level"] == "low"
    # 相同输入重复计算结果完全一致（确定性）
    assert compute_profile([event(1, "2026-09-24T00:00:00+00:00")], DEFAULT_RULE_CONFIG, as_of) == recent
    assert level_for(69.9, DEFAULT_RULE_CONFIG["thresholds"]) == "high"
    assert level_for(70.0, DEFAULT_RULE_CONFIG["thresholds"]) == "critical"
