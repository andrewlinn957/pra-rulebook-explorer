"""Stable legal identity and source-version keys.

The Rulebook publishes the same legal locator under dated page paths.  The
helpers in this module keep the legal locator independent of that page date,
while retaining the date (or an immutable snapshot identifier) on the version
and source-page identities.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date
from urllib.parse import urlparse


_DATE_SEGMENT_RE = re.compile(r"^(?P<day>\d{2})[-/](?P<month>\d{2})[-/](?P<year>\d{4})$")
_ISO_DATE_RE = re.compile(r"^(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})$")


def normalise_rulebook_date(value: str | None) -> str | None:
    """Return a valid rulebook date as ``DD-MM-YYYY``.

    The live site has used both slash and hyphen display forms, and test/data
    imports occasionally provide ISO dates.  Invalid or empty values are not
    legal identity dates and return ``None``.
    """

    if not value:
        return None
    candidate = str(value).strip()
    match = _DATE_SEGMENT_RE.fullmatch(candidate)
    if match:
        day, month, year = (int(match.group(name)) for name in ("day", "month", "year"))
    else:
        match = _ISO_DATE_RE.fullmatch(candidate)
        if not match:
            return None
        year, month, day = (int(match.group(name)) for name in ("year", "month", "day"))
    try:
        date(year, month, day)
    except ValueError:
        return None
    return f"{day:02d}-{month:02d}-{year:04d}"


def _path_from_url(value: str) -> str:
    parsed = urlparse(value)
    path = parsed.path if parsed.scheme or parsed.netloc else value.split("#", 1)[0].split("?", 1)[0]
    return "/".join(part for part in path.strip("/").split("/") if part)


def rulebook_date_from_url(url: str) -> str | None:
    """Extract a strict trailing date segment from a Rulebook URL/path."""

    path = _path_from_url(url)
    if not path:
        return None
    return normalise_rulebook_date(path.rsplit("/", 1)[-1])


def _date_free_path(url: str) -> str:
    path = _path_from_url(url)
    if path:
        last = path.rsplit("/", 1)[-1]
        if rulebook_date_from_url(url):
            path = path[: -(len(last) + 1)]
    return path


def canonical_document_path(url: str) -> str:
    """Return a Rulebook document path without query, fragment or date."""

    return _date_free_path(url)


def canonical_part_key(url: str) -> str:
    """Return the date-free legal identity of a PRA Rulebook Part."""

    return f"part:{_date_free_path(url)}"


def source_page_key(url: str) -> str:
    """Return the identity of the dated source page/snapshot location."""

    return f"source_page:{_path_from_url(url)}"


def canonical_provision_key(
    part_url: str,
    structural_locator: str,
    rule_number: str,
) -> str:
    """Build a date-free provision key scoped to its Part and structure.

    ``structural_locator`` is deliberately required.  Article/paragraph
    numbers such as ``1`` recur throughout CRR instruments and are not legal
    identities on their own.
    """

    part_path = _date_free_path(part_url)
    locator = ":".join(str(structural_locator).strip(":/").split()) or "root"
    number = ":".join(str(rule_number).strip(":/").split()) or "unnumbered"
    return f"provision:{part_path}:{locator}:{number}"


def provision_version_key(
    canonical_key: str,
    rulebook_date: str | None,
    *,
    snapshot: str | None = None,
) -> str:
    """Build a dated or immutable undated version key for a provision."""

    if not canonical_key.startswith("provision:"):
        raise ValueError("canonical_key must start with 'provision:'")
    date_key = normalise_rulebook_date(rulebook_date)
    if date_key:
        suffix = date_key
    elif snapshot:
        suffix = f"undated:{snapshot}"
    else:
        raise ValueError("an undated provision version requires a snapshot id")
    return f"provision_version:{canonical_key.removeprefix('provision:')}:{suffix}"


def snapshot_id(url: str, content: str) -> str:
    """Return a stable immutable identifier for one URL/content pair."""

    digest = hashlib.sha1(f"{url}\x00{content}".encode("utf-8")).hexdigest()
    return f"snapshot:{digest}"


def identity_type_for_node(node_type: str, stable_key: str) -> str | None:
    """Classify identity-bearing nodes while retaining legacy node types."""

    if node_type == "provision":
        return "canonical_provision"
    if stable_key.startswith("provision_version:"):
        return "provision_version"
    if node_type == "part":
        return "source_page"
    return None
