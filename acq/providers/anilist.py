"""AniList GraphQL (bibliographic database; covers anime and official links).

When AniList disables its API it answers HTTP 403 with an explanatory error;
that is reported as ProviderUnavailable and the run continues without it.
"""
from __future__ import annotations

from .. import taxonomy
from ..http import HttpClient, ProviderUnavailable

_FIELDS = """id type format status chapters volumes episodes countryOfOrigin
 title { romaji english native } synonyms startDate { year } siteUrl
 externalLinks { url site type language }
 staff(perPage: 4, sort: RELEVANCE) { edges { role node { name { full } } } }
 relations { edges { relationType(version: 2) node { id type format title { romaji english native } } } }"""


class AniList:
    name = "anilist"
    URL = "https://graphql.anilist.co"
    relation_map = taxonomy.ANILIST_RELATION
    traverse = taxonomy.ANILIST_TRAVERSE

    def __init__(self, http: HttpClient, ttl_days: float = 30):
        self.http = http
        self.ttl_days = ttl_days
        self.available: bool | None = None

    def _query(self, query: str, variables: dict, ttl_days: float | None = None) -> dict:
        if self.available is False:
            raise ProviderUnavailable("AniList marked unavailable for this run")
        status, data = self.http.json("POST", self.URL, body={"query": query, "variables": variables},
                                      ttl_days=self.ttl_days if ttl_days is None else ttl_days)
        if status != 200 or not isinstance(data, dict) or data.get("data") is None:
            msg = ""
            if isinstance(data, dict) and data.get("errors"):
                msg = data["errors"][0].get("message", "")
            if status in (403, 503) or "disabled" in msg.lower():
                self.available = False
            raise ProviderUnavailable(f"AniList HTTP {status}: {msg or 'no data'}")
        self.available = True
        return data["data"]

    def search(self, text: str, perpage: int = 10) -> list[dict]:
        q = ("query($s:String,$n:Int){ Page(perPage:$n){ media(search:$s){ id type format "
             "title{ romaji english native } startDate{year} } } }")
        data = self._query(q, {"s": text, "n": perpage})
        return [{"id": m["id"], "title": m["title"].get("english") or m["title"].get("romaji"),
                 "type": m.get("format"), "year": (m.get("startDate") or {}).get("year"),
                 "hit_title": m["title"].get("romaji")} for m in data["Page"]["media"]]

    def node(self, media_id, ttl_days: float | None = None) -> dict | None:
        data = self._query("query($id:Int){ Media(id:$id){ %s } }" % _FIELDS, {"id": int(media_id)}, ttl_days)
        m = data.get("Media")
        return summarize(m) if m else None

    @staticmethod
    def type_compatible(provider_type: str | None, medium: str) -> bool:
        fmt = (provider_type or "").upper()
        return taxonomy.ANILIST_FORMAT_MEDIUM.get(fmt, "manga") == medium or \
            (medium in ("manhwa", "manhua") and fmt == "MANGA")


def summarize(m: dict) -> dict:
    t = m.get("title") or {}
    fmt = m.get("format") or ""
    medium = taxonomy.ANILIST_FORMAT_MEDIUM.get(fmt, "manga")
    if medium == "manga" and m.get("countryOfOrigin") == "KR":
        medium = "manhwa"
    elif medium == "manga" and m.get("countryOfOrigin") == "CN":
        medium = "manhua"
    links = m.get("externalLinks") or []
    return {
        "provider": "anilist",
        "id": m.get("id"),
        "title": t.get("english") or t.get("romaji") or t.get("native"),
        "aliases": [x for x in (t.get("romaji"), t.get("native"), *(m.get("synonyms") or [])) if x],
        "type": fmt,
        "medium": medium,
        "completed": (m.get("status") == "FINISHED") if m.get("status") else None,
        "latest_chapter": m.get("chapters"),
        "volumes": m.get("volumes"),
        "status_text": m.get("status") or "",
        "year": (m.get("startDate") or {}).get("year"),
        "publishers": [],
        "authors": [f"{e['node']['name']['full']} ({e.get('role')})" for e in
                    ((m.get("staff") or {}).get("edges") or []) if e.get("node")],
        "url": m.get("siteUrl"),
        "official_urls": [{"site": l.get("site"), "url": l.get("url"), "language": l.get("language"),
                           "type": l.get("type")} for l in links if l.get("url")],
        "related": [(e.get("relationType"), e["node"]["id"],
                     (e["node"].get("title") or {}).get("english") or (e["node"].get("title") or {}).get("romaji"))
                    for e in ((m.get("relations") or {}).get("edges") or []) if e.get("node")],
        "not_yet_released": m.get("status") == "NOT_YET_RELEASED",
    }
