import argparse
import base64
import hashlib
import json
import os
import re
import sys
import threading
import time
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta
from math import ceil, floor, isfinite
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import requests
import yaml

from privacy import diagnostic_json, redact_diagnostic_data


DEFAULT_CONFIG = Path(__file__).with_name("config.yaml")
DEFAULT_BOOK_DAYS = 2
DEFAULT_MAX_TRIALS = 10
DEFAULT_RETRY_DELAY = 3.0
DEFAULT_HOLD_BEFORE_MINUTES = 0
DEFAULT_KEEPALIVE_INTERVAL = 120.0
MIN_KEEPALIVE_INTERVAL = 10.0
MIN_RETRY_DELAY = 3.0
EXECUTE_GRACE_SECONDS = 5.0
HEARTBEAT_GUARD_SECONDS = 35.0
DEFAULT_WARMUP_BEFORE_SECONDS = 5.0
MIN_WARMUP_REMAINING_SECONDS = 3.0
MSG_TIME_OUT_OF_RANGE = "超出可预约座位时间范围"
MSG_DUPLICATE = "已有预约，请勿重复预约"
MSG_SEAT_UNAVAILABLE = "选择的座位无法预约"
MAX_FALLBACK_SEATS = 5
DEFAULT_MY_BOOKINGS_URL = "https://hdu.huitu.zhishulib.com/Seat/Index/myBookingList"
DEFAULT_CANCEL_BOOKING_URL = "https://hdu.huitu.zhishulib.com/Seat/Index/cancelBooking"
DEFAULT_CANCEL_TIMES_LIMIT_URL = "https://hdu.huitu.zhishulib.com/Seat/Index/cancelTimesLimit"
DEFAULT_CHECK_IN_URL = "https://hdu.huitu.zhishulib.com/Seat/Index/checkIn"
DEFAULT_COME_BACK_URL = "https://hdu.huitu.zhishulib.com/Seat/Index/comeBack"
DEFAULT_AUTO_CHECK_IN_DELAY_MINUTES = 5.0
BOOKING_STATUS_LABELS = {
    "0": "待签到",
    "1": "签到成功，使用中",
    "2": "暂离中",
    "3": "已结束",
    "4": "已取消",
    "5": "已结束",
    "6": "暂离未归结束",
    "7": "系统签退结束",
    "8": "预约待确认",
    "9": "拒绝预约",
}
CANCELABLE_BOOKING_STATUSES = {"0", "8"}
CONTINUABLE_BOOKING_STATUSES = {"2"}
ACTIVE_BOOKING_STATUSES = {"0", "1", "2", "8"}


class TaskCancelled(RuntimeError):
    """No further operation was sent after the cancellation request."""


class ResultUncertain(RuntimeError):
    """An operation was sent but its outcome could not be confirmed."""


class RequestFailure(RuntimeError):
    def __init__(self, message, retryable=False, outcome_unknown=None):
        super().__init__(message)
        self.retryable = bool(retryable)
        self.outcome_unknown = self.retryable if outcome_unknown is None else bool(outcome_unknown)


def normalize_retry_delay(value):
    try:
        delay = float(value)
    except (TypeError, ValueError):
        return DEFAULT_RETRY_DELAY
    if not isfinite(delay):
        return DEFAULT_RETRY_DELAY
    return max(MIN_RETRY_DELAY, min(delay, 10.0))


def clock_offset_bounds(samples):
    """Bound server-minus-local clock offset using timestamp precision and RTT.

    Each server timestamp was generated between the local send and receive
    times. Integer Unix seconds are treated as a one-second interval, not as
    an exact instant. Intersecting independent samples can narrow the bound.
    """
    intervals = []
    for sample in samples:
        try:
            raw = sample["server_time"]
            server = Decimal(str(raw))
            sent = float(sample["sent_at"])
            received = float(sample["received_at"])
            if not server.is_finite() or not all(map(isfinite, (sent, received))):
                continue
            if received < sent or received - sent > 10 or abs(float(server) - received) > 86400:
                continue
            # PHP-style integer seconds are truncated. Preserve any finer
            # decimal precision if the endpoint starts returning it later.
            resolution = 1.0 if server == server.to_integral_value() else float(
                Decimal(1).scaleb(server.as_tuple().exponent)
            )
            intervals.append((float(server) - received, float(server) + resolution - sent))
        except (KeyError, TypeError, ValueError, OverflowError, InvalidOperation):
            continue
    if not intervals:
        return None
    lower = max(interval[0] for interval in intervals)
    upper = min(interval[1] for interval in intervals)
    if lower > upper:
        return {"consistent": False, "samples": len(intervals)}
    return {"consistent": True, "lower": lower, "upper": upper,
            "samples": len(intervals), "uncertainty": upper - lower}


def clock_offset_message(bounds):
    if bounds is None:
        return "服务端时钟差：接口未提供可用的时间样本"
    if not bounds["consistent"]:
        return "服务端时钟差：多次样本不一致，无法可靠估算"
    lower, upper = bounds["lower"], bounds["upper"]
    count = bounds["samples"]
    if lower <= 0 <= upper:
        return (
            f"服务端与本机的时差最多约 {max(-lower, upper):.3f} 秒；"
            f"本次无法判断谁快（{count} 次样本）"
        )
    if upper < 0:
        return f"服务端可能比本机慢 {-upper:.3f}～{-lower:.3f} 秒（{count} 次样本）"
    return f"服务端可能比本机快 {lower:.3f}～{upper:.3f} 秒（{count} 次样本）"


def recommend_booking_execute_time(bounds):
    """Recommend a local send time for a known 20:00:00 server opening."""
    if not bounds or not bounds["consistent"]:
        return {
            "execute_at": None,
            "basis": "时差测量不可用，无法按服务器 20:00:00 的开放时间计算；请重测",
        }
    if bounds["lower"] < -2 or bounds["upper"] > 2:
        return {
            "execute_at": None,
            "basis": "时钟差可能超过 2 秒，请先检查本机时间同步，再重新测量",
        }
    if bounds["uncertainty"] > 1.0:
        return {
            "execute_at": None,
            "basis": "时差范围超过 1 秒，无法可靠计算开放时间；请重测",
        }
    possible_local_lead_ms = max(0.0, -bounds["lower"] * 1000)
    suggested_ms = ceil(possible_local_lead_ms - 1e-9)
    return {
        "execute_at": f"20:00:{suggested_ms // 1000:02d}.{suggested_ms % 1000:03d}",
        "basis": (
            "按服务器 20:00:00 开放、本机可能超前的最大幅度计算，"
            "未额外加等待，也未用未知的单程网络延迟提前发包；"
            "以座位图和预约接口使用同一时钟为前提"
        ),
    }


class BookingSubmissionGate:
    """Serialize one account's POSTs and cool down after every response/error."""

    def __init__(self):
        self.lock = threading.Lock()
        self.next_allowed_at = 0.0

    @contextmanager
    def submission(self, should_cancel=None):
        def check_cancel():
            if should_cancel and should_cancel():
                raise TaskCancelled("任务已取消")

        check_cancel()
        while not self.lock.acquire(timeout=0.1):
            check_cancel()
        try:
            while True:
                check_cancel()
                remaining = self.next_allowed_at - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(0.1, remaining))
            try:
                yield
            finally:
                self.next_allowed_at = time.monotonic() + MIN_RETRY_DELAY
        finally:
            self.lock.release()


_BOOKING_GATES = {}
_BOOKING_GATES_LOCK = threading.Lock()


def booking_gate_for(uid):
    # Shared by successive jobs and different clients in this Python process.
    with _BOOKING_GATES_LOCK:
        key = str(uid)
        if key not in _BOOKING_GATES:
            _BOOKING_GATES[key] = BookingSubmissionGate()
        return _BOOKING_GATES[key]


def prepare_booking_request(uid, seat_id, begin_time, duration_hours, is_recommend=0):
    payload = {
        "beginTime": int(begin_time.timestamp()),
        "duration": duration_hours * 3600,
        "is_recommend": int(is_recommend),
        "api_time": 0,
        "seats[0]": str(seat_id),
        "seatBookers[0]": str(uid),
    }
    token_suffix = (
        f"&beginTime{payload['beginTime']}"
        f"&duration{payload['duration']}"
        f"&is_recommend{payload['is_recommend']}"
        f"&seatBookers[0]{payload['seatBookers[0]']}"
        f"&seats[0]{payload['seats[0]']}"
    )
    return {"payload": payload, "token_suffix": token_suffix}


def normalize_keepalive_interval(value):
    try:
        interval = float(value)
    except (TypeError, ValueError):
        return DEFAULT_KEEPALIVE_INTERVAL
    if interval <= 0:
        return 0.0
    return max(MIN_KEEPALIVE_INTERVAL, interval)


