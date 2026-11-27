"""环境阈值判定引擎。

环境监测不再只看单条读数：同一施工区域按“最近两次有效测量”判断是否持续超限，
并把判定结果同步到工序停工/复工清单和开工许可。所有会改变某一区域环境状态的操作
（新增读数、重新判定、开工、复工）都先获取该区域的区域锁，保证“迟到的超限报告”
和“现场开工请求”并发时看到的是同一份状态。

几个刻意保留的语义：
- 补录的旧读数会显式标记为「补录」，且不参与“最近两次有效测量”的选取，
  因此不能用来冒充最近一次测量，也不能据此提前复工。
- 缺测会单独形成「缺测」判定；若此前已经持续超限，缺测不会自动消除报警。
- 判断始终按「监测位置/施工区域」精确匹配，东廊报警不会波及西廊。
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator

from app.store import store

ENV_MODULE = "env_monitor"
THRESHOLD_MODULE = "env_threshold"
JUDGEMENT_MODULE = "env_judgement"
RESUME_MODULE = "work_resume"
PERMIT_MODULE = "work_permit"
WORK_MODULE = "work_sequence"
SCHEDULE_MODULE = "construction_schedule"
SAFETY_MODULE = "safety_briefing"

BACKFILL_FLAGS = ("补录", "是否补录", "补录标记", "backfill", "backfilled")
TIME_FIELDS = ("记录时间", "测量时间", "采集时间", "采样时间", "监测时间", "时间")
HUMIDITY_FIELDS = ("湿度读数", "湿度", "湿度值", "相对湿度")


class EnvironmentBusy(RuntimeError):
    """区域正在被另一个环境操作处理。"""


_local = threading.local()


def _held_areas() -> dict[str, int]:
    held = getattr(_local, "held_areas", None)
    if held is None:
        held = {}
        _local.held_areas = held
    return held


def _ensure_columns() -> None:
    """为环境读数补齐判定列，避免前端工作台因缺少列而显示空白。"""
    for row in store.rows(ENV_MODULE):
        row.setdefault("湿度阈值", None)
        row.setdefault("判定结果", "")
        row.setdefault("判定编号", "")
        row.setdefault("是否报警", False)
        row.setdefault("是否最近一次", False)
        row.setdefault("读数状态", "")


@contextmanager
def area_guard(area: str) -> Iterator[None]:
    """按施工区域加锁；同一线程可重入，便于判定逻辑内部复用。"""
    key = f"env_area:{normalize_area(area)}"
    held = _held_areas()
    depth = held.get(key, 0)
    if depth:
        held[key] = depth + 1
        try:
            yield
        finally:
            held[key] -= 1
        return
    if not store.acquire_resource(key, wait_timeout=10.0):
        raise EnvironmentBusy(f"施工区域「{area}」的环境判定正在处理，请稍后重试")
    held[key] = 1
    try:
        yield
    finally:
        held.pop(key, None)
        store.release_resource(key)


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def normalize_area(value: Any) -> str:
    return str(value or "").strip()


def parse_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().replace("％", "%").replace("%", "")
    text = text.replace("℃", "").replace("°C", "").strip()
    if not text:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().casefold() in {"1", "true", "yes", "y", "是", "补录", "旧读数"}


def parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in (
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M", "%Y-%m-%d", "%Y/%m/%d",
    ):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _row_time(row: dict[str, Any]) -> datetime | None:
    for field in TIME_FIELDS:
        parsed = parse_time(row.get(field))
        if parsed is not None:
            return parsed
    return parse_time(row.get("录入时间"))


def _sort_key(row: dict[str, Any]) -> tuple[datetime, int]:
    return (_row_time(row) or datetime.min, int(row.get("id", 0) or 0))


def _first_value(row: dict[str, Any], fields: tuple[str, ...]) -> Any:
    for field in fields:
        if field in row and row.get(field) not in (None, ""):
            return row.get(field)
    return None


def humidity_of(row: dict[str, Any]) -> float | None:
    return parse_number(_first_value(row, HUMIDITY_FIELDS))


def is_backfill(row: dict[str, Any]) -> bool:
    for flag in BACKFILL_FLAGS:
        if truthy(row.get(flag)):
            return True
    source = str(row.get("数据来源") or "")
    if "补录" in source:
        return True
    status = str(row.get("读数状态") or "")
    return status in {"补录", "旧读数"}


def is_missing(row: dict[str, Any]) -> bool:
    status = str(row.get("读数状态") or "")
    source = str(row.get("数据来源") or "")
    if status == "缺测" or "缺测" in source:
        return True
    return humidity_of(row) is None


def latest_recorded_time(area: str, exclude_id: int | None = None) -> datetime | None:
    """该区域已登记读数中最晚的测量时间（用于识别晚录入的旧读数）。"""
    times = []
    for row in area_rows(area):
        if exclude_id is not None and int(row.get("id", 0) or 0) == exclude_id:
            continue
        if is_backfill(row) or is_missing(row):
            continue
        parsed = _row_time(row)
        if parsed is not None:
            times.append(parsed)
    return max(times, default=None)


def is_late_old_reading(area: str, row: dict[str, Any]) -> bool:
    """晚录入的旧读数：记录时间早于已经登记过的最近读数，不能冒充最近一次测量。"""
    recorded = _row_time(row)
    latest_before = latest_recorded_time(area, int(row.get("id", 0) or 0))
    if recorded is None or latest_before is None:
        return False
    return recorded < latest_before


def area_rows(area: str) -> list[dict[str, Any]]:
    area = normalize_area(area)
    return [
        row for row in store.rows(ENV_MODULE)
        if normalize_area(row.get("监测位置")) == area
    ]


# ── 阈值 ──────────────────────────────────────────────────────────
def active_threshold_row(area: str) -> dict[str, Any] | None:
    area = normalize_area(area)
    candidates = [
        row for row in store.rows(THRESHOLD_MODULE)
        if normalize_area(row.get("施工区域") or row.get("监测位置")) == area
        and row.get("status") not in {"已停用", "已失效"}
    ]
    return max(candidates, key=lambda row: int(row.get("id", 0) or 0), default=None)


def get_threshold(area: str) -> float | None:
    row = active_threshold_row(area)
    if row is None:
        return None
    value = row.get("湿度阈值")
    if value in (None, ""):
        value = row.get("阈值")
    return parse_number(value)


def set_threshold(
    area: str,
    humidity: Any,
    *,
    operator: str = "",
    remark: str = "",
    auto_judge: bool = True,
) -> dict[str, Any]:
    area = normalize_area(area)
    value = parse_number(humidity)
    if not area:
        raise ValueError("施工区域不能为空")
    if value is None or value < 0 or value > 100:
        raise ValueError("湿度阈值必须是 0-100 之间的数值")

    with area_guard(area):
        row = active_threshold_row(area)
        if row is None:
            rows = store.rows(THRESHOLD_MODULE)
            row = {
                "id": max((int(r.get("id", 0) or 0) for r in rows), default=0) + 1,
                "施工区域": area,
                "湿度阈值": value,
                "设定人": operator,
                "备注说明": remark,
                "status": "已生效",
                "pending": False,
                "abnormal": False,
            }
            rows.append(row)
        else:
            row["施工区域"] = area
            row["湿度阈值"] = value
            if operator:
                row["设定人"] = operator
            if remark:
                row["备注说明"] = remark
            row["status"] = "已生效"
            row["pending"] = False
            row["abnormal"] = False
        row["更新时间"] = now_text()
        if auto_judge:
            _evaluate_locked(area)
        return row


def list_thresholds() -> list[dict[str, Any]]:
    return [dict(row) for row in store.rows(THRESHOLD_MODULE)]


# ── 最近两次有效测量 ──────────────────────────────────────────────
def annotate_area(area: str) -> list[dict[str, Any]]:
    """刷新该区域读数的显式标记：有效/缺测/补录、是否最近一次。"""
    _ensure_columns()
    rows = area_rows(area)
    current: list[dict[str, Any]] = []
    for row in rows:
        if is_missing(row):
            row["读数状态"] = "缺测"
            row["是否最近一次"] = False
            continue
        if is_backfill(row):
            row["读数状态"] = "补录"
            row["数据来源"] = "补录"
            row["是否最近一次"] = False
            row.setdefault("补录说明", "补录的旧读数，不作为最近一次测量")
            continue
        row["读数状态"] = "有效"
        current.append(row)

    current.sort(key=_sort_key, reverse=True)
    latest_id = int(current[0].get("id", 0)) if current else None
    for row in current:
        row["是否最近一次"] = int(row.get("id", 0)) == latest_id
    return current


def latest_valid(area: str) -> list[dict[str, Any]]:
    """返回按时间倒序排列的有效当前读数（补录旧读数不参与）。"""
    current = annotate_area(area)
    current.sort(key=_sort_key, reverse=True)
    return current


def latest_judgement(area: str) -> dict[str, Any] | None:
    area = normalize_area(area)
    candidates = [
        row for row in store.rows(JUDGEMENT_MODULE)
        if normalize_area(row.get("监测位置")) == area and row.get("是否当前判定")
    ]
    return max(candidates, key=lambda row: int(row.get("id", 0) or 0), default=None)


def active_alarm(area: str) -> dict[str, Any] | None:
    judgement = latest_judgement(area)
    if judgement and judgement.get("是否报警"):
        return judgement
    return None


# ── 判定 ──────────────────────────────────────────────────────────
def evaluate(area: str) -> dict[str, Any]:
    area = normalize_area(area)
    with area_guard(area):
        return _evaluate_locked(area)


def _evaluate_locked(area: str) -> dict[str, Any]:
    area = normalize_area(area)
    threshold_row = active_threshold_row(area)
    threshold = get_threshold(area)
    prior = active_alarm(area)
    recent = latest_valid(area)[:2]
    backfills = [row for row in area_rows(area) if is_backfill(row)]
    missing = [row for row in area_rows(area) if is_missing(row)]

    if threshold is None:
        return {
            "监测位置": area,
            "判定结果": "未设阈值",
            "湿度阈值": None,
            "是否报警": bool(prior),
            "是否当前判定": True,
            "判定说明": f"施工区域「{area}」尚未设定湿度阈值，暂不判定",
            "最近一次读数": (humidity_of(recent[0]) if recent else None),
            "前一次读数": (humidity_of(recent[1]) if len(recent) > 1 else None),
            "读数状态": "未设阈值",
        }

    if len(recent) < 2:
        result = "缺测"
        current = recent[0] if recent else None
        previous = recent[1] if len(recent) > 1 else None
        if prior:
            explanation = (
                f"有效读数不足两次（缺测），无法确认恢复；"
                f"此前持续超限报警继续保持，暂不得复工"
            )
        else:
            explanation = "有效读数不足两次（缺测），不能形成持续超限判定"
        if backfills:
            explanation += f"；另有{len(backfills)}条补录旧读数，未计入最近两次"
    else:
        current, previous = recent[0], recent[1]
        current_h = humidity_of(current) or 0.0
        previous_h = humidity_of(previous) or 0.0
        if current_h > threshold and previous_h > threshold:
            result = "持续超限"
            explanation = (
                f"最近两次有效测量 {previous_h:g}%、{current_h:g}% 均高于阈值 {threshold:g}%，"
                f"判定为持续超限"
            )
        elif current_h <= threshold and previous_h <= threshold:
            result = "未超限"
            explanation = (
                f"最近两次有效测量 {previous_h:g}%、{current_h:g}% 均未超过阈值 "
                f"{threshold:g}%，持续超限解除"
            )
        else:
            result = "单次超限"
            explanation = (
                f"最近两次有效测量 {previous_h:g}%、{current_h:g}% 中仅一次超过阈值 "
                f"{threshold:g}%，尚未构成持续超限"
            )
        if backfills:
            explanation += f"；{len(backfills)}条补录旧读数未计入最近两次"
        if missing:
            explanation += f"；另有{len(missing)}条缺测记录已显式标出"

    # 缺测/单次超限本身不新报警，但若此前已经持续超限，报警不会因为信息不足被自动解除。
    alarm = result == "持续超限" or bool(prior and result in {"缺测", "单次超限"})

    # 阈值可能在读数登记之后才设定：旧读数本身只带录入时间。把本轮判定采用的两次测量
    # 时间回填到参与判定的读数上，补录旧读数只保留自己的录入时间，二者不混用。
    for row in (current, previous):
        if row is not None:
            row["最近判定时间"] = now_text()

    # 判定只看最近两次有效测量；缺测和补录旧读数各自显式标出，不能冒充最近一次测量，
    # 也不会因为信息缺失把既有报警自动解除。
    state_parts: list[str] = []
    if backfills:
        state_parts.append("含补录旧读数")
    if missing:
        state_parts.append("存在缺测")
    if not state_parts:
        state_parts.append("缺测" if result == "缺测" else "有效")

    judgement: dict[str, Any] = {
        "监测位置": area,
        "湿度阈值": threshold,
        "判定结果": result,
        "是否报警": alarm,
        "是否当前判定": True,
        "最近一次读数": humidity_of(current) if current is not None else None,
        "前一次读数": humidity_of(previous) if previous is not None else None,
        "最近一次记录时间": current.get("记录时间") if current is not None else "",
        "前一次记录时间": previous.get("记录时间") if previous is not None else "",
        "最近一次记录ID": int(current.get("id", 0)) if current is not None else None,
        "前一次记录ID": int(previous.get("id", 0)) if previous is not None else None,
        "最近一次是否补录": False,
        "读数状态": "、".join(state_parts),
        "补录读数": "、".join(
            f"#{row.get('id')}:{humidity_of(row):g}%@{row.get('记录时间', '')}"
            for row in backfills if humidity_of(row) is not None
        ),
        "缺测记录": "、".join(f"#{row.get('id')}" for row in missing),
        "判定说明": explanation,
        "判定时间": now_text(),
        "阈值记录": threshold_row.get("id") if threshold_row else None,
        "status": result,
        "pending": result in {"持续超限", "缺测"},
        "abnormal": alarm,
    }
    signature = (
        int(current.get("id", 0)) if current is not None else None,
        int(previous.get("id", 0)) if previous is not None else None,
        result,
        tuple(int(row.get("id", 0)) for row in backfills),
        tuple(int(row.get("id", 0)) for row in missing),
        threshold,
    )
    judgement["判定签名"] = repr(signature)
    existing = next(
        (
            row for row in store.rows(JUDGEMENT_MODULE)
            if normalize_area(row.get("监测位置")) == area
            and row.get("判定签名") == judgement["判定签名"]
            and row.get("是否当前判定")
        ),
        None,
    )
    if existing is not None:
        return existing

    rows = store.rows(JUDGEMENT_MODULE)
    for row in rows:
        if normalize_area(row.get("监测位置")) == area:
            row["是否当前判定"] = False
    judgement["id"] = max((int(row.get("id", 0) or 0) for row in rows), default=0) + 1
    judgement["判定编号"] = f"JDG-{judgement['id']:04d}-{area}"
    rows.append(judgement)

    if alarm:
        suspend_area(area, judgement)
    elif result == "未超限":
        recover_area(area, judgement)
    return judgement


def _reason(area: str, judgement: dict[str, Any]) -> str:
    threshold = judgement.get("湿度阈值")
    if judgement.get("判定结果") == "持续超限":
        return (
            f"{area}湿度最近两次 {judgement.get('前一次读数')}%、"
            f"{judgement.get('最近一次读数')}% 均超过阈值 {threshold}%，持续超限"
        )
    if judgement.get("判定结果") == "缺测":
        return f"{area}环境读数缺测，且此前持续超限报警尚未解除"
    return f"{area}环境持续超限报警尚未解除"


def suspend_area(area: str, judgement: dict[str, Any]) -> list[dict[str, Any]]:
    """只暂停/阻断本施工区域的工序，并写入复工清单、阻断已有开工许可。"""
    area = normalize_area(area)
    affected: list[dict[str, Any]] = []
    reason = _reason(area, judgement)
    for work in store.rows(WORK_MODULE):
        if normalize_area(work.get("施工区域")) != area:
            continue
        status = work.get("status")
        if status not in {"施工中", "已暂停", "已发布"}:
            continue
        original = status
        if status == "施工中":
            work["status"] = "已暂停"
            work["环境暂停"] = True
            work["停工原因"] = reason
        elif status == "已发布":
            work["环境阻断"] = True
            work["停工原因"] = reason
        work["abnormal"] = True
        work["pending"] = True
        work["停工判定编号"] = judgement.get("判定编号")
        work["最近环境判定"] = judgement.get("判定结果")
        work["最近一次读数"] = judgement.get("最近一次读数")
        work["最近开工核实结果"] = "受阻"
        work["最近开工核实时间"] = now_text()
        _upsert_resume(work, judgement, original, reason)
        _invalidate_permits(work, judgement)
        affected.append(dict(work))

    # 只联动本区域受影响的工序，不再把其他区域的排期、交底全部标异常。
    affected_names = {work.get("工序名称") for work in affected}
    for schedule in store.rows(SCHEDULE_MODULE):
        if schedule.get("计划工序") in affected_names:
            schedule["abnormal"] = True
            schedule["环境阻断"] = True
            schedule["环境判定编号"] = judgement.get("判定编号")
    for safety in store.rows(SAFETY_MODULE):
        if safety.get("交底工序") in affected_names:
            safety["abnormal"] = True
            safety["环境阻断"] = True
            safety["环境判定编号"] = judgement.get("判定编号")
    return affected


def recover_area(area: str, judgement: dict[str, Any]) -> list[dict[str, Any]]:
    """环境恢复正常后只标记“可复工”，不自动改回施工中。"""
    area = normalize_area(area)
    affected: list[dict[str, Any]] = []
    for work in store.rows(WORK_MODULE):
        if normalize_area(work.get("施工区域")) != area:
            continue
        if work.get("status") not in {"已暂停", "已发布"} and not work.get("环境暂停"):
            continue
        work["环境可复工"] = True
        work["环境阻断"] = False
        work["abnormal"] = False
        work["恢复判定编号"] = judgement.get("判定编号")
        affected.append(dict(work))
    for resume in store.rows(RESUME_MODULE):
        if normalize_area(resume.get("施工区域")) != area:
            continue
        if resume.get("status") in {"待复工", "已阻断", "可复工"}:
            resume["status"] = "可复工"
            resume["pending"] = True
            resume["abnormal"] = False
            resume["判定结果"] = judgement.get("判定结果")
            resume["最近一次读数"] = judgement.get("最近一次读数")
            resume["恢复判定编号"] = judgement.get("判定编号")
            resume["状态说明"] = "最近两次有效测量均未超限，可申请复工"
    affected_names = {work.get("工序名称") for work in affected}
    for schedule in store.rows(SCHEDULE_MODULE):
        if schedule.get("计划工序") in affected_names:
            schedule["环境阻断"] = False
    for safety in store.rows(SAFETY_MODULE):
        if safety.get("交底工序") in affected_names:
            schedule_flag = safety.get("环境阻断")
            if schedule_flag:
                safety["环境阻断"] = False
    return affected


def _upsert_resume(
    work: dict[str, Any],
    judgement: dict[str, Any],
    original_status: str,
    reason: str,
) -> dict[str, Any]:
    rows = store.rows(RESUME_MODULE)
    row = next(
        (
            item for item in rows
            if int(item.get("关联工序ID", 0) or 0) == int(work.get("id", 0) or 0)
        ),
        None,
    )
    if row is None:
        row = {
            "id": max((int(item.get("id", 0) or 0) for item in rows), default=0) + 1,
            "关联工序ID": int(work.get("id", 0) or 0),
        }
        rows.append(row)
    row.update({
        "工序名称": work.get("工序名称", ""),
        "施工区域": work.get("施工区域", ""),
        "停工原因": reason,
        "环境判定": judgement.get("判定编号"),
        "判定结果": judgement.get("判定结果"),
        "湿度阈值": judgement.get("湿度阈值"),
        "前一次读数": judgement.get("前一次读数"),
        "最近一次读数": judgement.get("最近一次读数"),
        "读数状态": judgement.get("读数状态"),
        "状态说明": "环境持续超限，等待环境恢复后复工",
        "登记人": "环境监测",
        "备注说明": judgement.get("判定说明", ""),
        "status": "待复工",
        "pending": True,
        "abnormal": True,
    })
    # 保留第一次停工时的工序状态，后续重复判定不覆盖。
    if not row.get("原工序状态") or row.get("原工序状态") in {"已暂停", "施工中"}:
        row["原工序状态"] = original_status
    return row


# ── 开工许可 ──────────────────────────────────────────────────────
def _permits_for_work(work_id: int) -> list[dict[str, Any]]:
    return [
        row for row in store.rows(PERMIT_MODULE)
        if int(row.get("关联工序ID", 0) or 0) == int(work_id)
    ]


def active_permit(work_id: int) -> dict[str, Any] | None:
    candidates = [
        row for row in _permits_for_work(work_id)
        if row.get("status") == "有效"
    ]
    return max(candidates, key=lambda row: int(row.get("id", 0) or 0), default=None)


def ensure_permit(work: dict[str, Any], kind: str = "开工许可") -> dict[str, Any]:
    """同一工序同一时刻只保留一份有效开工许可；并发请求复用同一份。

    调用方必须已经持有该施工区域的区域锁（开工动作与读数登记共用），
    这里不再单独加锁，避免锁顺序反转。
    """
    work_id = int(work.get("id", 0) or 0)
    existing = active_permit(work_id)
    if existing is not None:
        work["开工许可编号"] = existing.get("许可编号")
        work["最近开工核实结果"] = existing.get("核实结果", "许可放行")
        work["最近开工核实时间"] = now_text()
        return existing

    rows = store.rows(PERMIT_MODULE)
    seq = len(_permits_for_work(work_id)) + 1
    prefix = "KG" if kind == "开工许可" else "FG"
    permit = {
        "id": max((int(row.get("id", 0) or 0) for row in rows), default=0) + 1,
        "许可编号": f"{prefix}-{work_id:04d}-{seq:02d}",
        "工序名称": work.get("工序名称", ""),
        "施工区域": work.get("施工区域", ""),
        "关联工序ID": work_id,
        "许可类型": kind,
        "许可状态": "有效",
        "关联判定": work.get("恢复判定编号") or work.get("停工判定编号") or "",
        "签发时间": now_text(),
        "核实结果": "允许开工" if kind == "开工许可" else "允许复工",
        "备注说明": "环境判定无阻断，许可唯一有效",
        "status": "有效",
        "pending": False,
        "abnormal": False,
    }
    rows.append(permit)
    work["开工许可编号"] = permit["许可编号"]
    work["最近开工核实结果"] = permit.get("核实结果")
    work["最近开工核实时间"] = now_text()
    work["最近环境判定"] = "无阻断"
    return permit


def _invalidate_permits(work: dict[str, Any], judgement: dict[str, Any]) -> None:
    work_id = int(work.get("id", 0) or 0)
    for permit in _permits_for_work(work_id):
        if permit.get("status") == "有效":
            permit["status"] = "已阻断"
            permit["许可状态"] = "已阻断"
            permit["阻断判定"] = judgement.get("判定编号")
            permit["阻断时间"] = now_text()
            permit["备注说明"] = "施工区域持续超限，开工许可已阻断"


def permits_for_area(area: str) -> list[dict[str, Any]]:
    area = normalize_area(area)
    return [
        dict(row) for row in store.rows(PERMIT_MODULE)
        if normalize_area(row.get("施工区域")) == area
    ]


# ── 复工清单与状态核实 ────────────────────────────────────────────
def list_resume(area: str | None = None) -> list[dict[str, Any]]:
    rows = [dict(row) for row in store.rows(RESUME_MODULE)]
    if area:
        area = normalize_area(area)
        rows = [row for row in rows if normalize_area(row.get("施工区域")) == area]
    return rows


def environmental_block(area: str) -> dict[str, Any]:
    """返回该区域当前是否阻断开工，以及对应的判定与读数依据。"""
    area = normalize_area(area)
    if not area:
        return {"施工区域": "", "blocked": False, "reason": "", "判定": None}
    threshold = get_threshold(area)
    if threshold is None:
        prior = latest_judgement(area)
        blocked = bool(prior and prior.get("是否报警"))
        return {
            "施工区域": area,
            "blocked": blocked,
            "reason": f"{area}此前持续超限报警尚未解除" if blocked else "",
            "阈值": None,
            "判定": prior,
        }
    judgement = evaluate(area)
    blocked = bool(judgement.get("是否报警"))
    return {
        "施工区域": area,
        "blocked": blocked,
        "reason": _reason(area, judgement) if blocked else "",
        "阈值": threshold,
        "判定": judgement,
    }


def start_status(area: str | None = None, process: str | None = None) -> dict[str, Any]:
    """开工/复工前双方都能查询的核实入口。"""
    work_row: dict[str, Any] | None = None
    if process:
        work_row = next(
            (row for row in store.rows(WORK_MODULE) if row.get("工序名称") == process),
            None,
        )
        if work_row is not None:
            area = work_row.get("施工区域") or area
    area = normalize_area(area)
    block = environmental_block(area) if area else {
        "blocked": False, "reason": "", "判定": None,
    }
    works = [
        dict(row) for row in store.rows(WORK_MODULE)
        if area and normalize_area(row.get("施工区域")) == area
    ]
    accepted = [
        row for row in works if row.get("status") in {"施工中", "已发布", "已暂停"}
    ]
    for row in accepted:
        row["开工是否受阻"] = bool(block.get("blocked"))
        row["需暂停"] = bool(block.get("blocked")) and row.get("status") == "施工中"
        row["已因环境暂停"] = row.get("status") == "已暂停" and bool(row.get("环境暂停"))
    resume = list_resume(area) if area else []
    permit = active_permit(int(work_row.get("id", 0) or 0)) if work_row else None
    if work_row is None and area:
        # 未指定工序时给出该区域最近一份许可，便于核对。
        area_permits = permits_for_area(area)
        permit = max(
            (row for row in area_permits if row.get("status") == "有效"),
            key=lambda row: int(row.get("id", 0) or 0),
            default=None,
        )
    # 有环境报警时，即便许可记录还没被同步阻断，也不能向调用方报告“可开工”。
    if block.get("blocked") and permit is not None and permit.get("status") == "有效":
        permit = None
    return {
        "施工区域": area,
        "工序": process,
        "开工是否受阻": bool(block.get("blocked")),
        "阻断原因": block.get("reason", ""),
        "判定结果": (block.get("判定") or {}).get("判定结果"),
        "湿度阈值": (block.get("判定") or {}).get("湿度阈值"),
        "前一次读数": (block.get("判定") or {}).get("前一次读数"),
        "最近一次读数": (block.get("判定") or {}).get("最近一次读数"),
        "读数状态": (block.get("判定") or {}).get("读数状态"),
        "当前判定": block.get("判定"),
        "开工许可": permit,
        "开工许可编号": (permit or {}).get("许可编号"),
        "许可状态": (permit or {}).get("status"),
        "施工状态": works,
        "已受理工序": accepted,
        "需暂停工序": [
            row for row in accepted
            if row.get("需暂停") or row.get("已因环境暂停")
        ],
        "复工清单": resume,
        "可以复工": not block.get("blocked"),
    }


def confirm_resume(resume_id: int) -> tuple[dict[str, Any] | None, str]:
    """确认复工：再次核实环境没有被持续超限阻断，然后恢复工序并签发复工许可。"""
    row = store.find(RESUME_MODULE, resume_id)
    if row is None:
        return None, f"复工条目 {resume_id} 不存在"
    area = normalize_area(row.get("施工区域"))
    work_id = int(row.get("关联工序ID", 0) or 0)
    with area_guard(area):
        block = environmental_block(area)
        if block.get("blocked"):
            row["状态说明"] = f"复工受阻：{block.get('reason')}"
            row["最近核实时间"] = now_text()
            return None, f"{area}环境持续超限，暂不能复工：{block.get('reason')}"
        work = store.find(WORK_MODULE, work_id)
        if work is None:
            return None, f"关联工序 {work_id} 不存在"
        if work.get("status") == "已完成":
            return None, f"工序「{work.get('工序名称')}」已完成，无需复工"
        original = row.get("原工序状态")
        if original == "已发布":
            # 从未真正开工、只是被环境阻断的工序：解除阻断后回到已发布，重新走开工核实。
            work["status"] = "已发布"
            work["环境阻断"] = False
            work["环境暂停"] = False
            work["环境可复工"] = True
            work["abnormal"] = False
            work["pending"] = True
            row["status"] = "已复工"
            row["pending"] = False
            row["abnormal"] = False
            row["状态说明"] = "环境阻断已解除，可重新申请开工"
            row["复工时间"] = now_text()
            return row, f"{area}环境阻断已解除，工序「{work.get('工序名称')}」可重新申请开工"
        # 复工意味着重新进入施工；原工序状态保留在复工清单里便于追溯。
        work["status"] = "施工中"
        work["pending"] = False
        work["abnormal"] = False
        work["环境暂停"] = False
        work["环境阻断"] = False
        work["环境可复工"] = True
        permit = ensure_permit(work, kind="复工许可")
        row["status"] = "已复工"
        row["pending"] = False
        row["abnormal"] = False
        row["复工许可编号"] = permit.get("许可编号")
        row["状态说明"] = "环境核实通过，已复工"
        row["复工时间"] = now_text()
        return row, f"工序「{work.get('工序名称')}」已复工"


def mark_resume_completed(work: dict[str, Any]) -> dict[str, Any] | None:
    work_id = int(work.get("id", 0) or 0)
    for row in store.rows(RESUME_MODULE):
        if int(row.get("关联工序ID", 0) or 0) == work_id:
            row["status"] = "已复工"
            row["pending"] = False
            row["abnormal"] = False
            row["状态说明"] = "工序已恢复施工"
            row["复工时间"] = now_text()
            return row
    return None


def environment_snapshot(area: str | None = None) -> dict[str, Any]:
    thresholds = list_thresholds()
    judgements = [dict(row) for row in store.rows(JUDGEMENT_MODULE)]
    if area:
        area = normalize_area(area)
        thresholds = [
            row for row in thresholds
            if normalize_area(row.get("施工区域")) == area
        ]
        judgements = [
            row for row in judgements
            if normalize_area(row.get("监测位置")) == area
        ]
    return {
        "thresholds": thresholds,
        "judgements": judgements,
        "resume_list": list_resume(area),
        "permits": [
            dict(row) for row in store.rows(PERMIT_MODULE)
            if not area or normalize_area(row.get("施工区域")) == area
        ],
    }
