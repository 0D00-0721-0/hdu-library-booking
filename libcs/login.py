"""Interactive browser login and private, validated Cookie capture."""

import copy
import json
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import yaml

from libcs import client, configuration, constants, errors

LOGIN_URL = "https://hdu.huitu.zhishulib.com/"
LOGIN_HOST = "hdu.huitu.zhishulib.com"
LOGIN_TIMEOUT = 300
LOGIN_LOCK = threading.Lock()
CONFIG_LOCK = threading.RLock()


def check_cancel(should_cancel):
    if should_cancel and should_cancel():
        raise errors.TaskCancelled("登录已取消，原登录态未修改")


def library_cookies(cookies):
    """Keep only unexpired cookies for this service; narrow parent-domain scope."""
    result = []
    for item in cookies:
        domain = str(item.get("domain", "")).lower()
        domain_matches = (domain == LOGIN_HOST or
                          (domain.startswith(".") and
                           (LOGIN_HOST == domain[1:] or LOGIN_HOST.endswith(domain))))
        if not domain_matches or not item.get("name") or not isinstance(item.get("value"), str):
            continue
        expires = item.get("expires", -1)
        if expires != -1 and expires <= time.time():
            continue
        if item.get("partitionKey"):
            continue
        cookie = {key: item[key] for key in
                  ("name", "value", "path", "secure", "httpOnly", "sameSite", "expires")
                  if key in item}
        cookie.update(domain=LOGIN_HOST, path=item.get("path") or "/")
        result.append(cookie)
    return result


def validate_login_config(config):
    # Captured credentials must never be sent to an arbitrary configured host.
    for key, route in (("query_rooms", "/Space/Category/list"),
                       ("query_seats", "/Seat/Index/searchSeats")):
        parsed = urlparse(str(config.get("urls", {}).get(key, "")))
        if (parsed.scheme != "https" or parsed.netloc != LOGIN_HOST or
                parsed.username or parsed.password or parsed.path != route or parsed.query or parsed.fragment):
            raise ValueError("自动登录仅支持示例中的官方 HTTPS 房间和座位查询接口")


def verify_cookies(config, cookies):
    """Read-only authenticated room query; reject anonymous browser cookies."""
    validate_login_config(config)
    probe_config = copy.deepcopy(config)
    probe_config["user_info"] = {}  # Do not accept a stale configured uid as proof.
    probe_config.setdefault("session", {})["verify"] = True
    probe_config.setdefault("request", {})["timeout"] = 5
    booker = client.InstantBooker(probe_config)
    try:
        if not booker._load_cookie_json({"cookies": cookies}):
            return None
        booker.keepalive(timeout=5)
        if not booker.uid or booker.uid == "0":
            return None
        # Preserve any Set-Cookie rotation from the verification request.
        return library_cookies([{
            "name": item.name, "value": item.value, "domain": item.domain,
            "path": item.path, "secure": item.secure,
            "expires": item.expires if item.expires is not None else -1,
            "httpOnly": item.has_nonstandard_attr("HttpOnly"),
        } for item in booker.session.cookies]) or None
    except (RuntimeError, ValueError, KeyError, TypeError, IndexError):
        return None
    finally:
        booker.session.close()


def private_write(path, text):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".login-", suffix=".tmp", delete=False) as file:
            temporary = Path(file.name)
            os.fchmod(file.fileno(), 0o600)
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def updated_config(text, cookie_path):
    """Replace only auth/user_info sections, retaining plan and other comments."""
    tree = yaml.compose(text)
    if not isinstance(tree, yaml.MappingNode):
        raise ValueError("配置必须是 YAML 字段映射")
    if tree.flow_style:
        raise ValueError("自动登录需要分行书写的 YAML 配置，请参考 config.example.yaml")
    sections = {
        "auth": {"cookie_file": str(cookie_path), "cookie": ""},
        "user_info": {"uid": "", "name": ""},
    }
    edits = []
    seen = set()
    for index, (key, value) in enumerate(tree.value):
        if key.value not in sections:
            continue
        if key.value in seen:
            raise ValueError("配置中 auth 或 user_info 重复，请先删除重复配置")
        seen.add(key.value)
        # End at the next top-level key, even for an inline mapping.
        end = tree.value[index + 1][0].start_mark.index if index + 1 < len(tree.value) else len(text)
        block = yaml.safe_dump({key.value: sections[key.value]}, allow_unicode=True, sort_keys=False)
        edits.append((key.start_mark.index, end, block + "\n"))
    for start, end, block in reversed(edits):
        text = text[:start] + block + text[end:]
    for key in sections.keys() - seen:
        text = text.rstrip() + "\n\n" + yaml.safe_dump({key: sections[key]}, sort_keys=False)
    try:
        yaml.safe_load(text)
    except yaml.YAMLError:
        raise ValueError("登录配置包含跨区块 YAML 引用，请先展开 auth 和 user_info 的引用") from None
    return text


