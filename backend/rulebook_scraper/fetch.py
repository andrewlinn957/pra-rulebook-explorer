from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

BASE_URL = "https://www.prarulebook.co.uk"
USER_AGENT = "rulebookexp/0.1 (+local research prototype)"


def normalise_url(url: str) -> str:
    return urljoin(BASE_URL, url)


def cache_name(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.strip("/").replace("/", "__") or "index"
    query = parsed.query.replace("&", "_").replace("=", "-")
    return f"{path}{('__' + query) if query else ''}.html"


def fetch_url(url: str, raw_dir: Path, *, refresh: bool = False) -> tuple[str, str, str]:
    full_url = normalise_url(url)
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / cache_name(full_url)
    if path.exists() and not refresh:
        return full_url, path.read_text(encoding="utf-8"), path.stat().st_mtime_ns.__str__()

    response = requests.get(full_url, timeout=30, headers={"User-Agent": USER_AGENT})
    response.raise_for_status()
    path.write_text(response.text, encoding="utf-8")
    return full_url, response.text, datetime.now(timezone.utc).isoformat()
