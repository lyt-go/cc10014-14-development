"""环境阈值判定、区域隔离与开工/复工并发场景测试。"""
from __future__ import annotations

import concurrent.futures

import pytest

from app.seed import SEED_ROWS
from app.services import environment_engine as engine
from app.services.construction_schedule import ConstructionScheduleService
from app.services.env_monitor import EnvMonitorService
from app.services.safety_briefing import SafetyBriefingService
from app.services.work_sequence import WorkSequenceService
from app.store import store


@pytest.fixture(autouse=True)
def reset_store():
    store._idempotent_log.clear()
    store._resource_locks.clear()
    for name in list(store._tables.keys()):
        store._tables[name] = [dict(row) for row in SEED_ROWS.get(name, [])]
    for name, rows in SEED_ROWS.items():
        store._tables.setdefault(name, [dict(row) for row in rows])
    yield


env_service = EnvMonitorService()
work_service = WorkSequenceService()
safety_service = SafetyBriefingService()
schedule_service = ConstructionScheduleService()


def add_reading(area: str, humidity, recorded_at: str, **extra):
    values = {
        "监测位置": area,
        "温度读数": 20,
        "湿度读数": humidity,
        "记录时间": recorded_at,
    }
    values.update(extra)
    entry, missing = env_service.create_entry(values)
    assert missing == [], f"读数登记失败：{missing}"
    return entry


def publish_work(area: str, name: str):
    safety, _ = safety_service.create_entry(
        {"交底工序": name, "危险源": "粉尘", "防护措施": "口罩"}
    )
    safety_service.run_action(safety["id"], "执行交底")
    safety_service.run_action(safety["id"], "签认确认")
    work, _ = work_service.create_entry(
        {"工序名称": name, "前置工序": "无", "所需工匠": "木工", "施工区域": area}
    )
    updated, message = work_service.run_action(work["id"], "发布工序")
    assert updated is not None, message
    return work


# ── 阈值与持续超限判定 ──
def test_threshold_is_per_area():
    engine.set_threshold("东廊", 85)
    engine.set_threshold("西廊", 90)
    assert engine.get_threshold("东廊") == 85
    assert engine.get_threshold("西廊") == 90
    assert engine.get_threshold("南廊") is None


def test_two_valid_readings_over_threshold_trigger_continuous_alarm():
    engine.set_threshold("东廊", 85)
    add_reading("东廊", 86, "2026-11-27 09:00:00")
    judgement = engine.latest_judgement("东廊")
    assert judgement["判定结果"] == "缺测"  # 只有一次有效测量，不能判定持续超限
    add_reading("东廊", 89, "2026-11-27 10:00:00")
    judgement = engine.latest_judgement("东廊")
    assert judgement["判定结果"] == "持续超限"
    assert judgement["前一次读数"] == 86
    assert judgement["最近一次读数"] == 89
    assert judgement["是否报警"] is True


def test_single_exceedance_is_not_continuous_alarm():
    engine.set_threshold("东廊", 85)
    add_reading("东廊", 80, "2026-11-27 09:00:00")
    add_reading("东廊", 89, "2026-11-27 10:00:00")
    judgement = engine.latest_judgement("东廊")
    assert judgement["判定结果"] == "单次超限"
    assert judgement["是否报警"] is False


def test_backfilled_old_reading_is_marked_and_not_latest():
    engine.set_threshold("东廊", 85)
    add_reading("东廊", 86, "2026-11-27 09:00:00")
    add_reading("东廊", 89, "2026-11-27 10:00:00")
    backfill = add_reading("东廊", 81, "2026-11-27 08:00:00", 补录=True)

    assert backfill["读数状态"] == "补录"
    assert backfill["是否最近一次"] is False
    rows = {row["id"]: row for row in engine.area_rows("东廊")}
    assert rows[backfill["id"]]["读数状态"] == "补录"
    # 最近两次仍是 89、86，旧读数不能冒充最近一次测量
    latest = engine.latest_valid("东廊")[:2]
    assert [row["湿度读数"] for row in latest] == [89, 86]
    assert latest[0]["是否最近一次"] is True
    judgement = engine.latest_judgement("东廊")
    assert judgement["判定结果"] == "持续超限"
    assert (judgement["前一次读数"], judgement["最近一次读数"]) == (86, 89)
    assert backfill["id"] is not None
    assert str(backfill["id"]) in judgement["补录读数"]


def test_late_old_reading_without_flag_is_still_treated_as_backfill():
    """未勾补录但记录时间早于已有最近读数，同样要显式标为补录旧读数。"""
    engine.set_threshold("东廊", 85)
    add_reading("东廊", 86, "2026-11-27 09:00:00")
    add_reading("东廊", 89, "2026-11-27 10:00:00")
    late = add_reading("东廊", 81, "2026-11-27 08:00:00")
    assert late["读数状态"] == "补录"
    assert late["是否最近一次"] is False
    assert (engine.latest_judgement("东廊")["最近一次读数"]) == 89