def save_login(config_path, cookies, should_cancel=None):
    path = Path(config_path).expanduser().resolve()
    cookies = library_cookies(cookies)
    if not cookies:
        raise ValueError("没有可保存的图书馆 Cookie")
    with CONFIG_LOCK:
        check_cancel(should_cancel)
        configuration.load_config(path)  # Re-read to preserve edits made during login.
        text = path.read_text(encoding="utf-8")
        directory = path.parent / "cookies"
        directory.mkdir(mode=0o700, exist_ok=True)
        target = directory / f"session-{uuid.uuid4().hex}.json"
        new_text = updated_config(text, target)
        try:
            private_write(target, json.dumps({"cookies": cookies}, ensure_ascii=False, indent=2) + "\n")
            check_cancel(should_cancel)
            # Switch the reference only after the complete new cookie file exists.
            private_write(path, new_text)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
    return {"ok": True, "message": "登录成功，Cookie 已保存，配置已更新"}


def playwright_factory():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("自动登录需要可选依赖：python -m pip install -r requirements-login.txt") from None
    return sync_playwright()


def launch_browser(playwright):
    for channel in ("chrome", None):
        try:
            options = {"headless": False}
            if channel:
                options["channel"] = channel
            return playwright.chromium.launch(**options)
        except Exception:
            continue
    raise RuntimeError("无法打开登录浏览器。请安装 Chrome，或运行 python -m playwright install chromium")


def capture_cookies(config, *, logger=print, should_cancel=None, timeout=LOGIN_TIMEOUT):
    validate_login_config(config)
    check_cancel(should_cancel)
    with playwright_factory() as playwright:
        browser = launch_browser(playwright)
        try:
            context = browser.new_context()
            page = context.new_page()
            logger("请在新打开的浏览器中完成登录和验证码；5 分钟内有效，可停止任务取消")
            try:
                page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=20000)
            except Exception:
                logger("登录页面尚未载入，请检查网络并在登录窗口刷新页面")
            deadline = time.monotonic() + timeout
            next_probe = 0.0
            while time.monotonic() < deadline:
                check_cancel(should_cancel)
                if not browser.is_connected() or not context.pages:
                    raise errors.TaskCancelled("登录窗口已关闭，原登录态未修改")
                if time.monotonic() >= next_probe:
                    cookies = library_cookies(context.cookies([
                        LOGIN_URL, config["urls"]["query_rooms"], config["urls"]["query_seats"],
                    ]))
                    if cookies:
                        verified = verify_cookies(config, cookies)
                        check_cancel(should_cancel)
                        if verified:
                            return verified
                    next_probe = time.monotonic() + 5
                context.pages[0].wait_for_timeout(250)
            raise RuntimeError("等待登录超时，原登录态未修改；请重新获取 Cookie")
        except errors.TaskCancelled:
            raise
        except RuntimeError:
            raise
        except Exception:
            # Browser diagnostics can contain redirect URLs with login tokens.
            raise RuntimeError("登录窗口已关闭或浏览器连接中断，原登录态未修改") from None
        finally:
            try:
                browser.close()
            except Exception:
                pass


def login(config_path=constants.DEFAULT_CONFIG, *, logger=print, should_cancel=None):
    if not LOGIN_LOCK.acquire(blocking=False):
        raise RuntimeError("已有登录任务正在运行")
    try:
        config = configuration.load_config(config_path)
        cookies = capture_cookies(config, logger=logger, should_cancel=should_cancel)
        result = save_login(config_path, cookies, should_cancel=should_cancel)
        logger(result["message"])
        return result
    finally:
        LOGIN_LOCK.release()
