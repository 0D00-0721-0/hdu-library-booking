"""Library HTTP client, request signatures and account submission cooldown."""

import base64
import hashlib
import json
import math
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import unquote, urljoin

import requests

from libcs import configuration, constants, errors, records


class BookingSubmissionGate:
    """Serialize one account's POSTs and cool down after every response/error."""

    def __init__(self):
        self.lock = threading.Lock()
        self.next_allowed_at = 0.0

    @contextmanager
    def submission(self, should_cancel=None):
        def check_cancel():
            if should_cancel and should_cancel():
                raise errors.TaskCancelled("任务已取消")

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
                self.next_allowed_at = time.monotonic() + constants.MIN_RETRY_DELAY
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


class InstantBooker:
    def __init__(self, config):
        self.config = config
        self.urls = config["urls"]
        request_config = configuration.validate_request_options(config.get("request") or {})
        self.timeout = request_config["timeout"]
        self.keepalive_interval = configuration.normalize_keepalive_interval(
            request_config.get("keepalive_interval", constants.DEFAULT_KEEPALIVE_INTERVAL)
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
            raise errors.RequestFailure(
                f"网络请求失败：{exc}",
                retryable=not isinstance(exc, requests.exceptions.SSLError),
                outcome_unknown=method != "GET" and not definitely_not_sent,
            ) from exc

        if response.status_code in (301, 302, 303, 307, 308, 401, 403):
            raise errors.RequestFailure(
                f"登录态可能已失效：HTTP {response.status_code} {url}",
                retryable=False,
            )
        if response.status_code == 429 or response.status_code >= 500:
            raise errors.RequestFailure(
                f"服务端暂时不可用：HTTP {response.status_code} {url}",
                retryable=True,
                outcome_unknown=method != "GET" and response.status_code >= 500,
            )
        if response.status_code != 200:
            raise errors.RequestFailure(
                f"请求失败：HTTP {response.status_code} {url}",
                retryable=False,
            )
        try:
            return response.json()
        except Exception as exc:
            raise errors.RequestFailure(
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
            "api_time": math.floor(time.time()),
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
            payload["api_time"] = math.floor(time.time())
            return {"dry_run": True, "payload": payload}

        self.last_submission_timing = None
        with booking_gate_for(self.uid).submission(should_cancel):
            if before_submit:
                before_submit()
            # Refresh dynamic fields after the cooldown. Cookies are also read
            # from the live Session when request() prepares the HTTP request.
            payload["api_time"] = math.floor(time.time())
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
        data = self.request("GET", self.urls.get("my_bookings", constants.DEFAULT_MY_BOOKINGS_URL))
        if not isinstance(data, (dict, list)) or data == {}:
            raise errors.RequestFailure("预约列表返回格式异常，不能确认预约状态")
        if isinstance(data, dict) and data.get("CODE") is not None:
            if str(data["CODE"]).strip().lower() != "ok":
                raise errors.RequestFailure("预约列表查询失败，请检查登录态")
        items = records.extract_booking_items(data)
        if not items and not records.is_explicit_empty_booking_list(data):
            raise errors.RequestFailure("预约列表结构无法识别，不能当作没有预约，请稍后刷新")
        return items

    def cancel_booking(self, booking_id, check_limit=True):
        booking_id = records.normalize_booking_id(booking_id)
        limit_result = None
        if check_limit:
            limit_url = records.append_booking_id(
                self.urls.get("cancel_times_limit", constants.DEFAULT_CANCEL_TIMES_LIMIT_URL),
                booking_id,
            )
            limit_result = self.request("GET", limit_url)

        self.session.headers.pop("Api-Token", None)
        cancel_url = records.append_booking_id(
            self.urls.get("cancel_booking", constants.DEFAULT_CANCEL_BOOKING_URL),
            booking_id,
        )
        result, confirmed = records.perform_confirmed_seat_action(
            self, booking_id, lambda: self.request("POST", cancel_url),
            expected_status="4", action_label="取消预约", logger=lambda _: None,
        )
        return {
            "booking_id": booking_id, "limit": limit_result, "result": result,
            "confirmed_booking": confirmed,
        }

    def check_in_booking(self, booking_id):
        booking_id = records.normalize_booking_id(booking_id)
        self.session.headers.pop("Api-Token", None)
        check_in_url = records.append_booking_id(
            self.urls.get("check_in", constants.DEFAULT_CHECK_IN_URL),
            booking_id,
        )
        return self.request("POST", check_in_url)

    def continue_booking(self, booking_id):
        booking_id = records.normalize_booking_id(booking_id)
        self.session.headers.pop("Api-Token", None)
        come_back_url = records.append_booking_id(
            self.urls.get("come_back", constants.DEFAULT_COME_BACK_URL),
            booking_id,
        )
        return self.request("POST", come_back_url)
