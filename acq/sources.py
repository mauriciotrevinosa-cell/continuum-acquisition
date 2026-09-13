"""Source registry: the sites and folders the user told Continuum to use.

The registry is PERSONAL DATA (``sources.json`` in the data directory). No
site is hardcoded in this module: a starter list of public, legal channels
ships as DATA in ``seed/default-sources.json`` and is copied into the
registry only when that registry does not exist yet. Delete it, edit it, or
start empty - the engine behaves the same.

Each entry is one source:

    id, name, url, adapter, enabled, capabilities[], access[], roles[],
    hosts[], search ("...{q}"), watch_url, languages[], media[],
    download_permitted, notes, origin, added_at, last_test{}

``download_permitted`` is the single switch that allows automatic
downloading, and it is meant only for DRM-free material the user is entitled
to. Hosts in ``unofficial_hosts`` are never used as a source, whatever an
entry or a link says.
"""
from __future__ import annotations

import copy
import os
from urllib.parse import quote, urlparse

from . import adapters as adapters_mod
from .util import LOG, norm, now_iso, read_json, slug, write_json, backup

REGISTRY_SCHEMA = "continuum.personal.sources/2"
SEED_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "seed",
                         "default-sources.json")

ACCESS_ORDER = ["DIRECT_DOWNLOAD_AUTHORIZED", "DRM_FREE_PURCHASE", "DRM_EBOOK", "FREE_OFFICIAL_WEB", "PAID_WEB",
                "SUBSCRIPTION_WEB", "LIBRARY_LENDING", "STREAMING", "PHYSICAL_ONLY", "UNKNOWN"]
ACCESS_TEXT = {
    "DIRECT_DOWNLOAD_AUTHORIZED": "authorized direct download",
    "DRM_FREE_PURCHASE": "DRM-free purchase (download after buying, with your account)",
    "DRM_EBOOK": "ebook purchase (DRM; read in the store's app)",
    "FREE_OFFICIAL_WEB": "free official web/app reader (no file)",
    "PAID_WEB": "pay-per-chapter official web/app reader (no file)",
    "SUBSCRIPTION_WEB": "subscription web/app reader (no file)",
    "LIBRARY_LENDING": "public-library lending (your library card)",
    "STREAMING": "official streaming",
    "PHYSICAL_ONLY": "physical copy only",
    "UNKNOWN": "access model not verified",
}

#: Material a source can carry. Classes, not titles.
MEDIA_DEFAULT = ("manga", "manhwa", "manhua", "light-novel", "web-novel", "anime", "guidebook", "art-book",
                 "anthology", "fanbook", "colored-edition", "visual-reference", "special", "other-official",
                 "official-doujin")

#: Roles let the engine ask for "a store to search" without naming a store.
ROLE_STORE_EN = "store-search-en"
ROLE_STORE_JA = "store-search-ja"
ROLE_ANIME = "anime-streaming"


class RefusedSource(ValueError):
    """The URL points at a host the user listed as unofficial."""


def host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


# ---------------------------------------------------------------------------
# registry lifecycle
# ---------------------------------------------------------------------------
def _blank() -> dict:
    return {"schema": REGISTRY_SCHEMA, "sources": {}, "unofficial_hosts": [], "publisher_routing": {}}


def load_seed(seed_path: str | None = None) -> dict:
    return read_json(seed_path or SEED_PATH) or {}