def test_missing_reading_is_explicit_and_keeps_existing_alarm():
    engine.set_threshold("东廊", 85)
    add_reading("东廊", 86, "2026-11-27 09:00:00")
    add_reading("东廊", 89, "2026-11-27 10:00:00")
    missing, problems = env_service.create_entry(
        {"监测位置": "东廊", "温度读数": 20, "湿度读数": "", "记录时间": "2026-11-27 11:00:00", "缺测": True}
    )
    assert problems == []
    assert missing["读数状态"] == "缺测"
    assert missing["是否最近一次"] is False
    judgement = engine.latest_judgement("东廊")
    # 缺测不占位：最近两次有效测量仍是 86、89，持续超限结论不变，
    # 但缺测必须在判定里显式列出，且不能把报警当成已解除。
    assert judgement["判定结果"] == "持续超限"
    assert judgement["是否报警"] is True
    assert "存在缺测" in judgement["读数状态"]
    assert str(missing["id"]) in judgement["缺测记录"]
    assert engine.start_status("东廊")["开工是否受阻"] is True


# ── 区域隔离 ──
def test_east_alarm_does_not_freeze_west_construction():
    engine.set_threshold("东廊", 85)
    engine.set_threshold("西廊", 85)
    east = publish_work("东廊", "东廊彩画")
    west = publish_work("西廊", "西廊彩画")
    work_service.run_action(west["id"], "开始施工")

    add_reading("东廊", 86, "2026-11-27 09:00:00")
    add_reading("东廊", 89, "2026-11-27 10:00:00")

    east_row = store.find("work_sequence", east["id"])
    west_row = store.find("work_sequence", west["id"])
    # 东廊工序只是已发布、尚未开工：被环境阻断，不能开始施工。
    assert east_row["status"] == "已发布"
    assert east_row.get("环境阻断") is True
    assert "东廊" in str(east_row.get("停工原因"))
    blocked, message = work_service.run_action(east["id"], "开始施工")
    assert blocked is None and "持续超限" in message
    # 西廊工序不受影响，继续施工。
    assert west_row["status"] == "施工中"
    assert west_row.get("停工原因") in (None, "")

    west_status = engine.start_status("西廊")
    assert west_status["开工是否受阻"] is False
    assert west_status["可以复工"] is True


def test_other_area_schedule_and_safety_are_not_marked():
    engine.set_threshold("东廊", 85)
    publish_work("东廊", "东廊彩画")
    west = publish_work("西廊", "西廊彩画")
    west_schedule, _ = schedule_service.create_entry(
        {"计划工序": "西廊彩画", "开始日期": "2026-11-27", "结束日期": "2026-12-01"}
    )
    add_reading("东廊", 86, "2026-11-27 09:00:00")
    add_reading("东廊", 89, "2026-11-27 10:00:00")
    assert store.find("construction_schedule", west_schedule["id"]).get("环境阻断") in (None, False)
    for safety in store.rows("safety_briefing"):
        if safety.get("交底工序") == "西廊彩画":
            assert safety.get("环境阻断") in (None, False)
    assert store.find("work_sequence", west["id"])["status"] == "已发布"


# ── 复工清单 ──
def test_resume_list_and_backfill_cannot_resume():
    engine.set_threshold("东廊", 85)
    east = publish_work("东廊", "东廊彩画")
    work_service.run_action(east["id"], "开始施工")
    add_reading("东廊", 86, "2026-11-27 09:00:00")
    add_reading("东廊", 89, "2026-11-27 10:00:00")

    resume_rows = engine.list_resume("东廊")
    assert len(resume_rows) == 1
    resume = resume_rows[0]
    assert resume["关联工序ID"] == east["id"]
    assert resume["status"] == "待复工"
    assert resume["前一次读数"] == 86 and resume["最近一次读数"] == 89

    add_reading("东廊", 81, "2026-11-27 08:00:00", 补录=True)
    updated, message = engine.confirm_resume(resume["id"])
    assert updated is None
    assert "持续超限" in message or "暂不能复工" in message
    assert store.find("work_sequence", east["id"])["status"] == "已暂停"
    assert engine.start_status("东廊")["开工是否受阻"] is True


def test_resume_requires_two_valid_recovered_readings():
    engine.set_threshold("东廊", 85)
    east = publish_work("东廊", "东廊彩画")
    work_service.run_action(east["id"], "开始施工")
    add_reading("东廊", 86, "2026-11-27 09:00:00")
    add_reading("东廊", 89, "2026-11-27 10:00:00")
    resume = engine.list_resume("东廊")[0]

    add_reading("东廊", 70, "2026-11-27 11:00:00")
    assert engine.latest_judgement("东廊")["判定结果"] == "单次超限"
    assert engine.confirm_resume(resume["id"])[0] is None

    add_reading("东廊", 72, "2026-11-27 12:00:00")
    assert engine.latest_judgement("东廊")["判定结果"] == "未超限"
    recovered, message = engine.confirm_resume(resume["id"])
    assert recovered is not None, message
    assert store.find("work_sequence", east["id"])["status"] == "施工中"
    permits = [p for p in engine.permits_for_area("东廊") if p.get("status") == "有效"]
    assert len(permits) == 1
    assert permits[0]["许可编号"] == recovered["复工许可编号"]


