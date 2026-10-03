"""Booking records, response interpretation and outcome reconciliation."""

import json
import time
from datetime import datetime, timedelta

from libcs import configuration, constants, errors, privacy


def normalize_booking_id(value):
    text = str(value or "").strip()
    if not text.isdigit():
        raise ValueError("bookingId 必须是数字")
    return text


def append_booking_id(url, booking_id):
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}bookingId={normalize_booking_id(booking_id)}"


def extract_booking_items(data):
    items = []

    def walk(value):
        if isinstance(value, dict):
            if value.get("ui_type") == "ht.Seat.OrderListItem" and value.get("id") is not None:
                items.append(format_booking_item(value))
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(data)
    return items


def is_explicit_empty_booking_list(data):
    """Accept explicit empty lists, never infer emptiness from unknown JSON."""
    if isinstance(data, list):
        return not data
    if not isinstance(data, dict):
        return False
    if str(data.get("CODE", "")).lower() == "ok" and data.get("DATA") == []:
        return True
    if data.get("ui_type") == "ht.Seat.OrderList":
        return data.get("children") == [] or data.get("items") == []
    # An empty typed list can be nested in a page; unrelated empty arrays
    # (e.g. headers or settings) must not imply that there are no bookings.
    return any(
        is_explicit_empty_booking_list(child)
        for child in data.values() if isinstance(child, dict)
    ) or any(
        is_explicit_empty_booking_list(child)
        for children in data.values() if isinstance(children, list)
        for child in children if isinstance(child, dict)
    )


def booking_status(item):
    value = item.get("status")
    return "" if value is None else str(value).strip()


def format_booking_item(item):
    status = booking_status(item)
    start_timestamp = int(float(item.get("time") or 0))
    duration_seconds = int(float(item.get("duration") or 0))
    order_timestamp = int(float(item.get("orderTime") or 0))
    limit_sign_ago = int(float(item.get("limitSignAgo") or 0))
    limit_sign_back = int(float(item.get("limitSignBack") or 0))
    room_name = str(item.get("roomName") or "")
    seat_num = str(item.get("seatNum") or "")
    ibeacons = item.get("ibeacons") if isinstance(item.get("ibeacons"), list) else []
    space = item.get("space") if isinstance(item.get("space"), dict) else {}
    sign_start_timestamp = start_timestamp - limit_sign_ago if start_timestamp and limit_sign_ago else 0
    sign_deadline_timestamp = start_timestamp + limit_sign_back if start_timestamp and limit_sign_back else 0
    auto_check_in_time = (
        auto_check_in_time_for_item({"start_timestamp": start_timestamp})
        if start_timestamp
        else None
    )
    return {
        "id": str(item.get("id")),
        "room_name": room_name,
        "addr": str(item.get("addr") or ""),
        "seat_num": seat_num,
        "seat_id": str(item.get("seatId") or ""),
        "floor_id": str(item.get("floorId") or ""),
        "status": status,
        "status_label": constants.BOOKING_STATUS_LABELS.get(status, f"状态 {status}" if status else "未知状态"),
        "start_timestamp": start_timestamp,
        "start_text": format_timestamp(start_timestamp),
        "duration_seconds": duration_seconds,
        "duration_text": format_duration(duration_seconds),
        "order_timestamp": order_timestamp,
        "order_text": format_timestamp(order_timestamp),
        "limit_sign_ago": limit_sign_ago,
        "limit_sign_back": limit_sign_back,
        "sign_start_timestamp": sign_start_timestamp,
        "sign_start_text": format_timestamp(sign_start_timestamp),
        "sign_deadline_timestamp": sign_deadline_timestamp,
        "sign_deadline_text": format_timestamp(sign_deadline_timestamp),
        "auto_check_in_delay_minutes": constants.DEFAULT_AUTO_CHECK_IN_DELAY_MINUTES,
        "auto_check_in_timestamp": int(auto_check_in_time.timestamp()) if auto_check_in_time else 0,
        "auto_check_in_text": format_datetime(auto_check_in_time),
        "space_minor": space.get("minor"),
        "ibeacons_count": len(ibeacons),
        "cancelable": status in constants.CANCELABLE_BOOKING_STATUSES,
        "continuable": status in constants.CONTINUABLE_BOOKING_STATUSES,
        "label": booking_item_label(room_name, seat_num, start_timestamp, duration_seconds, status),
    }


