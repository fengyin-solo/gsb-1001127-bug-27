"""告警对账台 HTTP 端到端验证：用 ASGI transport 直打，无需起端口。

运行：cd backend && python3 -m app.tests.smoke_reconciliation
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {name}" + (f"  -> {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(name)


def post(path: str, values: dict | None = None):
    response = client.post(path, json={"values": values or {}})
    return response.status_code, response.json()


def get(path: str):
    response = client.get(path)
    return response.status_code, response.json()


# 0. 健康
status, payload = get("/api/health")
check("health", status == 200 and payload["ok"])

# 1. 初始态：河埠路离线告警待处置，但看板仍可通行（脏数据待重建）
_, overview = get("/api/reconciliation/overview")
check("overview has stale mismatch",
      next(c for c in overview["cards"] if c["label"] == "三源阶段不一致")["value"] >= 1)

_, board = get("/api/reconciliation/board")
hebu_before = next(r for r in board["items"] if r["路段"] == "河埠路")
check("stale board shows passable before rebuild", hebu_before["可通行性"] == "可通行")

# 2. 重建：离线 -> 不可通行，三源对齐，看板/台账/防汛清单回写
status, payload = post("/api/reconciliation/facilities/1/rebuild")
check("rebuild ok", status == 200 and payload["ok"], json.dumps(payload, ensure_ascii=False))

_, fac1 = get("/api/reconciliation/facilities/1")
check("offline => impassable after rebuild", fac1["可通行性"] == "不可通行")
check("three sources aligned", fac1["对账状态"] == "已对齐")
check("frozen readings preserved", len(fac1["readings"]) == 3)
check("root cause mentions offline", "泵站离线" in fac1["根因"])

_, board = get("/api/reconciliation/board")
hebu = next(r for r in board["items"] if r["路段"] == "河埠路")
check("board aggregated impassable", hebu["可通行性"] == "不可通行" and hebu["泵站离线数"] == 1)

# 排水台账回写
_, drain = get("/api/drainage?keyword=DRAI-1001")
row = drain["items"][0]
check("drainage ledger written back",
      row["可通行性"] == "不可通行" and "泵站离线" in row["根因"])
status, detail = get("/api/drainage/101")
check("drainage detail same root cause", status == 200 and detail["根因"] == row["根因"])

# 防汛清单回写
_, flood = get("/api/flood?keyword=")
flood_row = next(r for r in flood["items"] if r.get("影响路段") == "河埠路")
check("flood list written back", flood_row["status"] == "响应中" and flood_row["积水深度"] == "超警戒")

# 3. 版本幂等
_, again = post("/api/reconciliation/facilities/1/rebuild")
check("rebuild idempotent", again["entry"]["status"] == "幂等跳过")

# 4. 人工复测（18cm 中水位）
_, handled = post("/api/reconciliation/alarms/1/handle", {
    "action": "人工复测", "measured_level": 18,
    "measured_at": "2026-09-28 21:00:00", "operator": "张三",
})
check("handle alarm ok", handled["ok"], json.dumps(handled, ensure_ascii=False))
_, fac1 = get("/api/reconciliation/facilities/1")
check("manual retest wins", fac1["风险阶段"] == "中水位" and fac1["可通行性"] == "可通行")

# 更晚的自动采集（45cm）不能推翻人工复测
post("/api/reconciliation/facilities/1/readings", {
    "level_cm": 45, "collected_at": "2026-09-28 21:30:00", "source": "自动采集",
})
_, fac1 = get("/api/reconciliation/facilities/1")
check("later auto reading does not override retest", fac1["风险阶段"] == "中水位")

# 历史水位冻结：旧记录阶段不变
readings = fac1["readings"]
check("history frozen",
      next(r for r in readings if r["collected_at"] == "2026-09-28 20:00:00")["stage"] == "高水位"
      and next(r for r in readings if r["collected_at"] == "2026-09-28 21:00:00")["stage"] == "中水位"
      and all(r["frozen"] for r in readings))

# 通知核销、防汛事件结束
check("notice resolved/event closed",
      all(n["resolved"] for n in fac1["notices"])
      and all(e["status"] == "已结束" for e in fac1["flood_events"]))

# 5. 重复处置同一告警被拒（并发只保留一个有效结论）
_, dup = post("/api/reconciliation/alarms/1/handle", {
    "action": "人工复测", "measured_level": 50, "measured_at": "2026-09-28 22:00:00",
})
check("duplicate disposition rejected", (not dup["ok"]) and "一个有效结论" in dup["message"])

# 6. 空设施号迁移归并
_, mig = post("/api/reconciliation/migrate", {"items": [
    {"设施编号": "", "设施类型": "泵站", "所属路段": "河埠路", "桩号位置": "K4+020", "临时编号": "TMP-A"},
    {"设施编号": "", "设施类型": "雨水口", "所属路段": "河埠路", "桩号位置": "K5+500", "临时编号": "TMP-D"},
]})
check("migration ok", mig["ok"], json.dumps(mig, ensure_ascii=False))
_, facilities = get("/api/reconciliation/facilities")
ps001 = next(f for f in facilities["items"] if f["设施编号"] == "PS-001")
check("blank code merged into master", "TMP-A" in ps001["merged_from"])
check("blank code created master when unmatched",
      any(f["设施类型"] == "雨水口" and f["桩号位置"] == "K5+500" for f in facilities["items"]))

# 7. 升版本后可再次重建
ps001["version"]  # 仅查看
_, tasks_before = get("/api/reconciliation/tasks")
status, _ = post("/api/reconciliation/facilities/2/rebuild")
check("first rebuild facility 2", status == 200)

# 8. 重算失败保留上一版风险面
_, board = get("/api/reconciliation/board")
binjiang_before = next(r for r in board["items"] if r["路段"] == "滨江路")
# 通过迁移制造一个新版本号：手动改不便，直接用 simulate_failure 在当前版本会被幂等跳过，
# 先给滨江路设施登记新告警不会升版本；因此这里直接验证失败路径：先造一个新设施再失败重建。
_, new_fac = post("/api/reconciliation/migrate", {"items": [
    {"设施编号": "PSG-050", "设施类型": "排水管渠", "所属路段": "云栖路", "桩号位置": "K0+100"},
]})
new_id = None
_, facilities = get("/api/reconciliation/facilities")
new_facility = next(f for f in facilities["items"] if f["设施编号"] == "PSG-050")
new_id = new_facility["id"]
_, fail_task = post(f"/api/reconciliation/facilities/{new_id}/rebuild",
                    {"simulate_failure": True})
check("failed rebuild marked", fail_task["entry"]["status"] == "失败"
      and "保留上一版风险面" in fail_task["entry"]["detail"])

# 9. 三源阶段不一致的新告警：入库即重算（离线立即不可通行）
_, new_alarm = post(f"/api/reconciliation/facilities/{new_id}/alarms", {
    "alarm_type": "泵站离线", "alarm_time": "2026-09-29 09:00:00",
    "stage": "中水位", "notice_stage": "高水位", "flood_stage": "超警戒",
})
check("new offline alarm accepted", new_alarm["ok"], json.dumps(new_alarm, ensure_ascii=False))
_, fac_new = get(f"/api/reconciliation/facilities/{new_id}")
check("new alarm immediately impassable", fac_new["可通行性"] == "不可通行")
check("three sources forced aligned on recompute", fac_new["对账状态"] == "已对齐")

print()
if failures:
    print(f"{len(failures)} 项失败：{failures}")
    raise SystemExit(1)
print("全部 HTTP 端到端校验通过")
