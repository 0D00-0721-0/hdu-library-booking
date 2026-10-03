"""Reservation orchestration: preparation, optional hold, submission and verification."""

import time
from datetime import datetime, timedelta

from libcs import client, configuration, constants, errors, privacy, records, scheduling


def run_booking(
    config_path=constants.DEFAULT_CONFIG,
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
    config = configuration.load_config(config_path)
    booking_cfg = dict(config.get("booking") or {})
    overrides = dict(plan=plan_text, fallback_seats=fallback_seats, book_days=days,
                     dry_run=dry_run_override, execute_at=execute_at, max_trials=max_trials,
                     retry_delay=retry_delay, hold_before_minutes=hold_before_minutes)
    booking_cfg.update({key: value for key, value in overrides.items() if value is not None})
    booking_cfg = configuration.validate_booking_options(booking_cfg)
    plan_text = booking_cfg['plan']
    plan = configuration.parse_plan(plan_text)
    fallback_seat_numbers = configuration.parse_fallback_seats(booking_cfg['fallback_seats'], primary_seat=plan['seat_num'])
    requested_seat_numbers = [plan['seat_num'], *fallback_seat_numbers]
    book_days = booking_cfg['book_days']
    dry_run = booking_cfg['dry_run']
    execute_at = booking_cfg['execute_at']
    planning_now = datetime.now().astimezone()
    execute_time = scheduling.build_execute_time(execute_at, now=planning_now)
    max_trials = booking_cfg['max_trials']
    retry_delay = booking_cfg['retry_delay']
    hold_before_minutes = booking_cfg['hold_before_minutes']

    def check_cancel():
        if should_cancel and should_cancel():
            raise errors.TaskCancelled("任务已取消")

    begin_time = scheduling.build_begin_time(plan["start_hour"], book_days, now=planning_now)
    now = planning_now
    if begin_time <= now:
        message = f"提醒：预约开始时间 {begin_time.strftime('%Y-%m-%d %H:%M')} 已不晚于当前时间，接口可能拒绝。"
        logger(message)
        if not dry_run:
            raise RuntimeError("预约开始时间已经过去，请改成当前时间之后，或选择其他预约日期。")
    if execute_time is not None and execute_time >= begin_time:
        raise RuntimeError(
            "执行时间不能晚于预约开始时间："
            f"执行 {scheduling.format_execute_datetime(execute_time)}，"
            f"预约 {begin_time.strftime('%Y-%m-%d %H:%M:%S')}"
        )

    check_cancel()
    booker = client.InstantBooker(config)
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

    clock_bounds = scheduling.clock_offset_bounds(
        [booker.last_seat_query_meta.get("clock_sample") or {}]
    )
    if clock_bounds:
        logger(scheduling.clock_offset_message(clock_bounds))
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
            candidate["request_template"] = client.prepare_booking_request(
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
        warm_clock_bounds = scheduling.clock_offset_bounds(
            [booker.last_seat_query_meta.get("clock_sample") or {}]
        )
        if warm_clock_bounds:
            logger("提交前" + scheduling.clock_offset_message(warm_clock_bounds))
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
        if enforce_grace and execute_time is not None and (now - execute_time).total_seconds() > constants.EXECUTE_GRACE_SECONDS:
            raise RuntimeError("准备或等待过程中已错过执行时间超过 5 秒，任务已停止")

    hold_result = None
    if hold_before_minutes and execute_time is not None:
        hold_time = execute_time - timedelta(minutes=hold_before_minutes)
        logger(
            f"提前预留：计划于 {scheduling.format_execute_datetime(hold_time)} "
            f"尝试锁定主座位 {seat_candidates[0]['seat_num']} 座；正式预约仍在执行时间提交"
        )
        if dry_run:
            logger("dry-run：已跳过临时预留请求")
        elif datetime.now().astimezone() < execute_time:
            scheduling.wait_until(
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
                except errors.TaskCancelled:
                    raise
                except Exception as exc:
                    logger(f"提前预留结果未确认：{exc}；不重发锁座请求，到点仍尝试正式预约")
            else:
                logger("到达预留时间时已过正式执行时间，跳过临时预留")

    ready_messages = []
    check_submission_deadline()
    scheduling.wait_until(
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
        logger(privacy.diagnostic_json(result))
    else:
        submission_attempted = False

        def before_submit():
            # Also runs inside book() after the account cooldown, in case the
            # clock changes between the main wait and the actual request.
            if execute_time is not None and datetime.now().astimezone() < execute_time:
                scheduling.wait_until(
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
                    match = records.find_matching_booking(
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
                except errors.RequestFailure as exc:
                    submission_attempted = True
                    elapsed_ms = log_submission()
                    logger(f"预约请求异常：耗时={elapsed_ms:.1f} ms，{exc}")
                    if exc.outcome_unknown:
                        logger("预约请求可能已经生效，正在复核；此时停止任务也会先查清已发请求的结果")
                        confirmation = confirm_target_booking(candidate)
                        if confirmation:
                            logger(f"预约结果复核成功：bookingId={confirmation['id']}")
                            return "success", None, confirmation
                        raise errors.ResultUncertain(
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
                logger(privacy.diagnostic_json(candidate_result))

                if records.booking_result_succeeded(candidate_result):
                    expected_booking_id = candidate_result["DATA"]["bookingId"]
                    confirmation = confirm_target_booking(
                        candidate,
                        expected_booking_id=expected_booking_id,
                    )
                    if not confirmation:
                        raise errors.ResultUncertain(
                            f"预约结果待确认：接口返回成功 bookingId={expected_booking_id}，"
                            f"但预约列表未找到匹配的 {candidate['seat_num']}座记录，请刷新预约列表"
                        )
                    logger(
                        "预约结果复核成功："
                        f"bookingId={confirmation['id']}，{confirmation['label']}"
                    )
                    return "success", candidate_result, confirmation

                if not records.seat_action_rejected(candidate_result):
                    logger("提交响应缺少关键字段或结果不明确，正在复核实际预约")
                    confirmation = confirm_target_booking(candidate)
                    if confirmation:
                        logger(f"预约结果复核成功：bookingId={confirmation['id']}")
                        return "success", candidate_result, confirmation
                    raise errors.ResultUncertain(
                        f"预约结果待确认：{candidate['seat_num']}座提交响应不明确，"
                        "已停止重试和换座，请刷新预约列表"
                    )

                if records.is_duplicate_booking(candidate_result):
                    confirmation = confirm_target_booking(candidate)
                    if confirmation:
                        logger(
                            "接口提示已有预约，经复核目标预约确实存在，按幂等成功处理："
                            f"bookingId={confirmation['id']}"
                        )
                        return "success", candidate_result, confirmation

                if records.is_time_out_of_range(candidate_result):
                    if trial < max_trials:
                        wait_before_retry("预约入口暂未开放")
                        continue
                    return "not_open", candidate_result, None

                if records.is_seat_unavailable(candidate_result):
                    return "unavailable", candidate_result, None

                records.validate_booking_result(candidate_result)
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
            records.validate_booking_result(result)

    return {
        "plan": plan,
        "book_days": book_days,
        "dry_run": dry_run,
        "execute_at": configuration.normalize_execute_at(execute_at),
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
