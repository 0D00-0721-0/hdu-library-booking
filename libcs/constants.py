"""Project paths, interface defaults and protocol status constants."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "config.yaml"

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