def _migrate_entry(key: str, entry: dict) -> bool:
    """Bring a v1 entry up to v2 without touching what the user set."""
    changed = False
    for field, value in (("id", key), ("enabled", True), ("roles", []), ("origin", "user"),
                         ("languages", ["en"]), ("media", list(MEDIA_DEFAULT)), ("notes", "")):
        if field not in entry:
            entry[field] = copy.deepcopy(value)
            changed = True
    if not entry.get("adapter"):
        entry["adapter"] = adapters_mod.detect_kind(entry.get("url") or "")
        changed = True
    if not entry.get("hosts"):
        entry["hosts"] = [host_of(entry.get("url") or "")] if entry.get("url") else []
        changed = True
    if not entry.get("capabilities"):
        cls = adapters_mod.KINDS.get(entry["adapter"], adapters_mod.WebAdapter)
        caps = [c for c in cls.declared_capabilities
                if c != adapters_mod.AUTOMATIC_ACQUISITION or entry.get("download_permitted")]
        entry["capabilities"] = caps or [adapters_mod.DISCOVERY_ONLY]
        changed = True
    return changed


def load_registry(data_dir: str, legacy: dict | None = None, *, seed: bool = True,
                  seed_path: str | None = None) -> tuple[dict, bool]:
    """Load (and migrate) the registry. Returns (registry, changed).

    The seed list is copied in only when the registry file does not exist, so
    a source the user removed stays removed.
    """
    path = os.path.join(data_dir, "sources.json")
    existing = read_json(path)
    changed = existing is None
    data = existing or _blank()
    data.setdefault("sources", {})
    data.setdefault("unofficial_hosts", [])
    data.setdefault("publisher_routing", {})

    seeded = load_seed(seed_path) if seed else {}
    if existing is None and seeded:
        for key, entry in (seeded.get("sources") or {}).items():
            data["sources"][key] = copy.deepcopy(entry)
        if seeded.get("publisher_routing"):
            data["publisher_routing"] = copy.deepcopy(seeded["publisher_routing"])
        data["seeded_at"] = now_iso()
        data["seed_note"] = ("Starter list copied from seed/default-sources.json. Edit or remove freely; "
                             "it is never re-applied.")
    elif seeded and not data.get("seed_fields_applied"):
        # A registry written before roles/publisher routing existed: fill the
        # gaps ONCE, so upgrading does not silently lose store fallbacks.
        # Recorded, so later edits (including emptying a field) are respected.
        for key, entry in data["sources"].items():
            source_seed = (seeded.get("sources") or {}).get(key) or {}
            if source_seed.get("roles") and not entry.get("roles"):
                entry["roles"] = copy.deepcopy(source_seed["roles"])
                changed = True
        if not data.get("publisher_routing") and seeded.get("publisher_routing"):
            data["publisher_routing"] = copy.deepcopy(seeded["publisher_routing"])
            changed = True
        data["seed_fields_applied"] = now_iso()

    for key, entry in (legacy or {}).items():  # v1 catalog 'sources' block (name/url/delivery)
        if key not in data["sources"]:
            data["sources"][key] = {"name": entry.get("name", key), "url": entry.get("url"),
                                    "access": ["UNKNOWN"], "download_permitted": False,
                                    "notes": entry.get("delivery", ""), "origin": "catalog-v1"}
            changed = True

    for key, entry in data["sources"].items():
        changed |= _migrate_entry(key, entry)
    if data.get("schema") != REGISTRY_SCHEMA:
        data["schema"] = REGISTRY_SCHEMA
        changed = True
    return data, changed


def save_registry(data_dir: str, reg: dict) -> None:
    """Atomic write plus a timestamped backup: a crash or a concurrent run
    can never leave a half-written registry behind."""
    path = os.path.join(data_dir, "sources.json")
    backup(path)
    reg["schema"] = REGISTRY_SCHEMA
    reg["updated_at"] = now_iso()
    write_json(path, reg)


# ---------------------------------------------------------------------------
# queries
# ---------------------------------------------------------------------------
def is_unofficial(url_or_host: str, reg: dict) -> bool:
    h = host_of(url_or_host) if "://" in url_or_host else url_or_host.lower().removeprefix("www.")
    listed = (x.lower().removeprefix("www.") for x in reg.get("unofficial_hosts", []))
    return any(h == u or h.endswith("." + u) for u in listed)


