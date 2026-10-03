"""Request checks for the local console and authenticated reverse proxies."""

import hmac
import ipaddress
import json
import re
from urllib.parse import urlsplit

MAX_BODY_BYTES = 64 * 1024


class RequestRejected(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def is_loopback(host):
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def authority(value, scheme):
    try:
        parsed = urlsplit(f"{scheme}://{value}")
        if (not parsed.hostname or parsed.username is not None or parsed.password is not None
                or parsed.path or parsed.query or parsed.fragment
                or re.search(r"[\s,\\]", value)):
            raise ValueError
        return parsed.hostname.lower(), parsed.port or (443 if scheme == "https" else 80)
    except ValueError:
        raise RequestRejected("请求地址无效", 403) from None


def validate_host(headers, authenticated, port):
    host = headers.get("Host", "")
    name, supplied_port = authority(host, "http")
    if not authenticated and (not is_loopback(name) or supplied_port != port):
        raise RequestRejected("本机控制台只接受本机地址", 403)
    if headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
        raise RequestRejected("不接受来自其他网站的请求", 403)


def validate_mutation(headers, token, authenticated):
    origin = headers.get("Origin", "")
    try:
        parsed = urlsplit(origin)
        if (parsed.scheme not in ("http", "https") or not parsed.netloc
                or parsed.path or parsed.query or parsed.fragment):
            raise ValueError
        if authority(parsed.netloc, parsed.scheme) != authority(headers.get("Host", ""), parsed.scheme):
            raise ValueError
        if not authenticated and parsed.scheme != "http":
            raise ValueError
    except ValueError:
        raise RequestRejected("请求来源不匹配，请从控制台页面操作", 403) from None
    supplied = headers.get("X-CSRF-Token", "")
    if not hmac.compare_digest(supplied.encode("utf-8"), token.encode("utf-8")):
        raise RequestRejected("页面已失效，请刷新后重试", 403)


def read_json_body(headers, stream):
    if headers.get("Transfer-Encoding"):
        raise RequestRejected("不支持分块请求")
    if headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise RequestRejected("请求必须使用 application/json", 415)
    length_text = headers.get("Content-Length", "")
    if not re.fullmatch(r"[0-9]{1,10}", length_text):
        raise RequestRejected("请求缺少有效的 Content-Length", 411)
    length = int(length_text)
    if length > MAX_BODY_BYTES:
        raise RequestRejected("请求内容过大", 413)
    raw = stream.read(length)
    if len(raw) != length:
        raise RequestRejected("请求内容不完整")
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result
    def reject_constant(_):
        raise ValueError
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object,
                             parse_constant=reject_constant)
    except (ValueError, UnicodeDecodeError):
        raise RequestRejected("请求必须是有效的 UTF-8 JSON，字段不能重复") from None
    if not isinstance(payload, dict):
        raise RequestRejected("请求内容必须是 JSON 对象")
    for value in payload.values():
        if isinstance(value, (dict, list)):
            raise RequestRejected("请求字段必须是简单值")
    for key in ("config_path", "job_id", "execute_at", "fallback_seats"):
        if key in payload and not isinstance(payload[key], str):
            raise RequestRejected(f"{key} 必须是字符串")
    if "dry_run" in payload and not isinstance(payload["dry_run"], bool):
        raise RequestRejected("dry_run 必须是布尔值")
    return payload