class InstantBooker:
    def __init__(self, config):
        self.config = config
        self.urls = config["urls"]
        request_config = config.get("request") or {}
        self.timeout = int(request_config.get("timeout") or 10)
        self.keepalive_interval = normalize_keepalive_interval(
            request_config.get("keepalive_interval", DEFAULT_KEEPALIVE_INTERVAL)
        )
        self.session = requests.Session()
        self.session.headers.update(config["session"]["headers"])
        # A fixed Cookie header overrides requests' cookie jar and prevents
        # rotated Set-Cookie values from being sent on later requests.
        self.session.headers.pop("Cookie", None)
        self.session.params = config["session"].get("params") or {"LAB_JSON": "1"}
        self.session.trust_env = bool(config["session"].get("trust_env", False))
        self.session.verify = bool(config["session"].get("verify", True))

        self.uid = str((config.get("user_info") or {}).get("uid") or "")
        self.name = str((config.get("user_info") or {}).get("name") or "")
        self.auth_probe_url = None
        self.last_seat_query_meta = {}
        self.last_seat_query_payload = None
        self.last_submission_timing = None

    def load_cookies(self):
        auth = self.config.get("auth") or {}
        loaded = False
        if auth.get("cookie"):
            loaded = self._load_cookie_header(auth["cookie"]) or loaded
        if auth.get("cookie_file"):
            loaded = self._load_cookie_file(auth["cookie_file"]) or loaded
        if not loaded:
            raise RuntimeError("没有加载到 cookie。请检查 config.yaml 里的 auth.cookie_file。")

    def _load_cookie_header(self, cookie_header):
        loaded = False
        for part in cookie_header.split(";"):
            if "=" not in part:
                continue
            name, value = part.split("=", 1)
            name = name.strip()
            value = value.strip()
            if not name:
                continue
            self.session.cookies.set(name, value, domain="hdu.huitu.zhishulib.com", path="/")
            loaded = True
        return loaded

    def _load_cookie_file(self, cookie_file):
        path = Path(os.path.expanduser(cookie_file))
        if not path.is_absolute():
            path = Path.cwd() / path
        if not path.exists():
            raise RuntimeError("Cookie 文件不存在，请检查 auth.cookie_file 配置")

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("Cookie 文件必须是 UTF-8 JSON，请参考 README 的登录态格式") from exc
        self._apply_user_info_candidate(self._find_user_info(data))
        return self._load_cookie_json(data)

    def _load_cookie_json(self, data):
        cookies = data.get("cookies") if isinstance(data, dict) else data
        if not isinstance(cookies, list):
            return False

        loaded = False
        for item in cookies:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            value = item.get("value")
            if not name or value is None:
                continue
            cookie = requests.cookies.create_cookie(
                name=str(name),
                value=str(value),
                domain=item.get("domain") or "hdu.huitu.zhishulib.com",
                path=item.get("path") or "/",
                secure=bool(item.get("secure", False)),
            )
            self.session.cookies.set_cookie(cookie)
            loaded = True
        return loaded

    def resolve_user(self):
        if self.uid:
            return
        for key in ("user_base_info", "user_center"):
            url = self.urls.get(key)
            if not url:
                continue
            data = self.request("GET", url)
            if self._apply_user_info_candidate(self._find_user_info(data)):
                return
        raise RuntimeError("未能识别用户 uid。请在 config.yaml 的 user_info.uid 中填写慧图内部 uid。")

    def keepalive(self, timeout=None):
        if self.auth_probe_url:
            data = self.request("GET", self.auth_probe_url, timeout=timeout)
            detail = data.get("data") if isinstance(data, dict) else None
            self._validate_authenticated_detail(detail)
            return "room_detail"

        room_items = self.query_room_items(timeout=timeout)
        if not room_items:
            raise RuntimeError("没有可用的登录态校验接口")
        self.query_room_detail(room_items[0], timeout=timeout)
        return "room_detail"

    def _validate_authenticated_detail(self, detail):
        if not isinstance(detail, dict):
            raise RuntimeError("登录态校验失败：房间接口未返回用户信息")
        is_login = detail.get("is_login")
        if is_login not in (True, 1, "1"):
            raise RuntimeError("登录态已失效，请重新获取 Cookie")
        remote_uid = str(detail.get("uid") or "")
        if not remote_uid:
            raise RuntimeError("登录态校验失败：接口未返回 uid")
        if self.uid and remote_uid != self.uid:
            raise RuntimeError("登录用户不匹配，请更新登录态或用户配置")
        self.uid = remote_uid
        if not self.name:
            self.name = str(detail.get("uname") or detail.get("unickname") or "")
        return True

    def _apply_user_info_candidate(self, candidate):
        if not candidate or not candidate.get("uid"):
            return False
        if not self.uid:
            self.uid = str(candidate["uid"])
        if not self.name and candidate.get("name"):
            self.name = str(candidate["name"])
        return True

    def _find_user_info(self, data):
        candidates = []

        def walk(obj, hint=""):
            if isinstance(obj, dict):
                if "name" in obj and "value" in obj and isinstance(obj.get("value"), str):
                    walk(obj["value"], str(obj.get("name") or hint))
                candidate = self._user_info_from_dict(obj, hint)
                if candidate:
                    candidates.append(candidate)
                for key, value in obj.items():
                    walk(value, f"{hint}.{key}" if hint else str(key))
            elif isinstance(obj, list):
                for item in obj:
                    walk(item, hint)
            elif isinstance(obj, str):
                value = obj.strip()
                if value and value[0] in "[{":
                    try:
                        walk(json.loads(value), hint)
                    except Exception:
                        pass

        walk(data)
        if not candidates:
            return None
        candidates.sort(key=lambda item: item.get("score", 0), reverse=True)
        return candidates[0]

    def _user_info_from_dict(self, data, hint=""):
        id_keys = ("uid", "user_id", "userId", "booker", "id")
        name_keys = ("name", "real_name", "realName", "bookerName", "username", "login_name", "nickname")
        uid = None
        name = None
        for key in id_keys:
            value = data.get(key)
            if value is not None and str(value).isdigit():
                uid = str(value)
                break
        for key in name_keys:
            value = data.get(key)
            if value:
                name = str(value)
                break

        score = 1 if name else 0
        hint = hint.lower()
        for keyword in ("current", "user", "login", "lab4"):
            if keyword in hint:
                score += 2
        if uid and (score > 0 or name):
            return {"uid": uid, "name": name, "score": score}
        return None

    def request(self, method, url, data=None, headers=None, timeout=None):
        request_timeout = self.timeout if timeout is None else timeout
        try:
            if method == "GET":
                response = self.session.get(
                    url,
                    headers=headers,
                    timeout=request_timeout,
                    allow_redirects=False,
                )
            else:
                response = self.session.post(
                    url,
                    data=data,
                    headers=headers,
                    timeout=request_timeout,
                    allow_redirects=False,
                )
        except requests.RequestException as exc:
            definitely_not_sent = isinstance(exc, (requests.ConnectTimeout, requests.exceptions.SSLError))
            raise RequestFailure(
                f"网络请求失败：{exc}",
                retryable=not isinstance(exc, requests.exceptions.SSLError),
                outcome_unknown=method != "GET" and not definitely_not_sent,
            ) from exc

        if response.status_code in (301, 302, 303, 307, 308, 401, 403):
            raise RequestFailure(
                f"登录态可能已失效：HTTP {response.status_code} {url}",
                retryable=False,
            )
        if response.status_code == 429 or response.status_code >= 500:
            raise RequestFailure(
                f"服务端暂时不可用：HTTP {response.status_code} {url}",
                retryable=True,
                outcome_unknown=method != "GET" and response.status_code >= 500,
            )
        if response.status_code != 200:
            raise RequestFailure(
                f"请求失败：HTTP {response.status_code} {url}",
                retryable=False,
            )
        try:
            return response.json()
        except Exception as exc:
            raise RequestFailure(
                f"JSON 解析失败：{exc}", retryable=False, outcome_unknown=method != "GET"
            ) from exc

    def query_room_items(self, timeout=None):
        data = self.request("GET", self.urls["query_rooms"], timeout=timeout)
        raw_items = data["content"]["children"][1]["defaultItems"]
        room_items = []
        for item in raw_items:
            url = unquote(item["link"]["url"])
            query = url.split("?", 1)[1]
            room_items.append({"name": item["name"], "query": query})
        return room_items

    def query_room_detail(self, room_item, timeout=None):
        url = self.urls["query_seats"] + "?" + room_item["query"]
        data = self.request("GET", url, timeout=timeout)
        detail = data.get("data")
        if not detail:
            raise RuntimeError(f"房间信息为空：{room_item['name']}")
        self._validate_authenticated_detail(detail)
        self.auth_probe_url = url
        return detail

    def validate_booking_time(self, room_detail, start_hour, duration_hours, begin_time=None):
        range_info = room_detail.get("range") or {}
        min_begin = range_info.get("minBeginTime")
        max_end = range_info.get("maxEndTime")
        if min_begin is not None and max_end is not None:
            min_begin = int(min_begin)
            max_end = int(max_end)
            end_hour = int(start_hour) + int(duration_hours)
            if int(start_hour) < min_begin or int(start_hour) >= max_end:
                raise RuntimeError(f"开始小时不在可预约范围内：允许 {min_begin}:00-{max_end}:00")
            if end_hour > max_end:
                raise RuntimeError(f"预约结束时间超出范围：允许最晚到 {max_end}:00")

        min_duration = range_info.get("min_duration")
        max_duration = range_info.get("max_duration")
        if min_duration is not None and int(duration_hours) < int(min_duration):
            raise RuntimeError(f"预约时长过短：最少 {int(min_duration)} 小时")
        if max_duration is not None and int(duration_hours) > int(max_duration):
            raise RuntimeError(f"预约时长过长：最多 {int(max_duration)} 小时")

        if begin_time is not None:
            begin_date = begin_time.astimezone().date()
            advance_date = range_info.get("advance_date")
            max_date = range_info.get("max_date")
            if advance_date is not None:
                earliest = datetime.fromtimestamp(float(advance_date)).astimezone().date()
                if begin_date < earliest:
                    raise RuntimeError(f"预约日期过早：最早可预约 {earliest.isoformat()}")
            if max_date is not None:
                latest = datetime.fromtimestamp(float(max_date)).astimezone().date()
                if begin_date > latest:
                    raise RuntimeError(f"预约日期超出开放范围：最晚可预约 {latest.isoformat()}")

    def query_seat_map(self, room_detail, begin_time, duration_hours, target_floor_id=None, logger=None):
        candidates = []
        seen_candidates = set()

        def add_candidate(label, when, hours):
            candidate = (
                label,
                when.replace(minute=0, second=0, microsecond=0),
                max(1, int(hours)),
            )
            key = (int(candidate[1].timestamp()), candidate[2])
            if key not in seen_candidates:
                seen_candidates.add(key)
                candidates.append(candidate)

        add_candidate("目标完整时段", begin_time, duration_hours)
        add_candidate("目标开始1小时", begin_time, 1)
        add_candidate("目标日08:00", begin_time.replace(hour=8), 1)

        now = datetime.now().astimezone()
        if now.hour >= 22:
            lookup_time = (now + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
        elif now.hour < 7:
            lookup_time = now.replace(hour=8, minute=0, second=0, microsecond=0)
        else:
            lookup_time = now
        add_candidate("当前可用时段", lookup_time, 1)

        merged = []
        seen_floor_ids = set()
        last_error = None
        for label, lookup_time, hours in candidates:
            try:
                floors = self._query_seat_map_once(room_detail, lookup_time, hours)
            except Exception as exc:
                last_error = exc
                if logger:
                    logger(f"座位图查询[{label}]失败：{exc}")
                continue
            if logger:
                logger(f"座位图查询[{label}]：获取 {len(floors)} 个楼层/区域")
            for floor in floors:
                floor_id = str(floor.get("seatMap", {}).get("info", {}).get("id"))
                if floor_id and floor_id not in seen_floor_ids:
                    seen_floor_ids.add(floor_id)
                    merged.append(floor)
            if target_floor_id and any(
                str(floor.get("seatMap", {}).get("info", {}).get("id")) == str(target_floor_id)
                for floor in floors
            ):
                return floors
            if not target_floor_id:
                return floors

        if merged:
            return merged
        if last_error:
            raise last_error
        raise RuntimeError("座位分布查询为空")

    def _query_seat_map_once(self, room_detail, lookup_time, duration_hours, timeout=None):
        payload = {
            "beginTime": lookup_time.timestamp(),
            "duration": duration_hours * 3600,
            "num": 1,
            "space_category[category_id]": room_detail["space_category"]["category_id"],
            "space_category[content_id]": room_detail["space_category"]["content_id"],
        }
        self.last_seat_query_payload = payload
        sent_at = time.time()
        data = self.request("POST", self.urls["query_seats"], payload, timeout=timeout)
        received_at = time.time()
        self.last_seat_query_meta = {
            "is_recommend": data.get("isRecommend"),
            "requires_image_code": data.get("IsImgCode"),
            "server_time": data.get("nowTime"),
            "clock_sample": {
                "server_time": data.get("nowTime"),
                "sent_at": sent_at,
                "received_at": received_at,
            },
        }
        try:
            return data["allContent"]["children"][2]["children"]["children"]
        except Exception as exc:
            raise RuntimeError(f"座位分布解析失败：{exc}") from exc

    def query_clock_sample(self, timeout=None):
        """Repeat the last read-only seat-map request to sample server time."""
        if not self.last_seat_query_payload:
            raise RuntimeError("尚未查询座位图，无法测量服务端时间")
        sent_at = time.time()
        data = self.request(
            "POST", self.urls["query_seats"], self.last_seat_query_payload,
            timeout=timeout,
        )
        received_at = time.time()
        return {
            "server_time": data.get("nowTime"),
            "sent_at": sent_at,
            "received_at": received_at,
        }

    def find_seat(self, floors, floor_id, seat_num):
        floor_id = str(floor_id)
        seat_num = str(seat_num)
        target_floor = None
        for item in floors:
            info = item.get("seatMap", {}).get("info", {})
            if str(info.get("id")) == floor_id:
                target_floor = item
                break
        if not target_floor:
            available = ", ".join(
                f"{item.get('roomName')}={item.get('seatMap', {}).get('info', {}).get('id')}"
                for item in floors
            )
            raise RuntimeError(f"找不到楼层 id={floor_id}。可用楼层：{available}")

        seats = target_floor["seatMap"]["POIs"]
        matches = [item for item in seats if str(item.get("title")) == seat_num]
        if not matches:
            raise RuntimeError(f"{target_floor.get('roomName')} 中找不到 {seat_num} 座")
        if len(matches) > 1:
            raise RuntimeError(f"{target_floor.get('roomName')} 中存在多个 {seat_num} 座")
        return target_floor, matches[0]

    def lock_seat(self, seat_id, begin_time, duration_hours, is_recommend=0):
        """Ask the official temporary-hold endpoint to reserve one seat."""
        payload = {
            "beginTime": int(begin_time.timestamp()),
            "duration": int(duration_hours) * 3600,
            "seats[0]": str(seat_id),
            "is_recommend": int(is_recommend),
            "api_time": floor(time.time()),
        }
        url = urljoin(self.urls["book_seat"], "/Seat/Index/lockSeats")
        return self.request("POST", url, payload)

    def book(
        self, seat_id, begin_time, duration_hours, dry_run=False, is_recommend=0,
        prepared=None, should_cancel=None, before_submit=None,
    ):
        template = prepared if prepared is not None else prepare_booking_request(
            self.uid, seat_id, begin_time, duration_hours, is_recommend,
        )
        payload = dict(template["payload"])
        if dry_run:
            payload["api_time"] = floor(time.time())
            return {"dry_run": True, "payload": payload}

        self.last_submission_timing = None
        with booking_gate_for(self.uid).submission(should_cancel):
            if before_submit:
                before_submit()
            # Refresh dynamic fields after the cooldown. Cookies are also read
            # from the live Session when request() prepares the HTTP request.
            payload["api_time"] = floor(time.time())
            token_source = (
                "post&/Seat/Index/bookSeats?LAB_JSON=1"
                f"&api_time{payload['api_time']}{template['token_suffix']}"
            )
            md5 = hashlib.md5(token_source.encode("utf-8")).hexdigest()
            api_token = base64.b64encode(md5.encode("utf-8")).decode("utf-8")
            sent_at = datetime.now().astimezone()
            started = time.monotonic()
            try:
                return self.request(
                    "POST", self.urls["book_seat"], payload, headers={"Api-Token": api_token},
                )
            finally:
                self.last_submission_timing = {
                    "sent_at": sent_at,
                    "elapsed_ms": (time.monotonic() - started) * 1000,
                }

    def current_bookings(self):
        data = self.request("GET", self.urls.get("my_bookings", DEFAULT_MY_BOOKINGS_URL))
        if not isinstance(data, (dict, list)) or data == {}:
            raise RequestFailure("预约列表返回格式异常，不能确认预约状态")
        if isinstance(data, dict) and data.get("CODE") is not None:
            if str(data["CODE"]).strip().lower() != "ok":
                raise RequestFailure("预约列表查询失败，请检查登录态")
        items = extract_booking_items(data)
        if not items and not is_explicit_empty_booking_list(data):
            raise RequestFailure("预约列表结构无法识别，不能当作没有预约，请稍后刷新")
        return items

    def cancel_booking(self, booking_id, check_limit=True):
        booking_id = normalize_booking_id(booking_id)
        limit_result = None
        if check_limit:
            limit_url = append_booking_id(
                self.urls.get("cancel_times_limit", DEFAULT_CANCEL_TIMES_LIMIT_URL),
                booking_id,
            )
            limit_result = self.request("GET", limit_url)

        self.session.headers.pop("Api-Token", None)
        cancel_url = append_booking_id(
            self.urls.get("cancel_booking", DEFAULT_CANCEL_BOOKING_URL),
            booking_id,
        )
        result, confirmed = perform_confirmed_seat_action(
            self, booking_id, lambda: self.request("POST", cancel_url),
            expected_status="4", action_label="取消预约", logger=lambda _: None,
        )
        return {
            "booking_id": booking_id, "limit": limit_result, "result": result,
            "confirmed_booking": confirmed,
        }

    def check_in_booking(self, booking_id):
        booking_id = normalize_booking_id(booking_id)
        self.session.headers.pop("Api-Token", None)
        check_in_url = append_booking_id(
            self.urls.get("check_in", DEFAULT_CHECK_IN_URL),
            booking_id,
        )
        return self.request("POST", check_in_url)

    def continue_booking(self, booking_id):
        booking_id = normalize_booking_id(booking_id)
        self.session.headers.pop("Api-Token", None)
        come_back_url = append_booking_id(
            self.urls.get("come_back", DEFAULT_COME_BACK_URL),
            booking_id,
        )
        return self.request("POST", come_back_url)


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
    return config


def check_config(path):
    """Check local setup without contacting the library or printing credentials."""
    config = load_config(path)
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
    booking = config.get("booking", {})
    plan = parse_plan(str(booking.get("plan") or ""))
    if (plan["room_type"] < 1 or plan["floor_id"] < 1 or not plan["seat_num"].isdigit()
            or not 0 <= plan["start_hour"] <= 23 or plan["duration_hours"] < 1
            or plan["start_hour"] + plan["duration_hours"] > 24):
        raise ValueError("booking.plan 的房间、楼层、座位、小时或时长无效")
    parse_fallback_seats(booking.get("fallback_seats"), primary_seat=plan["seat_num"])
    if int(booking.get("book_days", DEFAULT_BOOK_DAYS)) not in (0, 1, 2):
        raise ValueError("booking.book_days 只支持 0、1、2")
    normalize_execute_at(booking.get("execute_at"))
    booker = InstantBooker(config)
    try:
        booker.load_cookies()
    finally:
        booker.session.close()


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
    if len(seats) > MAX_FALLBACK_SEATS:
        raise ValueError(f"备选座位最多填写 {MAX_FALLBACK_SEATS} 个")
    return seats


def build_begin_time(start_hour, book_days, now=None):
    now = now or datetime.now().astimezone()
    return (now + timedelta(days=book_days)).replace(
        hour=start_hour,
        minute=0,
        second=0,
        microsecond=0,
    )


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
        "status_label": BOOKING_STATUS_LABELS.get(status, f"状态 {status}" if status else "未知状态"),
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
        "auto_check_in_delay_minutes": DEFAULT_AUTO_CHECK_IN_DELAY_MINUTES,
        "auto_check_in_timestamp": int(auto_check_in_time.timestamp()) if auto_check_in_time else 0,
        "auto_check_in_text": format_datetime(auto_check_in_time),
        "space_minor": space.get("minor"),
        "ibeacons_count": len(ibeacons),
        "cancelable": status in CANCELABLE_BOOKING_STATUSES,
        "continuable": status in CONTINUABLE_BOOKING_STATUSES,
        "label": booking_item_label(room_name, seat_num, start_timestamp, duration_seconds, status),
    }


