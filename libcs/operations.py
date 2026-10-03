"""Queries, configuration diagnostics and confirmed seat actions."""

import json
import time
from datetime import datetime
from urllib.parse import urlparse

from libcs import client, configuration, constants, errors, privacy, records, scheduling


def check_config(path):
    """Check local setup without contacting the library or printing credentials."""
    config = configuration.load_config(path)
    if "headers" not in config.get("session", {}):
        raise ValueError("缺少 session.headers，请参考 config.example.yaml")
    urls = config.get("urls", {})
    required_urls = ["query_rooms", "query_seats", "book_seat"]
    if not (config.get("user_info") or {}).get("uid"):
        required_urls.append("user_base_info")
    for key in required_urls:
        value = urls.get(key)
        parsed = urlparse(value) if isinstance(value, str) else None
        if parsed is None or parsed.scheme != "https" or not parsed.netloc:
            raise ValueError(f"配置 urls.{key} 必须是完整的 HTTPS 地址")
    configuration.validate_booking_options(config.get("booking", {}))
    booker = client.InstantBooker(config)
    try:
        booker.load_cookies()
    finally:
        booker.session.close()


def create_booker(config_path=constants.DEFAULT_CONFIG, logger=None):
    config = configuration.load_config(config_path)
    booker = client.InstantBooker(config)
    if logger:
        logger("正在加载 cookie...")
    booker.load_cookies()
    if logger:
        logger("正在识别登录用户...")
    booker.resolve_user()
    return booker


def get_current_bookings(config_path=constants.DEFAULT_CONFIG):
    booker = create_booker(config_path)
    return booker.current_bookings()


def measure_server_clock(config_path=constants.DEFAULT_CONFIG):
    """Take a few read-only samples; never use the result to move a booking."""
    config = configuration.load_config(config_path)
    plan = configuration.parse_plan(str((config.get("booking") or {}).get("plan") or ""))
    booker = client.InstantBooker(config)
    try:
        booker.load_cookies()
        room_items = booker.query_room_items()
        if not 1 <= plan["room_type"] <= len(room_items):
            raise RuntimeError(f"房间类型 {plan['room_type']} 不存在")
        room_detail = booker.query_room_detail(room_items[plan["room_type"] - 1])
        begin_time = scheduling.build_begin_time(plan["start_hour"], constants.DEFAULT_BOOK_DAYS)
        booker.query_seat_map(room_detail, begin_time, plan["duration_hours"])
        samples = [booker.last_seat_query_meta.get("clock_sample")]
        samples = [sample for sample in samples if sample]
        for _ in range(3):
            bounds = scheduling.clock_offset_bounds(samples)
            if bounds and bounds["consistent"] and bounds["uncertainty"] <= 0.15:
                break
            time.sleep(0.25)
            samples.append(booker.query_clock_sample(timeout=min(booker.timeout, 3)))
        bounds = scheduling.clock_offset_bounds(samples)
        return {
            "bounds": bounds,
            "message": scheduling.clock_offset_message(bounds),
            "recommendation": scheduling.recommend_booking_execute_time(bounds),
            "measured_at": datetime.now().astimezone().strftime("%H:%M:%S"),
        }
    finally:
        booker.session.close()


def get_pending_check_in_tasks(config_path=constants.DEFAULT_CONFIG, delay_minutes=constants.DEFAULT_AUTO_CHECK_IN_DELAY_MINUTES):
    booker = create_booker(config_path)
    return records.pending_check_in_tasks(booker.current_bookings(), delay_minutes)


def cancel_booking_by_id(config_path=constants.DEFAULT_CONFIG, booking_id=None, logger=print):
    booking_id = records.normalize_booking_id(booking_id)
    booker = create_booker(config_path, logger=logger)
    logger(f"正在取消预约 bookingId={booking_id}...")
    result = booker.cancel_booking(booking_id)
    logger("取消接口返回：")
    logger(privacy.diagnostic_json(result["result"]))
    logger("取消成功")
    return result


