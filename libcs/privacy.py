"""Keep account details and local paths out of diagnostics."""

import json
import re
from pathlib import Path

from libcs.constants import PROJECT_ROOT

_PROJECT_ROOT = str(PROJECT_ROOT)
_HOME = str(Path.home())
_PRIVATE_KEYS = {
    "uid", "userid", "userinfo", "userbaseinfo", "booker", "bookername",
    "name", "uname", "unickname", "nickname", "username", "realname", "loginname",
    "email", "mail", "phone", "mobile", "studentid", "studentno", "stuid", "sno",
    "cookie", "cookies", "setcookie", "sessionid", "phpsessid", "jsessionid",
    "authorization", "token", "accesstoken", "refreshtoken", "apitoken", "apikey",
    "password", "secret", "credentials",
}


def redact_private_text(value):
    text = str(value).replace(_PROJECT_ROOT, "<project>")
    if _HOME != "/":
        text = text.replace(_HOME, "~")
    return re.sub(r"/(Users|home)/[^/\s'\"<>]+", r"/\1/<user>", text)


def redact_diagnostic_data(value):
    """Copy diagnostics without changing the payload used for real requests."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            private = normalized in _PRIVATE_KEYS or normalized.startswith("seatbookers")
            result[key] = "<redacted>" if private else redact_diagnostic_data(item)
        return result
    if isinstance(value, (list, tuple)):
        return [redact_diagnostic_data(item) for item in value]
    if isinstance(value, str):
        return redact_private_text(value)
    return value


def diagnostic_json(value):
    return json.dumps(redact_diagnostic_data(value), ensure_ascii=False, indent=2)
