"""Adapter for a user-provided website.

Knows nothing about any particular site. It discovers what a site offers by
looking at what the site itself publishes:

  * robots.txt          what we are allowed to request (always honoured)
  * RSS / Atom feeds    releases and update tracking
  * OpenSearch document a real search endpoint, published by the site
  * a search template   provided by the user as `search` with a {q} slot
  * OpenGraph / JSON-LD work metadata

Downloads happen only when the registry entry is explicitly marked
download_permitted (DRM-free material the user is entitled to) AND the link
is a direct file. Nothing here logs in, solves a challenge, or works around
a paywall or DRM; when a site needs that, the answer is a manual instruction
for the user, not an automated workaround.
"""
from __future__ import annotations

import hashlib
import os
import re
import uuid
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from urllib.parse import quote, urljoin, urlparse

from ..util import LOG, norm, safe_folder_name, sha256_file, similarity, tokens
from .base import (AUTOMATIC_ACQUISITION, DISCOVERY_ONLY, MANUAL_ACQUISITION, METADATA, UPDATE_TRACKING,
                   Adapter, FileRef, Hit, NotSupported, RobotsGate, SourceRefused, UpdateStatus)

FILE_EXT = (".cbz", ".cbr", ".zip", ".epub", ".pdf", ".mobi", ".azw3", ".7z", ".rar", ".txt")
FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/feed+json")
OPENSEARCH_TYPE = "application/opensearchdescription+xml"
CHAPTER_TEXT = re.compile(r"(?:^|\b)(?:ch(?:apter)?|ep(?:isode)?|vol(?:ume)?|#)\s*\.?\s*(\d+(?:\.\d+)?)", re.I)


class _Page(HTMLParser):
    """Collects only what an adapter needs: links, feeds, and page metadata."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.anchors: list[tuple[str, str]] = []
        self.links: list[dict] = []
        self.meta: dict[str, str] = {}
        self.title = ""
        self._href: str | None = None
        self._text: list[str] = []
        self._in_title = False
        self._in_ld = False
        self.ld_json: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and a.get("href"):
            self._href, self._text = a["href"], []
        elif tag == "link" and a.get("href"):
            self.links.append({"rel": (a.get("rel") or "").lower(), "type": (a.get("type") or "").lower(),
                               "href": a["href"], "title": a.get("title") or ""})
        elif tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key and a.get("content"):
                self.meta.setdefault(key, a["content"])
        elif tag == "title":
            self._in_title = True
        elif tag == "script" and (a.get("type") or "").lower() == "application/ld+json":
            self._in_ld = True

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.anchors.append((self._href, " ".join(" ".join(self._text).split())))
            self._href, self._text = None, []
        elif tag == "title":
            self._in_title = False
        elif tag == "script":
            self._in_ld = False

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)
        if self._in_title:
            self.title += data
        if self._in_ld:
            self.ld_json.append(data)


def _feed_items(body: bytes) -> list[dict]:
    """RSS or Atom, whichever the site publishes."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return []
    out = []
    for node in root.iter():
        tag = node.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        item = {"title": "", "url": "", "updated": ""}
        for child in node:
            ctag = child.tag.split("}")[-1]
            text = (child.text or "").strip()
            if ctag == "title":
                item["title"] = text
            elif ctag == "link":
                item["url"] = text or child.attrib.get("href", "")
            elif ctag in ("pubDate", "updated", "published", "date"):
                item["updated"] = item["updated"] or text
        if item["title"] or item["url"]:
            out.append(item)
    return out


