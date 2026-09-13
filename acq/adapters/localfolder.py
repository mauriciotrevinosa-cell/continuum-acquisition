"""Adapter for a local folder the user already owns.

This is the legal automatic-acquisition path: files the user bought, ripped
from their own discs, exported from a DRM-free store, or copied from another
drive. The adapter reads that folder and COPIES into the intake, where the
normal identify / hash / duplicate-check / import flow takes over.

It never writes to, renames or deletes anything in the source folder, and it
refuses a source folder that is inside the Vault (the Vault is preservation,
not a source to re-import from).
"""
from __future__ import annotations

import os
import uuid
from urllib.parse import unquote, urlparse

from ..util import LOG, norm, safe_folder_name, sha256_file, similarity, tokens
from ..vault_scan import KIND, MEDIA_KINDS
from .base import (AUTOMATIC_ACQUISITION, DISCOVERY_ONLY, MANUAL_ACQUISITION, METADATA, UPDATE_TRACKING,
                   Adapter, FileRef, Hit, SourceRefused, UpdateStatus)

MAX_ENTRIES = 50_000
TEMP_PREFIX = ".continuum-import-"
INCOMPLETE = (".crdownload", ".part", ".partial", ".tmp", ".download")


def folder_of(url: str) -> str:
    """Accept file:///C:/x, file://C:/x or a plain path."""
    if url.lower().startswith("file:"):
        parts = urlparse(url)
        path = unquote(parts.path or "")
        if parts.netloc and len(parts.netloc) <= 2 and parts.netloc.endswith(":"):
            path = f"{parts.netloc}{path}"  # file://C:/x
        path = path.lstrip("/") if len(path) > 2 and path[2] == ":" else path
        return os.path.normpath(path)
    return os.path.normpath(url)


