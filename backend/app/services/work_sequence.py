"""工序编排领域服务：编排修缮工序顺序，发布工序时检查前置工序完成状态、安全交底和工匠到岗。

开工（开始施工）还要经过施工区域的环境核实：同一区域按最近两次有效测量判定持续超限后，
开工立即受阻；核实与签发开工许可在同一把区域锁内完成，并发请求只会得到一份有效许可。
"""
from __future__ import annotations

from typing import Any

from app.services import environment_engine as engine
from app.services.base import BaseModuleService
from app.store import store


class WorkSequenceService(BaseModuleService):
    MODULE = "work_sequence"
    ENTITY = "工序卡"
    OPERATION_TERM = "发布工序"
    LIST_FIELDS = ["工序名称", "前置工序", "所需工匠", "施工区域", "预计工时", "编排人", "工序状态", "备注说明"]
    REQUIRED_FIELDS = ["工序名称", "前置工序", "所需工匠"]
    STATUS_ORDER = ["待排期", "已发布", "施工中", "已完成"]
    ACTION_RULES = {"发布工序": "已发布", "开始施工": "施工中", "完成工序": "已完成"}
    NEGATIVE_ACTIONS = []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """开工/复工相关动作按施工区域串行，避免与迟到的环境读数交错。"""
        entry = store.find(self.MODULE, entry_id)
        area = engine.normalize_area(entry.get("施工区域")) if entry else ""
        if not area or action not in {"开始施工", "完成工序"}:
            return super().run_action(entry_id, action)
        try:
            with engine.area_guard(area):
                # 持有区域锁后先按最新读数重新判定，再做幂等、校验与状态变更。
                if action == "开始施工" and engine.get_threshold(area) is not None:
                    judgement = engine.evaluate(area)
                    if judgement.get("是否报警") and entry is not None and entry.get("status") != "施工中":
                        return None, (
                            f"{area}环境持续超限，开工受阻：{judgement.get('判定说明', '')}"
                        )
                    if entry is not None and entry.get("status") == "已暂停" and not judgement.get("是否报警"):
                        return self._restart_paused(entry)
                return super().run_action(entry_id, action)
        except engine.EnvironmentBusy as exc:
            return None, str(exc)

    def _restart_paused(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """环境恢复后从已暂停重新开工：重新签发（或复用）开工许可并更新复工清单。"""
        entry["status"] = "施工中"
        entry["pending"] = False
        entry["abnormal"] = False
        entry["环境暂停"] = False
        entry["环境阻断"] = False
        entry["环境可复工"] = True
        permit = engine.ensure_permit(entry)
        engine.mark_resume_completed(entry)
        return entry, f"工序「{entry.get('工序名称')}」环境核实通过，已重新开工（许可{permit.get('许可编号')}）"

    def cross_module_verify(
        self, entry_id: int, action: str, entry: dict[str, Any]
    ) -> tuple[bool, str]:
        if action == "发布工序":
            # 1. 检查前置工序是否已完成
            prereq = entry.get("前置工序", "")
            if prereq and prereq != "无":
                matched = False
                for row in store.rows("work_sequence"):
                    if row.get("工序名称", "") == prereq and row.get("id") != entry_id:
                        matched = True
                        if row.get("status") != "已完成":
                            return False, f"前置工序「{prereq}」尚未完成，当前状态「{row.get('status', '')}」"
                        break
                if not matched and prereq != "无":
                    return False, f"前置工序「{prereq}」在系统中不存在"
            # 2. 检查关联安全交底是否已签认
            process_name = entry.get("工序名称", "")
            safety_records = store.query_by_field("safety_briefing", "交底工序", process_name)
            if not safety_records:
                return False, f"工序「{process_name}」缺少安全交底记录"
            unsigned = [r for r in safety_records if r.get("status") != "已签认"]
            if unsigned:
                return False, f"安全交底未完全签认，尚有{len(unsigned)}条待签认"
        if action == "开始施工":
            process_name = entry.get("工序名称", "")
            # 检查工匠是否已到岗
            craftsmen = store.query_by_field("craftsman_assign", "分配工序", process_name)
            if craftsmen and any(c.get("status") != "已到岗" for c in craftsmen):
                return False, "该工序的工匠尚未全部到岗"
            # 检查关联排期是否已发布
            schedules = store.query_by_field("construction_schedule", "计划工序", process_name)
            if schedules:
                unreleased = [s for s in schedules if s.get("status") == "编制中"]
                if unreleased:
                    return False, "关联施工排期还未发布"
            # 环境核实：本施工区域持续超限时开工受阻，其他区域不受影响。
            area = engine.normalize_area(entry.get("施工区域"))
            if area and engine.get_threshold(area) is not None:
                judgement = engine.evaluate(area)
                if judgement.get("是否报警"):
                    return False, (
                        f"{area}环境持续超限，开工受阻："
                        f"{judgement.get('判定说明', '')}"
                    )
        return True, ""

    def cross_module_effect(
        self, entry_id: int, action: str, entry: dict[str, Any]
    ) -> list[dict]:
        effects = []
        if action == "发布工序":
            # 发布时该区域已在持续超限：直接标出环境阻断，进入复工清单等环境恢复。
            area = engine.normalize_area(entry.get("施工区域"))
            if area and engine.get_threshold(area) is not None:
                judgement = engine.evaluate(area)
                if judgement.get("是否报警"):
                    entry["环境阻断"] = True
                    entry["停工原因"] = judgement.get("判定说明", "")
                    engine.suspend_area(area, judgement)
                    effects.extend(
                        row for row in store.rows(engine.RESUME_MODULE)
                        if engine.normalize_area(row.get("施工区域")) == area
                    )
        if action == "开始施工":
            # 环境核实已通过，签发（或复用）本工序唯一一份有效开工许可。
            permit = engine.ensure_permit(entry)
            effects.append(permit)
            engine.mark_resume_completed(entry)
        if action == "完成工序":
            process_name = entry.get("工序名称", "")
            engine.mark_resume_completed(entry)
            # 工序完成后联动更新关联施工排期
            for sched in store.rows("construction_schedule"):
                if sched.get("计划工序", "") == process_name:
                    if sched.get("status") == "执行中":
                        sched["里程碑节点"] = f"{process_name}已完成"
                        effects.append(dict(sched))
            # 联动更新修缮日志中关联日志
            for log in store.rows("restoration_log"):
                if log.get("对应工序", "") == process_name:
                    if log.get("status") in ("已审核", "已填写"):
                        log["status"] = "已归档"
                        log["pending"] = False
                        effects.append(dict(log))
        return effects

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        # 返回工序依赖图
        all_rows = store.rows(self.MODULE)
        base["dependency_graph"] = [
            {"工序": r.get("工序名称"), "前置": r.get("前置工序"), "状态": r.get("status")}
            for r in all_rows
        ]
        base["completed"] = [r.get("工序名称") for r in all_rows if r.get("status") == "已完成"]
        base["resume_list"] = engine.list_resume()
        return base