"""Background task lifecycle, cancellation, snapshots and private job logs."""

import threading
import time
import uuid
from datetime import datetime

from libcs import (
    booking,
    configuration,
    constants,
    errors,
    login,
    operations,
    privacy,
    scheduling,
)
from libcs.web import forms

LOG_DIR = constants.PROJECT_ROOT / "logs"


JOBS = {}


JOBS_LOCK = threading.Lock()


class JobNotFound(ValueError):
    pass


def job_poll_after_ms(job):
    target = job.get("execute_timestamp")
    if job["status"] == "running" and target is not None:
        remaining = target - time.time()
        if 0 < remaining <= 2:
            # Let the browser wait across the send window without polling logs.
            return int((remaining + 0.5) * 1000) + 1
        if remaining > 2:
            return int(min(2000, max(500, (remaining - 2) * 1000)))
    return 1000


def job_snapshot_locked(job):
    return {
        "id": job["id"],
        "job_type": job["job_type"],
        "mode": job["mode"],
        "status": job["status"],
        "logs": list(job["logs"]),
        "error": job["error"],
        "message": job.get("message", ""),
        "poll_after_ms": job_poll_after_ms(job),
        "started_at": job["started_at"],
        "finished_at": job["finished_at"],
        "log_path": f"logs/{job['log_path'].name}",
    }


def running_job_locked():
    running = [job for job in JOBS.values() if job["status"] == "running"]
    if not running:
        return None
    return max(running, key=lambda job: job["started_at"])


def create_job_record(job_type="generic", mode=""):
    job_id = str(uuid.uuid4())
    cancel_event = threading.Event()
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    with JOBS_LOCK:
        running = running_job_locked()
        if running:
            raise RuntimeError(f"已有任务正在运行：{running['id']}")
        LOG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        LOG_DIR.chmod(0o700)
        log_path = LOG_DIR / f"job-{timestamp}-{job_id[:8]}.log"
        log_path.touch(mode=0o600, exist_ok=False)
        log_path.chmod(0o600)
        job = {
            "id": job_id,
            "job_type": str(job_type),
            "mode": str(mode),
            "status": "running",
            "logs": [],
            "error": "",
            "started_at": time.time(),
            "finished_at": None,
            "cancel_event": cancel_event,
            "log_path": log_path,
        }
        JOBS[job_id] = job
    return job


def append_job_log(job, message):
    with JOBS_LOCK:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        line = f"[{stamp}] {privacy.redact_private_text(message)}"
        job["logs"].append(line)
        try:
            with job["log_path"].open("a", encoding="utf-8") as file:
                file.write(line + "\n")
        except OSError:
            pass


def execute_job(job, action):
    """Use the actual outcome, not the cancellation flag, as the final status."""
    error = ""
    try:
        result = action()
        if isinstance(result, dict) and result.get("ok") is False:
            raise RuntimeError(result.get("message") or "操作未完成")
    except errors.TaskCancelled:
        status, message = "cancelled", "任务已停止"
    except errors.ResultUncertain as exc:
        status, error, message = "uncertain", privacy.redact_private_text(exc), privacy.redact_private_text(exc)
    except Exception as exc:
        status, error, message = "error", privacy.redact_private_text(exc), f"失败：{privacy.redact_private_text(exc)}"
    else:
        status, message = "done", "执行完成"
        if isinstance(result, dict):
            if result.get("dry_run"):
                message = "测试完成，未提交预约"
            elif result.get("confirmed_booking"):
                confirmed = result["confirmed_booking"]
                message = f"预约成功：{confirmed.get('label') or confirmed['id']}"
            elif job["job_type"] == "login":
                message = result["message"]
            elif job["job_type"] == "auto_check_in":
                message = result.get("message") or "自动签到成功，已复核为使用中"
    # Publish the terminal state only after its final log has been appended.
    append_job_log(job, message)
    with JOBS_LOCK:
        job.update(status=status, error=error, message=message, finished_at=time.time())


def start_booking_job(payload, force_immediate=False):
    config_path = forms.config_path_from_payload(payload)
    configuration.load_config(config_path)
    options = forms.booking_options_from_payload(payload, force_immediate)
    plan_text = options['plan']
    fallback_seats = options['fallback_seats']
    days = options['book_days']
    dry_run = options['dry_run']
    execute_at = options['execute_at']
    max_trials = options['max_trials']
    retry_delay = options['retry_delay']
    hold_before_minutes = options['hold_before_minutes']
    mode = "immediate" if not execute_at else "scheduled"
    planned_execution = scheduling.build_execute_time(execute_at)
    job = create_job_record(job_type="booking", mode=mode)
    job["execute_timestamp"] = planned_execution.timestamp() if planned_execution else None
    job_id = job["id"]
    cancel_event = job["cancel_event"]

    def append(message):
        append_job_log(job, message)

    def worker():
        execute_job(
            job,
            lambda: booking.run_booking(
                config_path=config_path,
                plan_text=plan_text,
                fallback_seats=fallback_seats,
                days=days,
                dry_run_override=dry_run,
                execute_at=execute_at,
                max_trials=max_trials,
                retry_delay=retry_delay,
                hold_before_minutes=hold_before_minutes,
                logger=append,
                should_cancel=cancel_event.is_set,
            ),
        )

    append_job_log(job, f"持久化日志：logs/{job['log_path'].name}")
    append_job_log(job, "执行模式：立即预约" if mode == "immediate" else f"执行模式：定时预约 {execute_at}")
    threading.Thread(target=worker, daemon=True).start()
    return job_id


def start_auto_check_in_job(payload):
    config_path = forms.config_path_from_payload(payload)
    configuration.load_config(config_path)
    booking_id = str(payload.get("booking_id") or "").strip() or None
    delay_minutes = configuration.normalize_check_in_delay_minutes(
        payload.get("delay_minutes", constants.DEFAULT_AUTO_CHECK_IN_DELAY_MINUTES)
    )
    job = create_job_record(job_type="auto_check_in", mode="scheduled")
    job_id = job["id"]
    cancel_event = job["cancel_event"]

    def append(message):
        append_job_log(job, message)

    def worker():
        execute_job(
            job,
            lambda: operations.run_auto_check_in(
                config_path=config_path,
                booking_id=booking_id,
                delay_minutes=delay_minutes,
                logger=append,
                should_cancel=cancel_event.is_set,
            ),
        )

    append_job_log(job, f"持久化日志：logs/{job['log_path'].name}")
    threading.Thread(target=worker, daemon=True).start()
    return job_id


def cancel_job(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise KeyError("任务不存在")
        if job["status"] != "running":
            return {"message": "任务已经结束"}
        job["cancel_event"].set()
    message = "已请求停止任务；已经发出的请求会先完成结果复核"
    append_job_log(job, message)
    return {"message": message}


def job_snapshot(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise JobNotFound("任务不存在，可能已重启服务")
        return job_snapshot_locked(job)


def active_job_snapshot():
    with JOBS_LOCK:
        job = running_job_locked()
        return {"job": job_snapshot_locked(job) if job else None}


def start_login_job(payload):
    config_path = forms.config_path_from_payload(payload)
    config = configuration.load_config(config_path)
    login.validate_login_config(config)
    job = create_job_record(job_type="login", mode="interactive")

    def worker():
        execute_job(job, lambda: login.login(
            config_path,
            logger=lambda message: append_job_log(job, message),
            should_cancel=job["cancel_event"].is_set,
        ))

    append_job_log(job, "正在打开登录窗口，请在该窗口完成登录；Cookie 将自动保存")
    threading.Thread(target=worker, daemon=True).start()
    return job["id"]
