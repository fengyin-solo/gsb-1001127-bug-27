"""告警对账台接口。

  GET  /api/reconciliation/desk       对账台主视图（三处口径并列）
  GET  /api/reconciliation/roads      路段风险看板（空间聚合结果）
  GET  /api/reconciliation/flood      防汛清单 / 处置通知口径
  GET  /api/reconciliation/facilities 排水设施列表（含根因、风险面状态）
  GET  /api/reconciliation/facilities/{id}  设施详情（冻结水位、告警、处置、风险面）
  POST /api/reconciliation/readings   采集一条水位读数（冻结）
  POST /api/reconciliation/dispositions  现场处置/人工复测（同事务回写三处台账）
  POST /api/reconciliation/rebuild    按设施版本幂等重建（?facility_id= 或全量）
  POST /api/reconciliation/merge-blank 空设施号迁移残档归并主档案
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.reconciliation_seed import bootstrap
from app.services.reconciliation import STAGE_ORDER, CommitError

router = APIRouter(prefix="/api/reconciliation", tags=["告警对账台"])

# 进程级单例：演示内存仓库
_repo, service = bootstrap()


class ReadingIn(BaseModel):
    facility_id: int
    collected_at: str
    stage: str
    water_cm: float
    pump_online: bool = True
    source: str = "sensor"


class DispositionIn(BaseModel):
    facility_id: int
    alert_id: int
    handled_at: str
    result: str = Field(description="confirmed / cleared")
    measured_stage: str | None = None
    pump_online: bool = True
    root_cause: str | None = None
    operator: str = ""


class RebuildIn(BaseModel):
    facility_id: int | None = None


def _validate_stage(stage: str | None) -> None:
    if stage is not None and stage not in STAGE_ORDER:
        raise HTTPException(status_code=400, detail=f"水位阶段必须是：{'、'.join(STAGE_ORDER)}")


@router.get("/desk")
def desk() -> dict[str, Any]:
    """对账台：每座设施的排水台账、风险面、处置通知、路段看板口径并列。"""
    return service.reconciliation_desk()


@router.get("/roads")
def roads() -> dict[str, Any]:
    return {"items": service.road_risk_board()}


@router.get("/flood")
def flood() -> dict[str, Any]:
    return {"items": service.flood_checklist()}


@router.get("/facilities")
def facilities() -> dict[str, Any]:
    return {"items": service.facility_list()}


@router.get("/facilities/{facility_id}")
def facility_detail(facility_id: int) -> dict[str, Any]:
    try:
        return service.facility_detail(facility_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"排水设施 {facility_id} 不存在或已归档")


@router.post("/readings")
def add_reading(payload: ReadingIn) -> dict[str, Any]:
    """采集一条水位读数。读数按采集时刻冻结，随后自动重建该设施。"""
    _validate_stage(payload.stage)
    try:
        reading = service.ingest_reading(
            facility_id=payload.facility_id,
            collected_at=payload.collected_at,
            stage=payload.stage,
            water_cm=payload.water_cm,
            pump_online=payload.pump_online,
            source=payload.source,
        )
        rebuild = service.rebuild_facility(payload.facility_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"设施 {payload.facility_id} 不存在")
    except CommitError as exc:
        return {"ok": True, "reading_id": reading.id, "rebuild_error": str(exc)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "reading_id": reading.id, "rebuild": rebuild}


@router.post("/dispositions")
def add_disposition(payload: DispositionIn) -> dict[str, Any]:
    """现场处置/人工复测：有效结论立即回写排水台账、防汛清单、路段风险看板。"""
    _validate_stage(payload.measured_stage)
    if payload.result not in ("confirmed", "cleared"):
        raise HTTPException(status_code=400, detail="处置结果只能是 confirmed 或 cleared")
    try:
        outcome = service.resolve_alert(
            facility_id=payload.facility_id,
            alert_id=payload.alert_id,
            handled_at=payload.handled_at,
            result=payload.result,
            measured_stage=payload.measured_stage,
            pump_online=payload.pump_online,
            root_cause=payload.root_cause,
            operator=payload.operator,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail=f"设施 {payload.facility_id} 不存在")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, **outcome}


@router.post("/rebuild")
def rebuild(payload: RebuildIn) -> dict[str, Any]:
    """幂等重建：指定设施按 (设施, 版本) 幂等；不指定则全量重建。"""
    if payload.facility_id is not None:
        try:
            return service.rebuild_facility(payload.facility_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"设施 {payload.facility_id} 不存在")
        except CommitError as exc:
            # 重算失败不是 500：上一版风险面保留，调用方应看到明确状态
            raise HTTPException(status_code=409, detail=f"重算未成功，已保留上一版风险面：{exc}")
    return service.rebuild_all()


@router.post("/merge-blank")
def merge_blank() -> dict[str, Any]:
    """空设施号迁移残档归并主档案，归并后自动重算涉及的主档案。"""
    merged = service.merge_blank_codes()
    rebuilt = []
    for item in merged:
        rebuilt.append(service.rebuild_facility(item["merged_into"]))
    return {"ok": True, "merged": merged, "rebuilt": rebuilt}
