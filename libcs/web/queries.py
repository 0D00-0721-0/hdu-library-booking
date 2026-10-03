"""Read-only web views of bookings, seat maps and server time."""

import time
from datetime import datetime

from libcs import client, configuration, operations, scheduling
from libcs.web import forms, jobs


def bookings_from_config(config_path):
    path = forms.web_config_path(config_path)
    return {
        "config_path": forms.config_path_for_display(path),
        "items": operations.get_current_bookings(path),
    }


def seat_map_from_config(config_path, room_type, floor_id, days, start_hour, duration_hours):
    """Return only the floor plan and seat coordinates needed by the UI."""
    with jobs.JOBS_LOCK:
        job = jobs.running_job_locked()
        if (
            job and job["job_type"] == "booking"
            and job.get("execute_timestamp") is not None
            and job["execute_timestamp"] - time.time() < 20
        ):
            raise ValueError("距离预约提交不足 20 秒，请稍后刷新座位图")
    path = forms.web_config_path(config_path)
    config = configuration.load_config(path)
    room_type = int(room_type)
    floor_id = str(floor_id).strip()
    days = int(days)
    start_hour = int(start_hour)
    duration_hours = int(duration_hours)
    if not floor_id.isdigit() or days not in (0, 1, 2):
        raise ValueError("请选择有效的楼层和预约日期")
    if not 0 <= start_hour <= 23 or not 1 <= duration_hours <= 24 or start_hour + duration_hours > 24:
        raise ValueError("请选择有效的预约时段")

    booker = client.InstantBooker(config)
    try:
        booker.load_cookies()
        room_items = booker.query_room_items()
        if not 1 <= room_type <= len(room_items):
            raise ValueError("房间类型不存在")
        detail = booker.query_room_detail(room_items[room_type - 1])
        begin_time = scheduling.build_begin_time(start_hour, days)
        # The exact slot supplies availability. If it is not yet open, a
        # fallback query still supplies the real floor plan and coordinates.
        exact = True
        try:
            floors = booker._query_seat_map_once(detail, begin_time, duration_hours)
            if not any(str((f.get("seatMap") or {}).get("info", {}).get("id")) == floor_id for f in floors):
                exact = False
                floors = booker.query_seat_map(detail, begin_time, duration_hours, target_floor_id=floor_id)
        except Exception:
            exact = False
            floors = booker.query_seat_map(detail, begin_time, duration_hours, target_floor_id=floor_id)
        floor = next((f for f in floors if str((f.get("seatMap") or {}).get("info", {}).get("id")) == floor_id), None)
        if floor is None:
            raise ValueError("所选区域没有座位图，请检查房间类型和楼层")
        seat_map = floor.get("seatMap") or {}
        info = seat_map.get("info") or {}
        width, height = float(info["width"]), float(info["height"])
        if not 0 < width <= 1000 or not 0 < height <= 1000:
            raise ValueError("座位图尺寸无效")
        seats = []
        for item in seat_map.get("POIs") or []:
            try:
                seat = {
                    "number": str(item["title"]),
                    "x": float(item["x"]), "y": float(item["y"]),
                    "w": float(item.get("w") or 2), "h": float(item.get("h") or 2),
                    "state": str(item.get("state", "")) if exact else "",
                }
            except (KeyError, TypeError, ValueError):
                continue
            if all(0 <= seat[key] <= 1000 for key in ("x", "y", "w", "h")):
                seats.append(seat)
        return {
            "floor_id": floor_id,
            "floor_name": str(floor.get("roomName") or info.get("title") or ""),
            "plan_url": str(info.get("plan") or ""),
            "width": width, "height": height,
            "seats": seats, "availability_exact": exact,
            "fetched_at": datetime.now().astimezone().strftime("%H:%M:%S"),
        }
    finally:
        booker.session.close()


def clock_offset_from_config(config_path):
    with jobs.JOBS_LOCK:
        job = jobs.running_job_locked()
        if (
            job and job["job_type"] == "booking"
            and job.get("execute_timestamp") is not None
            and job["execute_timestamp"] - time.time() < 20
        ):
            raise ValueError("距离预约提交不足 20 秒，已跳过测量以免影响发包")
    return operations.measure_server_clock(forms.web_config_path(config_path))