def source_for_host(host: str, reg: dict) -> str | None:
    for key, s in (reg.get("sources") or {}).items():
        for hh in s.get("hosts") or []:
            hh = (hh or "").lower().removeprefix("www.")
            if hh and (host == hh or host.endswith("." + hh)):
                return key
    return None


def get_source(reg: dict, source_id: str) -> dict | None:
    sources = reg.get("sources") or {}
    if source_id in sources:
        return sources[source_id]
    wanted = norm(source_id)
    for key, entry in sources.items():
        if norm(key) == wanted or norm(entry.get("name")) == wanted:
            return entry
    return None


def by_role(reg: dict, role: str, *, limit: int | None = None, enabled_only: bool = True) -> list[str]:
    out = [key for key, e in (reg.get("sources") or {}).items()
           if role in (e.get("roles") or []) and (e.get("enabled", True) or not enabled_only)]
    return out[:limit] if limit else out


# ---------------------------------------------------------------------------
# mutations
# ---------------------------------------------------------------------------
def add_unofficial_host(reg: dict, host: str) -> bool:
    h = host_of(host) if "://" in host else host.lower().strip().removeprefix("www.")
    if h and h not in reg.setdefault("unofficial_hosts", []):
        reg["unofficial_hosts"].append(h)
        return True
    return False


def suggest_id(url: str, reg: dict) -> str:
    """A stable, readable id from the location, unique in this registry."""
    if adapters_mod.detect_kind(url) == "local-folder":
        base = os.path.basename(os.path.normpath(adapters_mod.folder_of(url))) or "local-folder"
    else:
        host = host_of(url) or "source"
        parts = [p for p in host.split(".") if p not in ("com", "net", "org", "co", "jp", "io", "www")]
        base = parts[0] if parts else host
    candidate = slug(base) or "source"
    existing = set(reg.get("sources") or {})
    if candidate not in existing:
        return candidate
    n = 2
    while f"{candidate}-{n}" in existing:
        n += 1
    return f"{candidate}-{n}"


def add_source_url(reg: dict, url: str, *, source_id: str | None = None, name: str | None = None,
                   adapter: str | None = None, access: list[str] | None = None, languages=("en",),
                   media=None, search: str | None = None, watch_url: str | None = None,
                   download_permitted: bool = False, notes: str = "", roles=None,
                   replace: bool = False) -> dict:
    """Register a source from a URL or a local folder path."""
    url = (url or "").strip()
    if not url:
        raise ValueError("a source needs a URL or a folder path")
    kind = adapter or adapters_mod.detect_kind(url)
    if kind not in adapters_mod.KINDS:
        raise ValueError(f"unknown adapter {kind!r}; use one of {sorted(adapters_mod.KINDS)}")
    if kind == "web":
        if not url.lower().startswith(("http://", "https://")):
            url = "https://" + url.lstrip("/")
        if is_unofficial(url, reg):
            raise RefusedSource(f"{host_of(url)} is on your unofficial list; it cannot be registered as a source")
    unknown = [a for a in (access or []) if a not in ACCESS_ORDER]
    if unknown:
        raise ValueError(f"unknown access model(s) {unknown}; use {ACCESS_ORDER}")
    if search and "{q}" not in search:
        raise ValueError("a search template must contain {q}, e.g. https://example.org/search?q={q}")

    key = source_id or suggest_id(url, reg)
    if key in (reg.get("sources") or {}) and not replace:
        raise ValueError(f"source id {key!r} already exists; pass a different --id or --replace")
    existing_host = source_for_host(host_of(url), reg) if kind == "web" else None
    cls = adapters_mod.KINDS[kind]
    caps = [c for c in cls.declared_capabilities
            if c != adapters_mod.AUTOMATIC_ACQUISITION or download_permitted or kind == "local-folder"]
    entry = {
        "id": key,
        "name": name or (host_of(url) if kind == "web" else os.path.basename(os.path.normpath(
            adapters_mod.folder_of(url))) or key),
        "url": url,
        "adapter": kind,
        "enabled": True,
        "capabilities": caps,
        "access": list(access or ["UNKNOWN"]),
        "roles": list(roles or []),
        "hosts": [host_of(url)] if kind == "web" else [],
        "search": search,
        "watch_url": watch_url,
        "languages": list(languages),
        "media": list(media or MEDIA_DEFAULT),
        "download_permitted": bool(download_permitted) if kind != "local-folder" else True,
        "notes": notes,
        "origin": "user",
        "added_at": now_iso(),
        "last_test": None,
    }
    if existing_host and existing_host != key:
        entry["notes"] = (entry["notes"] + f" (another source already covers this host: {existing_host})").strip()
    reg.setdefault("sources", {})[key] = entry
    return entry


