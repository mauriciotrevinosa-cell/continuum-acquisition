"""National Diet Library (Japan) OpenSearch.

Japan's national legal-deposit catalogue: the strongest public evidence that a
Japanese guidebook, art book, anthology or colour edition was actually
PUBLISHED, and by whom.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from urllib.parse import urlencode

from ..http import HttpClient, ProviderUnavailable

OPENSEARCH_NS = "{http://a9.com/-/spec/opensearchrss/1.0/}"


class NDL:
    name = "ndl"
    URL = "https://ndlsearch.ndl.go.jp/api/opensearch"

    def __init__(self, http: HttpClient, ttl_days: float = 30):
        self.http = http
        self.ttl_days = ttl_days

    def search_books(self, title: str, max_records: int = 1000, ttl_days: float | None = None) -> list[dict]:
        out: list[dict] = []
        idx = 1
        while len(out) < max_records:
            query = {"title": title, "cnt": 500, "idx": idx, "mediatype": "books"}
            status, payload = self.http.request("GET", f"{self.URL}?{urlencode(query)}",
                                                ttl_days=self.ttl_days if ttl_days is None else ttl_days)
            if status != 200:
                raise ProviderUnavailable(f"NDL HTTP {status}")
            try:
                root = ET.fromstring(payload)
            except ET.ParseError as err:
                raise ProviderUnavailable(f"NDL returned unparsable XML: {err}") from err
            items = root.findall(".//item")
            out.extend(parse_item(it) for it in items)
            total_text = root.findtext(f".//{OPENSEARCH_NS}totalResults")
            total = int(total_text) if (total_text or "").isdigit() else len(out)
            idx += len(items)
            if not items or idx > total:
                break
        return out


def parse_item(item) -> dict:
    rec = {"title": (item.findtext("title") or "").strip(), "link": item.findtext("link"),
           "category": [c.text for c in item.findall("category") if c.text], "creators": [],
           "publishers": [], "isbn": [], "dc_title": None, "volume": None, "series_title": None, "issued": None}
    for child in item:
        tag = child.tag.split("}")[-1]
        text = (child.text or "").strip()
        attrs = " ".join(child.attrib.values())
        if tag == "title" and child.tag.startswith("{http://purl.org/dc"):
            rec["dc_title"] = text
        elif tag == "volume":
            rec["volume"] = text
        elif tag == "seriesTitle":
            rec["series_title"] = text
        elif tag == "creator" and text:
            rec["creators"].append(text)
        elif tag == "publisher" and text:
            rec["publishers"].append(text)
        elif tag in ("issued", "date") and text and not rec["issued"]:
            rec["issued"] = text
        elif tag == "identifier" and "ISBN" in attrs.upper() and text:
            rec["isbn"].append(re.sub(r"[^0-9Xx]", "", text))
    return rec


def is_book(rec: dict) -> bool:
    cats = rec.get("category") or []
    return (not cats) or any("図書" in c for c in cats)


_VOL_TAIL = re.compile(r"[\s　:：\-]*(?:第?\s*\d+\s*巻?|\(\s*\d+\s*\)|（\s*\d+\s*）|[上中下]|前編|後編|vol\.?\s*\d+)$",
                       re.IGNORECASE)


def base_title(rec: dict) -> str:
    t = (rec.get("dc_title") or rec.get("title") or "").strip()
    prev = None
    while prev != t:
        prev = t
        t = _VOL_TAIL.sub("", t).strip()
    return t


def volume_number(rec: dict) -> int | None:
    for src in (rec.get("volume"), rec.get("dc_title"), rec.get("title")):
        m = re.search(r"(\d+)\s*巻?\s*$", str(src or ""))
        if m:
            return int(m.group(1))
    return None
