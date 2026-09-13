"""Adapter over a bibliographic database (catalogue, not a file source).

These sources answer "what exists, published by whom, how far has it got" -
they never serve media. So they carry METADATA, DISCOVERY_ONLY and
UPDATE_TRACKING, and can never carry an acquisition capability.

The providers themselves live in ``acq.providers``; this wraps them in the
adapter contract so the registry, the CLI and the UI treat every source the
same way.
"""
from __future__ import annotations

from ..http import ProviderUnavailable
from ..providers import ndl as ndl_mod
from ..util import now_iso
from .base import (DISCOVERY_ONLY, METADATA, UPDATE_TRACKING, Adapter, Hit, NotSupported, SourceRefused,
                   UpdateStatus)


class BibliographicAdapter(Adapter):
    kind = "bibliographic"
    declared_capabilities = (DISCOVERY_ONLY, METADATA, UPDATE_TRACKING)
    implements = ("search", "get_work_metadata", "get_releases", "get_latest", "get_update_status")

    @property
    def provider_id(self) -> str:
        return self.entry.get("provider") or self.entry.get("id") or ""

    @property
    def provider(self):
        found = self.providers.get(self.provider_id)
        if found is None:
            raise SourceRefused(f"provider '{self.provider_id}' is not available in this run")
        return found

    def capabilities(self) -> list[str]:
        """A catalogue can never become an acquisition source, whatever the
        registry entry says."""
        caps = [c for c in super().capabilities() if c in self.declared_capabilities]
        return caps or [METADATA]

    # -- operations ---------------------------------------------------------
    def search(self, query: str, limit: int = 10, **kw) -> list[Hit]:
        self.require("search")
        provider = self.provider
        if hasattr(provider, "search"):
            rows = provider.search(query)
            return [Hit(title=r.get("title") or "", url=str(r.get("id") or ""), kind="series",
                        score=1.0, detail=f"{r.get('type') or ''} {r.get('year') or ''}".strip())
                    for r in rows[:limit]]
        if hasattr(provider, "search_books"):
            rows = [r for r in provider.search_books(query, max_records=limit * 5) if ndl_mod.is_book(r)]
            return [Hit(title=ndl_mod.base_title(r), url=r.get("link") or "", kind="book", score=1.0,
                        detail="; ".join(r.get("publishers") or [])) for r in rows[:limit]]
        raise NotSupported(f"{self.provider_id} has no search")

    def get_work_metadata(self, ref: str, **kw) -> dict:
        self.require("get_work_metadata")
        provider = self.provider
        if not hasattr(provider, "node"):
            raise NotSupported(f"{self.provider_id} has no per-work metadata")
        node = provider.node(ref)
        return node or {}

    def get_releases(self, ref: str, **kw) -> list[dict]:
        self.require("get_releases")
        provider = self.provider
        if hasattr(provider, "node"):
            node = provider.node(ref) or {}
            return [{"label": p.get("name"), "kind": p.get("type"), "detail": p.get("notes")}
                    for p in node.get("publishers") or []]
        rows = [r for r in provider.search_books(ref) if ndl_mod.is_book(r)]
        return [{"label": r.get("title"), "kind": "book", "detail": "; ".join(r.get("publishers") or []),
                 "isbn": r.get("isbn")} for r in rows]

    def get_latest(self, ref: str, **kw) -> dict:
        self.require("get_latest")
        node = self.get_work_metadata(ref)
        return {"latest_chapter": node.get("latest_chapter"), "volumes": node.get("volumes"),
                "status": node.get("status_text"), "completed": node.get("completed"),
                "checked_at": now_iso()}

    def get_update_status(self, state: dict | None = None, ref: str | None = None, **kw) -> UpdateStatus:
        self.require("get_update_status")
        if not ref:
            raise NotSupported("this source tracks a work at a time; pass ref=<series id>")
        latest = self.get_latest(ref)
        fingerprint = f"{latest.get('latest_chapter')}:{latest.get('volumes')}:{latest.get('completed')}"
        previous = (state or {}).get("fingerprint")
        return UpdateStatus(fingerprint=fingerprint, changed=bool(previous) and previous != fingerprint,
                            latest=str(latest.get("latest_chapter") or ""), detail=latest.get("status") or "")

    # -- diagnostics --------------------------------------------------------
    def self_test(self) -> dict:
        checks = [{"check": "provider wired", "ok": self.provider_id in self.providers,
                   "detail": self.provider_id or "(none)"}]
        if self.provider_id not in self.providers:
            return self._result(ok=False, checks=checks, error=f"provider '{self.provider_id}' not available")
        probe = self.entry.get("test_query") or "test"
        try:
            hits = self.search(probe, limit=1)
            checks.append({"check": "query", "ok": True, "detail": f"{len(hits)} result(s) for {probe!r}"})
        except ProviderUnavailable as err:
            checks.append({"check": "query", "ok": False, "detail": str(err)})
            return self._result(ok=False, checks=checks, error=str(err))
        except (NotSupported, SourceRefused) as err:
            checks.append({"check": "query", "ok": False, "detail": str(err)})
            return self._result(ok=False, checks=checks, error=str(err))
        caps = list(self.declared_capabilities)
        self.entry["capabilities"] = caps
        return self._result(ok=True, checks=checks, capabilities=caps)
