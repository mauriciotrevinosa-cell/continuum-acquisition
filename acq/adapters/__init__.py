"""Adapter selection. The registry says WHAT a source is; this says HOW.

    detect_kind(url) -> "web" | "local-folder"
    build(entry)     -> an Adapter instance for that registry entry

No site names live here. A new kind of source is a new module in this
package plus one line in KINDS; a new SITE is just a registry entry the user
adds with `sources add <url>`.
"""
from __future__ import annotations

import re

from .base import (AUTOMATIC_ACQUISITION, CAPABILITIES, DISCOVERY_ONLY, MANUAL_ACQUISITION, METADATA,
                   OPERATIONS, REQUIRES, UPDATE_TRACKING, Adapter, FileRef, Hit, NotSupported, Policy,
                   RobotsGate, SourceRefused, UpdateStatus)
from .bibliographic import BibliographicAdapter
from .localfolder import LocalFolderAdapter, folder_of
from .web import WebAdapter

__all__ = ["AUTOMATIC_ACQUISITION", "CAPABILITIES", "DISCOVERY_ONLY", "KINDS", "MANUAL_ACQUISITION",
           "METADATA", "OPERATIONS", "REQUIRES", "UPDATE_TRACKING", "Adapter", "BibliographicAdapter",
           "FileRef", "Hit", "LocalFolderAdapter", "NotSupported", "Policy", "RobotsGate", "SourceRefused",
           "UpdateStatus", "WebAdapter", "build", "detect_kind", "folder_of", "for_registry", "policy_from"]

KINDS: dict[str, type[Adapter]] = {
    "web": WebAdapter,
    "local-folder": LocalFolderAdapter,
    "bibliographic": BibliographicAdapter,
}

_WINDOWS_PATH = re.compile(r"^[a-zA-Z]:[\\/]")


def detect_kind(url: str) -> str:
    """Guess the adapter kind from the location the user gave us."""
    value = (url or "").strip()
    if value.lower().startswith("file:") or _WINDOWS_PATH.match(value) or value.startswith(("\\\\", "/")):
        return "local-folder"
    return "web"


def build(entry: dict, *, http=None, policy: Policy | None = None, providers: dict | None = None) -> Adapter:
    kind = entry.get("adapter") or detect_kind(entry.get("url") or "")
    cls = KINDS.get(kind, WebAdapter)
    return cls(entry, http=http, policy=policy, providers=providers)


def policy_from(registry: dict, *, vault_root: str | None = None, intake_root: str | None = None) -> Policy:
    return Policy(unofficial_hosts=tuple(registry.get("unofficial_hosts") or ()),
                  vault_root=vault_root, intake_root=intake_root)


def for_registry(registry: dict, *, http=None, policy: Policy | None = None, providers: dict | None = None,
                 enabled_only: bool = True, ids=None, capability: str | None = None) -> list[Adapter]:
    """Every registered source as an adapter, newest registration last."""
    wanted = {i.lower() for i in ids} if ids else None
    out: list[Adapter] = []
    for key, entry in (registry.get("sources") or {}).items():
        entry.setdefault("id", key)
        if wanted is not None and key.lower() not in wanted:
            continue
        if enabled_only and not entry.get("enabled", True):
            continue
        adapter = build(entry, http=http, policy=policy, providers=providers)
        if capability and capability not in adapter.capabilities():
            continue
        out.append(adapter)
    return out