def booking_item_label(room_name, seat_num, start_timestamp, duration_seconds, status):
    parts = [
        format_timestamp(start_timestamp),
        room_name,
        f"{seat_num}座" if seat_num else "",
        format_duration(duration_seconds),
        constants.BOOKING_STATUS_LABELS.get(status, f"状态 {status}" if status else "未知状态"),
    ]
    return " / ".join(part for part in parts if part)


def format_timestamp(timestamp):
    if not timestamp:
        return "-"
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")


def format_duration(seconds):
    if seconds <= 0:
        return "-"
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if minutes:
        return f"{hours}小时{minutes}分钟"
    return f"{hours}小时"


def format_datetime(value):
    if not value:
        return "-"
    return value.strftime("%Y-%m-%d %H:%M:%S")


def auto_check_in_time_for_item(item, delay_minutes=constants.DEFAULT_AUTO_CHECK_IN_DELAY_MINUTES):
    start_timestamp = int(item.get("start_timestamp") or 0)
    if not start_timestamp:
        return None
    start_time = datetime.fromtimestamp(start_timestamp).astimezone()
    display_minute = start_time.replace(second=0, microsecond=0)
    return display_minute + timedelta(minutes=configuration.normalize_check_in_delay_minutes(delay_minutes))


def enrich_check_in_task(item, delay_minutes=constants.DEFAULT_AUTO_CHECK_IN_DELAY_MINUTES):
    task = dict(item)
    delay_minutes = configuration.normalize_check_in_delay_minutes(delay_minutes)
    check_in_at = auto_check_in_time_for_item(item, delay_minutes)
    task["auto_check_in_delay_minutes"] = delay_minutes
    task["auto_check_in_timestamp"] = int(check_in_at.timestamp()) if check_in_at else 0
    task["auto_check_in_text"] = format_datetime(check_in_at)
    if check_in_at:
        task["seconds_until_auto_check_in"] = int(
            (check_in_at - datetime.now().astimezone()).total_seconds()
        )
    else:
        task["seconds_until_auto_check_in"] = 0
    return task


def pending_check_in_tasks(bookings, delay_minutes=constants.DEFAULT_AUTO_CHECK_IN_DELAY_MINUTES):
    tasks = [
        enrich_check_in_task(item, delay_minutes)
        for item in bookings
        if booking_status(item) == "0"
    ]
    tasks.sort(key=lambda item: item.get("start_timestamp") or 0)
    return tasks


def select_pending_check_in_task(bookings, booking_id=None, delay_minutes=constants.DEFAULT_AUTO_CHECK_IN_DELAY_MINUTES):
    if booking_id:
        item = find_booking_by_id(bookings, booking_id)
        if not item:
            raise RuntimeError(f"找不到预约 bookingId={booking_id}")
        if booking_status(item) != "0":
            raise RuntimeError(f"预约 {booking_id} 当前状态为 {item.get('status_label')}，不是待签到")
        return enrich_check_in_task(item, delay_minutes)

    tasks = pending_check_in_tasks(bookings, delay_minutes)
    return tasks[0] if tasks else None


def find_booking_by_id(bookings, booking_id):
    booking_id = normalize_booking_id(booking_id)
    for item in bookings:
        if str(item.get("id")) == booking_id:
            return item
    return None


