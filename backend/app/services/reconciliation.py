"""告警对账台业务规则。

把四件原来各算各的东西收拢到同一套口径里：

1. 风险面、处置通知、防汛事件三处的水位阶段以对账结果为准；
2. 泵站离线期间一律不可通行，修掉“离线后仍显示可通行”的旧问题；
3. 同一泵站多次告警，以最近一次人工复测为准，历史水位按采集时刻冻结；
4. 现场处置在同一事务里回写排水台账、防汛清单和路段风险看板，空间聚合同事务完成；
5. 并发处置只保留一个有效结论；重算失败保留上一版风险面，绝不归零；
6. 重建任务按设施版本幂等；空设施号迁移时归并到主档案。
"""
from __future__ import annotations

import threading
from copy import deepcopy
from datetime import datetime
from typing import Any

from app.store import store

# ---- 对账域的表 ----------------------------------------------------------------
FACILITY_TABLE = "rec_facility"      # 排水设施主档案（泵站/雨水口/排水管渠）
READING_TABLE = "rec_reading"        # 历史水位，采集即冻结
ALARM_TABLE = "rec_alarm"            # 告警
NOTICE_TABLE = "rec_notice"          # 处置通知
FLOOD_EVENT_TABLE = "rec_flood_event"  # 防汛事件
SURFACE_TABLE = "rec_surface"        # 路段风险面（带版本，旧版可回溯）
TASK_TABLE = "rec_rebuild_task"      # 重建任务
BOARD_TABLE = "road_risk"            # 路段风险看板

ALL_TABLES = (
    FACILITY_TABLE, READING_TABLE, ALARM_TABLE, NOTICE_TABLE, FLOOD_EVENT_TABLE,
    SURFACE_TABLE, TASK_TABLE, BOARD_TABLE, "drainage", "flood",
)

# 水位阶段按厘米划档；阶段在“采集时刻”就冻结，后续不重算
STAGES = ["低水位", "中水位", "高水位", "超警戒"]
STAGE_BOUNDS = (15.0, 27.0, 40.0)
PASS_ORDER = ["可通行", "限速通行", "不可通行"]
ALARM_TYPES = {"水位超标", "泵站离线"}
FACILITY_TYPES = {"泵站": "PS", "雨水口": "YSK", "排水管渠": "PSG"}


def stage_of(level_cm: float) -> str:
    for bound, stage in zip(STAGE_BOUNDS, STAGES):
        if level_cm < bound:
            return stage
    return STAGES[-1]


def passability_of(stage: str, pump_offline: bool = False) -> str:
    if pump_offline or stage == "超警戒":
        return "不可通行"
    if stage == "高水位":
        return "限速通行"
    return "可通行"


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _new_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


