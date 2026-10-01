"""告警对账台 API 集成测试：走真实 FastAPI 路由，验证端到端口径。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _desk_rows():
    return client.get("/api/reconciliation/desk").json()["rows"]


def test_health_and_seed():
    assert client.get("/api/health").json()["ok"] is True
    rows = _desk_rows()
    # 空设施号残档在种子阶段尚未归并；这里直接执行一次迁移
    merged = client.post("/api/reconciliation/merge-blank").json()["merged"]
    assert any(item["facility_type"] == "雨水口" for item in merged)
    # 归并后对账台不再有空号残档
    assert all(row["facility_code"] for row in _desk_rows())


def test_pump_offline_desk_not_passable():
    """端到端：泵站离线的 1# 泵站，三处口径都不能显示可通行。"""
    client.post("/api/reconciliation/merge-blank")
    rows = _desk_rows()
    p1 = next(r for r in rows if r["facility_code"] == "BPS-0001")
    assert p1["pump_online"] is False
    assert p1["surface_passable"] != "可通行"
    assert p1["notice_stage"] == p1["surface_stage"]
    flood = client.get("/api/reconciliation/flood").json()["items"]
    flood_p1 = next(f for f in flood if f["facility_code"] == "BPS-0001")
    assert flood_p1["passable"] != "可通行"
    roads = {r["road"]: r for r in client.get("/api/reconciliation/roads").json()["items"]}
    assert roads["G104-K2"]["passable"] != "可通行"
    assert roads["G104-K2"]["pump_offline"] is True


def test_disposition_writeback_end_to_end():
    """现场处置回写：3# 检查井处置消退后，三处台账同时变为低水位可通行。"""
    detail = client.get("/api/reconciliation/facilities").json()["items"]
    mh_id = next(f["id"] for f in detail if f["设施编号"] == "MH-0103")
    mh = client.get(f"/api/reconciliation/facilities/{mh_id}").json()
    assert mh["facility"]["status"] == "险情"
    alert_id = mh["alerts"][-1]["id"]

    resp = client.post("/api/reconciliation/dispositions", json={
        "facility_id": mh_id,
        "alert_id": alert_id,
        "handled_at": "2026-10-01T09:30:00",
        "result": "cleared",
        "measured_stage": "低水位",
        "pump_online": True,
        "operator": "赵六",
    })
    assert resp.json()["accepted"] is True

    mh2 = client.get(f"/api/reconciliation/facilities/{mh_id}").json()
    assert mh2["facility"]["status"] == "已处置"
    assert mh2["risk_surface"]["passable"] == "可通行"

    listing = next(f for f in client.get("/api/reconciliation/facilities").json()["items"]
                   if f["id"] == mh_id)
    assert listing["status"] == "已处置"
    assert listing["根因"] == "—"

    flood = next(f for f in client.get("/api/reconciliation/flood").json()["items"]
                 if f["facility_id"] == mh_id)
    assert flood["stage"] == "低水位"
    assert flood["state"] == "已核销"
    roads = {r["road"]: r for r in client.get("/api/reconciliation/roads").json()["items"]}
    assert roads["S225-K7"]["stage"] == "低水位"


def test_duplicate_disposition_rejected():
    """重复提交同一告警的处置：第二个结论作废，不产生有效回写。"""
    mh_id = next(f["id"] for f in client.get("/api/reconciliation/facilities").json()["items"]
                 if f["设施编号"] == "MH-0103")
    alert_id = client.get(f"/api/reconciliation/facilities/{mh_id}").json()["alerts"][-1]["id"]
    first = client.post("/api/reconciliation/dispositions", json={
        "facility_id": mh_id, "alert_id": alert_id,
        "handled_at": "2026-10-01T09:30:00", "result": "cleared",
        "measured_stage": "低水位", "operator": "赵六",
    }).json()
    second = client.post("/api/reconciliation/dispositions", json={
        "facility_id": mh_id, "alert_id": alert_id,
        "handled_at": "2026-10-01T09:35:00", "result": "confirmed",
        "measured_stage": "危险水位", "operator": "钱七",
    }).json()
    assert first["accepted"] is True
    assert second["accepted"] is False
    assert "并发冲突" in (second["voided_reason"] or "")
    # 看板仍维持第一份结论的口径
    roads = {r["road"]: r for r in client.get("/api/reconciliation/roads").json()["items"]}
    assert roads["S225-K7"]["stage"] == "低水位"


def test_idempotent_rebuild():
    body = client.post("/api/reconciliation/rebuild", json={"facility_id": None}).json()
    assert "results" in body and "failed" in body
    first = client.post("/api/reconciliation/rebuild", json={"facility_id": 1}).json()
    second = client.post("/api/reconciliation/rebuild", json={"facility_id": 1}).json()
    assert first["version"] == second["version"]
    assert second["idempotent"] is True
