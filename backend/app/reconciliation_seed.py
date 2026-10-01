"""告警对账台演示数据。

刻意复现本次工单描述的现场：
  * 1# 泵站（BPS-0001）：水位已到警戒、随后泵站离线——修复前风险面仍显示可通行；
  * 2# 雨水口（INL-0021）：低水位正常点；
  * 3# 检查井（MH-0103）：危险水位、已下发处置通知；
  * 一条空设施号迁移残档（待归并到雨水口主档案）；
  * 4# 泵站（BPS-0002）：同泵站多次告警，最近一次人工复测确认消退。

数据全部走领域服务的正式入口写入，不直接灌内部结构。
"""
from __future__ import annotations

from app.services.reconciliation import ReconciliationRepository, ReconciliationService


def bootstrap() -> tuple[ReconciliationRepository, ReconciliationService]:
    repo = ReconciliationRepository()
    svc = ReconciliationService(repo)

    # 1# 泵站：G104 下穿段，警戒水位后泵站离线（风险面不得放行的复现场景）
    p1 = svc.add_facility(code="BPS-0001", facility_type="泵站", road="G104-K2", chainage="K2+100")
    svc.ingest_reading(facility_id=p1.id, collected_at="2026-10-01T06:00:00",
                       stage="低水位", water_cm=35)
    svc.ingest_reading(facility_id=p1.id, collected_at="2026-10-01T07:30:00",
                       stage="警戒水位", water_cm=175)
    svc.ingest_reading(facility_id=p1.id, collected_at="2026-10-01T08:10:00",
                       stage="警戒水位", water_cm=190, pump_online=False)

    # 2# 雨水口：正常低水位
    inl = svc.add_facility(code="INL-0021", facility_type="雨水口", road="G104-K3", chainage="K3+220")
    svc.ingest_reading(facility_id=inl.id, collected_at="2026-10-01T07:00:00",
                       stage="低水位", water_cm=22)

    # 空设施号迁移残档：同路段同类型，等待归并到 INL-0021
    orphan = svc.add_facility(code="", facility_type="雨水口", road="G104-K3", chainage="K3+260")
    svc.ingest_reading(facility_id=orphan.id, collected_at="2026-10-01T05:40:00",
                       stage="低水位", water_cm=18)

    # 3# 检查井：危险水位，处置通知已下发
    mh = svc.add_facility(code="MH-0103", facility_type="检查井", road="S225-K7", chainage="K7+050")
    svc.ingest_reading(facility_id=mh.id, collected_at="2026-10-01T08:20:00",
                       stage="危险水位", water_cm=340)

    # 4# 泵站：同泵站多次告警，最近一次人工复测确认消退
    p2 = svc.add_facility(code="BPS-0002", facility_type="泵站", road="S225-K9", chainage="K9+400")
    svc.ingest_reading(facility_id=p2.id, collected_at="2026-10-01T04:00:00",
                       stage="警戒水位", water_cm=160)
    svc.ingest_reading(facility_id=p2.id, collected_at="2026-10-01T05:10:00",
                       stage="危险水位", water_cm=305)
    first_alert = max(
        aid for aid, a in repo.state().alerts.items() if a.facility_id == p2.id
    )
    svc.submit_disposition(
        facility_id=p2.id, alert_id=first_alert, handled_at="2026-10-01T06:00:00",
        result="confirmed", measured_stage="警戒水位", operator="王班",
    )
    # 最近一次人工复测：消退
    svc.ingest_reading(facility_id=p2.id, collected_at="2026-10-01T07:40:00",
                       stage="低水位", water_cm=30, source="manual")

    svc.rebuild_all()
    return repo, svc
