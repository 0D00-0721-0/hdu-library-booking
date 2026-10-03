"""YAML loading, plan parsing and shared input validation. No network I/O."""

import re
from datetime import datetime
from math import isfinite
from pathlib import Path

import yaml

from libcs import constants


def normalize_retry_delay(value):
    try:
        delay = float(value)
    except (TypeError, ValueError):
        return constants.DEFAULT_RETRY_DELAY
    if not isfinite(delay):
        return constants.DEFAULT_RETRY_DELAY
    return max(constants.MIN_RETRY_DELAY, min(delay, 10.0))


def normalize_keepalive_interval(value):
    try:
        interval = float(value)
    except (TypeError, ValueError):
        return constants.DEFAULT_KEEPALIVE_INTERVAL
    if interval <= 0:
        return 0.0
    return max(constants.MIN_KEEPALIVE_INTERVAL, interval)


def numeric_option(value, field, *, integer=False, minimum=None, maximum=None):
    """Parse user input without echoing its potentially private value."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError(f"{field} 必须是{'整数' if integer else '有限数字'}")
    try:
        if integer:
            if not re.fullmatch(r"[+-]?[0-9]+", str(value).strip()):
                raise ValueError
            number = int(value)
        else:
            number = float(value)
        if not isfinite(number):
            raise ValueError
    except (ValueError, OverflowError):
        raise ValueError(f"{field} 必须是{'整数' if integer else '有限数字'}") from None
    if minimum is not None and number < minimum:
        raise ValueError(f"{field} 不能小于 {minimum}")
    if maximum is not None and number > maximum:
        raise ValueError(f"{field} 不能大于 {maximum}")
    return number


def validate_request_options(options):
    result = dict(options)
    result['timeout'] = numeric_option(options.get('timeout', 10), 'request.timeout', minimum=0.001)
    interval = numeric_option(options.get('keepalive_interval', constants.DEFAULT_KEEPALIVE_INTERVAL),
                              'request.keepalive_interval', minimum=0)
    result['keepalive_interval'] = normalize_keepalive_interval(interval)
    return result


def validate_booking_options(options, *, require_plan=True):
    """Shared by offline checking, CLI overrides and web form submissions."""
    result = dict(options)
    if require_plan or 'plan' in options:
        if not isinstance(options.get('plan'), str):
            raise ValueError('booking.plan 必须是计划字符串')
        plan = parse_plan(options['plan'])
        if (plan['room_type'] < 1 or plan['floor_id'] < 1 or not plan['seat_num'].isascii()
                or not plan['seat_num'].isdigit() or not 0 <= plan['start_hour'] <= 23
                or plan['duration_hours'] < 1 or plan['start_hour'] + plan['duration_hours'] > 24):
            raise ValueError('booking.plan 的房间、楼层、座位、小时或时长无效')
        fallback = options.get('fallback_seats', '')
        if fallback is not None and not isinstance(fallback, (str, list, tuple)):
            raise ValueError('booking.fallback_seats 必须是座位号列表')
        result['fallback_seats'] = ','.join(parse_fallback_seats(fallback, primary_seat=plan['seat_num']))
    result['book_days'] = numeric_option(options.get('book_days', constants.DEFAULT_BOOK_DAYS),
                                        'booking.book_days', integer=True, minimum=0, maximum=2)
    result['max_trials'] = numeric_option(options.get('max_trials', constants.DEFAULT_MAX_TRIALS),
                                         'booking.max_trials', integer=True, minimum=1, maximum=20)
    delay = numeric_option(options.get('retry_delay', constants.DEFAULT_RETRY_DELAY), 'booking.retry_delay')
    result['retry_delay'] = normalize_retry_delay(delay)
    result['hold_before_minutes'] = numeric_option(options.get('hold_before_minutes', constants.DEFAULT_HOLD_BEFORE_MINUTES),
                                                   'booking.hold_before_minutes', integer=True, minimum=0, maximum=14)
    execute_at = options.get('execute_at', '')
    if execute_at is not None and not isinstance(execute_at, str):
        raise ValueError('booking.execute_at 必须是时间字符串')
    result['execute_at'] = normalize_execute_at(execute_at)
    if result['hold_before_minutes'] and not result['execute_at']:
        raise ValueError('booking.hold_before_minutes 启用时必须设置 booking.execute_at')
    dry_run = options.get('dry_run', False)
    if not isinstance(dry_run, bool):
        raise ValueError('booking.dry_run 必须是 true 或 false，不要加引号')
    result['dry_run'] = dry_run
    return result


def load_config(path):
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ValueError(
            "配置文件不存在。请在项目目录复制 config.example.yaml 为 config.yaml，"
            "按 README 填写登录态；自定义配置使用 --config 指定。"
        ) from exc
    except UnicodeDecodeError as exc:
        raise ValueError("配置文件必须使用 UTF-8 编码") from exc
    try:
        config = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = f"（第 {mark.line + 1} 行，第 {mark.column + 1} 列）" if mark else ""
        # Do not echo a YAML excerpt: it can contain Cookie values.
        raise ValueError(f"配置 YAML 格式错误{location}，请检查缩进和引号") from exc
    if not isinstance(config, dict):
        raise ValueError("配置内容必须是 YAML 字段映射，请参考 config.example.yaml")
    for section in ("urls", "session", "auth", "request", "booking", "user_info"):
        if section in config and not isinstance(config[section], dict):
            raise ValueError(f"配置 {section} 必须是字段映射，请检查缩进")
    session = config.get("session", {})
    for key in ("headers", "params"):
        if key in session and not isinstance(session[key], dict):
            raise ValueError(f"配置 session.{key} 必须是字段映射")
    for section, key in (("session", "verify"), ("session", "trust_env"), ("booking", "dry_run")):
        if key in config.get(section, {}) and not isinstance(config[section][key], bool):
            raise ValueError(f"配置 {section}.{key} 必须是 true 或 false，不要加引号")
    for key in ("cookie", "cookie_file"):
        if key in config.get("auth", {}) and not isinstance(config["auth"][key], str):
            raise ValueError(f"配置 auth.{key} 必须是字符串")
    if 'request' in config:
        config['request'] = validate_request_options(config['request'])
    if 'booking' in config:
        config['booking'] = validate_booking_options(config['booking'], require_plan=False)
    return config


def parse_plan(plan_text):
    try:
        room_type, floor_id, seat_num, start_hour, duration_hours = plan_text.split(":")
        return {
            "room_type": int(room_type),
            "floor_id": int(floor_id),
            "seat_num": str(seat_num),
            "start_hour": int(start_hour),
            "duration_hours": int(duration_hours),
        }
    except Exception as exc:
        raise ValueError("plan 格式应为 roomType:floorId:seatNum:startHour:durationHours") from exc


def parse_fallback_seats(value, primary_seat=None):
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        raw_items = [str(item).strip() for item in value]
    else:
        raw_items = re.split(r"[\s,，]+", str(value).strip())

    primary = str(primary_seat or "").strip()
    seats = []
    for item in raw_items:
        seat = str(item).strip()
        if not seat or seat == primary or seat in seats:
            continue
        if not seat.isdigit():
            raise ValueError(f"备选座位号必须是数字：{seat}")
        seats.append(seat)
    if len(seats) > constants.MAX_FALLBACK_SEATS:
        raise ValueError(f"备选座位最多填写 {constants.MAX_FALLBACK_SEATS} 个")
    return seats


def parse_execute_at(value):
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%H:%M:%S.%f", "%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            pass
    raise ValueError("execute_at 格式应为 HH:MM、HH:MM:SS 或 HH:MM:SS.sss")


def normalize_execute_at(value):
    parsed = parse_execute_at(value)
    if parsed is None:
        return ""
    base = f"{parsed.hour:02d}:{parsed.minute:02d}:{parsed.second:02d}"
    if parsed.microsecond:
        return f"{base}.{parsed.microsecond // 1000:03d}"
    return base


def normalize_check_in_delay_minutes(value):
    try:
        minutes = float(value)
    except (TypeError, ValueError):
        return constants.DEFAULT_AUTO_CHECK_IN_DELAY_MINUTES
    return max(0.0, min(minutes, 120.0))
