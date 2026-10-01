"""告警对账台接口：风险面 / 处置通知 / 防汛事件三源对账与现场处置回写。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from app.schemas import ActionResult, EntryPayload
from app.services.reconciliation import reconciliation

router = APIRouter(prefix="/api/reconciliation", tags=["告警对账台"])


@router.get("/overview")
def overview() -> dict[str, Any]:
    """对账台总览：待处置告警、离线泵站、不可通行点位、三源不一致数量。"""
    return reconciliation.overview()


@router.get("/facilities")
def list_facilities() -> dict[str, Any]:
    """设施主档案列表，附带当前风险阶段、可通行性与根因（与排水列表同口径）。"""
    return {"items": reconciliation.list_facilities(),
            "total": len(reconciliation.list_facilities())}


@router.get("/facilities/{facility_id}")
def get_facility(facility_id: int) -> dict[str, Any]:
    """设施详情：主档案、冻结的历史水位、告警、通知、防汛事件与重建任务。"""
    detail = reconciliation.get_facility(facility_id)
    if detail is None:
        return {"ok": False, "message": f"设施 {facility_id} 不在主档案中"}
    return detail


@router.get("/alarms")
def list_alarms(status: str | None = Query(default=None, description="待处置、已处置")) -> dict[str, Any]:
    """告警列表，可按处置状态过滤。"""
    items = reconciliation.list_alarms(status=status)
    return {"items": items, "total": len(items)}


@router.post("/alarms/{alarm_id}/handle", response_model=ActionResult)
def handle_alarm(alarm_id: int, payload: EntryPayload) -> ActionResult:
    """现场处置回写：人工复测 / 现场恢复。结论同事务落到台账、防汛清单和路段看板。"""
    values = dict(payload.values)
    alarm, message = reconciliation.handle_alarm(alarm_id, values)
    if alarm is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(
        ok=True,
        message=f"告警 {alarm_id} 已处置（{alarm['conclusion']}），排水台账、防汛清单与路段看板已同步回写",
        entry=alarm,
    )


@router.post("/facilities/{facility_id}/readings", response_model=ActionResult)
def ingest_reading(facility_id: int, payload: EntryPayload) -> ActionResult:
    """上报一条水位：阶段在采集时刻冻结并触发风险面重算。"""
    values = payload.values
    try:
        level_cm = float(values["level_cm"])
    except (TypeError, ValueError, KeyError):
        return ActionResult(ok=False, message="level_cm 必须是数字（厘米）")
    collected_at = str(values.get("collected_at") or "").strip()
    source = str(values.get("source") or "自动采集").strip()
    if not collected_at:
        return ActionResult(ok=False, message="必须提供 collected_at（采集时刻）")
    reading, message = reconciliation.ingest_reading(
        facility_id, level_cm, collected_at, source
    )
    if reading is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message="水位已按采集时刻冻结，风险面已重算", entry=reading)


@router.post("/facilities/{facility_id}/alarms", response_model=ActionResult)
def raise_alarm(facility_id: int, payload: EntryPayload) -> ActionResult:
    """登记一条告警，同时生成处置通知与防汛事件（可分别给阶段，模拟三源不一致）。"""
    values = payload.values
    alarm_type = str(values.get("alarm_type") or "").strip()
    source = str(values.get("source") or "SCADA").strip()
    alarm_time = str(values.get("alarm_time") or "").strip()
    if not alarm_time:
        return ActionResult(ok=False, message="必须提供 alarm_time（告警时刻）")
    alarm, message = reconciliation.raise_alarm(
        facility_id,
        alarm_type,
        source,
        alarm_time,
        stage=values.get("stage"),
        notice_stage=values.get("notice_stage"),
        flood_stage=values.get("flood_stage"),
    )
    if alarm is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message="告警、处置通知与防汛事件已同事务登记", entry=alarm)


@router.post("/facilities/{facility_id}/rebuild", response_model=ActionResult)
def rebuild_facility(facility_id: int, payload: EntryPayload | None = None) -> ActionResult:
    """按设施版本幂等重建风险面；重算失败保留上一版风险面。"""
    simulate = bool((payload.values if payload else {}).get("simulate_failure", False))
    task, message = reconciliation.rebuild_facility(facility_id, simulate_failure=simulate)
    if task is None:
        return ActionResult(ok=False, message=message)
    ok = task["status"] in ("成功", "幂等跳过")
    return ActionResult(
        ok=ok,
        message=task.get("detail") or message or f"重建任务状态：{task['status']}",
        entry=task,
    )


@router.post("/migrate", response_model=ActionResult)
def migrate(payload: EntryPayload) -> ActionResult:
    """迁移排水设施：空设施号按类型+路段+桩号归并到主档案，已存在则升版本。"""
    raw_items = payload.values.get("items")
    if not isinstance(raw_items, list):
        return ActionResult(ok=False, message="items 必须是设施记录数组")
    report, errors = reconciliation.migrate_facilities(raw_items)
    if errors:
        return ActionResult(ok=False, message="；".join(errors))
    return ActionResult(ok=True, message=f"已处理 {len(report)} 条迁移记录",
                        entry={"report": report})


@router.get("/tasks")
def list_tasks() -> dict[str, Any]:
    """重建任务台账，可看到幂等跳过与失败留痕。"""
    return {"items": reconciliation.list_tasks()}


@router.get("/board")
def board() -> dict[str, Any]:
    """路段风险看板：设施风险面按路段空间聚合的结果。"""
    return {"items": reconciliation.board()}
