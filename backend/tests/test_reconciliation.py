"""告警对账台规则测试。

覆盖：
  1. 三处口径（风险面 / 处置通知防汛清单 / 路段看板）水位阶段一致；
  2. 泵站离线后风险面不得放行；
  3. 现场处置回写排水台账、防汛清单、路段风险看板；
  4. 同泵站多次告警以最近一次人工复测为准；
  5. 历史水位按采集时刻冻结；
  6. 空设施号迁移归并主档案；
  7. 重建按设施版本幂等；
  8. 空间聚合同事务；
  9. 并发处置只保留一个有效结论；
 10. 重算失败保留上一版风险面，不归零；
 11. 根因同时出现在设施列表与详情。
"""
from __future__ import annotations

import threading

import pytest

from app.services.reconciliation import (
    PASSABLE_BY_STAGE,
    ROOT_CAUSE_HEAVY_RAIN,
    ROOT_CAUSE_MANUAL_CLEAR,
    ROOT_CAUSE_PUMP_OFFLINE,
    STAGE_ORDER,
    CommitError,
    ReconciliationRepository,
    ReconciliationService,
)


@pytest.fixture()
def svc() -> ReconciliationService:
    return ReconciliationService(ReconciliationRepository())


def _pump_station(svc: ReconciliationService, road: str = "G104-K2"):
    return svc.add_facility(code="BPS-0001", facility_type="泵站", road=road, chainage="K2+100")


def test_three_calibers_share_same_stage(svc: ReconciliationService) -> None:
    """风险面、防汛清单（处置通知）、路段风险看板三处水位阶段必须一致。"""
    fac = _pump_station(svc)
    svc.ingest_reading(
        facility_id=fac.id, collected_at="2026-10-01T08:00:00",
        stage="警戒水位", water_cm=180,
    )
    result = svc.rebuild_facility(fac.id)

    st = svc.reconciliation_desk()
    row = next(r for r in st["rows"] if r["facility_id"] == fac.id)
    flood = svc.flood_checklist()[0]
    board = svc.road_risk_board()[0]

    assert result["stage"] == "警戒水位"
    assert row["surface_stage"] == row["notice_stage"] == flood["stage"] == "警戒水位"
    assert board["stage"] == "警戒水位"
    # 通行口径同样一致
    assert row["surface_passable"] == row["board_passable"]
    assert flood["passable"] == PASSABLE_BY_STAGE["警戒水位"]


def test_pump_offline_forbids_passable(svc: ReconciliationService) -> None:
    """泵站离线后风险面不得显示可通行（即使读数停留在低水位）。"""
    fac = _pump_station(svc)
    svc.ingest_reading(
        facility_id=fac.id, collected_at="2026-10-01T09:00:00",
        stage="低水位", water_cm=40, pump_online=False,
    )
    result = svc.rebuild_facility(fac.id)

    assert result["pump_online"] is False
    assert result["passable"] != "可通行"
    assert result["stage"] in ("警戒水位", "危险水位")
    assert result["root_cause"] == ROOT_CAUSE_PUMP_OFFLINE

    board = svc.road_risk_board()[0]
    assert board["passable"] != "可通行"
    assert board["pump_offline"] is True

    # 排水台账行根因同步为泵站离线
    ledger = svc.repo.state().drainage_ledger[fac.id]
    assert ledger["root_cause"] == ROOT_CAUSE_PUMP_OFFLINE
    assert ledger["passable"] != "可通行"


def test_disposition_writes_back_three_ledgers(svc: ReconciliationService) -> None:
    """现场处置复测消退：排水台账、防汛清单、路段看板同事务更新为低风险。"""
    fac = _pump_station(svc)
    svc.ingest_reading(
        facility_id=fac.id, collected_at="2026-10-01T08:00:00",
        stage="危险水位", water_cm=320,
    )
    svc.rebuild_facility(fac.id)

    alert_id = max(svc.repo.state().alerts)  # 唯一告警
    svc.submit_disposition(
        facility_id=fac.id, alert_id=alert_id, handled_at="2026-10-01T10:00:00",
        result="cleared", measured_stage="低水位", pump_online=True,
        operator="张三",
    )
    result = svc.rebuild_facility(fac.id)

    assert result["cleared"] is True
    assert result["passable"] == "可通行"

    ledger = svc.repo.state().drainage_ledger[fac.id]
    flood = svc.repo.state().flood_items[fac.id]
    board = svc.road_risk_board()[0]
    assert ledger["status"] == "已处置"
    assert ledger["stage"] == flood["stage"] == board["stage"] == "低水位"
    assert flood["state"] == "已核销"
    assert board["passable"] == "可通行"
    assert ledger["disposition_id"] == flood["disposition_id"]


