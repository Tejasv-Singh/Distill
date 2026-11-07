"""Precise mode: CSS selectors -> rows (one row per repeating item)."""
from __future__ import annotations

from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from ..security import clean_text

URL_ATTRS = {"href", "src", "action", "data-src", "poster", "srcset"}


def _value(el: Tag, attr: str, base: str) -> str | None:
    if attr == "text":
        return clean_text(" ".join(el.get_text(" ", strip=True).split())) or None
    if attr == "html":
        return el.decode_contents().strip() or None
    v = el.get(attr)
    if isinstance(v, list):
        v = " ".join(v)
    if v and attr in URL_ATTRS:
        v = urljoin(base, v.split(" ")[0] if attr == "srcset" else v)
    return clean_text(v) if v else None


def extract_rows(html: str, url: str, item_selector: str | None, fields: list) -> list[dict]:
    if not fields:
        return []
    soup = BeautifulSoup(html, "lxml")
    try:
        scopes = soup.select(item_selector) if item_selector else [soup]
    except Exception as e:  # noqa: BLE001  (invalid selector)
        raise ValueError(f"Invalid item selector: {e}") from e
    rows = []
    for scope in scopes[:5000]:
        row: dict = {}
        for f in fields:
            try:
                if f.multiple:
                    row[f.name] = [v for v in (_value(e, f.attr, url) for e in scope.select(f.selector)) if v]
                else:
                    el = scope.select_one(f.selector)
                    row[f.name] = _value(el, f.attr, url) if el else None
            except Exception as e:  # noqa: BLE001
                raise ValueError(f"Invalid selector for field '{f.name}': {e}") from e
        if any(v not in (None, "", []) for v in row.values()):
            rows.append(row)
    return rows
