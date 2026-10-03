"""Local web listening and authentication settings."""

import os
import secrets

from libcs import constants

DEFAULT_CONFIG = constants.PROJECT_ROOT / "config.yaml"

HOST = "127.0.0.1"

PORT = 8765

WEB_AUTH_USERNAME = os.environ.get("HDU_WEB_USERNAME", "hdu")

WEB_AUTH_PASSWORD = os.environ.get("HDU_WEB_PASSWORD", "")

WEB_CSRF_TOKEN = secrets.token_urlsafe(32)