class LocalFolderAdapter(Adapter):
    kind = "local-folder"
    declared_capabilities = (DISCOVERY_ONLY, METADATA, UPDATE_TRACKING, MANUAL_ACQUISITION,
                             AUTOMATIC_ACQUISITION)
    implements = ("search", "get_work_metadata", "get_releases", "get_chapters", "get_latest",
                  "get_available_files", "acquire", "get_update_status")

    @property
    def root(self) -> str:
        return folder_of(self.url)

    def capabilities(self) -> list[str]:
        """A folder the user owns is acquirable by default: the permission
        question ("may this be downloaded?") does not arise for their own
        disk, so download_permitted is implied unless they turned it off."""
        if self.entry.get("download_permitted") is None:
            self.entry["download_permitted"] = True
        return super().capabilities()

    # -- plumbing -----------------------------------------------------------
    def _guard(self) -> str:
        root = self.root
        if not root or not os.path.isdir(root):
            raise SourceRefused(f"folder not found: {root}")
        if self.policy.vault_root and os.path.normcase(os.path.abspath(root)).startswith(
                os.path.normcase(os.path.abspath(self.policy.vault_root))):
            raise SourceRefused("this folder is inside the Vault; the Vault is preservation, not a source")
        return root

    def _walk(self, limit: int = MAX_ENTRIES):
        root = self._guard()
        count = 0
        for base, dirs, files in os.walk(root):
            dirs.sort()
            for name in sorted(files):
                if name.startswith(TEMP_PREFIX) or name.lower().endswith(INCOMPLETE):
                    continue
                count += 1
                if count > limit:
                    LOG.warning("source '%s': stopped listing at %d files", self.id, limit)
                    return
                yield os.path.join(base, name)

    def _describe(self, path: str) -> FileRef:
        ext = os.path.splitext(path)[1].lower()
        try:
            size = os.path.getsize(path)
        except OSError:
            size = None
        return FileRef(name=os.path.basename(path), location=path, bytes=size,
                       kind=KIND.get(ext, "other"), direct=True,
                       note=os.path.relpath(os.path.dirname(path), self.root))

    # -- operations ---------------------------------------------------------
    def search(self, query: str, limit: int = 25, **kw) -> list[Hit]:
        self.require("search")
        wanted = tokens(query)
        scored: list[tuple[float, str]] = []
        seen_dirs: dict[str, float] = {}
        for path in self._walk():
            rel = os.path.relpath(path, self.root)
            label = f"{os.path.basename(os.path.dirname(path))} {os.path.basename(path)}"
            overlap = len(wanted & tokens(label)) / max(1, len(wanted))
            score = max(similarity(query, os.path.splitext(os.path.basename(path))[0]),
                        similarity(query, os.path.basename(os.path.dirname(path))), overlap)
            if score >= 0.5:
                scored.append((score, rel))
                folder = os.path.dirname(rel)
                seen_dirs[folder] = max(seen_dirs.get(folder, 0.0), score)
        hits = [Hit(title=os.path.basename(folder) or os.path.basename(self.root),
                    url=os.path.join(self.root, folder), kind="folder", score=round(score, 3),
                    detail=f"{sum(1 for s, r in scored if os.path.dirname(r) == folder)} matching files")
                for folder, score in sorted(seen_dirs.items(), key=lambda kv: -kv[1])]
        return hits[:limit]

    def get_work_metadata(self, ref: str = "", **kw) -> dict:
        self.require("get_work_metadata")
        root = self._guard()
        target = os.path.join(root, ref) if ref else root
        if not os.path.isdir(target):
            raise SourceRefused(f"not a folder inside this source: {ref}")
        files = [self._describe(p) for p in self._walk() if os.path.dirname(p).startswith(target)]
        media = [f for f in files if f.kind in MEDIA_KINDS]
        return {"source": self.id, "title": os.path.basename(target), "location": target,
                "files": len(files), "media_files": len(media),
                "bytes": sum(f.bytes or 0 for f in files)}

    def get_releases(self, ref: str = "", **kw) -> list[dict]:
        self.require("get_releases")
        return [{"label": f.name, "location": f.location, "bytes": f.bytes, "kind": f.kind}
                for f in self.get_available_files(ref)]

    def get_chapters(self, ref: str = "", **kw) -> list[dict]:
        self.require("get_chapters")
        return self.get_releases(ref, **kw)

    def get_latest(self, ref: str = "", **kw) -> dict:
        self.require("get_latest")
        newest, when = None, -1.0
        for path in self._walk():
            if ref and not os.path.normcase(path).startswith(os.path.normcase(os.path.join(self.root, ref))):
                continue
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if mtime > when:
                newest, when = path, mtime
        return {"label": os.path.basename(newest), "location": newest, "modified": when} if newest else {}

    def get_available_files(self, ref: str = "", **kw) -> list[FileRef]:
        self.require("get_available_files")
        base = os.path.join(self.root, ref) if ref else self.root
        base_nc = os.path.normcase(os.path.abspath(base))
        return [self._describe(p) for p in self._walk()
                if os.path.normcase(os.path.abspath(p)).startswith(base_nc)]

    def acquire(self, file_ref: FileRef, dest_dir: str, **kw) -> dict:
        """Copy one file into the intake. Never moves, never overwrites."""
        self.require("acquire")
        self._guard()
        src = file_ref.location
        if not os.path.isfile(src):
            return {"result": "source file vanished", "path": src, "source": self.id}
        if self.policy.vault_root and os.path.normcase(os.path.abspath(dest_dir)).startswith(
                os.path.normcase(os.path.abspath(self.policy.vault_root))):
            raise SourceRefused("acquisition copies into the intake, never into the Vault")
        os.makedirs(dest_dir, exist_ok=True)
        target = os.path.join(dest_dir, safe_folder_name(file_ref.name))
        sha = sha256_file(src)
        if os.path.exists(target):
            same = os.path.isfile(target) and sha256_file(target) == sha
            return {"result": "already in intake" if same else "name taken by different bytes; left alone",
                    "path": target, "sha256": sha, "source": self.id}
        tmp = os.path.join(dest_dir, f"{TEMP_PREFIX}{uuid.uuid4().hex}.partial")
        with open(src, "rb") as fh_in, open(tmp, "wb") as fh_out:
            while True:
                chunk = fh_in.read(1 << 20)
                if not chunk:
                    break
                fh_out.write(chunk)
        if sha256_file(tmp) != sha:
            os.remove(tmp)
            return {"result": "copy verification failed", "path": target, "source": self.id}
        try:
            os.rename(tmp, target)  # refuses to replace an existing file on Windows
        except FileExistsError:
            os.remove(tmp)
            return {"result": "target appeared meanwhile; kept it", "path": target, "source": self.id}
        return {"result": "copied", "path": target, "bytes": os.path.getsize(target), "sha256": sha,
                "source": self.id, "origin": src}

    def get_update_status(self, state: dict | None = None, **kw) -> UpdateStatus:
        self.require("get_update_status")
        count = 0
        newest = 0.0
        total = 0
        latest_name = None
        for path in self._walk():
            count += 1
            try:
                stat = os.stat(path)
            except OSError:
                continue
            total += stat.st_size
            if stat.st_mtime > newest:
                newest, latest_name = stat.st_mtime, os.path.basename(path)
        fingerprint = f"{count}:{total}:{int(newest)}"
        previous = (state or {}).get("fingerprint")
        return UpdateStatus(fingerprint=fingerprint, changed=bool(previous) and previous != fingerprint,
                            latest=latest_name, detail=f"{count} files, {total / 1e9:.2f} GB")

    # -- diagnostics --------------------------------------------------------
    def self_test(self) -> dict:
        checks = []
        root = self.root
        exists = os.path.isdir(root)
        checks.append({"check": "folder exists", "ok": exists, "detail": root})
        if not exists:
            return self._result(ok=False, checks=checks, capabilities=[], error=f"folder not found: {root}")
        inside_vault = bool(self.policy.vault_root) and os.path.normcase(os.path.abspath(root)).startswith(
            os.path.normcase(os.path.abspath(self.policy.vault_root or "")))
        checks.append({"check": "outside the Vault", "ok": not inside_vault,
                       "detail": "the Vault is preservation, not a source" if inside_vault else "yes"})
        if inside_vault:
            return self._result(ok=False, checks=checks, capabilities=[], error="source folder is inside the Vault")
        files = 0
        media = 0
        size = 0
        for path in self._walk(limit=MAX_ENTRIES):
            files += 1
            ext = os.path.splitext(path)[1].lower()
            if KIND.get(ext, "other") in MEDIA_KINDS:
                media += 1
            try:
                size += os.path.getsize(path)
            except OSError:
                pass
        checks.append({"check": "readable", "ok": True,
                       "detail": f"{files} files ({media} media), {size / 1e9:.2f} GB"})
        caps = [DISCOVERY_ONLY, METADATA, UPDATE_TRACKING, MANUAL_ACQUISITION]
        if self.entry.get("download_permitted", True):
            caps.append(AUTOMATIC_ACQUISITION)
        checks.append({"check": "acquisition", "ok": True,
                       "detail": "copies into the intake (never moves or deletes your files)"})
        self.entry["capabilities"] = caps
        return self._result(ok=True, checks=checks, capabilities=caps)