# ── 开工许可证唯一性 ──
def test_concurrent_start_only_creates_one_permit():
    engine.set_threshold("西廊", 85)
    west = publish_work("西廊", "西廊彩画")

    def start():
        return WorkSequenceService().run_action(west["id"], "开始施工")

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result() for future in [pool.submit(start), pool.submit(start)]]

    permits = engine.permits_for_area("西廊")
    assert len(permits) == 1, permits
    assert permits[0]["status"] == "有效"
    assert store.find("work_sequence", west["id"])["status"] == "施工中"
    assert any(entry is not None for entry, _ in results)


def test_late_alarm_reading_and_start_request_see_same_state():
    """迟到超限报告与现场开工并发：要么开工先落地后被暂停，要么开工被直接挡住。"""
    engine.set_threshold("东廊", 85)
    east = publish_work("东廊", "东廊彩画")
    add_reading("东廊", 86, "2026-11-27 09:00:00")

    def late_reading():
        return EnvMonitorService().create_entry(
            {"监测位置": "东廊", "温度读数": 20, "湿度读数": 89, "记录时间": "2026-11-27 10:00:00"}
        )

    def start():
        return WorkSequenceService().run_action(east["id"], "开始施工")

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        started = pool.submit(start)
        reported = pool.submit(late_reading)
        start_result = started.result()
        reported.result()

    work = store.find("work_sequence", east["id"])
    valid_permits = [p for p in engine.permits_for_area("东廊") if p.get("status") == "有效"]
    assert engine.latest_judgement("东廊")["判定结果"] == "持续超限"
    assert work["status"] in {"已发布", "已暂停"}
    if work["status"] == "施工中":
        raise AssertionError("持续超限后不可能仍处于施工中")
    assert len(valid_permits) == 0
    status = engine.start_status("东廊")
    assert status["开工是否受阻"] is True
    assert any(
        row.get("工序名称") == "东廊彩画" for row in status["需暂停工序"]
    ) or work["status"] == "已发布"
    if start_result[0] is not None:
        # 开工先落地：随后必须能在复工清单里看到并标记为需暂停
        assert work["status"] == "已暂停"
        assert any(r["关联工序ID"] == east["id"] for r in status["复工清单"])


def test_delayed_reading_after_start_blocks_existing_permit():
    engine.set_threshold("东廊", 85)
    east = publish_work("东廊", "东廊彩画")
    add_reading("东廊", 86, "2026-11-27 09:00:00")
    assert work_service.run_action(east["id"], "开始施工")[0] is not None
    permit = engine.permits_for_area("东廊")[0]
    assert permit["status"] == "有效"

    add_reading("东廊", 89, "2026-11-27 10:00:00")
    result = engine.start_status("东廊", "东廊彩画")
    assert result["开工是否受阻"] is True
    assert result["开工许可"] is None
    assert result["许可状态"] is None
    assert store.find("work_sequence", east["id"])["status"] == "已暂停"
    assert store.find("work_permit", permit["id"])["status"] == "已阻断"


def test_blocked_not_started_work_returns_to_published_after_recovery():
    """从未开工、仅被环境阻断的工序：环境恢复后回到已发布，重新走开工核实。"""
    engine.set_threshold("东廊", 85)
    east = publish_work("东廊", "东廊彩画")
    add_reading("东廊", 86, "2026-11-27 09:00:00")
    add_reading("东廊", 89, "2026-11-27 10:00:00")

    blocked, message = work_service.run_action(east["id"], "开始施工")
    assert blocked is None and "持续超限" in message
    resume = engine.list_resume("东廊")[0]
    assert resume["原工序状态"] == "已发布"

    add_reading("东廊", 70, "2026-11-27 11:00:00")
    add_reading("东廊", 71, "2026-11-27 12:00:00")
    recovered, message = engine.confirm_resume(resume["id"])
    assert recovered is not None, message
    assert store.find("work_sequence", east["id"])["status"] == "已发布"
    assert engine.permits_for_area("东廊") == []

    started, message = work_service.run_action(east["id"], "开始施工")
    assert started is not None, message
    permits = [p for p in engine.permits_for_area("东廊") if p.get("status") == "有效"]
    assert len(permits) == 1
    assert permits[0]["关联判定"] == recovered.get("恢复判定编号") or permits[0]["关联判定"] != ""