def test_latest_manual_retest_wins(svc: ReconciliationService) -> None:
    """同一泵站多次告警：以最近一次人工复测为准，更早的系统告警被取代。"""
    fac = _pump_station(svc)
    svc.ingest_reading(
        facility_id=fac.id, collected_at="2026-10-01T08:00:00",
        stage="警戒水位", water_cm=180,
    )
    alert_1 = max(svc.repo.state().alerts)
    svc.submit_disposition(
        facility_id=fac.id, alert_id=alert_1, handled_at="2026-10-01T09:00:00",
        result="confirmed", measured_stage="危险水位", pump_online=True,
        operator="李四",
    )
    svc.rebuild_facility(fac.id)

    # 第二次复测确认消退（时间更晚）
    manual_alerts = [a for a in svc.repo.state().alerts.values() if a.source == "manual"]
    latest_alert = max(manual_alerts, key=lambda a: a.occurred_at)
    # 再开一轮处置需要先让旧结论失效（模拟复测修正流程：直接追加 manual 读数）
    svc.ingest_reading(
        facility_id=fac.id, collected_at="2026-10-01T10:30:00",
        stage="低水位", water_cm=30, source="manual",
    )
    result = svc.rebuild_facility(fac.id)

    assert result["stage"] == "低水位"
    detail = svc.facility_detail(fac.id)
    sensor_alerts = [a for a in detail["alerts"] if a["source"] == "sensor"]
    assert all(a["superseded"] for a in sensor_alerts)
    # 列表与详情根因一致
    listing = svc.facility_list()[0]
    assert listing["根因"] == detail["root_cause_text"]


def test_historical_readings_are_frozen(svc: ReconciliationService) -> None:
    """历史水位按采集时刻冻结：后续重建不改变旧读数的阶段与值。"""
    fac = _pump_station(svc)
    svc.ingest_reading(
        facility_id=fac.id, collected_at="2026-10-01T08:00:00",
        stage="警戒水位", water_cm=180,
    )
    svc.ingest_reading(
        facility_id=fac.id, collected_at="2026-10-01T11:00:00",
        stage="危险水位", water_cm=330, pump_online=False,
    )
    svc.rebuild_facility(fac.id)
    snapshot_before = [
        dict(vars(r)) for r in svc.repo.state().readings.values()
    ]

    # 再次重建（且插入一条新的人工复测），历史读数保持原样
    svc.ingest_reading(
        facility_id=fac.id, collected_at="2026-10-01T12:00:00",
        stage="低水位", water_cm=20, source="manual",
    )
    svc.rebuild_facility(fac.id)
    snapshot_after = [
        dict(vars(r)) for r in svc.repo.state().readings.values()
        if r.source == "sensor"
    ]
    frozen_before = [r for r in snapshot_before if r["source"] == "sensor"]
    assert snapshot_after == frozen_before


def test_blank_code_merged_into_primary(svc: ReconciliationService) -> None:
    """空设施号迁移残档按同路段同类型归并到主档案，历史数据随迁。"""
    primary = svc.add_facility(code="INL-0007", facility_type="雨水口", road="G104-K3")
    orphan = svc.add_facility(code="", facility_type="雨水口", road="G104-K3")
    svc.ingest_reading(
        facility_id=orphan.id, collected_at="2026-10-01T08:00:00",
        stage="警戒水位", water_cm=150,
    )

    merged = svc.merge_blank_codes()
    assert len(merged) == 1
    assert merged[0]["orphan_id"] == orphan.id
    assert merged[0]["merged_into"] == primary.id

    state = svc.repo.state()
    assert state.facilities[orphan.id].merged_into == primary.id
    # 读数改挂主档案
    assert all(r.facility_id == primary.id for r in state.readings.values())
    # 对账台、列表都不再出现残档
    desk_ids = {r["facility_id"] for r in svc.reconciliation_desk()["rows"]}
    list_ids = {item["id"] for item in svc.facility_list()}
    assert orphan.id not in desk_ids | list_ids
    # 主档案版本因归并递增，重建后能看到随迁的读数
    result = svc.rebuild_facility(primary.id)
    assert result["stage"] == "警戒水位"


def test_rebuild_idempotent_by_version(svc: ReconciliationService) -> None:
    """同一设施版本重复重建：返回幂等命中，风险面版本不变。"""
    fac = _pump_station(svc)
    svc.ingest_reading(
        facility_id=fac.id, collected_at="2026-10-01T08:00:00",
        stage="警戒水位", water_cm=180,
    )
    first = svc.rebuild_facility(fac.id)
    surface_1 = svc.repo.state().risk_surfaces[fac.id]
    second = svc.rebuild_facility(fac.id)
    surface_2 = svc.repo.state().risk_surfaces[fac.id]

    assert first["idempotent"] is False
    assert second["idempotent"] is True
    assert second["version"] == first["version"]
    # 幂等命中：风险面字段与上一版完全一致，没有重复计算
    surface_2 = svc.repo.state().risk_surfaces[fac.id]
    assert vars(surface_2) == vars(surface_1)


