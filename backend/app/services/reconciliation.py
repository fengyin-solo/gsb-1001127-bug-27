"""告警对账台领域内核。

排水设施在汛期会同时产生三类数据：
  * 传感器/人工上报的水位读数（reading）
  * 按阈值生成的告警（alert），并产生处置通知、防汛事件、路段风险面三处外显口径
  * 现场班组的处置结论与人工复测（disposition）

历史上三处口径各算各的，出现过「泵站离线后风险面仍显示可通行」「同一泵站多次告警
结论互相覆盖」「重算失败风险面归零」等问题。本模块把对账规则收敛成一个纯领域内核：

  * 历史水位按采集时刻冻结，重放结果与当时一致；
  * 同一泵站多次告警时，以最近一次人工复测为准；
  * 现场处置结论在同一事务里回写排水台账、防汛清单、路段风险看板；
  * 重建按 (设施, 设施版本) 幂等；
  * 空间（路段）聚合与回写在同一事务内完成，失败整体回滚；
  * 并发处置只保留一个有效结论，后到的并发结论被拒绝；
  * 重算未成功时沿用上一版风险面，绝不清空/归零；
  * 空设施号的迁移记录按「同路段+同类型」归并到主档案；
  * 设施根因同时写回设施列表行与设施详情。

内核不依赖 FastAPI、不依赖全局状态，所有数据经 Repository 注入，方便单测重放。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable


# ---------------------------------------------------------------------------
# 常量与口径
# ---------------------------------------------------------------------------

# 水位阶段（有序，序号越大越危险）
STAGE_ORDER = ["低水位", "警戒水位", "危险水位"]

# 风险面通行口径：低水位可通行；警戒水位缓行；危险水位（或泵站离线导致强排中断）禁行
PASSABLE_BY_STAGE = {
    "低水位": "可通行",
    "警戒水位": "减速缓行",
    "危险水位": "禁止通行",
}

# 设施业务状态
STATUS_NORMAL = "正常"
STATUS_WARNING = "预警"
STATUS_DANGER = "险情"
STATUS_CLEARED = "已处置"

# 根因代码：同一设施只认定一个根因，列表行与详情共用
ROOT_CAUSE_PUMP_OFFLINE = "PUMP_OFFLINE"      # 泵站离线，强排中断
ROOT_CAUSE_BLOCKED = "BLOCKED"                # 设施堵塞
ROOT_CAUSE_SILTED = "SILTED"                  # 淤积
ROOT_CAUSE_HEAVY_RAIN = "HEAVY_RAIN"          # 短时强降雨
ROOT_CAUSE_MANUAL_CLEAR = "MANUAL_CLEAR"      # 人工复测确认消退

ROOT_CAUSE_TEXT = {
    ROOT_CAUSE_PUMP_OFFLINE: "泵站离线，强排中断",
    ROOT_CAUSE_BLOCKED: "设施堵塞",
    ROOT_CAUSE_SILTED: "淤积超限",
    ROOT_CAUSE_HEAVY_RAIN: "短时强降雨",
    ROOT_CAUSE_MANUAL_CLEAR: "人工复测确认积水消退",
}


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


# ---------------------------------------------------------------------------
# 领域对象（全部可 JSON 序列化）
# ---------------------------------------------------------------------------

@dataclass
class Facility:
    """排水设施主档案。"""

    id: int
    code: str                    # 设施编号，空串表示待归并的迁移残档
    facility_type: str           # 设施类型（泵站 / 雨水口 / 检查井 ...）
    road: str                    # 所属路段（空间聚合键）
    chainage: str = ""           # 桩号位置
    is_primary: bool = True      # 是否主档案；迁移残档归并后为 False
    merged_into: int | None = None
    version: int = 0             # 设施版本：输入每变化一次 +1，重建幂等键的一部分
    # 对账回写字段（排水台账口径）
    status: str = STATUS_NORMAL
    root_cause: str | None = None
    last_disposition_id: int | None = None


@dataclass
class Reading:
    """水位读数。一经采集即按采集时刻冻结，后续不允许修改。"""

    id: int
    facility_id: int
    collected_at: str            # ISO 时间，采集时刻
    stage: str                   # 采集当时认定的水位阶段
    water_cm: float
    pump_online: bool = True     # 采集当时泵站是否在线
    source: str = "sensor"       # sensor / manual


@dataclass
class Alert:
    """告警。系统告警与人工复测单都落在这里。"""

    id: int
    facility_id: int
    occurred_at: str
    stage: str
    pump_online: bool
    source: str = "sensor"       # sensor / manual
    root_cause: str | None = None
    disposition_id: int | None = None   # 已被哪条处置结论核销
    superseded: bool = False             # 是否被更新的人工复测取代


@dataclass
class Disposition:
    """现场处置结论与人工复测。"""

    id: int
    facility_id: int
    alert_id: int
    handled_at: str
    result: str                  # confirmed / cleared
    measured_stage: str | None = None    # 人工复测水位阶段
    pump_online: bool = True
    root_cause: str | None = None
    operator: str = ""
    valid: bool = True                    # 并发竞争后只有一个有效结论
    voided_reason: str | None = None


@dataclass
class RiskSurface:
    """一版风险面（同时是处置通知/防汛事件/路段看板的统一口径）。"""

    facility_version: int
    stage: str
    passable: str
    pump_online: bool
    root_cause: str | None
    disposition_id: int | None
    alerts: list[int]
    computed_at: str


# ---------------------------------------------------------------------------
# 内存仓库：copy-on-write 事务
# ---------------------------------------------------------------------------

class CommitError(RuntimeError):
    """事务提交阶段失败（例如注入的重算失败），整笔事务回滚。"""


@dataclass
class LedgerState:
    facilities: dict[int, Facility] = field(default_factory=dict)
    readings: dict[int, Reading] = field(default_factory=dict)
    alerts: dict[int, Alert] = field(default_factory=dict)
    dispositions: dict[int, Disposition] = field(default_factory=dict)
    # 已发布风险面：facility_id -> 最新一版（重算失败时保留它，不清空）
    risk_surfaces: dict[int, RiskSurface] = field(default_factory=dict)
    # 路段风险看板：road -> 聚合行（空间聚合同事务发布）
    road_board: dict[str, dict[str, Any]] = field(default_factory=dict)
    # 排水台账行：facility_id -> 行（现场处置结果回写处之一）
    drainage_ledger: dict[int, dict[str, Any]] = field(default_factory=dict)
    # 防汛清单条目：facility_id -> 行
    flood_items: dict[int, dict[str, Any]] = field(default_factory=dict)
    # 已消费的幂等键：(facility_id, version) -> 重建结果快照
    rebuilds: dict[tuple[int, int], dict[str, Any]] = field(default_factory=dict)
    seq: int = 0

    def clone(self) -> "LedgerState":
        return LedgerState(
            facilities={k: Facility(**vars(v)) for k, v in self.facilities.items()},
            readings={k: Reading(**vars(v)) for k, v in self.readings.items()},
            alerts={k: Alert(**{key: val for key, val in vars(v).items()
                                if key in Alert.__dataclass_fields__})
                    for k, v in self.alerts.items()},
            dispositions={k: Disposition(**vars(v)) for k, v in self.dispositions.items()},
            risk_surfaces={k: RiskSurface(**vars(v)) for k, v in self.risk_surfaces.items()},
            road_board={k: dict(v) for k, v in self.road_board.items()},
            drainage_ledger={k: dict(v) for k, v in self.drainage_ledger.items()},
            flood_items={k: dict(v) for k, v in self.flood_items.items()},
            rebuilds={k: dict(v) for k, v in self.rebuilds.items()},
            seq=self.seq,
        )


class ReconciliationRepository:
    """带事务与设施级互斥锁的内存仓库。

    真实项目里 mutate() 对应一个数据库事务 + SELECT ... FOR UPDATE；
    这里用深拷贝快照保证「要么全成、要么全不动」，用 per-facility 锁保证
    并发处置只落一个有效结论。
    """

    def __init__(self) -> None:
        self._state = LedgerState()
        self._locks: dict[int, threading.Lock] = {}
        self._global = threading.RLock()
        # 测试钩子：置 True 后下一次风险面重算抛错（模拟重算失败）
        self.fail_next_recompute = False

    # ---- 基础读 ----
    def state(self) -> LedgerState:
        return self._state

    def facility_lock(self, facility_id: int) -> threading.Lock:
        with self._global:
            return self._locks.setdefault(facility_id, threading.Lock())

    def mutate(
        self,
        facility_id: int | None,
        fn: Callable[[LedgerState], dict[str, Any]],
    ) -> dict[str, Any]:
        """在设施锁内对状态快照执行 fn；fn 抛错或提交校验失败则整体回滚。"""
        lock = self.facility_lock(facility_id if facility_id is not None else -1)
        with lock:
            snapshot = self._state.clone()
            try:
                result = fn(snapshot)
            except CommitError:
                raise
            except Exception as exc:  # 业务/重算失败：快照丢弃，状态原样保留
                raise CommitError(str(exc)) from exc
            # 提交：整体替换。任一引用未就绪都不会发生，因为 result 已在快照上算完
            self._state = snapshot
            return result


# ---------------------------------------------------------------------------
# 领域服务
# ---------------------------------------------------------------------------

class ReconciliationService:
    def __init__(self, repo: ReconciliationRepository) -> None:
        self.repo = repo

    # ================= 输入侧 =================

    def add_facility(
        self,
        *,
        code: str,
        facility_type: str,
        road: str,
        chainage: str = "",
        is_primary: bool = True,
    ) -> Facility:
        def tx(st: LedgerState) -> dict[str, Any]:
            st.seq += 1
            fac = Facility(
                id=st.seq,
                code=code.strip(),
                facility_type=facility_type,
                road=road,
                chainage=chainage,
                # 空设施号是待归并的迁移残档，不能作为主档案参与对账
                is_primary=is_primary and bool(code.strip()),
            )
            st.facilities[fac.id] = fac
            return {"facility": fac}

        return self.repo.mutate(None, tx)["facility"]

    def ingest_reading(
        self,
        *,
        facility_id: int,
        collected_at: str,
        stage: str,
        water_cm: float,
        pump_online: bool = True,
        source: str = "sensor",
    ) -> Reading:
        """采集一条水位读数（冻结），并按当时口径生成一条告警；设施版本 +1。"""

        def tx(st: LedgerState) -> dict[str, Any]:
            fac = st.facilities[facility_id]
            st.seq += 1
            reading = Reading(
                id=st.seq, facility_id=facility_id, collected_at=collected_at,
                stage=stage, water_cm=water_cm, pump_online=pump_online, source=source,
            )
            st.readings[reading.id] = reading

            root_cause = (
                ROOT_CAUSE_MANUAL_CLEAR if source == "manual" and stage == STAGE_ORDER[0]
                else ROOT_CAUSE_PUMP_OFFLINE if not pump_online
                else self._stage_cause(stage)
            )
            st.seq += 1
            alert = Alert(
                id=st.seq, facility_id=facility_id, occurred_at=collected_at,
                stage=stage, pump_online=pump_online, source=source, root_cause=root_cause,
            )
            st.alerts[alert.id] = alert
            fac.version += 1
            return {"reading": reading}

        return self.repo.mutate(facility_id, tx)["reading"]

    # ================= 现场处置 / 人工复测 =================

    def submit_disposition(
        self,
        *,
        facility_id: int,
        alert_id: int,
        handled_at: str,
        result: str,
        measured_stage: str | None = None,
        pump_online: bool = True,
        root_cause: str | None = None,
        operator: str = "",
    ) -> Disposition:
        """登记现场处置结论。

        并发竞争：同一设施只允许一个有效结论，已存在有效结论时后来者直接作废
        （voided_reason 标注），不覆盖既有结论。
        """

        def tx(st: LedgerState) -> dict[str, Any]:
            fac = st.facilities.get(facility_id)
            if fac is None:
                raise ValueError(f"设施 {facility_id} 不存在或已归档")
            alert = st.alerts.get(alert_id)
            if alert is None or alert.facility_id != facility_id:
                raise ValueError("处置通知对应的告警不存在")

            st.seq += 1
            disp = Disposition(
                id=st.seq, facility_id=facility_id, alert_id=alert_id,
                handled_at=handled_at, result=result, measured_stage=measured_stage,
                pump_online=pump_online, root_cause=root_cause, operator=operator,
            )

            existing_valid = next(
                (d for d in st.dispositions.values()
                 if d.facility_id == facility_id and d.alert_id == alert_id and d.valid),
                None,
            )
            if existing_valid is not None:
                # 并发处置同一告警：只保留一个有效结论；后续新告警仍可顺序处置
                disp.valid = False
                disp.voided_reason = f"并发冲突：告警 #{alert_id} 已有有效处置结论 #{existing_valid.id}"
                st.dispositions[disp.id] = disp
                return {"disposition": disp, "accepted": False}

            st.dispositions[disp.id] = disp
            alert.disposition_id = disp.id
            fac.version += 1
            fac.last_disposition_id = disp.id

            # 人工复测单同时作为一条 manual 告警落账，后续对账以它为准
            if measured_stage is not None:
                st.seq += 1
                manual_alert = Alert(
                    id=st.seq, facility_id=facility_id, occurred_at=handled_at,
                    stage=measured_stage, pump_online=pump_online, source="manual",
                    root_cause=ROOT_CAUSE_MANUAL_CLEAR if result == "cleared" else root_cause,
                    disposition_id=disp.id,
                )
                st.alerts[manual_alert.id] = manual_alert
            return {"disposition": disp, "accepted": True}

        outcome = self.repo.mutate(facility_id, tx)
        return outcome["disposition"]

    # ================= 现场处置 + 即时回写（应用事务） =================

    def resolve_alert(
        self,
        *,
        facility_id: int,
        alert_id: int,
        handled_at: str,
        result: str,
        measured_stage: str | None = None,
        pump_online: bool = True,
        root_cause: str | None = None,
        operator: str = "",
    ) -> dict[str, Any]:
        """登记现场处置并在同一业务动作里重建回写三处台账。

        处置登记与对账重建是两笔存储事务，但重建失败时处置已经落库（现场事实
        不能丢），上一版风险面保留；返回里会显式标注 rebuild_error。
        """
        disp = self.submit_disposition(
            facility_id=facility_id, alert_id=alert_id, handled_at=handled_at,
            result=result, measured_stage=measured_stage, pump_online=pump_online,
            root_cause=root_cause, operator=operator,
        )
        payload: dict[str, Any] = {
            "disposition_id": disp.id,
            "accepted": disp.valid,
            "voided_reason": disp.voided_reason,
        }
        if disp.valid:
            try:
                payload["rebuild"] = self.rebuild_facility(facility_id)
            except CommitError as exc:
                payload["rebuild_error"] = str(exc)
        return payload

    # ================= 迁移：空设施号归并主档案 =================

    def merge_blank_codes(self) -> list[dict[str, Any]]:
        """把空设施号的迁移残档按「同路段+同类型」归并到主档案。

        读数/告警/处置/风险面都改挂到主档案；残档标记 merged_into 后不再参与
        对账。返回归并清单。
        """

        def tx(st: LedgerState) -> dict[str, Any]:
            merged: list[dict[str, Any]] = []
            primaries = [f for f in st.facilities.values() if f.is_primary]
            orphans = [
                f for f in st.facilities.values()
                if not f.is_primary or f.code == ""
            ]
            for orphan in orphans:
                target = next(
                    (p for p in primaries
                     if p.id != orphan.id and p.road == orphan.road
                     and p.facility_type == orphan.facility_type and p.code != ""),
                    None,
                )
                if target is None:
                    continue
                for r in st.readings.values():
                    if r.facility_id == orphan.id:
                        r.facility_id = target.id
                for a in st.alerts.values():
                    if a.facility_id == orphan.id:
                        a.facility_id = target.id
                for d in st.dispositions.values():
                    if d.facility_id == orphan.id:
                        d.facility_id = target.id
                if orphan.id in st.risk_surfaces:
                    st.risk_surfaces.pop(orphan.id, None)
                st.flood_items.pop(orphan.id, None)
                st.drainage_ledger.pop(orphan.id, None)
                orphan.is_primary = False
                orphan.merged_into = target.id
                target.version += 1
                merged.append({
                    "orphan_id": orphan.id,
                    "merged_into": target.id,
                    "road": target.road,
                    "facility_type": target.facility_type,
                    "primary_code": target.code,
                })
            return {"merged": merged}

        return self.repo.mutate(None, tx)["merged"]

    # ================= 重建对账台 =================

    def rebuild_facility(self, facility_id: int) -> dict[str, Any]:
        """按设施版本幂等重建一个设施的对账口径。

        步骤（同一事务内）：
          1. 幂等检查：(设施, version) 已重建过则原样返回；
          2. 认定对账水位/根因：最近一次人工复测优先，否则取最新冻结读数；
          3. 重算风险面；若重算抛错，沿用上一版，不归零、不提交半拉子数据；
          4. 回写排水台账、防汛清单、路段风险看板（空间聚合）。
        """

        def tx(st: LedgerState) -> dict[str, Any]:
            fac = st.facilities[facility_id]
            idem_key = (facility_id, fac.version)
            if idem_key in st.rebuilds:
                # 幂等命中：快照不做任何修改，提交后状态原样保留
                return dict(st.rebuilds[idem_key], idempotent=True)

            alerts = [a for a in st.alerts.values() if a.facility_id == facility_id]
            # 标记被人工复测取代的历史系统告警（同一泵站多次告警，以最近复测为准）
            manual_alerts = sorted(
                [a for a in alerts if a.source == "manual"],
                key=lambda a: _parse_ts(a.occurred_at),
            )
            latest_manual = manual_alerts[-1] if manual_alerts else None
            for a in alerts:
                a.superseded = bool(
                    latest_manual is not None
                    and a.source == "sensor"
                    and _parse_ts(a.occurred_at) <= _parse_ts(latest_manual.occurred_at)
                )

            valid_dispositions = sorted(
                (d for d in st.dispositions.values()
                 if d.facility_id == facility_id and d.valid),
                key=lambda d: _parse_ts(d.handled_at),
            )
            valid_disp = valid_dispositions[-1] if valid_dispositions else None

            # --- 认定对账口径 ---
            if latest_manual is not None:
                stage = latest_manual.stage
                pump_online = latest_manual.pump_online
                root_cause = latest_manual.root_cause
            else:
                frozen = sorted(
                    [r for r in st.readings.values() if r.facility_id == facility_id],
                    key=lambda r: _parse_ts(r.collected_at),
                )
                latest = frozen[-1] if frozen else None
                stage = latest.stage if latest else STAGE_ORDER[0]
                pump_online = latest.pump_online if latest else True
                root_cause = (
                    ROOT_CAUSE_PUMP_OFFLINE
                    if latest is not None and not latest.pump_online
                    else (self._stage_cause(stage) if latest else None)
                )

            # --- 重算风险面（失败保留上一版，事务回滚） ---
            if self.repo.fail_next_recompute:
                self.repo.fail_next_recompute = False
                raise CommitError("风险面重算失败：空间服务不可用")

            # 泵站离线 => 强排中断，即使水位不高也不得放行
            if not pump_online:
                stage = max(stage, STAGE_ORDER[1], key=lambda s: STAGE_ORDER.index(s))
                root_cause = ROOT_CAUSE_PUMP_OFFLINE
            passable = PASSABLE_BY_STAGE[stage]
            if not pump_online and stage != STAGE_ORDER[2]:
                passable = "减速缓行"

            new_surface = RiskSurface(
                facility_version=fac.version,
                stage=stage,
                passable=passable,
                pump_online=pump_online,
                root_cause=root_cause,
                disposition_id=valid_disp.id if valid_disp else None,
                alerts=[a.id for a in sorted(alerts, key=lambda a: a.id)],
                computed_at=datetime.now().isoformat(timespec="seconds"),
            )

            # 处置完成且复测低水位 => 解除
            cleared = (
                valid_disp is not None
                and valid_disp.result == "cleared"
                and stage == STAGE_ORDER[0]
                and pump_online
            )

            # --- 同事务回写三处台账 ---
            fac.status = STATUS_CLEARED if cleared else self._status_of(stage, pump_online)
            fac.root_cause = None if cleared else root_cause
            # 风险面先发布到本事务快照，路段聚合与防汛清单才能引用同一口径
            st.risk_surfaces[facility_id] = new_surface
            self._write_drainage_ledger(st, fac, new_surface, cleared)
            self._write_flood_item(st, fac, new_surface, cleared)
            self._aggregate_road_board(st)

            result_payload = {
                "idempotent": False,
                "facility_id": facility_id,
                "version": fac.version,
                "stage": stage,
                "passable": passable,
                "pump_online": pump_online,
                "root_cause": root_cause,
                "root_cause_text": ROOT_CAUSE_TEXT.get(root_cause or "", root_cause),
                "status": fac.status,
                "cleared": cleared,
                "surface": vars(new_surface),
                "active_alert_ids": new_surface.alerts,
            }
            st.rebuilds[idem_key] = dict(result_payload)
            return result_payload

        return self.repo.mutate(facility_id, tx)

    def rebuild_all(self) -> dict[str, Any]:
        """对全部主档案设施逐座重建；任一设施重算失败不影响其他设施。"""
        st = self.repo.state()
        ids = [f.id for f in st.facilities.values() if f.merged_into is None]
        results, failed = [], []
        for fid in ids:
            try:
                results.append(self.rebuild_facility(fid))
            except CommitError as exc:
                failed.append({"facility_id": fid, "reason": str(exc)})
        return {"results": results, "failed": failed}

    # ================= 查询侧 =================

    def reconciliation_desk(self) -> dict[str, Any]:
        """告警对账台视图：每座主档案一行，三处口径并列，便于人工核对。"""
        st = self.repo.state()
        rows = []
        for fac in sorted(st.facilities.values(), key=lambda f: f.id):
            if fac.merged_into is not None:
                continue
            surface = st.risk_surfaces.get(fac.id)
            flood = st.flood_items.get(fac.id)
            board = st.road_board.get(fac.road)
            active_alerts = [
                a for a in st.alerts.values()
                if a.facility_id == fac.id and not a.superseded
            ]
            rows.append({
                "facility_id": fac.id,
                "facility_code": fac.code,
                "facility_type": fac.facility_type,
                "road": fac.road,
                "chainage": fac.chainage,
                "version": fac.version,
                "ledger_status": fac.status,                  # 排水台账
                "root_cause": fac.root_cause,
                "root_cause_text": ROOT_CAUSE_TEXT.get(fac.root_cause or "", fac.root_cause),
                "surface_stage": surface.stage if surface else None,      # 风险面
                "surface_passable": surface.passable if surface else None,
                "notice_stage": flood["stage"] if flood else None,        # 处置通知/防汛清单
                "board_passable": board["passable"] if board else None,   # 路段看板
                "pump_online": surface.pump_online if surface else None,
                "active_alert_count": len(active_alerts),
                "active_alerts": [self._alert_dict(a) for a in sorted(
                    active_alerts, key=lambda a: _parse_ts(a.occurred_at))],
                "last_disposition_id": fac.last_disposition_id,
                "frozen_readings": [
                    vars(r) for r in sorted(
                        (r for r in st.readings.values() if r.facility_id == fac.id),
                        key=lambda r: _parse_ts(r.collected_at))
                ],
            })
        return {"rows": rows, "roads": list(st.road_board.values())}

    def facility_detail(self, facility_id: int) -> dict[str, Any]:
        """设施详情：冻结水位曲线、告警（含被复测取代标记）、处置结论、当前风险面。"""
        st = self.repo.state()
        fac = st.facilities.get(facility_id)
        if fac is None:
            raise KeyError(facility_id)
        return {
            "facility": vars(fac),
            "root_cause": fac.root_cause,
            "root_cause_text": ROOT_CAUSE_TEXT.get(fac.root_cause or "", fac.root_cause),
            "readings": [
                vars(r) for r in sorted(
                    (r for r in st.readings.values() if r.facility_id == facility_id),
                    key=lambda r: _parse_ts(r.collected_at))
            ],
            "alerts": [
                self._alert_dict(a) for a in sorted(
                    (a for a in st.alerts.values() if a.facility_id == facility_id),
                    key=lambda a: _parse_ts(a.occurred_at))
            ],
            "dispositions": [
                vars(d) for d in sorted(
                    (d for d in st.dispositions.values() if d.facility_id == facility_id),
                    key=lambda d: _parse_ts(d.handled_at))
            ],
            "risk_surface": vars(st.risk_surfaces[facility_id])
            if facility_id in st.risk_surfaces else None,
        }

    def facility_list(self) -> list[dict[str, Any]]:
        """排水设施列表：根因与风险面状态随行返回（与详情同一来源）。"""
        st = self.repo.state()
        items = []
        for fac in sorted(st.facilities.values(), key=lambda f: f.id):
            if fac.merged_into is not None:
                continue
            surface = st.risk_surfaces.get(fac.id)
            items.append({
                "id": fac.id,
                "设施编号": fac.code,
                "设施类型": fac.facility_type,
                "所属路段": fac.road,
                "桩号位置": fac.chainage,
                "status": fac.status,
                "version": fac.version,
                "root_cause": fac.root_cause,
                "根因": ROOT_CAUSE_TEXT.get(fac.root_cause or "", fac.root_cause or "—"),
                "水位阶段": surface.stage if surface else "—",
                "通行口径": surface.passable if surface else "—",
                "泵站在线": "是" if (surface is None or surface.pump_online) else "否",
                "pending": fac.status != STATUS_CLEARED,
                "abnormal": fac.status in (STATUS_WARNING, STATUS_DANGER),
            })
        return items

    def road_risk_board(self) -> list[dict[str, Any]]:
        return list(self.repo.state().road_board.values())

    def flood_checklist(self) -> list[dict[str, Any]]:
        return list(self.repo.state().flood_items.values())

    # ================= 内部规则 =================

    @staticmethod
    def _stage_cause(stage: str) -> str | None:
        if stage == STAGE_ORDER[2]:
            return ROOT_CAUSE_BLOCKED
        if stage == STAGE_ORDER[1]:
            return ROOT_CAUSE_HEAVY_RAIN
        return None

    @staticmethod
    def _status_of(stage: str, pump_online: bool) -> str:
        if stage == STAGE_ORDER[2] or not pump_online:
            return STATUS_DANGER
        if stage == STAGE_ORDER[1]:
            return STATUS_WARNING
        return STATUS_NORMAL

    def _write_drainage_ledger(
        self, st: LedgerState, fac: Facility, surface: RiskSurface, cleared: bool
    ) -> None:
        """排水台账：现场处置结果回写到设施台账行，列表与详情读同一份数据。"""
        st.drainage_ledger[fac.id] = {
            "facility_id": fac.id,
            "设施编号": fac.code,
            "设施类型": fac.facility_type,
            "所属路段": fac.road,
            "status": fac.status,
            "root_cause": fac.root_cause,
            "root_cause_text": ROOT_CAUSE_TEXT.get(fac.root_cause or "", fac.root_cause),
            "stage": surface.stage,
            "passable": surface.passable,
            "pump_online": surface.pump_online,
            "disposition_id": surface.disposition_id,
            "version": fac.version,
            "updated_at": surface.computed_at,
        }

    def _write_flood_item(
        self, st: LedgerState, fac: Facility, surface: RiskSurface, cleared: bool
    ) -> None:
        """防汛清单 + 处置通知：水位阶段必须与风险面完全一致。"""
        st.flood_items[fac.id] = {
            "facility_id": fac.id,
            "facility_code": fac.code,
            "road": fac.road,
            "stage": surface.stage,          # 与风险面同一 stage
            "passable": surface.passable,
            "pump_online": surface.pump_online,
            "root_cause": surface.root_cause,
            "root_cause_text": ROOT_CAUSE_TEXT.get(surface.root_cause or "", surface.root_cause),
            "disposition_id": surface.disposition_id,
            "state": "已核销" if cleared else "处置中",
            "updated_at": surface.computed_at,
        }

    def _aggregate_road_board(self, st: LedgerState) -> None:
        """路段风险看板：按路段空间聚合，取最严水位阶段与最保守通行口径。

        与设施重算在同一事务内完成：聚合结果与设施风险面要么同时发布、
        要么一起回滚。
        """
        roads: dict[str, list[Facility]] = {}
        for fac in st.facilities.values():
            if fac.merged_into is not None:
                continue
            roads.setdefault(fac.road, []).append(fac)

        new_board: dict[str, dict[str, Any]] = {}
        for road, facs in roads.items():
            worst_idx = 0
            any_pump_offline = False
            active = 0
            for fac in facs:
                surf = st.risk_surfaces.get(fac.id)
                if surf is None:
                    continue
                worst_idx = max(worst_idx, STAGE_ORDER.index(surf.stage))
                any_pump_offline = any_pump_offline or not surf.pump_online
                if fac.status in (STATUS_WARNING, STATUS_DANGER):
                    active += 1
            worst_stage = STAGE_ORDER[worst_idx]
            passable = PASSABLE_BY_STAGE[worst_stage]
            if any_pump_offline and worst_stage != STAGE_ORDER[2]:
                passable = "减速缓行"
            new_board[road] = {
                "road": road,
                "stage": worst_stage,
                "passable": passable,
                "facility_count": len(facs),
                "active_count": active,
                "pump_offline": any_pump_offline,
                "risk_level": ["低", "中", "高"][worst_idx],
            }
        st.road_board = new_board

    @staticmethod
    def _alert_dict(alert: Alert) -> dict[str, Any]:
        data = vars(alert)
        data["root_cause_text"] = ROOT_CAUSE_TEXT.get(alert.root_cause or "", alert.root_cause)
        return data