class WebAdapter(Adapter):
    kind = "web"
    declared_capabilities = (DISCOVERY_ONLY, METADATA, UPDATE_TRACKING, MANUAL_ACQUISITION)
    implements = ("search", "get_work_metadata", "get_releases", "get_chapters", "get_latest",
                  "get_available_files", "acquire", "get_update_status")

    def __init__(self, entry, *, http=None, policy=None, providers=None):
        super().__init__(entry, http=http, policy=policy, providers=providers)
        self.robots = RobotsGate(http, getattr(http, "user_agent", "ContinuumAcquisition")) if http else None

    # -- plumbing -----------------------------------------------------------
    def _guard(self, url: str) -> None:
        refused = self.policy.host_refused(url)
        if refused:
            raise SourceRefused(refused)
        if self.robots is not None:
            self.robots.check(url)

    def _get(self, url: str, ttl_days: float | None = None) -> tuple[int, bytes]:
        if self.http is None:
            raise SourceRefused("no HTTP client available for this adapter")
        self._guard(url)
        return self.http.request("GET", url, ttl_days=ttl_days)

    def _page(self, url: str, ttl_days: float | None = None) -> tuple[int, _Page]:
        status, body = self._get(url, ttl_days=ttl_days)
        if status >= 400:
            # An error page has a <title> too. Parsing it would turn "Not
            # Found" into a work title, so a failed fetch stays a failure.
            raise SourceRefused(f"HTTP {status} for {url}")
        page = _Page()
        if body:
            try:
                page.feed(body.decode("utf-8", "replace"))
            except Exception as err:  # a malformed page is data, not a crash
                LOG.debug("HTML parse failed for %s: %s", url, err)
        return status, page

    def _search_url(self, query: str) -> str | None:
        template = self.entry.get("search") or (self.entry.get("discovered") or {}).get("opensearch_template")
        if not template or "{q}" not in template:
            return None
        return template.replace("{q}", quote(query))

    def _feed_url(self) -> str | None:
        return self.entry.get("feed") or (self.entry.get("discovered") or {}).get("feed")

    # -- operations ---------------------------------------------------------
    def search(self, query: str, limit: int = 10, **kw) -> list[Hit]:
        self.require("search")
        url = self._search_url(query)
        if not url:
            raise NotSupported(f"source '{self.id}' has no search endpoint; add one with "
                               f"--search 'https://example.org/search?q={{q}}' or let `sources test` "
                               f"discover an OpenSearch document")
        _status, page = self._page(url, ttl_days=1)
        wanted = tokens(query)
        hits: list[Hit] = []
        seen: set[str] = set()
        for href, text in page.anchors:
            if not text or len(text) < 2:
                continue
            absolute = urljoin(url, href)
            if absolute in seen or urlparse(absolute).scheme not in ("http", "https"):
                continue
            overlap = len(wanted & tokens(text)) / max(1, len(wanted))
            score = max(similarity(query, text), overlap)
            if score < 0.34:
                continue
            seen.add(absolute)
            hits.append(Hit(title=text[:200], url=absolute, kind="work", score=round(score, 3),
                            detail=f"result on {urlparse(absolute).hostname}"))
        hits.sort(key=lambda h: -h.score)
        return hits[:limit]

    def get_work_metadata(self, ref: str, **kw) -> dict:
        self.require("get_work_metadata")
        url = urljoin(self.url or ref, ref)
        _status, page = self._page(url, ttl_days=7)
        meta = {"url": url, "title": page.meta.get("og:title") or page.title.strip() or None,
                "description": page.meta.get("og:description") or page.meta.get("description"),
                "site": page.meta.get("og:site_name"), "image": page.meta.get("og:image"),
                "type": page.meta.get("og:type"), "source": self.id}
        for blob in page.ld_json:
            for key in ("name", "author", "datePublished", "numberOfEpisodes"):
                found = re.search(rf'"{key}"\s*:\s*"([^"]{{1,200}})"', blob)
                if found:
                    meta.setdefault(f"ld_{key}", found.group(1))
        return meta

    def get_chapters(self, ref: str, limit: int = 200, **kw) -> list[dict]:
        self.require("get_chapters")
        feed = self._feed_url()
        if feed:
            _status, body = self._get(feed, ttl_days=0.5)
            items = _feed_items(body)
            if items:
                return [{"label": i["title"], "url": i["url"], "updated": i["updated"],
                         "number": (CHAPTER_TEXT.search(i["title"]) or [None, None])[1]
                         if CHAPTER_TEXT.search(i["title"]) else None} for i in items[:limit]]
        url = urljoin(self.url or ref, ref)
        _status, page = self._page(url, ttl_days=1)
        out = []
        for href, text in page.anchors:
            found = CHAPTER_TEXT.search(text)
            if found:
                out.append({"label": text[:120], "url": urljoin(url, href), "number": found.group(1),
                            "updated": ""})
        return out[:limit]

    def get_releases(self, ref: str, **kw) -> list[dict]:
        self.require("get_releases")
        return self.get_chapters(ref, **kw)

    def get_latest(self, ref: str = "", **kw) -> dict:
        self.require("get_latest")
        items = self.get_chapters(ref or self.url, limit=1)
        return items[0] if items else {}

    def get_available_files(self, ref: str = "", **kw) -> list[FileRef]:
        self.require("get_available_files")
        url = urljoin(self.url or ref, ref) if ref else self.url
        if not url:
            return []
        _status, page = self._page(url, ttl_days=1)
        permitted = bool(self.entry.get("download_permitted"))
        out = []
        for href, text in page.anchors:
            absolute = urljoin(url, href)
            if not absolute.lower().split("?")[0].endswith(FILE_EXT):
                continue
            out.append(FileRef(name=os.path.basename(urlparse(absolute).path) or (text[:80] or "file"),
                               location=absolute, kind="download", direct=permitted,
                               note="" if permitted else "manual: this source is not marked download_permitted"))
        return out

    def acquire(self, file_ref: FileRef, dest_dir: str, **kw) -> dict:
        self.require("acquire")
        if not file_ref.direct:
            raise SourceRefused("not a direct file this source is permitted to serve automatically")
        url = file_ref.location
        self._guard(url)
        if self.policy.vault_root and os.path.normcase(dest_dir).startswith(
                os.path.normcase(self.policy.vault_root)):
            raise SourceRefused("downloads never land in the Vault; they go to the intake")
        os.makedirs(dest_dir, exist_ok=True)
        target = os.path.join(dest_dir, safe_folder_name(file_ref.name or "download.bin"))
        if os.path.exists(target):
            return {"result": "already in intake", "path": target, "source": self.id}
        partial = target + ".partial"
        try:
            size = self.http.download(url, partial)
            os.rename(partial, target)  # Windows refuses to replace an existing file
        except FileExistsError:
            os.remove(partial)
            return {"result": "target appeared meanwhile; kept it", "path": target, "source": self.id}
        except Exception as err:
            if os.path.exists(partial):
                os.remove(partial)
            return {"result": f"failed: {err}", "path": target, "source": self.id}
        return {"result": "downloaded", "path": target, "bytes": size, "sha256": sha256_file(target),
                "source": self.id, "url": url}

    def get_update_status(self, state: dict | None = None, **kw) -> UpdateStatus:
        self.require("get_update_status")
        watch = self.entry.get("watch_url") or self._feed_url() or self.url
        if not watch:
            raise NotSupported(f"source '{self.id}' has nothing to watch (no feed, no watch_url)")
        _status, body = self._get(watch, ttl_days=0)
        items = _feed_items(body)
        if items:
            material = "\n".join(f"{i['title']}|{i['url']}" for i in items[:50])
            latest = items[0]["title"]
        else:
            text = body.decode("utf-8", "replace")
            material = "\n".join(sorted({t for _h, t in _anchors(text) if t}))[:20000]
            latest = None
        fingerprint = hashlib.sha256(material.encode("utf-8")).hexdigest()
        previous = (state or {}).get("fingerprint")
        return UpdateStatus(fingerprint=fingerprint, changed=bool(previous) and previous != fingerprint,
                            latest=latest, items=tuple(items[:20]),
                            detail="first observation (baseline)" if not previous else
                            ("new content on the page/feed" if previous != fingerprint else "unchanged"))

    # -- diagnostics --------------------------------------------------------
    def self_test(self) -> dict:
        checks: list[dict] = []
        caps = {DISCOVERY_ONLY, MANUAL_ACQUISITION}
        discovered: dict = {}
        url = self.url
        refused = self.policy.host_refused(url)
        if refused:
            return self._result(ok=False, checks=[{"check": "policy", "ok": False, "detail": refused}],
                                capabilities=[], error=refused)
        if self.http is None:
            return self._result(ok=False, checks=[{"check": "http", "ok": False, "detail": "no HTTP client"}],
                                error="no HTTP client")
        allowed = True
        if self.robots is not None:
            allowed = self.robots.allows(url)
            checks.append({"check": "robots.txt", "ok": allowed,
                           "detail": "allowed" if allowed else "the site disallows this path for our user agent"})
        if not allowed:
            return self._result(ok=False, checks=checks, capabilities=[], error="robots.txt disallows this source")
        try:
            status, page = self._page(url, ttl_days=1)
            checks.append({"check": "reachable", "ok": status == 200, "detail": f"HTTP {status}"})
            if status != 200:
                return self._result(ok=False, checks=checks, error=f"HTTP {status}")
        except Exception as err:
            checks.append({"check": "reachable", "ok": False, "detail": str(err)})
            return self._result(ok=False, checks=checks, error=str(err))
        feed = next((urljoin(url, x["href"]) for x in page.links if x["type"] in FEED_TYPES), None)
        if feed:
            discovered["feed"] = feed
            caps.add(UPDATE_TRACKING)
        checks.append({"check": "feed", "ok": bool(feed), "detail": feed or "no RSS/Atom feed advertised"})
        opensearch = next((urljoin(url, x["href"]) for x in page.links if x["type"] == OPENSEARCH_TYPE), None)
        if opensearch:
            template = self._opensearch_template(opensearch)
            if template:
                discovered["opensearch_template"] = template
        has_search = bool(self.entry.get("search") or discovered.get("opensearch_template"))
        checks.append({"check": "search", "ok": has_search,
                       "detail": self.entry.get("search") or discovered.get("opensearch_template")
                       or "no search endpoint (add one with --search '...{q}')"})
        if page.meta.get("og:title") or page.title.strip():
            caps.add(METADATA)
        checks.append({"check": "metadata", "ok": METADATA in caps,
                       "detail": "OpenGraph/title present" if METADATA in caps else "no page metadata found"})
        if self.entry.get("watch_url"):
            caps.add(UPDATE_TRACKING)
        if self.entry.get("download_permitted"):
            caps.add(AUTOMATIC_ACQUISITION)
        checks.append({"check": "download permission", "ok": True,
                       "detail": "automatic downloads allowed by your registry entry"
                       if self.entry.get("download_permitted")
                       else "manual only (no automatic downloads from this source)"})
        if discovered:
            self.entry["discovered"] = {**(self.entry.get("discovered") or {}), **discovered}
        ordered = [c for c in (DISCOVERY_ONLY, METADATA, UPDATE_TRACKING, MANUAL_ACQUISITION,
                               AUTOMATIC_ACQUISITION) if c in caps]
        self.entry["capabilities"] = ordered
        return self._result(ok=True, checks=checks, capabilities=ordered)

    def _opensearch_template(self, url: str) -> str | None:
        try:
            _status, body = self._get(url, ttl_days=7)
            root = ET.fromstring(body)
        except Exception:
            return None
        for node in root.iter():
            if node.tag.split("}")[-1] == "Url" and "html" in (node.attrib.get("type") or ""):
                template = node.attrib.get("template") or ""
                if "{searchTerms}" in template:
                    return template.replace("{searchTerms}", "{q}")
        return None


def _anchors(text: str) -> list[tuple[str, str]]:
    page = _Page()
    try:
        page.feed(text)
    except Exception:
        return []
    return page.anchors
