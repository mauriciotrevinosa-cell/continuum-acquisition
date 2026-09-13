"""Adapter contract: one interface, many kinds of source.

A SOURCE is something the user registered (a website, a bibliographic API, a
folder of files they own). An ADAPTER is the code that knows how to talk to
that kind of source. Nothing in this package knows any particular site: the
site lives in the registry (personal data), the behaviour lives here.

    CAPABILITIES declare what a source may be used for
    OPERATIONS   are what an adapter can be asked to do

An operation is only callable when the source carries the capability it
needs, so "this source can be searched" and "this source may be downloaded
from" stay separate, explicit facts rather than an accident of code paths.

Policy, never bypassed:
  * a host on the user's unofficial list is refused outright;
  * robots.txt is honoured - a disallowed path is refused, never fetched;
  * DRM, paywalls, logins and anti-bot measures are never circumvented;
  * an automatic download requires the source to be explicitly marked
    download_permitted (DRM-free material the user is entitled to);
  * downloads land in the intake, never in the Vault.
"""
from __future__ import annotations

import urllib.robotparser
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from ..util import LOG, norm, now_iso

DISCOVERY_ONLY = "DISCOVERY_ONLY"
METADATA = "METADATA"
UPDATE_TRACKING = "UPDATE_TRACKING"
MANUAL_ACQUISITION = "MANUAL_ACQUISITION"
AUTOMATIC_ACQUISITION = "AUTOMATIC_ACQUISITION"
CAPABILITIES = (DISCOVERY_ONLY, METADATA, UPDATE_TRACKING, MANUAL_ACQUISITION, AUTOMATIC_ACQUISITION)

OPERATIONS = ("search", "get_work_metadata", "get_releases", "get_chapters", "get_latest",
              "get_available_files", "acquire", "get_update_status")

#: What each operation requires the source to be capable of. An adapter that
#: implements an operation still cannot run it without the capability.
REQUIRES = {
    "search": (DISCOVERY_ONLY, METADATA),
    "get_work_metadata": (METADATA, DISCOVERY_ONLY),
    "get_releases": (METADATA,),
    "get_chapters": (METADATA,),
    "get_latest": (METADATA, UPDATE_TRACKING),
    "get_available_files": (MANUAL_ACQUISITION, AUTOMATIC_ACQUISITION),
    "acquire": (AUTOMATIC_ACQUISITION,),
    "get_update_status": (UPDATE_TRACKING,),
}


class NotSupported(RuntimeError):
    """This adapter cannot perform that operation for this source."""


class SourceRefused(RuntimeError):
    """Policy refuses the request (unofficial host, robots.txt, no permission)."""


@dataclass(frozen=True, slots=True)
class Hit:
    """One search result: enough to show the user and to follow up."""

    title: str
    url: str
    kind: str = "work"
    score: float = 0.0
    detail: str = ""


@dataclass(frozen=True, slots=True)
class FileRef:
    """One acquirable file. `direct` means it can be fetched without a login."""

    name: str
    location: str
    bytes: int | None = None
    kind: str = "file"
    direct: bool = False
    note: str = ""


@dataclass(frozen=True, slots=True)
class UpdateStatus:
    """Result of an update check: a fingerprint plus what changed."""

    fingerprint: str
    changed: bool
    latest: str | None = None
    items: tuple = ()
    detail: str = ""
    checked_at: str = field(default_factory=now_iso)


@dataclass(frozen=True, slots=True)
class Policy:
    """Hard limits applied to every adapter, from personal settings."""

    unofficial_hosts: tuple[str, ...] = ()
    vault_root: str | None = None
    intake_root: str | None = None

    def host_refused(self, url: str) -> str | None:
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
        for bad in (h.lower().removeprefix("www.") for h in self.unofficial_hosts):
            if host and bad and (host == bad or host.endswith("." + bad)):
                return (f"{host} is on your unofficial-source list: it is never used as a source. "
                        f"Remove it from unofficial_hosts in sources.json if that is wrong.")
        return None