def test_concurrent_dispositions_keep_single_valid(svc: ReconciliationService) -> None:
    """并发处置：同一设施只保留一个有效结论，其余作废且不覆盖。"""
    fac = _pump_station(svc)
    svc.ingest_reading(
        facility_id=fac.id, collected_at="2026-10-01T08:00:00",
        stage="危险水位", water_cm=300,
    )
    alert_id = max(svc.repo.state().alerts)

    results: list[str] = []
    barrier = threading.Barrier(2)

    def worker(operator: str) -> None:
        barrier.wait()
        disp = svc.submit_disposition(
            facility_id=fac.id, alert_id=alert_id,
            handled_at="2026-10-01T09:00:00",
            result="confirmed", measured_stage="危险水位", operator=operator,
        )
        results.append(f"{disp.id}:{disp.valid}")

    t1 = threading.Thread(target=worker, args=("甲",))
    t2 = threading.Thread(target=worker, args=("乙",))
    t1.start(); t2.start(); t1.join(); t2.join()

    valid = [d for d in svc.repo.state().dispositions.values() if d.valid]
    voided = [d for d in svc.repo.state().dispositions.values() if not d.valid]
    assert len(valid) == 1
    assert len(voided) == 1
    assert "并发冲突" in (voided[0].voided_reason or "")

    # 后续重建绑定的是唯一有效结论
    rebuilt = svc.rebuild_facility(fac.id)
    assert rebuilt["surface"]["disposition_id"] == valid[0].id


def test_failed_recompute_keeps_previous_surface(svc: ReconciliationService) -> None:
    """重算失败：上一版风险面保留，事务回滚，状态不归零。"""
    fac = _pump_station(svc)
    svc.ingest_reading(
        facility_id=fac.id, collected_at="2026-10-01T08:00:00",
        stage="警戒水位", water_cm=180,
    )
    svc.rebuild_facility(fac.id)
    previous = svc.repo.state().risk_surfaces[fac.id]

    # 新输入使版本前进，但下一次重算被空间服务故障打断
    svc.ingest_reading(
        facility_id=fac.id, collected_at="2026-10-01T12:00:00",
        stage="危险水位", water_cm=350,
    )
    svc.repo.fail_next_recompute = True
    with pytest.raises(CommitError):
        svc.rebuild_facility(fac.id)

    state = svc.repo.state()
    kept = state.risk_surfaces[fac.id]
    # 上一版风险面原样保留，没有被清空/归零
    assert vars(kept) == vars(previous)
    assert kept.stage == "警戒水位"
    assert kept.passable == "减速缓行"
    # 看板也保留上一轮聚合
    board = state.road_board[fac.road]
    assert board["stage"] == "警戒水位"
    # 幂等表没有为新版本留下半成品
    assert (fac.id, state.facilities[fac.id].version) not in state.rebuilds

    # 故障恢复后可正常重算
    fixed = svc.rebuild_facility(fac.id)
    assert fixed["stage"] == "危险水位"
    assert fixed["passable"] == "禁止通行"


def test_root_cause_same_in_list_and_detail(svc: ReconciliationService) -> None:
    """同一排水设施根因同时影响列表行与详情。"""
    fac = _pump_station(svc)
    svc.ingest_reading(
        facility_id=fac.id, collected_at="2026-10-01T08:00:00",
        stage="低水位", water_cm=20, pump_online=False,
    )
    svc.rebuild_facility(fac.id)

    listing = next(item for item in svc.facility_list() if item["id"] == fac.id)
    detail = svc.facility_detail(fac.id)
    assert listing["root_cause"] == detail["root_cause"] == ROOT_CAUSE_PUMP_OFFLINE
    assert listing["根因"] == detail["root_cause_text"] == "泵站离线，强排中断"


def test_spatial_aggregation_picks_worst(svc: ReconciliationService) -> None:
    """路段看板空间聚合取最严阶段：两座设施任一危险，全路段禁行。"""
    a = svc.add_facility(code="BPS-0001", facility_type="泵站", road="G104-K5")
    b = svc.add_facility(code="INL-0002", facility_type="雨水口", road="G104-K5")
    svc.ingest_reading(facility_id=a.id, collected_at="2026-10-01T08:00:00",
                       stage="低水位", water_cm=20)
    svc.ingest_reading(facility_id=b.id, collected_at="2026-10-01T08:05:00",
                       stage="危险水位", water_cm=360)
    svc.rebuild_all()

    board = next(r for r in svc.road_risk_board() if r["road"] == "G104-K5")
    assert board["stage"] == "危险水位"
    assert board["passable"] == "禁止通行"
    assert board["facility_count"] == 2
    assert board["active_count"] == 1
    assert board["risk_level"] == "高"