def continue_seat_by_id(config_path=constants.DEFAULT_CONFIG, booking_id=None, logger=print):
    booking_id = records.normalize_booking_id(booking_id)
    booker = create_booker(config_path, logger=logger)
    item = records.find_booking_by_id(booker.current_bookings(), booking_id)
    if not item:
        raise RuntimeError(f"找不到预约 bookingId={booking_id}")

    status = records.booking_status(item)
    status_label = item.get("status_label") or f"状态 {status}"
    if status not in constants.CONTINUABLE_BOOKING_STATUSES:
        if status == "6":
            raise RuntimeError("该预约已因暂离未归结束，续座期限已过，服务器不允许续座")
        raise RuntimeError(f"当前预约状态为 {status_label}，只有“暂离中”的预约可以续座")

    logger(f"正在续座 bookingId={booking_id}，{item.get('room_name')} {item.get('seat_num')}座...")
    result, after_item = records.perform_confirmed_seat_action(
        booker, booking_id, lambda: booker.continue_booking(booking_id),
        expected_status="1", action_label="续座", logger=logger,
    )
    safe_response = records.safe_check_in_response(result)
    after_status = records.booking_status(after_item)

    logger(f"续座结果复核成功：{after_item.get('status_label')}")
    return {
        "ok": True,
        "sent": True,
        "method": "POST",
        "path": f"/Seat/Index/comeBack?bookingId={booking_id}",
        "body": None,
        "booking_id": booking_id,
        "response": safe_response,
        "status_code_before": status,
        "status_label_before": status_label,
        "status_code_after": after_status,
        "status_label_after": after_item.get("status_label"),
    }


def check_in_test_by_id(config_path=constants.DEFAULT_CONFIG, booking_id=None, logger=print):
    booking_id = records.normalize_booking_id(booking_id)
    booker = create_booker(config_path, logger=logger)
    bookings = booker.current_bookings()
    item = records.find_booking_by_id(bookings, booking_id)
    if not item:
        return {
            "ok": False,
            "sent": False,
            "stage": "precheck",
            "message": "bookingId 不在当前登录态的预约列表中，未发送签到请求",
            "bookingId": booking_id,
        }

    precheck = records.check_in_precheck_from_item(item, booking_id)
    if precheck["status_code_before"] != "0":
        return {
            "ok": False,
            "sent": False,
            "stage": "precheck",
            "message": f"当前预约状态为 {precheck['status_label_before']}，只允许测试待签到预约",
            "precheck": precheck,
        }

    logger(f"正在测试签到接口 bookingId={booking_id}...")
    result = booker.check_in_booking(booking_id)
    after_item = records.find_booking_by_id(booker.current_bookings(), booking_id)
    return {
        "ok": True,
        "sent": True,
        "method": "POST",
        "path": f"/Seat/Index/checkIn?bookingId={booking_id}",
        "body": None,
        "proximity_fields_in_request": [],
        "precheck": precheck,
        "response": records.safe_check_in_response(result),
        "status_code_after": records.booking_status(after_item) if after_item else None,
        "status_label_after": after_item.get("status_label") if after_item else None,
    }


def print_pending_check_in_tasks(config_path=constants.DEFAULT_CONFIG, delay_minutes=constants.DEFAULT_AUTO_CHECK_IN_DELAY_MINUTES):
    tasks = get_pending_check_in_tasks(config_path, delay_minutes)
    if not tasks:
        print("暂无待签到预约")
        return
    for item in tasks:
        print(
            f"{item['id']}  {item['label']}  "
            f"[计划签到 {item['auto_check_in_text']}]"
        )


def print_booking_list(config_path=constants.DEFAULT_CONFIG):
    bookings = get_current_bookings(config_path)
    if not bookings:
        print("暂无座位预约")
        return
    for item in bookings:
        marker = "可取消" if item["cancelable"] else "不可取消"
        print(f"{item['id']}  {item['label']}  [{marker}]")


