"""Fixed console assets. Never expose arbitrary paths from the project directory."""

from pathlib import Path

ASSET_DIR = Path(__file__).with_name("static")
INDEX_HTML = (ASSET_DIR / "index.html").read_text(encoding="utf-8")
STATIC_ASSETS = {
    "/static/console.css": ("text/css; charset=utf-8", (ASSET_DIR / "console.css").read_bytes()),
    "/static/console.js": ("text/javascript; charset=utf-8", (ASSET_DIR / "console.js").read_bytes()),
}
