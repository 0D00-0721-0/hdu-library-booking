"""Command-line argument dispatch and process exit codes."""

import argparse
import sys

from libcs import booking, constants, errors, login, operations


def parse_args():
    parser = argparse.ArgumentParser(description="HDU 图书馆即时预约")
    parser.add_argument("--config", default=str(constants.DEFAULT_CONFIG), help="配置文件路径")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--login", action="store_true", help="打开浏览器登录并自动保存 Cookie，不提交预约")
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
    parser.add_argument("--check-in-delay-minutes", type=float, default=constants.DEFAULT_AUTO_CHECK_IN_DELAY_MINUTES, help="自动签到延迟分钟数，默认 5")
    parser.add_argument("--dry-run", action="store_true", help="只查询并打印，不真正提交预约")
    return parser.parse_args()


def main():
    args = parse_args()
    if args.login:
        login.login(args.config)
        return
    if args.check_config:
        operations.check_config(args.config)
        print("本地配置和 Cookie 格式检查通过；尚未验证登录有效性、座位状态或服务端预约规则。")
        return
    if args.list_bookings:
        operations.print_booking_list(args.config)
        return
    if args.list_checkins:
        operations.print_pending_check_in_tasks(args.config, args.check_in_delay_minutes)
        return
    if args.cancel_booking:
        operations.cancel_booking_by_id(args.config, args.cancel_booking)
        return
    if args.auto_check_in is not None:
        operations.run_auto_check_in(
            config_path=args.config,
            booking_id=args.auto_check_in or None,
            delay_minutes=args.check_in_delay_minutes,
        )
        return
    booking.run_booking(
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


def run():
    try:
        main()
    except KeyboardInterrupt:
        print("\n已中断；如果请求已经发出，请用 --list-bookings 确认实际预约状态")
        sys.exit(130)
    except errors.TaskCancelled as exc:
        print(str(exc))
        sys.exit(130)
    except errors.ResultUncertain as exc:
        print(str(exc))
        sys.exit(2)
    except Exception as exc:
        print(f"失败：{exc}")
        sys.exit(1)
