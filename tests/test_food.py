from __future__ import annotations


def lot(client, code="LOT-001"):
    response = client.post("/api/food/lots", json={"lot_code": code, "product_name": "菠菜", "category": "叶菜", "supplier": "安心农场", "origin": "山东寿光", "harvest_date": "2026-09-20", "quantity_kg": 500, "trace_code": code + "-TRACE"})
    assert response.status_code == 201, response.text
    return response.json()


def test_food_chain_and_risk_flow(client):
    created = lot(client)
    sample = client.post(f"/api/food/lots/{created['id']}/samples", json={"sample_code": "S-001", "collected_at": "2026-09-21T08:00:00+00:00", "collector": "监管员", "location": "批发市场", "sample_weight_g": 250})
    assert sample.status_code == 201
    result = client.post(f"/api/food/samples/{sample.json()['id']}/results", json={"analyte": "毒死蜱", "method": "GB/T 5009", "value_mg_kg": 0.02, "limit_mg_kg": 0.05, "lab_operator": "实验员", "tested_at": "2026-09-21T18:00:00+00:00"})
    assert result.status_code == 201
    shipment = client.post(f"/api/food/lots/{created['id']}/shipments", json={"shipment_code": "SHIP-001", "carrier": "冷链物流", "vehicle_no": "鲁A001", "departure_at": "2026-09-22T01:00:00+00:00", "arrival_due_at": "2026-09-22T10:00:00+00:00", "destination": "市民餐桌", "target_temp_min": 0, "target_temp_max": 8})
    assert shipment.status_code == 201
    temp = client.post(f"/api/food/shipments/{shipment.json()['id']}/temperatures", json={"recorded_at": "2026-09-22T04:00:00+00:00", "temperature_c": 12, "source": "sensor-A"})
    assert temp.status_code == 201 and temp.json()["in_range"] == 0
    decision = client.post(f"/api/food/lots/{created['id']}/risk", json={"decision": "release", "reason": "检测合格且已复核", "operator": "监管员"})
    assert decision.status_code == 200 and decision.json()["status"] == "released"


def test_failed_residue_holds_lot(client):
    created = lot(client, "LOT-002")
    sample = client.post(f"/api/food/lots/{created['id']}/samples", json={"sample_code": "S-002", "collected_at": "2026-09-21T08:00:00+00:00", "collector": "监管员", "location": "农贸市场", "sample_weight_g": 250}).json()
    result = client.post(f"/api/food/samples/{sample['id']}/results", json={"analyte": "氯氰菊酯", "method": "GB/T 5009", "value_mg_kg": 0.3, "limit_mg_kg": 0.05, "lab_operator": "实验员", "tested_at": "2026-09-21T18:00:00+00:00"})
    assert result.json()["verdict"] == "fail"
    detail = client.get(f"/api/food/lots/{created['id']}").json()
    assert detail["status"] == "held" and detail["risk_level"] == "high"