def remove_source(reg: dict, source_id: str) -> dict | None:
    sources = reg.get("sources") or {}
    entry = get_source(reg, source_id)
    if entry is None:
        return None
    key = entry.get("id") or source_id
    return sources.pop(key, None) or sources.pop(source_id, None)


def set_enabled(reg: dict, source_id: str, enabled: bool) -> dict | None:
    entry = get_source(reg, source_id)
    if entry is not None:
        entry["enabled"] = bool(enabled)
    return entry


def record_test(entry: dict, result: dict) -> dict:
    """Store a self-test result on the entry (and adopt what it discovered)."""
    entry["last_test"] = result
    if result.get("ok") and result.get("capabilities"):
        entry["capabilities"] = list(result["capabilities"])
    return entry


def add_source(reg: dict, key: str, name: str, url: str, access: list[str], *, languages=("en",),
               download_permitted: bool = False, notes: str = "") -> dict:
    """Backwards-compatible wrapper for the original add-source CLI."""
    return add_source_url(reg, url, source_id=key, name=name, access=access, languages=languages,
                          download_permitted=download_permitted, notes=notes, replace=True)


def add_link(reg: dict, w: dict, url: str, *, note: str = "", direct_file: bool = False) -> dict:
    """Attach a user-provided official link to a work."""
    if not url.lower().startswith(("https://", "http://")):
        raise ValueError("a link must start with https:// or http://")
    if is_unofficial(url, reg):
        raise RefusedSource(f"{host_of(url)} is listed as unofficial; links there are not used as sources")
    host = host_of(url)
    key = source_for_host(host, reg)
    link = {"url": url, "host": host, "source": key or "user-link", "note": note,
            "direct_file": bool(direct_file), "added_at": now_iso()}
    links = w.setdefault("links", [])
    if not any(x.get("url") == url for x in links):
        links.append(link)
    return link


# ---------------------------------------------------------------------------
# per-work candidates
# ---------------------------------------------------------------------------
def _search_url(entry: dict, query: str | None) -> str | None:
    if entry.get("search") and query:
        return entry["search"].replace("{q}", quote(query))
    return entry.get("url")


def _route(name: str, table: dict):
    n = norm(name)
    for key, value in (table or {}).items():
        if norm(key) and norm(key) in n:
            return value
    return None