def run_auto_check_in(
    config_path=constants.DEFAULT_CONFIG,
    booking_id=None,
    delay_minutes=constants.DEFAULT_AUTO_CHECK_IN_DELAY_MINUTES,
    logger=print,
    should_cancel=None,
):
    delay_minutes = configuration.normalize_check_in_delay_minutes(delay_minutes)

    def check_cancel():
        if should_cancel and should_cancel():
            raise errors.TaskCancelled("任务已取消")

    booker = create_booker(config_path, logger=logger)
    check_cancel()
    logger("正在获取待签到任务信息...")
    bookings = booker.current_bookings()
    task = records.select_pending_check_in_task(bookings, booking_id, delay_minutes)
    if not task:
        raise RuntimeError("没有待签到预约")

    current_booking_id = records.normalize_booking_id(task["id"])
    check_in_at = records.auto_check_in_time_for_item(task, delay_minutes)
    if not check_in_at:
        raise RuntimeError(f"预约 {current_booking_id} 缺少开始时间，无法计算自动签到时间")

    sign_deadline = None
    if int(task.get("sign_deadline_timestamp") or 0):
        sign_deadline = datetime.fromtimestamp(int(task["sign_deadline_timestamp"])).astimezone()
        if check_in_at > sign_deadline:
            raise RuntimeError(
                f"计划签到时间 {records.format_datetime(check_in_at)} 晚于签到截止 {records.format_datetime(sign_deadline)}"
            )

    logger(
        f"待签到任务：bookingId={current_booking_id}，{task.get('room_name')} "
        f"{task.get('seat_num')}座，预约开始 {task.get('start_text')}"
    )
    logger(f"自动签到时间：预约开始后 {delay_minutes:g} 分钟，{records.format_datetime(check_in_at)}")
    if task.get("sign_start_text") and task.get("sign_deadline_text"):
        logger(f"签到窗口：{task.get('sign_start_text')} 至 {task.get('sign_deadline_text')}")

    now = datetime.now().astimezone()
    if sign_deadline and now > sign_deadline:
        raise RuntimeError(f"签到截止时间已过：{records.format_datetime(sign_deadline)}")
    if check_in_at > now:
        scheduling.wait_until(
            check_in_at,
            logger=logger,
            should_cancel=should_cancel,
            heartbeat=booker.keepalive,
            heartbeat_interval=booker.keepalive_interval,
            announce_label="自动签到",
            ready_message="已到自动签到时间，开始签到",
        )
    else:
        logger("计划签到时间已经到达或已过去，立即签到")

    check_cancel()
    refreshed = booker.current_bookings()
    current = records.find_booking_by_id(refreshed, current_booking_id)
    if not current:
        message = f"bookingId={current_booking_id} 不在当前登录态的预约列表中，未发送签到请求"
        raise RuntimeError(message)

    current_task = records.enrich_check_in_task(current, delay_minutes)
    precheck = records.check_in_precheck_from_item(current_task, current_booking_id)
    if precheck["status_code_before"] != "0":
        message = f"当前预约状态已变为 {precheck['status_label_before']}，未重复发送签到请求"
        if precheck["status_code_before"] != "1":
            raise RuntimeError(message)
        logger(message)
        return {
            "ok": True,
            "sent": False,
            "stage": "precheck_after_wait",
            "message": message,
            "precheck": precheck,
            "task": task,
        }

    deadline_timestamp = int(current_task.get("sign_deadline_timestamp") or 0)
    if deadline_timestamp:
        sign_deadline = datetime.fromtimestamp(deadline_timestamp).astimezone()
        if datetime.now().astimezone() > sign_deadline:
            raise RuntimeError(f"签到截止时间已过：{records.format_datetime(sign_deadline)}")

    check_cancel()
    logger(f"正在自动签到 bookingId={current_booking_id}...")
    request_error = None
    try:
        result = booker.check_in_booking(current_booking_id)
    except errors.RequestFailure as exc:
        if not exc.outcome_unknown:
            raise
        request_error = exc
        result = None
        logger(f"签到请求结果不确定，正在查询实际状态：{exc}")
    safe_response = records.safe_check_in_response(result)
    logger("签到接口返回：")
    logger(json.dumps(safe_response, ensure_ascii=False, indent=2))
    if request_error is None and records.check_in_result_failed(result):
        raise RuntimeError(f"签到失败：{records.check_in_result_message(result)}")

    after_item = records.confirm_booking_status(booker, current_booking_id, "1", logger=logger)
    if not after_item:
        raise errors.ResultUncertain(
            f"签到结果待确认：bookingId={current_booking_id} 未确认状态为使用中，请刷新预约列表"
        ) from request_error
    logger(f"签到结果复核成功：{after_item.get('status_label') or '使用中'}")
    return {
        "ok": True,
        "sent": True,
        "method": "POST",
        "path": f"/Seat/Index/checkIn?bookingId={current_booking_id}",
        "body": None,
        "auto_check_in_at": records.format_datetime(check_in_at),
        "precheck": precheck,
        "response": safe_response,
        "status_code_after": records.booking_status(after_item),
        "status_label_after": after_item.get("status_label") if after_item else None,
    }