def find_matching_booking(
    bookings, seat_num, begin_time, duration_hours, *,
    seat_id=None, floor_id=None, room_name=None, expected_booking_id=None,
):
    target_timestamp = int(begin_time.timestamp())
    target_duration = int(duration_hours) * 3600
    for item in bookings:
        if expected_booking_id is not None and str(item.get("id")) != str(expected_booking_id):
            continue
        if str(item.get("seat_num") or "") != str(seat_num):
            continue
        if int(item.get("start_timestamp") or 0) != target_timestamp:
            continue
        if int(item.get("duration_seconds") or 0) != target_duration:
            continue
        if booking_status(item) not in constants.ACTIVE_BOOKING_STATUSES:
            continue
        # Seat numbers are only unique within a room. A returned bookingId can
        # identify a successful submission even when the list omits room IDs.
        identity_matched = expected_booking_id is not None
        if not identity_matched:
            # Prefer stable IDs; display names can differ between API views.
            for key, target in (("seat_id", seat_id), ("floor_id", floor_id), ("room_name", room_name)):
                actual = item.get(key)
                if target is not None and str(target).strip() and actual is not None and str(actual).strip():
                    identity_matched = str(actual).strip() == str(target).strip()
                    break
        if any(value is not None for value in (seat_id, floor_id, room_name)) and not identity_matched:
            continue
        return item
    return None


def booking_result_message(result):
    if not isinstance(result, dict):
        return "预约接口返回失败"
    data = result.get("DATA") if isinstance(result.get("DATA"), dict) else {}
    messages = []
    for value in (data.get("msg"), result.get("MESSAGE")):
        text = str(value or "").strip()
        if text and text not in messages:
            messages.append(text)
    return " / ".join(messages) or "预约接口返回格式异常"


def booking_result_succeeded(result):
    if not isinstance(result, dict):
        return False
    data = result.get("DATA") if isinstance(result.get("DATA"), dict) else {}
    code = str(result.get("CODE") or "").strip().lower()
    status = str(data.get("result") or "").strip().lower()
    booking_id = str(data.get("bookingId") or "").strip()
    return code == "ok" and status == "success" and booking_id.isdigit()


def booking_result_failed(result):
    return not booking_result_succeeded(result)


def seat_action_rejected(result):
    """Only explicit business rejection allows failure/retry decisions."""
    if not isinstance(result, dict):
        return False
    data = result.get("DATA") if isinstance(result.get("DATA"), dict) else {}
    code = str(result.get("CODE") or "").strip().lower()
    status = str(data.get("result") or "").strip().lower()
    if status in {"success", "pending"}:
        return False
    return code in {"error", "fail", "failed", "paramerror", "notlogin", "not_login", "unauthorized", "forbidden"} or (
        status in {"fail", "failed", "failure", "error"}
    )


def is_time_out_of_range(result):
    return constants.MSG_TIME_OUT_OF_RANGE in booking_result_message(result)


def is_duplicate_booking(result):
    return constants.MSG_DUPLICATE in booking_result_message(result)


def is_seat_unavailable(result):
    message = booking_result_message(result)
    keywords = (
        constants.MSG_SEAT_UNAVAILABLE,
        "座位不可用",
        "座位已被预约",
        "座位已被占用",
        "锁定或占用",
    )
    return any(keyword in message for keyword in keywords)


def validate_booking_result(result):
    failed = booking_result_failed(result)
    message = booking_result_message(result)
    if not failed:
        return

    hint = ""
    if constants.MSG_TIME_OUT_OF_RANGE in message:
        hint = "。这通常表示预约入口还没开放；当前建议执行时间为 20:00:00.500。"
    raise RuntimeError(f"预约失败：{message}{hint}")


def cancel_result_message(result):
    if not isinstance(result, dict):
        return "取消接口返回失败"
    data = result.get("DATA") if isinstance(result.get("DATA"), dict) else {}
    return str(data.get("msg") or result.get("MESSAGE") or "取消接口返回失败").strip()