def candidates(fam: dict, w: dict, reg: dict) -> list[dict]:
    """Every legal channel known for this work, best first.

    Sources are looked up by ROLE and by publisher routing held in the
    registry, so no site name appears in this module.
    """
    titles = w.get("titles") or {}
    en_q = titles.get("en") or w.get("work")
    ja_q = titles.get("ja") or en_q
    medium = w.get("material_class") or "manga"
    remote = w.get("remote") or {}
    routing = reg.get("publisher_routing") or {}
    out: dict[str, dict] = {}

    def add(key: str, why: str, *, url: str | None = None, language: str | None = None):
        s = (reg.get("sources") or {}).get(key)
        if s is None or not s.get("enabled", True) or is_unofficial(s.get("url") or "", reg):
            return
        lang = language or (s.get("languages") or ["en"])[0]
        query = ja_q if lang == "ja" else en_q
        if key in out:
            out[key]["evidence"].append(why)
            return
        out[key] = {"source": key, "name": s.get("name") or key, "access": s.get("access") or ["UNKNOWN"],
                    "download_permitted": bool(s.get("download_permitted")),
                    "capabilities": s.get("capabilities") or [], "adapter": s.get("adapter") or "web",
                    "url": url or _search_url(s, query), "language": lang, "evidence": [why],
                    "download_url": None}

    for c in w.get("candidate_sources") or []:
        add(c["source"], f"curated candidate ({c.get('confidence')})")
    for link in w.get("links") or []:
        if is_unofficial(link["url"], reg):
            continue
        key = link.get("source")
        entry = (reg.get("sources") or {}).get(key) if key else None
        if entry is not None:
            add(key, "your link", url=link["url"])
            if link.get("direct_file") and entry.get("download_permitted") and key in out:
                out[key]["download_url"] = link["url"]
        else:
            out.setdefault(f"link:{link['host']}", {
                "source": f"link:{link['host']}", "name": link["host"], "access": ["UNKNOWN"],
                "download_permitted": False, "capabilities": [], "adapter": "web", "url": link["url"],
                "language": None, "download_url": None,
                "evidence": ["your link (host not registered: `sources add` it to classify its access model)"]})

    en_found = False
    for p in remote.get("publishers") or []:
        ptype = (p.get("type") or "").lower()
        if ptype == "english":
            en_found = True
            key = _route(p.get("name") or "", routing.get("english") or {})
            if key:
                add(key, f"English publisher on record: {p['name']} {p.get('notes') or ''}".strip())
            else:
                store = by_role(reg, ROLE_STORE_EN, limit=1)
                out.setdefault(f"publisher:{p['name']}", {
                    "source": f"publisher:{p['name']}", "name": p["name"], "access": ["DRM_EBOOK"],
                    "download_permitted": False, "capabilities": [], "adapter": "web",
                    "url": _search_url((reg["sources"] or {}).get(store[0], {}), en_q) if store else None,
                    "language": "en", "download_url": None,
                    "evidence": [f"English publisher {p['name']} is not in your registry; search a store for it"]})
        elif ptype == "original":
            for key in _route(p.get("name") or "", routing.get("original") or {}) or []:
                add(key, f"original publisher {p['name']}", language="ja")

    ndl = remote.get("ndl") or {}
    if ndl.get("isbns"):
        for key in by_role(reg, ROLE_STORE_JA, limit=2):
            add(key, f"published in Japan ({len(ndl['isbns'])} ISBN(s) on record)", language="ja")
    if medium == "anime":
        for key in by_role(reg, ROLE_ANIME, limit=3):
            add(key, "anime: check official streaming")
    elif en_found or any(c.get("language") == "en" for c in out.values()):
        for key in by_role(reg, ROLE_STORE_EN, limit=3):
            add(key, "store search for the English edition")
    else:
        for key in by_role(reg, ROLE_STORE_JA, limit=2):
            add(key, "store search for the Japanese edition", language="ja")

    def rank(c):
        best = min((ACCESS_ORDER.index(a) for a in c["access"] if a in ACCESS_ORDER), default=len(ACCESS_ORDER))
        return (0 if c.get("download_url") else 1,
                0 if c["language"] == "en" else 1 if c["language"] == "ja" else 2,
                0 if any("curated" in e or "your link" in e or "publisher" in e for e in c["evidence"]) else 1,
                best)

    return sorted(out.values(), key=rank)


def availability(cands: list[dict]) -> str:
    if any(c["download_permitted"] and c.get("download_url") for c in cands):
        return "AVAILABLE_AUTHORIZED"
    if cands:
        return "AVAILABLE_MANUAL"
    return "NO_OFFICIAL_SOURCE_FOUND"
