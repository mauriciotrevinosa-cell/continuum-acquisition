"""MangaUpdates public API (bibliographic database; not an official source).

Used for relations between series, original/English publishers, status and
latest chapter. Publisher entries are what point at OFFICIAL sources.
"""
from __future__ import annotations

import re

from .. import taxonomy
from ..http import HttpClient, ProviderUnavailable


class MangaUpdates:
    name = "mangaupdates"
    BASE = "https://api.mangaupdates.com/v1"
    relation_map = taxonomy.MU_RELATION
    traverse = taxonomy.MU_TRAVERSE

    def __init__(self, http: HttpClient, ttl_days: float = 30):
        self.http = http
        self.ttl_days = ttl_days

    def search(self, text: str, perpage: int = 25) -> list[dict]:
        status, data = self.http.json("POST", f"{self.BASE}/series/search",
                                      body={"search": text, "perpage": perpage}, ttl_days=self.ttl_days)
        if status != 200 or not isinstance(data, dict):
            raise ProviderUnavailable(f"MangaUpdates search HTTP {status}")
        out = []
        for r in data.get("results", []):
            rec = r.get("record", {})
            out.append({"id": rec.get("series_id"), "title": rec.get("title"), "type": rec.get("type"),
                        "year": rec.get("year"), "hit_title": r.get("hit_title")})
        return out

    def node(self, series_id, ttl_days: float | None = None) -> dict | None:
        status, data = self.http.json("GET", f"{self.BASE}/series/{series_id}",
                                      ttl_days=self.ttl_days if ttl_days is None else ttl_days)
        if status == 404:
            return None
        if status != 200 or not isinstance(data, dict):
            raise ProviderUnavailable(f"MangaUpdates series HTTP {status}")
        return summarize(data)

    @staticmethod
    def type_compatible(provider_type: str | None, medium: str) -> bool:
        t = (provider_type or "").lower()
        if t == "doujinshi":
            return False
        if medium in ("manga",):
            return t in ("manga", "oel", "")
        if medium in ("manhwa", "manhua"):
            return t == medium
        if medium in ("light-novel", "web-novel", "novel"):
            return t == "novel"
        return True


def _volumes(status_text: str) -> int | None:
    m = re.search(r"(\d+)\s+Volumes?", status_text or "", re.I)
    return int(m.group(1)) if m else None


def summarize(s: dict) -> dict:
    ptype = s.get("type") or ""
    title = s.get("title") or ""
    medium = taxonomy.MU_TYPE_MEDIUM.get(ptype.lower(), "manga")
    if medium == "light-novel" and re.search(r"web ?novel|\(wn\)", title, re.I):
        medium = "web-novel"
    try:
        latest = int(float(s.get("latest_chapter"))) if s.get("latest_chapter") not in (None, "", 0) else None
    except (TypeError, ValueError):
        latest = None
    return {
        "provider": "mangaupdates",
        "id": s.get("series_id"),
        "title": title,
        "aliases": [a.get("title") for a in s.get("associated", []) if a.get("title")],
        "type": ptype,
        "medium": medium,
        "completed": s.get("completed"),
        "latest_chapter": latest,
        "volumes": _volumes(s.get("status") or ""),
        "status_text": (s.get("status") or "").strip(),
        "year": s.get("year"),
        "licensed": s.get("licensed"),
        "publishers": [{"name": p.get("publisher_name"), "type": p.get("type"), "notes": p.get("notes") or ""}
                       for p in s.get("publishers", []) if p.get("publisher_name")],
        "publications": [p.get("publication_name") for p in s.get("publications", []) if p.get("publication_name")],
        "authors": [f"{a.get('name')} ({a.get('type')})" for a in s.get("authors", []) if a.get("name")],
        "url": s.get("url"),
        "official_urls": [],
        "related": [(r.get("relation_type"), r.get("related_series_id"), r.get("related_series_name"))
                    for r in s.get("related_series", []) if r.get("related_series_id")],
    }
