"""Web form translation, config path policy and atomic plan persistence."""

import os
import re
import tempfile
from pathlib import Path

import yaml

from libcs import configuration, constants, login
from libcs.web import settings

CONFIG_LOCK = login.CONFIG_LOCK


def booking_form_from_config(config_path):
    path = web_config_path(config_path)
    config = configuration.load_config(path)
    booking = configuration.validate_booking_options(config.get("booking") or {})
    plan = configuration.parse_plan(booking['plan'])
    fallback_seats = booking['fallback_seats']
    return {
        "config_path": config_path_for_display(path),
        "room_type": plan["room_type"],
        "floor_id": plan["floor_id"],
        "seat_num": plan["seat_num"],
        "fallback_seats": fallback_seats,
        "start_hour": plan["start_hour"],
        "duration_hours": plan["duration_hours"],
        "execute_at": booking["execute_at"],
        "max_trials": booking["max_trials"],
        "retry_delay": booking["retry_delay"],
        "hold_before_minutes": booking["hold_before_minutes"],
        "days": booking["book_days"],
        "dry_run": booking["dry_run"],
    }


def booking_execute_at_from_payload(payload, force_immediate=False):
    if force_immediate:
        return ""
    return configuration.normalize_execute_at(payload.get("execute_at"))


def plan_from_payload(payload):
    required = ("room_type", "floor_id", "seat_num", "start_hour", "duration_hours")
    values = {}
    for key in required:
        value = str(payload.get(key, "")).strip()
        if not value:
            raise ValueError(f"{key} 不能为空")
        values[key] = value

    plan_text = (
        f"{values['room_type']}:{values['floor_id']}:{values['seat_num']}:"
        f"{values['start_hour']}:{values['duration_hours']}"
    )
    plan = configuration.parse_plan(plan_text)
    if not 0 <= plan["start_hour"] <= 23:
        raise ValueError("开始小时必须在 0 到 23 之间")
    if plan["duration_hours"] <= 0:
        raise ValueError("时长必须大于 0")
    return plan_text


def booking_options_from_payload(payload, force_immediate=False):
    return configuration.validate_booking_options({
        'plan': plan_from_payload(payload),
        'fallback_seats': payload.get('fallback_seats', ''),
        'book_days': payload.get('days', 1),
        'dry_run': payload.get('dry_run', False),
        'execute_at': booking_execute_at_from_payload(payload, force_immediate),
        'max_trials': payload.get('max_trials', constants.DEFAULT_MAX_TRIALS),
        'retry_delay': payload.get('retry_delay', constants.DEFAULT_RETRY_DELAY),
        'hold_before_minutes': 0 if force_immediate else payload.get('hold_before_minutes', constants.DEFAULT_HOLD_BEFORE_MINUTES),
    })


def config_path_from_payload(payload):
    raw_path = str(payload.get("config_path") or settings.DEFAULT_CONFIG).strip()
    return web_config_path(raw_path)


def web_config_path(raw_path):
    """Resolve the UI config path and lock remote sessions to the default file."""
    value = str(raw_path or settings.DEFAULT_CONFIG).strip()
    path = settings.DEFAULT_CONFIG if value == settings.DEFAULT_CONFIG.name else Path(value).expanduser()
    if settings.WEB_AUTH_PASSWORD and path.resolve() != settings.DEFAULT_CONFIG.resolve():
        raise ValueError("远程访问仅允许使用默认配置文件")
    return path


def config_path_for_display(path):
    if path.resolve() == settings.DEFAULT_CONFIG.resolve():
        return settings.DEFAULT_CONFIG.name
    return str(path)


def atomic_write_config(path, text):
    config = yaml.safe_load(text)
    if not isinstance(config, dict) or not isinstance(config.get("booking"), dict):
        raise ValueError("配置必须包含 booking 配置项")
    configuration.parse_plan(str(config["booking"].get("plan") or ""))
    path = Path(path).resolve()
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as file:
            temp_path = Path(file.name)
            os.fchmod(file.fileno(), path.stat().st_mode & 0o600)
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def write_booking_values(
    path,
    plan_text,
    fallback_seats,
    days,
    dry_run,
    execute_at,
    max_trials,
    retry_delay,
    hold_before_minutes=constants.DEFAULT_HOLD_BEFORE_MINUTES,
):
    options = configuration.validate_booking_options(dict(plan=plan_text, fallback_seats=fallback_seats,
        book_days=days, dry_run=dry_run, execute_at=execute_at, max_trials=max_trials,
        retry_delay=retry_delay, hold_before_minutes=hold_before_minutes))
    values = {
        "plan": options['plan'],
        "fallback_seats": f"'{options['fallback_seats']}'",
        "execute_at": f"'{options['execute_at']}'",
        "max_trials": str(options['max_trials']),
        "retry_delay": f"{options['retry_delay']:g}",
        "hold_before_minutes": str(options['hold_before_minutes']),
        "dry_run": "true" if options['dry_run'] else "false",
    }
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    booking_start = find_booking_section(lines)

    if booking_start is None:
        prefix = "" if not text or text.endswith("\n") else "\n"
        block = (
            f"{prefix}booking:\n"
            f"  plan: {values['plan']}\n"
            f"  fallback_seats: {values['fallback_seats']}\n"
            f"  execute_at: {values['execute_at']}\n"
            f"  max_trials: {values['max_trials']}\n"
            f"  retry_delay: {values['retry_delay']}\n"
            f"  hold_before_minutes: {values['hold_before_minutes']}\n"
            f"  dry_run: {values['dry_run']}\n"
        )
        atomic_write_config(path, text + block)
        return

    booking_end = find_section_end(lines, booking_start)
    seen = set()
    for index in range(booking_start + 1, booking_end):
        match = re.match(
            r"^(\s+)(plan|fallback_seats|book_days|execute_at|max_trials|retry_delay|hold_before_minutes|dry_run)\s*:",
            lines[index],
        )
        if not match:
            continue
        indent, key = match.groups()
        if key == "book_days":
            lines[index] = ""
            continue
        seen.add(key)
        lines[index] = f"{indent}{key}: {values[key]}{inline_comment(lines[index])}\n"

    missing = [
        key
        for key in (
            "plan",
            "fallback_seats",
            "execute_at",
            "max_trials",
            "retry_delay",
            "hold_before_minutes",
            "dry_run",
        )
        if key not in seen
    ]
    if missing:
        if booking_end > 0 and lines[booking_end - 1] and not lines[booking_end - 1].endswith("\n"):
            lines[booking_end - 1] += "\n"
        lines[booking_end:booking_end] = [f"  {key}: {values[key]}\n" for key in missing]
    atomic_write_config(path, "".join(lines))


def find_booking_section(lines):
    for index, line in enumerate(lines):
        if re.match(r"^booking\s*:", line):
            return index
    return None


def find_section_end(lines, section_start):
    for index in range(section_start + 1, len(lines)):
        line = lines[index]
        if line.strip() and not line.startswith((" ", "\t", "#")) and ":" in line:
            return index
    return len(lines)


def inline_comment(line):
    body = line.rstrip("\r\n")
    if "#" not in body:
        return ""
    before, after = body.split("#", 1)
    return f" #{after}" if before.strip() else ""