def validate_cancel_result(result):
    if not isinstance(result, dict):
        raise RuntimeError("取消失败：接口返回格式异常")
    data = result.get("DATA") if isinstance(result.get("DATA"), dict) else {}
    code = str(result.get("CODE") or "").strip().lower()
    status = str(data.get("result") or "").strip().lower()
    if code == "ok" and status == "success":
        return
    raise RuntimeError(f"取消失败：{cancel_result_message(result)}")


def safe_check_in_response(result):
    if not isinstance(result, dict):
        return {"response_type": type(result).__name__}
    safe = {
        "CODE": result.get("CODE"),
        "MESSAGE": result.get("MESSAGE"),
    }
    data = result.get("DATA")
    if isinstance(data, dict):
        safe["DATA"] = {
            key: data.get(key)
            for key in ("result", "msg", "message", "bookingId")
            if key in data
        }
    else:
        safe["DATA_type"] = type(data).__name__
    return privacy.redact_diagnostic_data(safe)


def check_in_precheck_from_item(item, booking_id=None):
    booking_id = normalize_booking_id(booking_id or item.get("id"))
    return {
        "bookingId": booking_id,
        "status_code_before": booking_status(item),
        "status_label_before": item.get("status_label"),
        "room": item.get("room_name"),
        "seat": item.get("seat_num"),
        "start_text": item.get("start_text"),
        "sign_start_text": item.get("sign_start_text"),
        "sign_deadline_text": item.get("sign_deadline_text"),
        "auto_check_in_text": item.get("auto_check_in_text"),
        "ibeacons_count": item.get("ibeacons_count", 0),
        "space_minor": item.get("space_minor"),
    }


def check_in_result_message(result, default="签到接口返回失败"):
    if not isinstance(result, dict):
        return default
    data = result.get("DATA") if isinstance(result.get("DATA"), dict) else {}
    return str(data.get("msg") or data.get("message") or result.get("MESSAGE") or default).strip()


def check_in_result_failed(result):
    return not seat_action_succeeded(result)


def seat_action_succeeded(result):
    if not isinstance(result, dict):
        return False
    data = result.get("DATA") if isinstance(result.get("DATA"), dict) else {}
    code = str(result.get("CODE") or "").strip().lower()
    status = str(data.get("result") or "").strip().lower()
    return code == "ok" and status == "success"


def confirm_booking_status(booker, booking_id, expected_status, logger=print, attempts=3):
    """Finish read-only confirmation even if cancellation arrived after POST."""
    for trial in range(1, attempts + 1):
        try:
            item = find_booking_by_id(booker.current_bookings(), booking_id)
        except Exception as exc:
            logger(f"状态复核失败[{trial}/{attempts}]：{exc}")
        else:
            if item and booking_status(item) == expected_status:
                return item
            label = (item.get("status_label") or booking_status(item)) if item else "记录不存在"
            logger(f"状态复核[{trial}/{attempts}]：{label}")
        if trial < attempts:
            time.sleep(0.2)
    return None


def perform_confirmed_seat_action(
    booker, booking_id, send, *, expected_status, action_label, logger=print,
):
    """Send once; reconcile success/ambiguous replies with a concrete state."""
    request_error = None
    try:
        result = send()
    except errors.RequestFailure as exc:
        if not exc.outcome_unknown:
            raise
        request_error, result = exc, None
        logger(f"{action_label}请求结果不确定，正在查询实际状态：{exc}")
    logger(f"{action_label}接口返回：")
    logger(json.dumps(safe_check_in_response(result), ensure_ascii=False, indent=2))
    if request_error is None and seat_action_rejected(result):
        message = check_in_result_message(result, default="接口明确拒绝本次操作")
        raise RuntimeError(f"{action_label}失败：{message}")
    confirmed = confirm_booking_status(booker, booking_id, expected_status, logger=logger)
    if not confirmed:
        label = constants.BOOKING_STATUS_LABELS[expected_status]
        raise errors.ResultUncertain(
            f"{action_label}结果待确认：bookingId={booking_id} 未确认状态为{label}，请刷新预约列表"
        ) from request_error
    return result, confirmed