class Adapter:
    """Base class. Every operation refuses by default; subclasses opt in."""

    kind = "base"
    #: What this KIND of source can do at best. The registry entry may narrow
    #: it (an AUTOMATIC capability also needs download_permitted).
    declared_capabilities: tuple[str, ...] = ()
    #: Operations this subclass actually implements.
    implements: tuple[str, ...] = ()

    def __init__(self, entry: dict, *, http=None, policy: Policy | None = None,
                 providers: dict | None = None):
        self.entry = entry
        self.http = http
        self.policy = policy or Policy()
        self.providers = providers or {}

    # -- identity -----------------------------------------------------------
    @property
    def id(self) -> str:
        return self.entry.get("id") or self.entry.get("key") or norm(self.entry.get("name") or "source")

    @property
    def name(self) -> str:
        return self.entry.get("name") or self.id

    @property
    def url(self) -> str:
        return self.entry.get("url") or ""

    @property
    def enabled(self) -> bool:
        return bool(self.entry.get("enabled", True))

    def capabilities(self) -> list[str]:
        """Declared capabilities, narrowed by what this entry is allowed."""
        stored = self.entry.get("capabilities")
        caps = [c for c in (stored or self.declared_capabilities) if c in CAPABILITIES]
        if AUTOMATIC_ACQUISITION in caps and not self.entry.get("download_permitted"):
            caps = [c for c in caps if c != AUTOMATIC_ACQUISITION]
            if MANUAL_ACQUISITION not in caps:
                caps.append(MANUAL_ACQUISITION)
        if not caps:
            caps = [DISCOVERY_ONLY]
        return sorted(set(caps), key=CAPABILITIES.index)

    def supports(self, operation: str) -> bool:
        if operation not in self.implements:
            return False
        return bool(set(self.capabilities()) & set(REQUIRES.get(operation, ())))

    def require(self, operation: str) -> None:
        if operation not in self.implements:
            raise NotSupported(f"{self.kind} adapter does not implement {operation}()")
        if not self.supports(operation):
            need = " or ".join(REQUIRES.get(operation, ()))
            raise SourceRefused(f"source '{self.id}' lacks the capability for {operation}() (needs {need}; "
                                f"it has {', '.join(self.capabilities())})")

    # -- operations (every one refuses unless a subclass overrides) ----------
    def search(self, query: str, **kw) -> list[Hit]:
        raise NotSupported(f"{self.kind}: search")

    def get_work_metadata(self, ref: str, **kw) -> dict:
        raise NotSupported(f"{self.kind}: get_work_metadata")

    def get_releases(self, ref: str, **kw) -> list[dict]:
        raise NotSupported(f"{self.kind}: get_releases")

    def get_chapters(self, ref: str, **kw) -> list[dict]:
        raise NotSupported(f"{self.kind}: get_chapters")

    def get_latest(self, ref: str, **kw) -> dict:
        raise NotSupported(f"{self.kind}: get_latest")

    def get_available_files(self, ref: str, **kw) -> list[FileRef]:
        raise NotSupported(f"{self.kind}: get_available_files")

    def acquire(self, file_ref: FileRef, dest_dir: str, **kw) -> dict:
        raise NotSupported(f"{self.kind}: acquire")

    def get_update_status(self, state: dict | None = None, **kw) -> UpdateStatus:
        raise NotSupported(f"{self.kind}: get_update_status")

    # -- diagnostics --------------------------------------------------------
    def self_test(self) -> dict:
        """Probe the source and report what it can actually do right now."""
        return self._result(ok=False, checks=[{"check": "implemented", "ok": False,
                                               "detail": f"{self.kind} adapter has no self-test"}])

    def _result(self, *, ok: bool, checks: list[dict], capabilities: list[str] | None = None,
                error: str | None = None) -> dict:
        return {"at": now_iso(), "source": self.id, "adapter": self.kind, "ok": ok, "checks": checks,
                "capabilities": capabilities if capabilities is not None else self.capabilities(),
                "operations": sorted(op for op in OPERATIONS if self.supports(op)), "error": error}


class RobotsGate:
    """robots.txt for one origin, fetched once and cached in memory.

    A disallowed path is REFUSED, never fetched. When robots.txt cannot be
    read at all there is no rule to honour, so the request proceeds - that is
    what the standard says an absent robots.txt means.
    """

    def __init__(self, http, user_agent: str):
        self.http = http
        self.user_agent = user_agent
        self._cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def _parser(self, url: str):
        parts = urlparse(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin in self._cache:
            return self._cache[origin]
        parser = None
        try:
            status, body = self.http.request("GET", urljoin(origin, "/robots.txt"), ttl_days=7)
            if status == 200 and body:
                parser = urllib.robotparser.RobotFileParser()
                parser.parse(body.decode("utf-8", "replace").splitlines())
        except Exception as err:  # offline, refused, malformed: no rule to honour
            LOG.debug("robots.txt unavailable for %s: %s", origin, err)
        self._cache[origin] = parser
        return parser

    def allows(self, url: str) -> bool:
        parser = self._parser(url)
        return True if parser is None else bool(parser.can_fetch(self.user_agent, url))

    def check(self, url: str) -> None:
        if not self.allows(url):
            raise SourceRefused(f"robots.txt disallows {url} for {self.user_agent}; not fetched")
