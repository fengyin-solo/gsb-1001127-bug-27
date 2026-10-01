"""告警对账台业务规则测试：不依赖 FastAPI，直接用标准库跑。

运行：cd backend && python3 -m unittest app.tests.test_reconciliation -v
"""
from __future__ import annotations

import threading
import unittest

from app.services.reconciliation import (
    reconciliation as svc,
)
from app.store import store


class ReconciliationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        # 每个用例用一份全新的内存仓库，避免种子态相互干扰
        store.__init__()  # type: ignore[misc]

    # 1. 初始脏数据：离线告警已到，风险面却还可通行，且三源阶段不一致
    def test_seed_has_stale_passable_surface(self) -> None:
        fac = svc.get_facility(1)
        self.assertEqual(fac["可通行性"], "可通行")  # 待重建的旧版脏数据
        self.assertTrue(fac["阶段差异"])
        self.assertIn("风险面", fac["阶段差异"])

    # 2. 重建后：泵站离线 => 不可通行，三源对账，看板/台账/防汛清单同事务回写
    def test_rebuild_makes_offline_impassable_and_writebacks(self) -> None:
        task, msg = svc.rebuild_facility(1)
        self.assertEqual(task["status"], "成功", msg)
        fac = svc.get_facility(1)
        self.assertEqual(fac["可通行性"], "不可通行")
        self.assertTrue(fac["泵站离线"])
        self.assertEqual(fac["风险阶段"], "超警戒")
        self.assertEqual(fac["对账状态"], "已对齐")  # 重建后三源一致
        self.assertIn("泵站离线", fac["根因"])

        # 旧风险面被冻结、可回溯，不是覆盖归零
        surfaces = [s for s in store.rows("rec_surface") if int(s["facility_id"]) == 1]
        self.assertEqual(surfaces[0]["passability"], "可通行")
        self.assertFalse(surfaces[0]["active"])
        self.assertTrue(surfaces[-1]["active"])

        # 排水台账回写
        drai = next(r for r in store.rows("drainage") if r["设施编号"] == "DRAI-1001")
        self.assertEqual(drai["可通行性"], "不可通行")
        self.assertIn("泵站离线", drai["根因"])
        self.assertTrue(drai["abnormal"])

        # 防汛清单回写
        floo = next(r for r in store.rows("flood") if r["影响路段"] == "河埠路")
        self.assertEqual(floo["status"], "响应中")

        # 路段风险看板空间聚合
        board = next(r for r in svc.board() if r["路段"] == "河埠路")
        self.assertEqual(board["可通行性"], "不可通行")
        self.assertEqual(board["泵站离线数"], 1)

    # 3. 重建任务按设施版本幂等
    def test_rebuild_is_idempotent_per_facility_version(self) -> None:
        first, _ = svc.rebuild_facility(1)
        second, _ = svc.rebuild_facility(1)
        self.assertEqual(first["status"], "成功")
        self.assertEqual(second["status"], "幂等跳过")
        surfaces = [s for s in store.rows("rec_surface") if int(s["facility_id"]) == 1]
        self.assertEqual(sum(1 for s in surfaces if s["active"]), 1)

    # 4. 重算失败保留上一版风险面，不归零；任务留失败痕迹
    def test_failed_rebuild_keeps_previous_surface(self) -> None:
        svc.rebuild_facility(1)
        before = svc.get_facility(1)["可通行性"]
        task, msg = svc.rebuild_facility(1, simulate_failure=True)
        # 同一版本幂等先命中，为制造失败先升版本
        self.assertEqual(task["status"], "幂等跳过")

        fac = store.find("rec_facility", 1)
        fac["version"] = 2
        task, msg = svc.rebuild_facility(1, simulate_failure=True)
        self.assertEqual(task["status"], "失败")
        self.assertIn("保留上一版风险面", msg)
        self.assertEqual(svc.get_facility(1)["可通行性"], before)
        # 事务回滚后看板仍是旧聚合，不出现空白行/归零
        board = next(r for r in svc.board() if r["路段"] == "河埠路")
        self.assertEqual(board["可通行性"], before)

    # 5. 同一泵站多次告警，以最近一次人工复测为准（即使采集时刻更晚的自动读数存在）
    def test_latest_manual_recheck_wins(self) -> None:
        svc.rebuild_facility(1)
        # 处置离线告警：现场复测 18cm（中水位），并恢复
        alarm, msg = svc.handle_alarm(1, {
            "action": "人工复测", "measured_level": 18,
            "measured_at": "2026-09-28 21:00:00", "operator": "张三",
        })
        self.assertIsNotNone(alarm, msg)
        self.assertEqual(alarm["status"], "已处置")
        self.assertEqual(svc.get_facility(1)["风险阶段"], "中水位")
        self.assertEqual(svc.get_facility(1)["可通行性"], "可通行")

        # 复测后再来一条更晚的自动采集（45cm 超警戒），结论仍以最近人工复测为准
        reading, msg = svc.ingest_reading(1, 45, "2026-09-28 21:30:00", "自动采集")
        self.assertIsNotNone(reading, msg)
        self.assertEqual(svc.get_facility(1)["风险阶段"], "中水位")
        self.assertIn("最近一次人工复测", svc.get_facility(1)["根因"])

        # 再来一次告警 + 更新的复测（33cm 高水位），最近复测生效
        alarm2, _ = svc.raise_alarm(1, "水位超标", "SCADA", "2026-09-28 21:35:00")
        svc.handle_alarm(alarm2["id"], {
            "action": "人工复测", "measured_level": 33,
            "measured_at": "2026-09-28 22:00:00", "operator": "李四",
        })
        self.assertEqual(svc.get_facility(1)["风险阶段"], "高水位")
        self.assertEqual(svc.get_facility(1)["可通行性"], "限速通行")

    # 6. 历史水位按采集时刻冻结：旧读数的阶段不随阈值/复测改变
    def test_readings_frozen_at_collection_time(self) -> None:
        svc.handle_alarm(1, {
            "action": "人工复测", "measured_level": 18,
            "measured_at": "2026-09-28 21:00:00",
        })
        detail = svc.get_facility(1)
        frozen = {r["collected_at"]: r["stage"] for r in detail["readings"]}
        self.assertEqual(frozen["2026-09-28 18:00:00"], "低水位")
        self.assertEqual(frozen["2026-09-28 20:00:00"], "高水位")
        self.assertTrue(all(r["frozen"] for r in detail["readings"]))

    # 7. 并发处置只保留一个有效结论
    def test_concurrent_disposition_keeps_single_conclusion(self) -> None:
        svc.rebuild_facility(1)
        outcomes: list[tuple[str, str]] = []

        def worker(level: float, name: str) -> None:
            _, msg = svc.handle_alarm(1, {
                "action": "人工复测", "measured_level": level,
                "measured_at": f"2026-09-28 21:{name}:00", "operator": name,
            })
            outcomes.append((name, msg))

        threads = [threading.Thread(target=worker, args=(v, f"{i:02d}"))
                   for i, v in enumerate((10, 42, 20), start=1)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        winners = [o for o in outcomes if o[1] == ""]
        losers = [o for o in outcomes if o[1]]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(losers), 2)
        self.assertTrue(all("一个有效结论" in m for _, m in losers))
        alarm = store.find("rec_alarm", 1)
        self.assertEqual(alarm["status"], "已处置")
        self.assertIsNotNone(alarm["closed_by_reading_id"])

    # 8. 空设施号迁移归并到主档案；已存在设施升版本
    def test_blank_code_migration_merges_into_master(self) -> None:
        report, errors = svc.migrate_facilities([
            {"设施编号": "", "设施类型": "泵站", "所属路段": "河埠路",
             "桩号位置": "K4+020", "临时编号": "TMP-A"},
            {"设施编号": "", "设施类型": "泵站", "所属路段": "河埠路",
             "桩号位置": "K4+020", "临时编号": "TMP-B"},
            {"设施编号": "", "设施类型": "雨水口", "所属路段": "新欣路",
             "桩号位置": "K0+900", "临时编号": "TMP-C"},
            {"设施编号": "PS-001", "设施类型": "泵站", "所属路段": "河埠路",
             "桩号位置": "K4+020"},
        ])
        self.assertEqual(errors, [])
        ps001 = next(f for f in svc.list_facilities() if f["设施编号"] == "PS-001")
        # 两条空号都归并到同一个主档案，且没有新建空号设施
        self.assertEqual(ps001["version"], 2)
        self.assertIn("TMP-A", ps001["merged_from"])
        self.assertIn("TMP-B", ps001["merged_from"])
        self.assertTrue(any(f["所属路段"] == "新欣路" for f in svc.list_facilities()))
        actions = {r["设施编号"]: r["动作"] for r in report}
        self.assertEqual(actions["PS-001"], "匹配主档案（无变更）")

    # 9. 设施升版本后，新版本可以再重建一次（版本幂等而不是全局幂等）
    def test_rebuild_runs_again_after_version_bump(self) -> None:
        first, _ = svc.rebuild_facility(2)
        skipped, _ = svc.rebuild_facility(2)
        store.find("rec_facility", 2)["version"] = 2
        again, msg = svc.rebuild_facility(2)
        self.assertEqual(first["status"], "成功")
        self.assertEqual(skipped["status"], "幂等跳过")
        self.assertEqual(again["status"], "成功", msg)

    # 10. 处置后处置通知核销、防汛事件结束，根因回灌排水列表与详情
    def test_disposition_closes_notice_and_event_and_root_cause_flows(self) -> None:
        svc.rebuild_facility(1)
        svc.handle_alarm(1, {"action": "现场恢复", "measured_at": "2026-09-28 22:00:00"})
        self.assertTrue(all(n["resolved"] for n in store.rows("rec_notice")))
        self.assertTrue(all(e["status"] == "已结束" for e in store.rows("rec_flood_event")))

        from app.services.drainage import DrainageService

        items, total = DrainageService().list_entries(keyword="DRAI-1001")
        self.assertEqual(total, 1)
        self.assertIn("现场确认", items[0]["根因"])
        detail = DrainageService().get_entry(101)
        self.assertEqual(detail["可通行性"], "可通行")
        self.assertEqual(detail["根因"], items[0]["根因"])  # 列表与详情同口径

    # 11. 空间聚合同事务：同路段多设施按最差阶段/通行性聚合
    def test_board_aggregates_worst_in_same_transaction(self) -> None:
        # 滨江路原有低水位雨水口，新增一个超警戒雨水口并建档+采集+告警
        report, _ = svc.migrate_facilities([
            {"设施编号": "YSK-009", "设施类型": "雨水口", "所属路段": "滨江路",
             "桩号位置": "K0+600"},
        ])
        fid = next(f["id"] for f in svc.list_facilities() if f["设施编号"] == "YSK-009")
        svc.ingest_reading(fid, 45, "2026-09-29 08:00:00", "自动采集")
        board = next(r for r in svc.board() if r["路段"] == "滨江路")
        self.assertEqual(board["风险阶段"], "超警戒")
        self.assertEqual(board["可通行性"], "不可通行")
        self.assertEqual(board["纳入设施数"], 2)
        # 防汛清单同事务更新为该路段最差阶段
        floo = next(r for r in store.rows("flood") if r["影响路段"] == "滨江路")
        self.assertEqual(floo["积水深度"], "超警戒")

    # 12. 高水位 => 限速通行；低/中水位 => 可通行
    def test_passability_levels(self) -> None:
        from app.services.reconciliation import passability_of, stage_of

        self.assertEqual(stage_of(10), "低水位")
        self.assertEqual(stage_of(20), "中水位")
        self.assertEqual(stage_of(30), "高水位")
        self.assertEqual(stage_of(50), "超警戒")
        self.assertEqual(passability_of("高水位"), "限速通行")
        self.assertEqual(passability_of("中水位", pump_offline=True), "不可通行")


if __name__ == "__main__":
    unittest.main()