class ReconciliationService:
    def __init__(self) -> None:
        self._facility_locks: dict[int, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _facility_lock(self, facility_id: int) -> threading.Lock:
        with self._locks_guard:
            if facility_id not in self._facility_locks:
                self._facility_locks[facility_id] = threading.Lock()
            return self._facility_locks[facility_id]

    # ---- 查询 ----------------------------------------------------------------
    def list_facilities(self) -> list[dict[str, Any]]:
        rows = store.rows(FACILITY_TABLE)
        return [self._facility_view(row) for row in sorted(rows, key=lambda r: int(r["id"]))]

    def get_facility(self, facility_id: int) -> dict[str, Any] | None:
        fac = store.find(FACILITY_TABLE, facility_id)
        if fac is None:
            return None
        detail = self._facility_view(fac)
        readings = [dict(r) for r in store.rows(READING_TABLE)
                    if int(r["facility_id"]) == facility_id]
        alarms = [dict(a) for a in store.rows(ALARM_TABLE)
                  if int(a["facility_id"]) == facility_id]
        notices = [dict(n) for n in store.rows(NOTICE_TABLE)
                   if int(n["facility_id"]) == facility_id]
        events = [dict(e) for e in store.rows(FLOOD_EVENT_TABLE)
                  if int(e["facility_id"]) == facility_id]
        detail["readings"] = sorted(readings, key=lambda r: str(r["collected_at"]))
        detail["alarms"] = sorted(alarms, key=lambda a: str(a["alarm_time"]))
        detail["notices"] = sorted(notices, key=lambda n: str(n["notified_at"]))
        detail["flood_events"] = sorted(events, key=lambda e: str(e["event_time"]))
        detail["tasks"] = [
            dict(t) for t in sorted(store.rows(TASK_TABLE), key=lambda t: int(t["id"]))
            if int(t["facility_id"]) == facility_id
        ]
        return detail

    def _active_surface(self, facility_id: int) -> dict[str, Any] | None:
        actives = [s for s in store.rows(SURFACE_TABLE)
                   if int(s["facility_id"]) == facility_id and s.get("active")]
        return actives[-1] if actives else None

    def _facility_view(self, fac: dict[str, Any]) -> dict[str, Any]:
        """主档案 + 当前风险面 + 三源对账状态。根因从这里向排水列表/详情透出。"""
        view = dict(fac)
        surface = self._active_surface(int(fac["id"]))
        active_alarms = [a for a in store.rows(ALARM_TABLE)
                         if int(a["facility_id"]) == int(fac["id"]) and a["status"] == "待处置"]
        view["active_alarm_count"] = len(active_alarms)
        if surface:
            view["风险阶段"] = surface["stage"]
            view["可通行性"] = surface["passability"]
            view["泵站离线"] = surface["pump_offline"]
            view["根因"] = surface["root_cause"]
            view["风险面版本"] = surface["version"]
        mismatch = self._stage_mismatch(int(fac["id"]), surface)
        view["对账状态"] = "三源阶段不一致" if mismatch else "已对齐"
        view["阶段差异"] = mismatch
        return view

    def _stage_mismatch(
        self, facility_id: int, surface: dict[str, Any] | None
    ) -> dict[str, str]:
        """风险面 / 处置通知 / 防汛事件 三处水位阶段是否一致。"""
        surface_stage = surface["stage"] if surface else None
        notices = [n for n in store.rows(NOTICE_TABLE)
                   if int(n["facility_id"]) == facility_id and not n.get("resolved")]
        events = [e for e in store.rows(FLOOD_EVENT_TABLE)
                  if int(e["facility_id"]) == facility_id and e["status"] != "已结束"]
        notice_stage = notices[-1]["stage"] if notices else None
        event_stage = events[-1]["stage"] if events else None
        stages = {"风险面": surface_stage, "处置通知": notice_stage, "防汛事件": event_stage}
        present = {k: v for k, v in stages.items() if v}
        if len(set(present.values())) > 1:
            return stages
        return {}

    def list_alarms(self, status: str | None = None) -> list[dict[str, Any]]:
        items = []
        for alarm in sorted(store.rows(ALARM_TABLE), key=lambda a: int(a["id"])):
            if status and alarm["status"] != status:
                continue
            fac = store.find(FACILITY_TABLE, int(alarm["facility_id"]))
            item = dict(alarm)
            item["设施编号"] = fac["设施编号"] if fac else "（主档案缺失）"
            item["所属路段"] = fac["所属路段"] if fac else ""
            item["设施类型"] = fac["设施类型"] if fac else ""
            items.append(item)
        return items

    def list_tasks(self) -> list[dict[str, Any]]:
        return [dict(t) for t in sorted(store.rows(TASK_TABLE), key=lambda t: int(t["id"]))]

    def board(self) -> list[dict[str, Any]]:
        return [dict(r) for r in sorted(store.rows(BOARD_TABLE), key=lambda r: str(r["路段"]))]

    def overview(self) -> dict[str, Any]:
        facilities = store.rows(FACILITY_TABLE)
        surfaces = [self._active_surface(int(f["id"])) for f in facilities]
        surfaces = [s for s in surfaces if s]
        pending_alarms = [a for a in store.rows(ALARM_TABLE) if a["status"] == "待处置"]
        offline = [s for s in surfaces if s.get("pump_offline")]
        blocked = [s for s in surfaces if s["passability"] == "不可通行"]
        mismatch_facilities = [
            int(f["id"]) for f in facilities
            if self._stage_mismatch(int(f["id"]), self._active_surface(int(f["id"])))
        ]
        cards = [
            {"label": "纳管设施", "value": len(facilities)},
            {"label": "待处置告警", "value": len(pending_alarms)},
            {"label": "泵站离线", "value": len(offline)},
            {"label": "不可通行点位", "value": len(blocked)},
            {"label": "三源阶段不一致", "value": len(mismatch_facilities)},
        ]
        return {"cards": cards, "facilities": self.list_facilities()}

    def root_cause_for_code(self, facility_code: str) -> dict[str, Any]:
        """供排水设施列表/详情复用：同一根因口径，不在两处各算一遍。"""
        for fac in store.rows(FACILITY_TABLE):
            if fac.get("设施编号") == facility_code or fac.get("drainage_code") == facility_code:
                view = self._facility_view(fac)
                return {
                    "根因": view.get("根因"),
                    "风险阶段": view.get("风险阶段"),
                    "可通行性": view.get("可通行性"),
                    "泵站离线": view.get("泵站离线"),
                    "风险面版本": view.get("风险面版本"),
                    "对账状态": view.get("对账状态"),
                }
        return {}

    # ---- 采集与告警 ----------------------------------------------------------
    def ingest_reading(
        self, facility_id: int, level_cm: float, collected_at: str, source: str
    ) -> tuple[dict[str, Any] | None, str]:
        fac = store.find(FACILITY_TABLE, facility_id)
        if fac is None:
            return None, f"设施 {facility_id} 不在主档案中，水位不予采集"
        reading = {
            "id": _new_id(store.rows(READING_TABLE)),
            "facility_id": facility_id,
            "collected_at": collected_at,
            "level_cm": float(level_cm),
            # 阶段在采集时刻冻结，历史记录永不重算
            "stage": stage_of(float(level_cm)),
            "source": source,
            "frozen": True,
        }
        with self._facility_lock(facility_id), store.transaction(*ALL_TABLES):
            store.rows(READING_TABLE).append(reading)
            self._rebuild_surface_locked(fac, trigger=f"{source}水位入库")
        return reading, ""

    def raise_alarm(
        self,
        facility_id: int,
        alarm_type: str,
        source: str,
        alarm_time: str,
        stage: str | None = None,
        notice_stage: str | None = None,
        flood_stage: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        fac = store.find(FACILITY_TABLE, facility_id)
        if fac is None:
            return None, f"设施 {facility_id} 不在主档案中，告警无法挂接"
        if alarm_type not in ALARM_TYPES:
            return None, f"告警类型「{alarm_type}」不在对账范围"
        if stage is None:
            latest = self._latest_reading(facility_id, alarm_time)
            stage = latest["stage"] if latest else STAGES[0]
        notice_stage = notice_stage or stage
        flood_stage = flood_stage or stage
        with self._facility_lock(facility_id), store.transaction(*ALL_TABLES):
            alarm = {
                "id": _new_id(store.rows(ALARM_TABLE)),
                "facility_id": facility_id,
                "alarm_type": alarm_type,
                "source": source,
                "alarm_time": alarm_time,
                "stage": stage,
                "status": "待处置",
                "conclusion": None,
                "root_cause": None,
                "closed_by_reading_id": None,
                "disposed_at": None,
            }
            store.rows(ALARM_TABLE).append(alarm)
            store.rows(NOTICE_TABLE).append({
                "id": _new_id(store.rows(NOTICE_TABLE)),
                "alarm_id": alarm["id"],
                "facility_id": facility_id,
                "stage": notice_stage,
                "passability": passability_of(notice_stage, alarm_type == "泵站离线"),
                "notified_at": alarm_time,
                "resolved": False,
            })
            store.rows(FLOOD_EVENT_TABLE).append({
                "id": _new_id(store.rows(FLOOD_EVENT_TABLE)),
                "alarm_id": alarm["id"],
                "facility_id": facility_id,
                "road": fac["所属路段"],
                "stage": flood_stage,
                "water_depth": flood_stage,
                "status": "待响应",
                "event_time": alarm_time,
                "closed_at": None,
            })
            # 告警入库即重算风险面：泵站离线不允许再出现“可通行”
            self._rebuild_surface_locked(fac, trigger=f"告警入库：{alarm_type}")
        return alarm, ""

    # ---- 现场处置（对账台核心动作）-------------------------------------------
    def handle_alarm(
        self, alarm_id: int, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str]:
        alarm = store.find(ALARM_TABLE, alarm_id)
        if alarm is None:
            return None, f"告警 {alarm_id} 不存在"
        facility_id = int(alarm["facility_id"])
        fac = store.find(FACILITY_TABLE, facility_id)
        action = str(values.get("action") or "人工复测").strip()
        operator = str(values.get("operator") or "现场班组").strip()
        measured_at = str(values.get("measured_at") or now_text()).strip()

        # 并发处置：设施级互斥，后到的结论不能覆盖先落定的有效结论
        with self._facility_lock(facility_id):
            if alarm["status"] == "已处置":
                return None, (
                    f"告警 {alarm_id} 已有有效结论（{alarm.get('conclusion')}），"
                    "同一告警只保留一个有效结论"
                )
            reading: dict[str, Any] | None = None
            if action == "现场恢复":
                conclusion_stage = "低水位"
                conclusion = "可通行"
                # 现场恢复同样是人工结论：落一条复测记录（默认积水退去 0cm），
                # 让它成为“最近一次人工复测”，而不是让旧自动读数重新说了算
                raw = values.get("measured_level", 0)
                try:
                    level_cm = float(raw)
                except (TypeError, ValueError):
                    return None, "复测水位必须是数字（厘米）"
                root_cause = (
                    f"现场确认泵站恢复运行、积水退去（复测水位 {level_cm:g}cm，"
                    f"处置人：{operator}）"
                )
                reading = {
                    "id": _new_id(store.rows(READING_TABLE)),
                    "facility_id": facility_id,
                    "collected_at": measured_at,
                    "level_cm": level_cm,
                    "stage": conclusion_stage,
                    "source": "人工复测",
                    "note": "现场恢复确认",
                    "frozen": True,
                }
            elif action == "人工复测":
                raw = values.get("measured_level")
                try:
                    level_cm = float(raw)
                except (TypeError, ValueError):
                    return None, "人工复测必须填报复测水位（厘米）"
                conclusion_stage = stage_of(level_cm)
                conclusion = passability_of(conclusion_stage)
                root_cause = (
                    f"最近一次人工复测水位 {level_cm:g}cm（{conclusion_stage}），"
                    f"多源告警以此为准（复测人：{operator}）"
                )
                reading = {
                    "id": _new_id(store.rows(READING_TABLE)),
                    "facility_id": facility_id,
                    "collected_at": measured_at,
                    "level_cm": level_cm,
                    "stage": conclusion_stage,
                    "source": "人工复测",
                    "note": "",
                    "frozen": True,
                }
            else:
                return None, f"处置动作「{action}」不在对账台范围"

            # 处置结论、复测水位、通知核销、防汛事件、台账回写、看板聚合：一个事务
            with store.transaction(*ALL_TABLES):
                if reading is not None:
                    store.rows(READING_TABLE).append(reading)
                    alarm["closed_by_reading_id"] = reading["id"]
                alarm["status"] = "已处置"
                alarm["conclusion"] = conclusion
                alarm["root_cause"] = root_cause
                alarm["disposed_at"] = measured_at
                for notice in store.rows(NOTICE_TABLE):
                    if int(notice.get("alarm_id", 0)) == alarm_id:
                        notice["resolved"] = True
                        notice["stage"] = conclusion_stage
                        notice["passability"] = conclusion
                for event in store.rows(FLOOD_EVENT_TABLE):
                    if int(event.get("alarm_id", 0)) == alarm_id:
                        event["status"] = "已结束"
                        event["stage"] = conclusion_stage
                        event["water_depth"] = conclusion_stage
                        event["closed_at"] = measured_at
                self._rebuild_surface_locked(fac, trigger=f"现场处置·{action}")
            return dict(alarm), ""

    # ---- 重建任务（按设施版本幂等）-------------------------------------------
    def rebuild_facility(
        self, facility_id: int, simulate_failure: bool = False
    ) -> tuple[dict[str, Any] | None, str]:
        fac = store.find(FACILITY_TABLE, facility_id)
        if fac is None:
            return None, f"设施 {facility_id} 不在主档案中，无法重建"
        version = int(fac["version"])
        with self._facility_lock(facility_id):
            done = [
                t for t in store.rows(TASK_TABLE)
                if int(t["facility_id"]) == facility_id
                and int(t["facility_version"]) == version
                and t["status"] == "成功"
            ]
            if done:
                task = dict(done[-1])
                task["status"] = "幂等跳过"
                task["detail"] = f"设施版本 v{version} 已重建过，沿用既有风险面，不重复落版"
                return task, ""

            task = {
                "id": _new_id(store.rows(TASK_TABLE)),
                "facility_id": facility_id,
                "facility_version": version,
                "status": "进行中",
                "reason": "手动重建",
                "detail": "",
                "created_at": now_text(),
            }
            # 任务行先于事务落库：事务回滚后任务仍要留下失败痕迹
            store.rows(TASK_TABLE).append(task)
            try:
                with store.transaction(*ALL_TABLES):
                    if simulate_failure:
                        raise RuntimeError("风险面重算失败（模拟）：空间聚合未完成")
                    self._rebuild_surface_locked(fac, trigger="重建任务")
                    task["status"] = "成功"
                    active = self._active_surface(facility_id)
                    task["detail"] = (
                        f"已按设施版本 v{version} 重算风险面"
                        f"（{active['stage']}·{active['passability']}），三源已对账并回写台账"
                    )
            except Exception as exc:  # 重算失败：事务整体回滚，上一版风险面原样保留
                task["status"] = "失败"
                task["detail"] = f"{exc}；已保留上一版风险面，未归零"
                return dict(task), task["detail"]
            return dict(task), ""

    # ---- 空设施号迁移归并 ----------------------------------------------------
    def migrate_facilities(
        self, items: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[str]]:
        report: list[dict[str, Any]] = []
        merged_facility_ids: set[int] = set()
        with store.transaction(*ALL_TABLES):
            for item in items:
                code = str(item.get("设施编号") or "").strip()
                ftype = str(item.get("设施类型") or "").strip()
                road = str(item.get("所属路段") or "").strip()
                stake = str(item.get("桩号位置") or "").strip()
                if ftype not in FACILITY_TYPES:
                    return [], [f"设施类型「{ftype}」不在主档案分类中"]
                existing_by_code = next(
                    (f for f in store.rows(FACILITY_TABLE) if f.get("设施编号") == code),
                    None,
                ) if code else None
                if not code:
                    # 空设施号：同类型+同路段+同桩号即视为同一主档案，归并不重建
                    main = next(
                        (f for f in store.rows(FACILITY_TABLE)
                         if f["设施类型"] == ftype and f["所属路段"] == road
                         and f["桩号位置"] == stake),
                        None,
                    )
                    temp_code = str(item.get("临时编号") or f"空号-{road}-{stake}")
                    if main is not None:
                        # 同一批迁移里多条空号并入同一主档案，版本只升一次
                        if int(main["id"]) not in merged_facility_ids:
                            main["version"] = int(main["version"]) + 1
                            merged_facility_ids.add(int(main["id"]))
                        main.setdefault("merged_from", [])
                        if temp_code not in main["merged_from"]:
                            main["merged_from"].append(temp_code)
                        report.append({
                            "设施编号": main["设施编号"], "动作": "归并主档案",
                            "设施版本": main["version"], "并入记录": temp_code,
                        })
                        continue
                    seq = _new_id(store.rows(FACILITY_TABLE))
                    code = f"{FACILITY_TYPES[ftype]}-{seq:03d}"
                    fac = self._new_facility(code, ftype, road, stake, merged_from=[temp_code])
                    report.append({
                        "设施编号": code, "动作": "空号建主档案",
                        "设施版本": fac["version"], "并入记录": temp_code,
                    })
                    continue
                if existing_by_code is not None:
                    changed = any(existing_by_code.get(k) != v
                                  for k, v in (("设施类型", ftype), ("所属路段", road),
                                               ("桩号位置", stake)) if v)
                    existing_by_code["设施类型"] = ftype or existing_by_code["设施类型"]
                    existing_by_code["所属路段"] = road or existing_by_code["所属路段"]
                    existing_by_code["桩号位置"] = stake or existing_by_code["桩号位置"]
                    if changed:
                        existing_by_code["version"] = int(existing_by_code["version"]) + 1
                    report.append({
                        "设施编号": code,
                        "动作": "更新主档案（版本+1）" if changed else "匹配主档案（无变更）",
                        "设施版本": existing_by_code["version"], "并入记录": "",
                    })
                else:
                    fac = self._new_facility(code, ftype, road, stake)
                    report.append({
                        "设施编号": code, "动作": "新建主档案",
                        "设施版本": fac["version"], "并入记录": "",
                    })
        return report, []

    def _new_facility(
        self, code: str, ftype: str, road: str, stake: str,
        merged_from: list[str] | None = None,
    ) -> dict[str, Any]:
        fac = {
            "id": _new_id(store.rows(FACILITY_TABLE)),
            "version": 1,
            "设施编号": code,
            "设施类型": ftype,
            "所属路段": road,
            "桩号位置": stake,
            "merged_from": merged_from or [],
            "updated_at": now_text(),
        }
        store.rows(FACILITY_TABLE).append(fac)
        return fac

    # ---- 风险面重算（纯计算 + 同事务落库/回写/聚合）---------------------------
    def _latest_reading(
        self, facility_id: int, before: str | None = None
    ) -> dict[str, Any] | None:
        rows = [
            r for r in store.rows(READING_TABLE)
            if int(r["facility_id"]) == facility_id
            and (before is None or str(r["collected_at"]) <= before)
        ]
        return sorted(rows, key=lambda r: str(r["collected_at"]))[-1] if rows else None

    def _compute_surface(self, facility_id: int) -> dict[str, Any]:
        active_alarms = [
            a for a in store.rows(ALARM_TABLE)
            if int(a["facility_id"]) == facility_id and a["status"] == "待处置"
        ]
        readings = sorted(
            (r for r in store.rows(READING_TABLE) if int(r["facility_id"]) == facility_id),
            key=lambda r: str(r["collected_at"]),
        )
        manuals = [r for r in readings if r["source"] == "人工复测"]
        offline = any(a["alarm_type"] == "泵站离线" for a in active_alarms)

        basis = ""
        if offline:
            stage, passability = STAGES[-1], "不可通行"
            latest_offline = max(
                (a["alarm_time"] for a in active_alarms if a["alarm_type"] == "泵站离线"),
                default="",
            )
            root_cause = f"泵站离线（最近离线告警 {latest_offline}），离线期间无法强排，按不可通行兜底"
        elif manuals:
            chosen = manuals[-1]  # 多次告警时以最近一次人工复测为准
            stage, passability = chosen["stage"], passability_of(chosen["stage"])
            basis = "最近一次人工复测"
            if chosen.get("note") == "现场恢复确认":
                root_cause = (
                    f"现场确认泵站恢复运行、积水退去（复测水位 "
                    f"{chosen['level_cm']:g}cm，采集时刻 {chosen['collected_at']}）；"
                    "历史水位按采集时刻冻结，多源告警以此为准"
                )
            else:
                root_cause = (
                    f"最近一次人工复测水位 {chosen['level_cm']:g}cm（{stage}，"
                    f"采集时刻 {chosen['collected_at']}）；历史水位按采集时刻冻结，多源告警以此为准"
                )
        elif readings:
            chosen = readings[-1]
            stage, passability = chosen["stage"], passability_of(chosen["stage"])
            basis = "最近一次自动采集"
            root_cause = (
                f"自动采集水位 {chosen['level_cm']:g}cm（{stage}，"
                f"采集时刻 {chosen['collected_at']}）"
            )
        else:
            stage, passability = STAGES[0], "可通行"
            root_cause = "暂无水位采集，默认低水位可通行"

        mismatch = self._stage_mismatch(
            facility_id, self._active_surface(facility_id)
        )
        if mismatch and not offline and not manuals:
            root_cause += "；风险面/处置通知/防汛事件阶段不一致，已按最近采集口径强制对账"
        return {
            "stage": stage,
            "passability": passability,
            "pump_offline": offline,
            "basis": basis,
            "root_cause": root_cause,
            "frozen_readings": deepcopy(readings),
            "active_alarm_ids": [int(a["id"]) for a in active_alarms],
        }

    def _rebuild_surface_locked(
        self, fac: dict[str, Any], *, trigger: str
    ) -> dict[str, Any]:
        """必须在设施锁 + store.transaction 内调用。"""
        facility_id = int(fac["id"])
        data = self._compute_surface(facility_id)
        surface_rows = store.rows(SURFACE_TABLE)
        new_id = _new_id(surface_rows)
        for old in surface_rows:
            if int(old["facility_id"]) == facility_id and old.get("active"):
                old["active"] = False
                old["superseded_by"] = new_id
        surface = {
            "id": new_id,
            "facility_id": facility_id,
            "road": fac["所属路段"],
            "version": int(fac["version"]),
            "stage": data["stage"],
            "passability": data["passability"],
            "pump_offline": data["pump_offline"],
            "basis": data["basis"],
            "root_cause": data["root_cause"],
            "frozen_readings": data["frozen_readings"],
            "active": True,
            "superseded_by": None,
            "trigger": trigger,
            "created_at": now_text(),
        }
        surface_rows.append(surface)
        # 三源对账：未核销的处置通知、未结束的防汛事件，阶段强制对齐到重算结果
        for notice in store.rows(NOTICE_TABLE):
            if int(notice["facility_id"]) == facility_id and not notice.get("resolved"):
                notice["stage"] = data["stage"]
                notice["passability"] = data["passability"]
        for event in store.rows(FLOOD_EVENT_TABLE):
            if int(event["facility_id"]) == facility_id and event["status"] != "已结束":
                event["stage"] = data["stage"]
                event["water_depth"] = data["stage"]
        # 处置结果同事务回写排水台账、防汛清单
        self._writeback_drainage(fac, surface)
        self._writeback_flood(fac, surface)
        # 路段空间聚合同事务完成
        self._aggregate_board()
        return surface

    def _writeback_drainage(
        self, fac: dict[str, Any], surface: dict[str, Any]
    ) -> None:
        rows = store.rows("drainage")
        row = next(
            (r for r in rows
             if r.get("设施编号") in {fac["设施编号"], fac.get("drainage_code")}),
            None,
        )
        status_map = {"不可通行": "损坏", "限速通行": "淤积", "可通行": "正常"}
        status = status_map[surface["passability"]]
        state_text = (
            f"{surface['stage']}·{surface['passability']}"
            + ("·泵站离线" if surface["pump_offline"] else "")
        )
        payload = {
            "设施类型": fac["设施类型"],
            "所属路段": fac["所属路段"],
            "桩号位置": fac["桩号位置"],
            "设施状态": state_text,
            "status": status,
            "pending": surface["passability"] != "可通行",
            "abnormal": surface["passability"] == "不可通行",
            "风险阶段": surface["stage"],
            "可通行性": surface["passability"],
            "根因": surface["root_cause"],
            "风险面版本": surface["version"],
        }
        if row is None:
            code = fac.get("drainage_code") or fac["设施编号"]
            row = {"id": _new_id(rows), "设施编号": code}
            rows.append(row)
            fac["drainage_code"] = code
        row.update(payload)

    def _writeback_flood(self, fac: dict[str, Any], surface: dict[str, Any]) -> None:
        road = fac["所属路段"]
        facility_ids_on_road = [
            int(f["id"]) for f in store.rows(FACILITY_TABLE) if f["所属路段"] == road
        ]
        active_alarms = [
            a for a in store.rows(ALARM_TABLE)
            if int(a["facility_id"]) in facility_ids_on_road and a["status"] == "待处置"
        ]
        surfaces = [s for fid in facility_ids_on_road for s in
                    [self._active_surface(fid)] if s]
        if not surfaces and not active_alarms:
            return
        worst_stage = max(
            (STAGES.index(s["stage"]) for s in surfaces), default=0
        )
        worst_stage_text = STAGES[worst_stage]
        worst_pass = max(
            (PASS_ORDER.index(s["passability"]) for s in surfaces), default=0
        )
        worst_pass_text = PASS_ORDER[worst_pass]
        level_map = {"低水位": "蓝色", "中水位": "黄色", "高水位": "橙色", "超警戒": "红色"}

        rows = store.rows("flood")
        row = next((r for r in rows if r.get("影响路段") == road), None)
        if row is None:
            row = {"id": _new_id(rows), "记录编号": f"FLOO-{_new_id(rows):04d}",
                   "影响路段": road}
            rows.append(row)
        row.update({
            "预警级别": level_map[worst_stage_text],
            "积水深度": worst_stage_text,
            "防汛状态": "响应中" if active_alarms else "已结束",
            "status": "响应中" if active_alarms else "已结束",
            "pending": bool(active_alarms),
            "abnormal": worst_pass_text == "不可通行",
            "风险阶段": worst_stage_text,
            "可通行性": worst_pass_text,
        })

    def _aggregate_board(self) -> None:
        rows = store.rows(BOARD_TABLE)
        by_road: dict[str, list[dict[str, Any]]] = {}
        for fac in store.rows(FACILITY_TABLE):
            surface = self._active_surface(int(fac["id"]))
            if surface:
                by_road.setdefault(fac["所属路段"], []).append(surface)
        for road, surfaces in by_road.items():
            worst_stage = STAGES[max(STAGES.index(s["stage"]) for s in surfaces)]
            worst_pass = PASS_ORDER[max(PASS_ORDER.index(s["passability"]) for s in surfaces)]
            worst_surface = next(s for s in surfaces if s["passability"] == worst_pass)
            row = next((r for r in rows if r.get("路段") == road), None)
            payload = {
                "路段": road,
                "风险阶段": worst_stage,
                "可通行性": worst_pass,
                "泵站离线数": sum(1 for s in surfaces if s["pump_offline"]),
                "受影响设施数": sum(1 for s in surfaces if s["passability"] != "可通行"),
                "纳入设施数": len(surfaces),
                "根因摘要": worst_surface["root_cause"],
                "风险面版本": max(int(s["version"]) for s in surfaces),
                "更新时间": now_text(),
            }
            if row is None:
                row = {"id": _new_id(rows)}
                row.update(payload)
                rows.append(row)
            else:
                row.update(payload)


reconciliation = ReconciliationService()
