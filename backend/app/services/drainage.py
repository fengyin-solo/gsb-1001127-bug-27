"""排水设施业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "drainage"
REQUIRED_FIELDS = ["设施编号", "设施类型", "所属路段"]
STATUS_ORDER = ["正常", "淤积", "堵塞", "损坏"]
ACTION_RULES = {"安排清淤": "淤积", "安排疏通": "堵塞", "登记损坏": "损坏"}
NEGATIVE_ACTIONS = []


def _attach_reconciliation(row: dict[str, Any]) -> dict[str, Any]:
    """把告警对账台算出的根因/风险阶段回灌到排水列表与详情，两处口径保持一致。"""
    # 延迟导入：对账服务依赖 store，避免模块初始化期的循环引用
    from app.services.reconciliation import reconciliation

    cause = reconciliation.root_cause_for_code(str(row.get("设施编号", "")))
    if cause:
        row = dict(row)
        for key, value in cause.items():
            row.setdefault(key, value)
    return row


class DrainageService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("设施编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        page_rows = [_attach_reconciliation(row) for row in rows[start:start + size]]
        return page_rows, total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        if row is None:
            return None
        return _attach_reconciliation(row)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"排水设施 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于排水设施可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"排水设施已{action}"
