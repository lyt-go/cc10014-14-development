"""环境监测领域服务：按施工区域设定湿度阈值，基于最近两次有效测量判定持续超限。

判定结果会同步到「环境判定」「工序复工清单」和「开工许可」：
- 同一位置只取最近两次有效测量判断，缺测与补录旧读数显式标出且不参与选取；
- 报警只作用于本施工区域，东廊超限不会冻结西廊；
- 新增读数与开工请求共用区域锁，迟到的超限报告和现场开工看到的是同一份环境状态。
"""
from __future__ import annotations

from typing import Any

from app.services import environment_engine as engine
from app.services.base import BaseModuleService
from app.store import store


class EnvMonitorService(BaseModuleService):
    MODULE = "env_monitor"
    ENTITY = "监测数据"
    OPERATION_TERM = "采集数据"
    LIST_FIELDS = ["监测位置", "温度读数", "湿度读数", "光照强度", "振动频率", "记录时间", "监测设备", "备注说明"]
    REQUIRED_FIELDS = ["监测位置", "温度读数", "湿度读数"]
    STATUS_ORDER = ["待采集", "已采集", "已上传", "异常报警"]
    ACTION_RULES = {"采集数据": "已采集", "上传数据": "已上传", "标记异常": "异常报警"}
    NEGATIVE_ACTIONS = []

    # ── 读数登记：与开工请求串行化，保证迟到的超限读数能立即阻断开工 ──
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        area = engine.normalize_area(values.get("监测位置"))
        if not area:
            return None, ["监测位置"]
        explicitly_missing = (
            engine.truthy(values.get("缺测"))
            or str(values.get("湿度读数") or "").strip() == "缺测"
            or any(
                str(values.get(field) or "").strip() == "缺测"
                for field in ("读数状态", "数据来源")
            )
        )
        required = self.REQUIRED_FIELDS
        if explicitly_missing:
            # 缺测记录本身就代表“这次没有测到”，不要求湿度读数，但会被显式标记且不参与判定。
            required = [field for field in required if field != "湿度读数"]
        missing = [
            field for field in required
            if not str(values.get(field) or "").strip()
        ]
        if missing:
            return None, missing
        try:
            with engine.area_guard(area):
                rows = store.rows(self.MODULE)
                entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
                entry.update({field: values.get(field) for field in self.LIST_FIELDS})
                entry["status"] = self.STATUS_ORDER[0]
                entry["pending"] = True
                entry["abnormal"] = False
                if explicitly_missing:
                    entry["读数状态"] = "缺测"
                    entry["数据来源"] = "缺测"
                    entry["缺测"] = True
                elif any(engine.truthy(values.get(flag)) for flag in engine.BACKFILL_FLAGS):
                    # 补录旧读数独立标记，不参与“最近两次有效测量”的选取。
                    entry["补录"] = True
                    entry["数据来源"] = "补录"
                    entry["读数状态"] = "补录"
                elif engine.is_late_old_reading(area, entry):
                    # 没有声明补录，但记录时间早于已经登记的最近读数：同样显式标为补录旧读数。
                    entry["补录"] = True
                    entry["数据来源"] = "补录"
                    entry["读数状态"] = "补录"
                    entry["补录说明"] = "晚录入的旧读数，不作为最近一次测量"
                else:
                    entry["补录"] = False
                    entry.setdefault("数据来源", "现场采集")
                entry.setdefault("录入时间", engine.now_text())
                entry["是否最近一次"] = False
                rows.append(entry)
                # 本区域有已生效阈值时，读数一落库就重新判定，报警立刻同步到复工清单和开工许可。
                if engine.get_threshold(area) is not None:
                    judgement = engine.evaluate(area)
                    entry["判定结果"] = judgement.get("判定结果")
                    entry["判定编号"] = judgement.get("判定编号")
                    entry["是否报警"] = judgement.get("是否报警")
                else:
                    engine.annotate_area(area)
                    entry["判定结果"] = "未设阈值"
                    entry["判定编号"] = ""
                    entry["是否报警"] = False
                return entry, []
        except engine.EnvironmentBusy as exc:
            return None, [str(exc)]

    def list_entries(self, **kwargs: Any) -> tuple[list[dict[str, Any]], int]:
        # 列表中的读数状态需要显式标出补录/缺测/最近一次，先统一刷新一遍标记。
        engine._ensure_columns()
        areas = {
            engine.normalize_area(row.get("监测位置")) for row in store.rows(self.MODULE)
        }
        for area in areas:
            if area:
                engine.annotate_area(area)
        items, total = super().list_entries(**kwargs)
        return [self._enrich(row) for row in items], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = super().get_entry(entry_id)
        return self._enrich(row) if row is not None else None

    def _enrich(self, row: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(row)
        area = engine.normalize_area(row.get("监测位置"))
        judgement = engine.latest_judgement(area)
        threshold = engine.get_threshold(area)
        enriched["湿度阈值"] = threshold
        enriched["判定结果"] = (judgement or {}).get("判定结果", "")
        enriched["是否报警"] = (judgement or {}).get("是否报警", False)
        enriched["读数状态"] = "补录" if engine.is_backfill(row) else (
            "缺测" if engine.is_missing(row) else "有效"
        )
        enriched["是否最近一次"] = "是" if row.get("是否最近一次") else "否"
        enriched["是否补录"] = "是" if engine.is_backfill(row) else "否"
        enriched["是否缺测"] = "是" if engine.is_missing(row) else "否"
        return enriched

    # ── 动作：标记异常时用最近两次有效测量重新判定，而不是只看当前这一条 ──
    def cross_module_verify(
        self, entry_id: int, action: str, entry: dict[str, Any]
    ) -> tuple[bool, str]:
        if action == "标记异常":
            area = engine.normalize_area(entry.get("监测位置"))
            if not engine.area_rows(area):
                return False, "该位置没有监测读数，无法标记异常报警"
        return True, ""

    def cross_module_effect(
        self, entry_id: int, action: str, entry: dict[str, Any]
    ) -> list[dict]:
        if action != "标记异常":
            return []
        area = engine.normalize_area(entry.get("监测位置"))
        try:
            judgement = engine.evaluate(area)
        except engine.EnvironmentBusy:
            return []
        entry["判定编号"] = judgement.get("判定编号")
        return [
            row for row in store.rows(engine.RESUME_MODULE)
            if engine.normalize_area(row.get("施工区域")) == area
        ]

    # ── 对外业务能力 ──
    def set_threshold(
        self,
        area: str,
        humidity: Any,
        *,
        operator: str = "监测员",
        remark: str = "",
    ) -> dict[str, Any]:
        return engine.set_threshold(area, humidity, operator=operator, remark=remark)

    def judge(self, area: str) -> dict[str, Any]:
        return engine.evaluate(area)

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        all_rows = store.rows(self.MODULE)
        base["alerts"] = [
            {
                "位置": r.get("监测位置"),
                "温度": r.get("温度读数"),
                "湿度": r.get("湿度读数"),
                "读数状态": "补录" if engine.is_backfill(r) else (
                    "缺测" if engine.is_missing(r) else "有效"
                ),
            }
            for r in all_rows if r.get("status") == "异常报警"
        ]
        base["thresholds"] = engine.list_thresholds()
        base["judgements"] = [dict(row) for row in store.rows(engine.JUDGEMENT_MODULE)]
        base["resume_list"] = engine.list_resume()
        return base


class EnvThresholdService(BaseModuleService):
    MODULE = engine.THRESHOLD_MODULE
    ENTITY = "湿度阈值"
    OPERATION_TERM = "启用阈值"
    LIST_FIELDS = ["施工区域", "湿度阈值", "设定人", "更新时间", "备注说明"]
    REQUIRED_FIELDS = ["施工区域", "湿度阈值"]
    STATUS_ORDER = ["已生效", "已停用"]
    ACTION_RULES = {"启用阈值": "已生效", "停用阈值": "已停用"}
    NEGATIVE_ACTIONS = ["停用阈值"]

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        area = values.get("施工区域")
        humidity = values.get("湿度阈值") or values.get("阈值")
        if not str(area or "").strip():
            return None, ["施工区域"]
        if engine.parse_number(humidity) is None:
            return None, ["湿度阈值"]
        try:
            row = engine.set_threshold(
                str(area), humidity, operator=str(values.get("设定人") or "监测员"),
                remark=str(values.get("备注说明") or ""),
            )
        except ValueError as exc:
            return None, [str(exc)]
        return row, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action not in self.ACTION_RULES:
            return None, f"动作「{action}」不属于{self.ENTITY}可执行范围"
        row = store.find(self.MODULE, entry_id)
        if row is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return entry, message
        area = engine.normalize_area(row.get("施工区域"))
        # 阈值启用/停用都会改变该区域能否开工，动作完成后重新判定一次。
        engine.evaluate(area)
        if action == "停用阈值":
            # 停用阈值不会抹掉已经发生的持续超限报警，复工仍需环境核实通过。
            alarm = engine.active_alarm(area)
            label = "已启用" if action == "启用阈值" else "已停用"
            suffix = "；此前的持续超限报警仍保留，复工需环境核实通过" if alarm else ""
            return entry, f"{area}湿度阈值{label}{suffix}"
        return entry, f"{area}湿度阈值{entry.get('湿度阈值')}%已启用"


class EnvJudgementService(BaseModuleService):
    MODULE = engine.JUDGEMENT_MODULE
    ENTITY = "环境判定"
    LIST_FIELDS = [
        "监测位置", "判定结果", "湿度阈值", "前一次读数", "最近一次读数",
        "是否报警", "读数状态", "补录读数", "判定说明", "判定时间",
    ]
    REQUIRED_FIELDS = ["监测位置"]
    STATUS_ORDER = ["未设阈值", "未超限", "单次超限", "持续超限", "缺测"]
    ACTION_RULES = {"重新判定": "持续超限"}
    NEGATIVE_ACTIONS = []

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        area = str(values.get("监测位置") or values.get("施工区域") or "").strip()
        if not area:
            return None, ["监测位置"]
        judgement = engine.evaluate(area)
        return judgement, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action != "重新判定":
            return None, f"动作「{action}」不属于{self.ENTITY}可执行范围"
        row = store.find(self.MODULE, entry_id)
        if row is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"
        judgement = engine.evaluate(engine.normalize_area(row.get("监测位置")))
        return judgement, f"{judgement.get('监测位置')}重新判定：{judgement.get('判定结果')}"


class WorkResumeService(BaseModuleService):
    MODULE = engine.RESUME_MODULE
    ENTITY = "复工条目"
    OPERATION_TERM = "确认复工"
    LIST_FIELDS = [
        "工序名称", "施工区域", "停工原因", "判定结果", "湿度阈值", "前一次读数",
        "最近一次读数", "读数状态", "状态说明", "复工时间",
    ]
    REQUIRED_FIELDS = ["工序名称", "施工区域"]
    STATUS_ORDER = ["待复工", "可复工", "已复工", "已阻断"]
    ACTION_RULES = {"确认复工": "已复工"}
    NEGATIVE_ACTIONS = []

    def list_entries(self, **kwargs: Any) -> tuple[list[dict[str, Any]], int]:
        area = (kwargs.get("filters") or {}).get("施工区域")
        if area:
            # 施工区域是复工清单的关键检索字段，用精确匹配避免“东廊”匹配到“东廊屋面”。
            rows = [
                dict(row) for row in store.rows(self.MODULE)
                if engine.normalize_area(row.get("施工区域")) == engine.normalize_area(area)
            ]
            keyword = kwargs.get("keyword")
            if keyword:
                rows = [
                    row for row in rows
                    if keyword.casefold() in str(row.get("工序名称", "")).casefold()
                ]
            total = len(rows)
            page = int(kwargs.get("page", 1))
            size = int(kwargs.get("size", 20))
            start = (page - 1) * size
            return rows[start:start + size], total
        return super().list_entries(**kwargs)

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        """按条目 id 或关联工序 id 都能查到复工入口。"""
        row = store.find(self.MODULE, entry_id)
        if row is not None:
            return row
        return next(
            (
                row for row in store.rows(self.MODULE)
                if int(row.get("关联工序ID", 0) or 0) == entry_id
            ),
            None,
        )

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action != "确认复工":
            return None, f"动作「{action}」不属于{self.ENTITY}可执行范围"
        row = self.get_entry(entry_id)
        if row is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"
        if row.get("status") == "已复工":
            return row, "该工序已复工，无需重复确认"
        try:
            updated, message = engine.confirm_resume(int(row["id"]))
        except engine.EnvironmentBusy as exc:
            return None, str(exc)
        return updated, message

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["resume_list"] = engine.list_resume()
        return base


class WorkPermitService(BaseModuleService):
    MODULE = engine.PERMIT_MODULE
    ENTITY = "开工许可"
    OPERATION_TERM = "核实许可"
    LIST_FIELDS = [
        "许可编号", "工序名称", "施工区域", "许可类型", "许可状态",
        "关联判定", "签发时间", "核实结果", "备注说明",
    ]
    REQUIRED_FIELDS = ["工序名称", "施工区域"]
    STATUS_ORDER = ["有效", "已阻断", "已失效"]
    ACTION_RULES = {"核实许可": "有效"}
    NEGATIVE_ACTIONS = []

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        # 开工许可只能由开工核实签发，避免手工登记出第二份“有效许可”。
        return None, ["开工许可由开工核实自动签发，不能手工登记"]

    def list_entries(self, **kwargs: Any) -> tuple[list[dict[str, Any]], int]:
        area = (kwargs.get("filters") or {}).get("施工区域")
        if area:
            rows = [
                dict(row) for row in store.rows(self.MODULE)
                if engine.normalize_area(row.get("施工区域")) == engine.normalize_area(area)
            ]
            total = len(rows)
            page = int(kwargs.get("page", 1))
            size = int(kwargs.get("size", 20))
            start = (page - 1) * size
            return rows[start:start + size], total
        return super().list_entries(**kwargs)

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action != "核实许可":
            return None, f"动作「{action}」不属于{self.ENTITY}可执行范围"
        row = store.find(self.MODULE, entry_id)
        if row is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"
        area = engine.normalize_area(row.get("施工区域"))
        blocked = engine.environmental_block(area).get("blocked")
        if blocked and row.get("status") == "有效":
            engine.suspend_area(area, engine.evaluate(area))
            row = store.find(self.MODULE, entry_id) or row
            return row, f"{area}环境持续超限，许可已阻断"
        return row, f"许可{row.get('许可编号')}当前状态：{row.get('status')}"
