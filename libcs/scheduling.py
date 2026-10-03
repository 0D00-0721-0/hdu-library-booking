"""Clock bounds, local schedule construction and cancellable waiting."""

import time
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from math import ceil, isfinite

from libcs import configuration, constants, errors


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


def build_begin_time(start_hour, book_days, now=None):
    now = now or datetime.now().astimezone()
    return (now + timedelta(days=book_days)).replace(
        hour=start_hour,
        minute=0,
        second=0,
        microsecond=0,
    )


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
    grace_seconds=constants.EXECUTE_GRACE_SECONDS,
    allow_next_day=False,
):
    parsed = configuration.parse_execute_at(execute_at)
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


def wait_until(
    execute_time,
    logger=print,
    should_cancel=None,
    heartbeat=None,
    heartbeat_interval=constants.DEFAULT_KEEPALIVE_INTERVAL,
    heartbeat_guard_seconds=constants.HEARTBEAT_GUARD_SECONDS,
    warmup=None,
    warmup_before_seconds=constants.DEFAULT_WARMUP_BEFORE_SECONDS,
    announce_label="定时提交",
    ready_message="已到执行时间，开始提交",
    ready_logger=None,
):
    if execute_time is None:
        return
    ready_logger = ready_logger if ready_logger is not None else logger
    logger(f"{announce_label}：将在 {format_execute_datetime(execute_time)} 执行")
    next_notice_at = 0
    heartbeat_interval = configuration.normalize_keepalive_interval(heartbeat_interval)
    heartbeat_enabled = bool(heartbeat) and heartbeat_interval > 0
    next_heartbeat_at = time.monotonic() + heartbeat_interval if heartbeat_enabled else None
    warmup_before_seconds = max(0.0, float(warmup_before_seconds))
    warmup_done = not bool(warmup)
    if heartbeat_enabled:
        logger(f"keepalive 心跳已开启：每 {heartbeat_interval:g} 秒请求一次登录态接口")
    while True:
        if should_cancel and should_cancel():
            raise errors.TaskCancelled("任务已取消")
        current_monotonic = time.monotonic()
        # This is a calendar deadline: re-read wall time so clock corrections
        # cannot turn a stale monotonic deadline into an early submission.
        # Heartbeats and the 3-second submission cooldown stay monotonic.
        remaining = (execute_time - datetime.now().astimezone()).total_seconds()
        if remaining <= 0:
            break

        if not warmup_done and remaining <= warmup_before_seconds:
            if remaining < constants.MIN_WARMUP_REMAINING_SECONDS:
                ready_logger(f"距离执行仅剩 {remaining:.3f} 秒，跳过可选预热")
                warmup_done = True
                continue
            logger(f"提交前预热：校验登录态并刷新连接（T-{remaining:.3f}s）")
            try:
                warmup_key = warmup()
            except errors.RequestFailure as exc:
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
        if current_monotonic >= next_notice_at and remaining > constants.DEFAULT_WARMUP_BEFORE_SECONDS:
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