def booking_item_label(room_name, seat_num, start_timestamp, duration_seconds, status):
    parts = [
        format_timestamp(start_timestamp),
        room_name,
        f"{seat_num}座" if seat_num else "",
        format_duration(duration_seconds),
        BOOKING_STATUS_LABELS.get(status, f"状态 {status}" if status else "未知状态"),
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


def format_execute_datetime(value):
    if value is None:
        return "-"
    base = value.strftime("%Y-%m-%d %H:%M:%S")
    if value.microsecond:
        return f"{base}.{value.microsecond // 1000:03d}"
    return base


def build_execute_time(
    execute_at,
    now=None,
    grace_seconds=EXECUTE_GRACE_SECONDS,
    allow_next_day=False,
):
    parsed = parse_execute_at(execute_at)
    if parsed is None:
        return None
    now = now or datetime.now().astimezone()
    target = now.replace(
        hour=parsed.hour,
        minute=parsed.minute,
        second=parsed.second,
        microsecond=parsed.microsecond,
    )
    if target < now:
        late_seconds = (now - target).total_seconds()
        if late_seconds <= max(0.0, float(grace_seconds)):
            return now
        if allow_next_day:
            target += timedelta(days=1)
        else:
            raise ValueError(
                f"今天的执行时间已过去 {late_seconds:.1f} 秒；"
                "为避免静默等到明天并提交错误日期，任务已停止"
            )
    return target


def normalize_check_in_delay_minutes(value):
    try:
        minutes = float(value)
    except (TypeError, ValueError):
        return DEFAULT_AUTO_CHECK_IN_DELAY_MINUTES
    return max(0.0, min(minutes, 120.0))


def auto_check_in_time_for_item(item, delay_minutes=DEFAULT_AUTO_CHECK_IN_DELAY_MINUTES):
    start_timestamp = int(item.get("start_timestamp") or 0)
    if not start_timestamp:
        return None
    start_time = datetime.fromtimestamp(start_timestamp).astimezone()
    display_minute = start_time.replace(second=0, microsecond=0)
    return display_minute + timedelta(minutes=normalize_check_in_delay_minutes(delay_minutes))


def enrich_check_in_task(item, delay_minutes=DEFAULT_AUTO_CHECK_IN_DELAY_MINUTES):
    task = dict(item)
    delay_minutes = normalize_check_in_delay_minutes(delay_minutes)
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


def pending_check_in_tasks(bookings, delay_minutes=DEFAULT_AUTO_CHECK_IN_DELAY_MINUTES):
    tasks = [
        enrich_check_in_task(item, delay_minutes)
        for item in bookings
        if booking_status(item) == "0"
    ]
    tasks.sort(key=lambda item: item.get("start_timestamp") or 0)
    return tasks


def select_pending_check_in_task(bookings, booking_id=None, delay_minutes=DEFAULT_AUTO_CHECK_IN_DELAY_MINUTES):
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
        if booking_status(item) not in ACTIVE_BOOKING_STATUSES:
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
    return MSG_TIME_OUT_OF_RANGE in booking_result_message(result)


def is_duplicate_booking(result):
    return MSG_DUPLICATE in booking_result_message(result)


def is_seat_unavailable(result):
    message = booking_result_message(result)
    keywords = (
        MSG_SEAT_UNAVAILABLE,
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
    if MSG_TIME_OUT_OF_RANGE in message:
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


def wait_until(
    execute_time,
    logger=print,
    should_cancel=None,
    heartbeat=None,
    heartbeat_interval=DEFAULT_KEEPALIVE_INTERVAL,
    heartbeat_guard_seconds=HEARTBEAT_GUARD_SECONDS,
    warmup=None,
    warmup_before_seconds=DEFAULT_WARMUP_BEFORE_SECONDS,
    announce_label="定时提交",
    ready_message="已到执行时间，开始提交",
    ready_logger=None,
):
    if execute_time is None:
        return
    ready_logger = ready_logger if ready_logger is not None else logger
    logger(f"{announce_label}：将在 {format_execute_datetime(execute_time)} 执行")
    next_notice_at = 0
    heartbeat_interval = normalize_keepalive_interval(heartbeat_interval)
    heartbeat_enabled = bool(heartbeat) and heartbeat_interval > 0
    next_heartbeat_at = time.monotonic() + heartbeat_interval if heartbeat_enabled else None
    warmup_before_seconds = max(0.0, float(warmup_before_seconds))
    warmup_done = not bool(warmup)
    if heartbeat_enabled:
        logger(f"keepalive 心跳已开启：每 {heartbeat_interval:g} 秒请求一次登录态接口")
    while True:
        if should_cancel and should_cancel():
            raise TaskCancelled("任务已取消")
        current_monotonic = time.monotonic()
        # This is a calendar deadline: re-read wall time so clock corrections
        # cannot turn a stale monotonic deadline into an early submission.
        # Heartbeats and the 3-second submission cooldown stay monotonic.
        remaining = (execute_time - datetime.now().astimezone()).total_seconds()
        if remaining <= 0:
            break

        if not warmup_done and remaining <= warmup_before_seconds:
            if remaining < MIN_WARMUP_REMAINING_SECONDS:
                ready_logger(f"距离执行仅剩 {remaining:.3f} 秒，跳过可选预热")
                warmup_done = True
                continue
            logger(f"提交前预热：校验登录态并刷新连接（T-{remaining:.3f}s）")
            try:
                warmup_key = warmup()
            except RequestFailure as exc:
                if not exc.retryable:
                    raise RuntimeError(f"提交前预热失败：{exc}") from exc
                logger(f"提交前预热遇到临时网络错误，将按时继续提交：{exc}")
            except Exception as exc:
                raise RuntimeError(f"提交前预热失败：{exc}") from exc
            else:
                logger(f"提交前预热正常：{warmup_key}")
            warmup_done = True
            continue

        if (
            heartbeat_enabled
            and remaining > heartbeat_guard_seconds
            and current_monotonic >= next_heartbeat_at
        ):
            try:
                heartbeat_key = heartbeat()
            except Exception as exc:
                logger(f"keepalive 心跳失败：{exc}")
            else:
                logger(f"keepalive 心跳正常：{heartbeat_key}")
            next_heartbeat_at = time.monotonic() + heartbeat_interval
            # Recalculate the deadline after a possibly slow heartbeat.
            continue
        if current_monotonic >= next_notice_at and remaining > DEFAULT_WARMUP_BEFORE_SECONDS:
            logger(f"未到执行时间，还差 {int(remaining)} 秒，等待中...")
            next_notice_at = current_monotonic + 60

        if remaining > 10:
            sleep_seconds = min(10.0, remaining - 5.0)
        elif remaining > 1:
            sleep_seconds = min(0.5, remaining - 0.5)
        elif remaining > 0.05:
            sleep_seconds = max(0.001, remaining - 0.02)
        else:
            sleep_seconds = min(0.001, remaining)

        if heartbeat_enabled and remaining > heartbeat_guard_seconds:
            sleep_seconds = min(sleep_seconds, max(0.001, next_heartbeat_at - time.monotonic()))
        if not warmup_done and remaining > warmup_before_seconds:
            sleep_seconds = min(
                sleep_seconds,
                max(0.001, remaining - warmup_before_seconds),
            )
        if should_cancel:
            sleep_seconds = min(sleep_seconds, 0.25)
        time.sleep(sleep_seconds)
    drift_ms = (datetime.now().astimezone() - execute_time).total_seconds() * 1000
    ready_logger(f"{ready_message}（到点偏差 {drift_ms:+.1f} ms）")


def parse_args():
    parser = argparse.ArgumentParser(description="HDU 图书馆即时预约")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="配置文件路径")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check-config", action="store_true", help="离线检查配置和 Cookie 格式，不发送网络请求")
    action.add_argument("--list-bookings", action="store_true", help="列出当前座位预约")
    action.add_argument("--list-checkins", action="store_true", help="列出当前待签到预约和计划自动签到时间")
    action.add_argument("--cancel-booking", metavar="BOOKING_ID", help="取消指定 bookingId 的座位预约")
    action.add_argument(
        "--auto-check-in",
        nargs="?",
        const="",
        metavar="BOOKING_ID",
        help="自动在预约开始后指定分钟数签到；不传 bookingId 时选择最早的待签到预约",
    )
    parser.add_argument("--plan", help="roomType:floorId:seatNum:startHour:durationHours")
    parser.add_argument("--fallback-seats", help="备选座位号，多个用逗号分隔；主座位不可用时依次尝试")
    parser.add_argument("--days", type=int, help="预约日期偏移：0=今天，1=明天，2=后天")
    parser.add_argument("--execute-at", help="覆盖配置中的执行时间，支持毫秒；传空字符串表示立即提交")
    parser.add_argument("--max-trials", type=int, help="入口未开放或确定未提交的临时错误最多尝试次数")
    parser.add_argument("--retry-delay", type=float, help="收到响应后的重试/换座间隔，最少 3 秒")
    parser.add_argument("--hold-before-minutes", type=int, help="定时提交前几分钟尝试临时预留主座位，0=关闭，最多 14 分钟")
    parser.add_argument("--check-in-delay-minutes", type=float, default=DEFAULT_AUTO_CHECK_IN_DELAY_MINUTES, help="自动签到延迟分钟数，默认 5")
    parser.add_argument("--dry-run", action="store_true", help="只查询并打印，不真正提交预约")
    return parser.parse_args()


def create_booker(config_path=DEFAULT_CONFIG, logger=None):
    config = load_config(config_path)
    booker = InstantBooker(config)
    if logger:
        logger("正在加载 cookie...")
    booker.load_cookies()
    if logger:
        logger("正在识别登录用户...")
    booker.resolve_user()
    return booker


def get_current_bookings(config_path=DEFAULT_CONFIG):
    booker = create_booker(config_path)
    return booker.current_bookings()


def measure_server_clock(config_path=DEFAULT_CONFIG):
    """Take a few read-only samples; never use the result to move a booking."""
    config = load_config(config_path)
    plan = parse_plan(str((config.get("booking") or {}).get("plan") or ""))
    booker = InstantBooker(config)
    try:
        booker.load_cookies()
        room_items = booker.query_room_items()
        if not 1 <= plan["room_type"] <= len(room_items):
            raise RuntimeError(f"房间类型 {plan['room_type']} 不存在")
        room_detail = booker.query_room_detail(room_items[plan["room_type"] - 1])
        begin_time = build_begin_time(plan["start_hour"], DEFAULT_BOOK_DAYS)
        booker.query_seat_map(room_detail, begin_time, plan["duration_hours"])
        samples = [booker.last_seat_query_meta.get("clock_sample")]
        samples = [sample for sample in samples if sample]
        for _ in range(3):
            bounds = clock_offset_bounds(samples)
            if bounds and bounds["consistent"] and bounds["uncertainty"] <= 0.15:
                break
            time.sleep(0.25)
            samples.append(booker.query_clock_sample(timeout=min(booker.timeout, 3)))
        bounds = clock_offset_bounds(samples)
        return {
            "bounds": bounds,
            "message": clock_offset_message(bounds),
            "recommendation": recommend_booking_execute_time(bounds),
            "measured_at": datetime.now().astimezone().strftime("%H:%M:%S"),
        }
    finally:
        booker.session.close()


def get_pending_check_in_tasks(config_path=DEFAULT_CONFIG, delay_minutes=DEFAULT_AUTO_CHECK_IN_DELAY_MINUTES):
    booker = create_booker(config_path)
    return pending_check_in_tasks(booker.current_bookings(), delay_minutes)


def cancel_booking_by_id(config_path=DEFAULT_CONFIG, booking_id=None, logger=print):
    booking_id = normalize_booking_id(booking_id)
    booker = create_booker(config_path, logger=logger)
    logger(f"正在取消预约 bookingId={booking_id}...")
    result = booker.cancel_booking(booking_id)
    logger("取消接口返回：")
    logger(diagnostic_json(result["result"]))
    logger("取消成功")
    return result


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
    return redact_diagnostic_data(safe)


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
    except RequestFailure as exc:
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
        label = BOOKING_STATUS_LABELS[expected_status]
        raise ResultUncertain(
            f"{action_label}结果待确认：bookingId={booking_id} 未确认状态为{label}，请刷新预约列表"
        ) from request_error
    return result, confirmed


def continue_seat_by_id(config_path=DEFAULT_CONFIG, booking_id=None, logger=print):
    booking_id = normalize_booking_id(booking_id)
    booker = create_booker(config_path, logger=logger)
    item = find_booking_by_id(booker.current_bookings(), booking_id)
    if not item:
        raise RuntimeError(f"找不到预约 bookingId={booking_id}")

    status = booking_status(item)
    status_label = item.get("status_label") or f"状态 {status}"
    if status not in CONTINUABLE_BOOKING_STATUSES:
        if status == "6":
            raise RuntimeError("该预约已因暂离未归结束，续座期限已过，服务器不允许续座")
        raise RuntimeError(f"当前预约状态为 {status_label}，只有“暂离中”的预约可以续座")

    logger(f"正在续座 bookingId={booking_id}，{item.get('room_name')} {item.get('seat_num')}座...")
    result, after_item = perform_confirmed_seat_action(
        booker, booking_id, lambda: booker.continue_booking(booking_id),
        expected_status="1", action_label="续座", logger=logger,
    )
    safe_response = safe_check_in_response(result)
    after_status = booking_status(after_item)

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


def check_in_test_by_id(config_path=DEFAULT_CONFIG, booking_id=None, logger=print):
    booking_id = normalize_booking_id(booking_id)
    booker = create_booker(config_path, logger=logger)
    bookings = booker.current_bookings()
    item = find_booking_by_id(bookings, booking_id)
    if not item:
        return {
            "ok": False,
            "sent": False,
            "stage": "precheck",
            "message": "bookingId 不在当前登录态的预约列表中，未发送签到请求",
            "bookingId": booking_id,
        }

    precheck = check_in_precheck_from_item(item, booking_id)
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
    after_item = find_booking_by_id(booker.current_bookings(), booking_id)
    return {
        "ok": True,
        "sent": True,
        "method": "POST",
        "path": f"/Seat/Index/checkIn?bookingId={booking_id}",
        "body": None,
        "proximity_fields_in_request": [],
        "precheck": precheck,
        "response": safe_check_in_response(result),
        "status_code_after": booking_status(after_item) if after_item else None,
        "status_label_after": after_item.get("status_label") if after_item else None,
    }


def print_pending_check_in_tasks(config_path=DEFAULT_CONFIG, delay_minutes=DEFAULT_AUTO_CHECK_IN_DELAY_MINUTES):
    tasks = get_pending_check_in_tasks(config_path, delay_minutes)
    if not tasks:
        print("暂无待签到预约")
        return
    for item in tasks:
        print(
            f"{item['id']}  {item['label']}  "
            f"[计划签到 {item['auto_check_in_text']}]"
        )


def print_booking_list(config_path=DEFAULT_CONFIG):
    bookings = get_current_bookings(config_path)
    if not bookings:
        print("暂无座位预约")
        return
    for item in bookings:
        marker = "可取消" if item["cancelable"] else "不可取消"
        print(f"{item['id']}  {item['label']}  [{marker}]")


def run_auto_check_in(
    config_path=DEFAULT_CONFIG,
    booking_id=None,
    delay_minutes=DEFAULT_AUTO_CHECK_IN_DELAY_MINUTES,
    logger=print,
    should_cancel=None,
):
    delay_minutes = normalize_check_in_delay_minutes(delay_minutes)

    def check_cancel():
        if should_cancel and should_cancel():
            raise TaskCancelled("任务已取消")

    booker = create_booker(config_path, logger=logger)
    check_cancel()
    logger("正在获取待签到任务信息...")
    bookings = booker.current_bookings()
    task = select_pending_check_in_task(bookings, booking_id, delay_minutes)
    if not task:
        raise RuntimeError("没有待签到预约")

    current_booking_id = normalize_booking_id(task["id"])
    check_in_at = auto_check_in_time_for_item(task, delay_minutes)
    if not check_in_at:
        raise RuntimeError(f"预约 {current_booking_id} 缺少开始时间，无法计算自动签到时间")

    sign_deadline = None
    if int(task.get("sign_deadline_timestamp") or 0):
        sign_deadline = datetime.fromtimestamp(int(task["sign_deadline_timestamp"])).astimezone()
        if check_in_at > sign_deadline:
            raise RuntimeError(
                f"计划签到时间 {format_datetime(check_in_at)} 晚于签到截止 {format_datetime(sign_deadline)}"
            )

    logger(
        f"待签到任务：bookingId={current_booking_id}，{task.get('room_name')} "
        f"{task.get('seat_num')}座，预约开始 {task.get('start_text')}"
    )
    logger(f"自动签到时间：预约开始后 {delay_minutes:g} 分钟，{format_datetime(check_in_at)}")
    if task.get("sign_start_text") and task.get("sign_deadline_text"):
        logger(f"签到窗口：{task.get('sign_start_text')} 至 {task.get('sign_deadline_text')}")

    now = datetime.now().astimezone()
    if sign_deadline and now > sign_deadline:
        raise RuntimeError(f"签到截止时间已过：{format_datetime(sign_deadline)}")
    if check_in_at > now:
        wait_until(
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
    current = find_booking_by_id(refreshed, current_booking_id)
    if not current:
        message = f"bookingId={current_booking_id} 不在当前登录态的预约列表中，未发送签到请求"
        raise RuntimeError(message)

    current_task = enrich_check_in_task(current, delay_minutes)
    precheck = check_in_precheck_from_item(current_task, current_booking_id)
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
            raise RuntimeError(f"签到截止时间已过：{format_datetime(sign_deadline)}")

    check_cancel()
    logger(f"正在自动签到 bookingId={current_booking_id}...")
    request_error = None
    try:
        result = booker.check_in_booking(current_booking_id)
    except RequestFailure as exc:
        if not exc.outcome_unknown:
            raise
        request_error = exc
        result = None
        logger(f"签到请求结果不确定，正在查询实际状态：{exc}")
    safe_response = safe_check_in_response(result)
    logger("签到接口返回：")
    logger(json.dumps(safe_response, ensure_ascii=False, indent=2))
    if request_error is None and check_in_result_failed(result):
        raise RuntimeError(f"签到失败：{check_in_result_message(result)}")

    after_item = confirm_booking_status(booker, current_booking_id, "1", logger=logger)
    if not after_item:
        raise ResultUncertain(
            f"签到结果待确认：bookingId={current_booking_id} 未确认状态为使用中，请刷新预约列表"
        ) from request_error
    logger(f"签到结果复核成功：{after_item.get('status_label') or '使用中'}")
    return {
        "ok": True,
        "sent": True,
        "method": "POST",
        "path": f"/Seat/Index/checkIn?bookingId={current_booking_id}",
        "body": None,
        "auto_check_in_at": format_datetime(check_in_at),
        "precheck": precheck,
        "response": safe_response,
        "status_code_after": booking_status(after_item),
        "status_label_after": after_item.get("status_label") if after_item else None,
    }


def run_booking(
    config_path=DEFAULT_CONFIG,
    plan_text=None,
    fallback_seats=None,
    days=None,
    dry_run_override=None,
    execute_at=None,
    max_trials=None,
    retry_delay=None,
    hold_before_minutes=None,
    logger=print,
    should_cancel=None,
):
    config = load_config(config_path)
    booking_cfg = config.get("booking") or {}
    plan_text = plan_text or str(booking_cfg.get("plan") or "")
    plan = parse_plan(plan_text)
    if fallback_seats is None:
        fallback_seats = booking_cfg.get("fallback_seats")
    fallback_seat_numbers = parse_fallback_seats(fallback_seats, primary_seat=plan["seat_num"])
    requested_seat_numbers = [plan["seat_num"], *fallback_seat_numbers]
    config_days = booking_cfg.get("book_days")
    book_days = days if days is not None else int(DEFAULT_BOOK_DAYS if config_days is None else config_days)
    if book_days not in (0, 1, 2):
        raise ValueError("预约日期偏移只支持 0=今天、1=明天、2=后天")
    dry_run = bool(booking_cfg.get("dry_run")) if dry_run_override is None else bool(dry_run_override)
    if execute_at is None:
        execute_at = booking_cfg.get("execute_at")
    planning_now = datetime.now().astimezone()
    execute_time = build_execute_time(execute_at, now=planning_now)
    max_trials = int(max_trials if max_trials is not None else booking_cfg.get("max_trials", DEFAULT_MAX_TRIALS))
    retry_delay = normalize_retry_delay(
        retry_delay if retry_delay is not None else booking_cfg.get("retry_delay", DEFAULT_RETRY_DELAY)
    )
    hold_before_minutes = int(
        hold_before_minutes if hold_before_minutes is not None
        else booking_cfg.get("hold_before_minutes", DEFAULT_HOLD_BEFORE_MINUTES)
    )
    if not 0 <= hold_before_minutes <= 14:
        raise ValueError("提前预留时间须为 0 到 14 分钟")
    if hold_before_minutes and execute_time is None:
        raise ValueError("提前预留需要设置定时提交时间")
    max_trials = max(1, min(max_trials, 20))

    def check_cancel():
        if should_cancel and should_cancel():
            raise TaskCancelled("任务已取消")

    begin_time = build_begin_time(plan["start_hour"], book_days, now=planning_now)
    now = planning_now
    if begin_time <= now:
        message = f"提醒：预约开始时间 {begin_time.strftime('%Y-%m-%d %H:%M')} 已不晚于当前时间，接口可能拒绝。"
        logger(message)
        if not dry_run:
            raise RuntimeError("预约开始时间已经过去，请改成当前时间之后，或选择其他预约日期。")
    if execute_time is not None and execute_time >= begin_time:
        raise RuntimeError(
            "执行时间不能晚于预约开始时间："
            f"执行 {format_execute_datetime(execute_time)}，"
            f"预约 {begin_time.strftime('%Y-%m-%d %H:%M:%S')}"
        )

    check_cancel()
    booker = InstantBooker(config)
    logger("正在加载 cookie...")
    booker.load_cookies()
    check_cancel()
    logger("正在读取登录用户标识...")
    booker.resolve_user()
    logger("用户标识已加载；稍后校验登录态")

    check_cancel()
    logger("正在查询房间类型...")
    room_items = booker.query_room_items()
    if plan["room_type"] < 1 or plan["room_type"] > len(room_items):
        for index, item in enumerate(room_items, 1):
            logger(f"{index}. {item['name']}")
        raise RuntimeError(f"房间类型 {plan['room_type']} 不存在")

    room_item = room_items[plan["room_type"] - 1]
    logger(f"选择房间类型：{room_item['name']}")
    check_cancel()
    logger("正在查询房间详情...")
    room_detail = booker.query_room_detail(room_item)
    logger("登录态校验通过，服务端用户与配置一致")
    booker.validate_booking_time(
        room_detail,
        plan["start_hour"],
        plan["duration_hours"],
        begin_time=begin_time,
    )
    check_cancel()
    logger("正在查询座位图...")
    floors = booker.query_seat_map(
        room_detail,
        begin_time,
        plan["duration_hours"],
        target_floor_id=plan["floor_id"],
        logger=logger,
    )
    check_cancel()
    logger("正在定位主座位和备选座位...")
    seat_candidates = []
    floor_item = None
    for requested_index, seat_number in enumerate(requested_seat_numbers):
        try:
            candidate_floor, candidate_seat = booker.find_seat(
                floors,
                plan["floor_id"],
                seat_number,
            )
        except RuntimeError as exc:
            if requested_index == 0:
                raise
            logger(f"跳过无效备选座位 {seat_number}：{exc}")
            continue
        if floor_item is None:
            floor_item = candidate_floor
        candidate_index = len(seat_candidates)
        seat_candidates.append(
            {
                "index": candidate_index,
                "role": "主座位" if candidate_index == 0 else f"备选座位 {candidate_index}",
                "seat_num": seat_number,
                "seat_item": candidate_seat,
            }
        )
    seat_item = seat_candidates[0]["seat_item"]
    submission_options = {
        "is_recommend": 1 if str(booker.last_seat_query_meta.get("is_recommend")) == "1" else 0,
    }
    requires_image_code = str(booker.last_seat_query_meta.get("requires_image_code") or "0") == "1"
    if requires_image_code and not dry_run:
        raise RuntimeError("当前预约接口要求图形验证码，自动提交已停止，请改用官方页面预约")
    if requires_image_code:
        logger("提醒：当前预约接口要求图形验证码；dry-run 不会提交")

    clock_bounds = clock_offset_bounds(
        [booker.last_seat_query_meta.get("clock_sample") or {}]
    )
    if clock_bounds:
        logger(clock_offset_message(clock_bounds))
        if clock_bounds["consistent"] and (
            clock_bounds["lower"] > 2 or clock_bounds["upper"] < -2
        ):
            logger("警告：本机与服务端时钟差超过 2 秒，定时提交可能失准")
    logger(
        "主座位："
        f"{room_item['name']} / {floor_item.get('roomName')} / {seat_item.get('title')}座 "
        f"(seatId={seat_item.get('id')})"
    )
    valid_fallback_numbers = [candidate["seat_num"] for candidate in seat_candidates[1:]]
    if valid_fallback_numbers:
        logger(f"备选顺序：{' → '.join(f'{seat}座' for seat in valid_fallback_numbers)}")
    else:
        logger("未配置备选座位" if not fallback_seat_numbers else "没有可用的备选座位")
    logger(f"目标时间：{begin_time.strftime('%Y-%m-%d %H:%M')}，{plan['duration_hours']} 小时")
    logger(f"提交模式：is_recommend={submission_options['is_recommend']}")
    for candidate in seat_candidates:
        candidate_item = candidate["seat_item"]
        seat_state = str(
            candidate_item.get("state") if candidate_item.get("state") is not None else "-"
        )
        seat_state_label = "当前可用" if seat_state == "0" else "当前不可用或已占用"
        logger(
            f"{candidate['role']} {candidate['seat_num']}座："
            f"seatId={candidate_item.get('id')}，状态={seat_state}（{seat_state_label}）"
        )
        if seat_state not in {"0", "-"}:
            logger(f"警告：{candidate['seat_num']}座预查询显示不可用；提交时仍会按顺序尝试")

    def prepare_candidate_requests():
        for candidate in seat_candidates:
            candidate["request_template"] = prepare_booking_request(
                booker.uid, candidate["seat_item"]["id"], begin_time,
                plan["duration_hours"], submission_options["is_recommend"],
            )

    prepare_candidate_requests()

    def prewarm_submission():
        nonlocal floor_item
        warm_floors = booker._query_seat_map_once(
            room_detail,
            begin_time,
            plan["duration_hours"],
            timeout=min(float(booker.timeout), 1.0),
        )
        warm_clock_bounds = clock_offset_bounds(
            [booker.last_seat_query_meta.get("clock_sample") or {}]
        )
        if warm_clock_bounds:
            logger("提交前" + clock_offset_message(warm_clock_bounds))
        if str(booker.last_seat_query_meta.get("requires_image_code") or "0") == "1":
            raise RuntimeError("预约接口已切换为图形验证码模式，请改用官方页面预约")
        warm_mode = (
            1 if str(booker.last_seat_query_meta.get("is_recommend")) == "1" else 0
        )
        warm_candidates = []
        warm_floor = None
        for candidate in seat_candidates:
            try:
                candidate_floor, warm_seat = booker.find_seat(
                    warm_floors,
                    plan["floor_id"],
                    candidate["seat_num"],
                )
            except RuntimeError as exc:
                logger(f"预热移除失效的{candidate['role']} {candidate['seat_num']}座：{exc}")
                continue
            if warm_floor is None:
                warm_floor = candidate_floor
            warm_candidates.append({**candidate, "seat_item": warm_seat})
            warm_state = str(
                warm_seat.get("state") if warm_seat.get("state") is not None else "-"
            )
            logger(f"提交前 {candidate['role']} {candidate['seat_num']}座状态={warm_state}")
        if not warm_candidates:
            raise RuntimeError("预热后没有可定位的主座位或备选座位，已停止提交")
        if warm_candidates[0]["index"] != seat_candidates[0]["index"]:
            logger(f"主候选已失效，到点将从 {warm_candidates[0]['seat_num']}座开始提交")
        seat_candidates[:] = warm_candidates
        floor_item = warm_floor
        submission_options["is_recommend"] = warm_mode
        prepare_candidate_requests()
        return "searchSeats"

    def check_submission_deadline(enforce_grace=True):
        now = datetime.now().astimezone()
        if not dry_run and begin_time <= now:
            raise RuntimeError("预约开始时间已经过去，停止提交")
        if enforce_grace and execute_time is not None and (now - execute_time).total_seconds() > EXECUTE_GRACE_SECONDS:
            raise RuntimeError("准备或等待过程中已错过执行时间超过 5 秒，任务已停止")

    hold_result = None
    if hold_before_minutes and execute_time is not None:
        hold_time = execute_time - timedelta(minutes=hold_before_minutes)
        logger(
            f"提前预留：计划于 {format_execute_datetime(hold_time)} "
            f"尝试锁定主座位 {seat_candidates[0]['seat_num']} 座；正式预约仍在执行时间提交"
        )
        if dry_run:
            logger("dry-run：已跳过临时预留请求")
        elif datetime.now().astimezone() < execute_time:
            wait_until(
                hold_time,
                logger=logger,
                should_cancel=should_cancel,
                heartbeat=booker.keepalive,
                heartbeat_interval=booker.keepalive_interval,
                announce_label="提前预留",
                ready_message="已到预留时间，尝试锁定主座位",
            )
            check_cancel()
            if datetime.now().astimezone() < execute_time:
                candidate = seat_candidates[0]
                try:
                    hold_floors = booker._query_seat_map_once(
                        room_detail, begin_time, plan["duration_hours"],
                        timeout=min(float(booker.timeout), 5.0),
                    )
                    _, fresh_seat = booker.find_seat(
                        hold_floors, plan["floor_id"], candidate["seat_num"]
                    )
                    if str(fresh_seat.get("state")) != "0":
                        logger(
                            f"主座位 {candidate['seat_num']} 座预留前状态为 "
                            f"{fresh_seat.get('state')}，跳过预留；到点仍按原计划提交"
                        )
                    else:
                        candidate["seat_item"] = fresh_seat
                        prepare_candidate_requests()
                        check_cancel()
                        hold_result = booker.lock_seat(
                            fresh_seat["id"], begin_time, plan["duration_hours"],
                            is_recommend=1 if str(booker.last_seat_query_meta.get("is_recommend")) == "1" else 0,
                        )
                        hold_data = hold_result.get("DATA") or {}
                        if str(hold_result.get("CODE")).lower() == "ok" and str(hold_data.get("result")).lower() == "success":
                            server_time = float(hold_data.get("time") or time.time())
                            expiry = datetime.fromtimestamp(server_time + 900).astimezone()
                            logger(
                                f"主座位 {candidate['seat_num']} 座临时预留成功，"
                                f"官方 15 分钟确认窗口预计到 {expiry.strftime('%H:%M:%S')}；"
                                "到点仍须提交正式预约并复核"
                            )
                        else:
                            message = (hold_data.get("msg") or hold_result.get("MESSAGE") or "接口未确认成功")
                            logger(f"提前预留未成功：{message}；到点仍按原计划提交")
                except TaskCancelled:
                    raise
                except Exception as exc:
                    logger(f"提前预留结果未确认：{exc}；不重发锁座请求，到点仍尝试正式预约")
            else:
                logger("到达预留时间时已过正式执行时间，跳过临时预留")

    ready_messages = []
    check_submission_deadline()
    wait_until(
        execute_time,
        logger=logger,
        should_cancel=should_cancel,
        heartbeat=booker.keepalive,
        heartbeat_interval=booker.keepalive_interval,
        warmup=prewarm_submission,
        ready_logger=ready_messages.append,
    )
    check_cancel()
    check_submission_deadline()

    result = None
    confirmed_booking = None
    booked_candidate = None
    if dry_run:
        for message in ready_messages:
            logger(message)
        ready_messages.clear()
        logger("正在生成主座位和备选座位请求...")
        candidate_payloads = []
        for candidate in seat_candidates:
            candidate_result = booker.book(
                candidate["seat_item"]["id"],
                begin_time,
                plan["duration_hours"],
                dry_run=True,
                is_recommend=submission_options["is_recommend"],
                prepared=candidate["request_template"],
            )
            candidate_payloads.append(
                {
                    "role": candidate["role"],
                    "seat_num": candidate["seat_num"],
                    "payload": candidate_result["payload"],
                }
            )
        result = {"dry_run": True, "candidates": candidate_payloads}
        logger("dry-run：已跳过所有预约提交。")
        logger(diagnostic_json(result))
    else:
        submission_attempted = False

        def before_submit():
            # Also runs inside book() after the account cooldown, in case the
            # clock changes between the main wait and the actual request.
            if execute_time is not None and datetime.now().astimezone() < execute_time:
                wait_until(
                    execute_time, logger=ready_messages.append,
                    ready_logger=ready_messages.append, should_cancel=should_cancel,
                )
            check_cancel()
            check_submission_deadline(enforce_grace=not submission_attempted)

        def confirm_target_booking(candidate, expected_booking_id=None, attempts=3):
            for confirm_trial in range(1, attempts + 1):
                try:
                    bookings = booker.current_bookings()
                except Exception as exc:
                    logger(f"预约结果复核失败[{confirm_trial}/{attempts}]：{exc}")
                else:
                    match = find_matching_booking(
                        bookings,
                        candidate["seat_num"],
                        begin_time,
                        plan["duration_hours"],
                        seat_id=candidate["seat_item"]["id"],
                        floor_id=plan["floor_id"],
                        room_name=floor_item.get("roomName"),
                        expected_booking_id=expected_booking_id,
                    )
                    if match:
                        return match
                if confirm_trial < attempts:
                    time.sleep(0.2)
            return None

        def wait_for_retry_delay():
            end_wait = time.monotonic() + retry_delay
            while time.monotonic() < end_wait:
                check_cancel()
                time.sleep(min(0.02, max(0.0, end_wait - time.monotonic())))

        def wait_before_retry(reason):
            logger(f"{reason}，{retry_delay:g} 秒后重试...")
            wait_for_retry_delay()

        def submit_candidate(candidate):
            nonlocal submission_attempted
            for trial in range(1, max_trials + 1):
                before_submit()
                sent_at = datetime.now().astimezone()
                request_started = time.monotonic()

                def log_submission():
                    timing = getattr(booker, "last_submission_timing", None) or {}
                    actual_sent = timing.get("sent_at", sent_at)
                    elapsed_ms = timing.get("elapsed_ms", (time.monotonic() - request_started) * 1000)
                    for message in ready_messages:
                        logger(message)
                    ready_messages.clear()
                    logger(
                        f"预约请求：{candidate['role']} {candidate['seat_num']}座"
                        f"...[try={trial}/{max_trials}] "
                        f"发包时间={actual_sent.strftime('%H:%M:%S.%f')[:-3]}"
                    )
                    return elapsed_ms

                try:
                    candidate_result = booker.book(
                        candidate["seat_item"]["id"],
                        begin_time,
                        plan["duration_hours"],
                        dry_run=False,
                        is_recommend=submission_options["is_recommend"],
                        prepared=candidate["request_template"],
                        should_cancel=should_cancel,
                        before_submit=before_submit,
                    )
                except RequestFailure as exc:
                    submission_attempted = True
                    elapsed_ms = log_submission()
                    logger(f"预约请求异常：耗时={elapsed_ms:.1f} ms，{exc}")
                    if exc.outcome_unknown:
                        logger("预约请求可能已经生效，正在复核；此时停止任务也会先查清已发请求的结果")
                        confirmation = confirm_target_booking(candidate)
                        if confirmation:
                            logger(f"预约结果复核成功：bookingId={confirmation['id']}")
                            return "success", None, confirmation
                        raise ResultUncertain(
                            f"预约结果待确认：{candidate['seat_num']}座提交后未能确认结果，"
                            "已停止重复提交，请刷新预约列表"
                        ) from exc
                    if exc.retryable and trial < max_trials:
                        wait_before_retry("临时网络或服务端错误")
                        continue
                    raise

                submission_attempted = True
                elapsed_ms = log_submission()
                logger(f"预约接口已响应：耗时={elapsed_ms:.1f} ms")
                logger("预约接口返回：")
                logger(diagnostic_json(candidate_result))

                if booking_result_succeeded(candidate_result):
                    expected_booking_id = candidate_result["DATA"]["bookingId"]
                    confirmation = confirm_target_booking(
                        candidate,
                        expected_booking_id=expected_booking_id,
                    )
                    if not confirmation:
                        raise ResultUncertain(
                            f"预约结果待确认：接口返回成功 bookingId={expected_booking_id}，"
                            f"但预约列表未找到匹配的 {candidate['seat_num']}座记录，请刷新预约列表"
                        )
                    logger(
                        "预约结果复核成功："
                        f"bookingId={confirmation['id']}，{confirmation['label']}"
                    )
                    return "success", candidate_result, confirmation

                if not seat_action_rejected(candidate_result):
                    logger("提交响应缺少关键字段或结果不明确，正在复核实际预约")
                    confirmation = confirm_target_booking(candidate)
                    if confirmation:
                        logger(f"预约结果复核成功：bookingId={confirmation['id']}")
                        return "success", candidate_result, confirmation
                    raise ResultUncertain(
                        f"预约结果待确认：{candidate['seat_num']}座提交响应不明确，"
                        "已停止重试和换座，请刷新预约列表"
                    )

                if is_duplicate_booking(candidate_result):
                    confirmation = confirm_target_booking(candidate)
                    if confirmation:
                        logger(
                            "接口提示已有预约，经复核目标预约确实存在，按幂等成功处理："
                            f"bookingId={confirmation['id']}"
                        )
                        return "success", candidate_result, confirmation

                if is_time_out_of_range(candidate_result):
                    if trial < max_trials:
                        wait_before_retry("预约入口暂未开放")
                        continue
                    return "not_open", candidate_result, None

                if is_seat_unavailable(candidate_result):
                    return "unavailable", candidate_result, None

                validate_booking_result(candidate_result)
            raise RuntimeError(f"{candidate['seat_num']}座预约尝试异常结束")

        for candidate_position, candidate in enumerate(seat_candidates):
            outcome, result, confirmation = submit_candidate(candidate)
            if outcome == "success":
                confirmed_booking = confirmation
                booked_candidate = candidate
                break
            has_next = candidate_position + 1 < len(seat_candidates)
            if has_next:
                next_candidate = seat_candidates[candidate_position + 1]
                reason = "预约入口未开放" if outcome == "not_open" else "已不可用"
                logger(
                    f"{candidate['seat_num']}座{reason}，{retry_delay:g} 秒后切换到"
                    f"{next_candidate['role']} {next_candidate['seat_num']}座"
                )
                wait_for_retry_delay()
                continue
            validate_booking_result(result)

    return {
        "plan": plan,
        "book_days": book_days,
        "dry_run": dry_run,
        "execute_at": normalize_execute_at(execute_at),
        "execute_time": execute_time,
        "max_trials": max_trials,
        "retry_delay": retry_delay,
        "hold_before_minutes": hold_before_minutes,
        "hold_result": hold_result,
        "begin_time": begin_time,
        "room_item": room_item,
        "floor_item": floor_item,
        "seat_item": booked_candidate["seat_item"] if booked_candidate else seat_candidates[0]["seat_item"],
        "fallback_seats": fallback_seat_numbers,
        "seat_candidates": [
            {
                "role": candidate["role"],
                "seat_num": candidate["seat_num"],
                "seat_id": str(candidate["seat_item"].get("id") or ""),
            }
            for candidate in seat_candidates
        ],
        "result": result,
        "confirmed_booking": confirmed_booking,
        "booked_seat_num": booked_candidate["seat_num"] if booked_candidate else None,
        "used_fallback": bool(booked_candidate and booked_candidate["index"] > 0),
    }


def main():
    args = parse_args()
    if args.check_config:
        check_config(args.config)
        print("本地配置和 Cookie 格式检查通过；尚未验证登录有效性、座位状态或服务端预约规则。")
        return
    if args.list_bookings:
        print_booking_list(args.config)
        return
    if args.list_checkins:
        print_pending_check_in_tasks(args.config, args.check_in_delay_minutes)
        return
    if args.cancel_booking:
        cancel_booking_by_id(args.config, args.cancel_booking)
        return
    if args.auto_check_in is not None:
        run_auto_check_in(
            config_path=args.config,
            booking_id=args.auto_check_in or None,
            delay_minutes=args.check_in_delay_minutes,
        )
        return
    run_booking(
        config_path=args.config,
        plan_text=args.plan,
        fallback_seats=args.fallback_seats,
        days=args.days,
        dry_run_override=True if args.dry_run else None,
        execute_at=args.execute_at,
        max_trials=args.max_trials,
        retry_delay=args.retry_delay,
        hold_before_minutes=args.hold_before_minutes,
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已中断；如果请求已经发出，请用 --list-bookings 确认实际预约状态")
        sys.exit(130)
    except ResultUncertain as exc:
        print(str(exc))
        sys.exit(2)
    except Exception as exc:
        print(f"失败：{exc}")
        sys.exit(1)
