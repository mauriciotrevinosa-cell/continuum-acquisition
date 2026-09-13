"""Vault inventory: read-only walk, cached SHA-256, archive directory metadata.

Vault files are only ever opened for READING. Nothing is extracted, renamed,
moved or written inside the Vault; the only output is the index in the data
directory. Hashes are cached by (path, size, mtime) so re-scans are cheap, and
the index is checkpointed while hashing so an interrupted scan resumes.
"""
from __future__ import annotations

import json
import os
import re
import time
import xml.etree.ElementTree as ET
import zipfile
from urllib.parse import urlparse

from .util import LOG, now_iso, read_json, sha256_file, write_json

INDEX_SCHEMA = "continuum.personal.vault-index/1"
SCAN_VERSION = 2
ARCHIVE_EXT = {".zip", ".cbz"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".bmp", ".jxl"}
EXECUTABLE_EXT = {".exe", ".dll", ".msi", ".bat", ".cmd", ".ps1", ".sh", ".asar", ".node", ".pak"}
KIND = {}
KIND.update({e: "archive" for e in (".zip", ".cbz", ".cbr", ".rar", ".7z", ".cb7")})
KIND.update({e: "document" for e in (".pdf", ".epub", ".mobi", ".azw", ".azw3", ".kfx", ".txt", ".docx")})
KIND.update({e: "video" for e in (".mkv", ".mp4", ".avi", ".webm", ".mov", ".m4v", ".ts")})
KIND.update({e: "image" for e in IMAGE_EXT})
KIND.update({e: "subtitle" for e in (".ass", ".ssa", ".srt", ".vtt")})
KIND.update({e: "audio" for e in (".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav")})
KIND.update({e: "executable" for e in EXECUTABLE_EXT})
MEDIA_KINDS = {"archive", "document", "video", "image", "subtitle", "audio"}

CHAPTER_DIR_RE = re.compile(r"^(?:ch|chapter|chap|c)[ _.-]?(\d+)(?:\.(\d+))?$", re.I)
VOLUME_DIR_RE = re.compile(r"^(?:vol|volume|v)[ _.-]?(\d+)$", re.I)
FILE_VOL_RE = re.compile(r"(?:^|[^a-z])(?:vol(?:ume)?\.?|v)[ _.-]?(\d{1,3})(?![0-9])", re.I)
FILE_CH_RE = re.compile(r"(?:^|[^a-z])(?:ch(?:apter)?\.?|c)[ _.-]?(\d{1,4}(?:\.\d{1,2})?)(?![0-9])", re.I)
JP_VOL_RE = re.compile(r"第?\s*(\d{1,3})\s*巻")
TEMP_PREFIX = ".continuum-import-"


def chapter_key(major: str, minor: str | None) -> str:
    return f"{int(major)}.{int(minor)}" if minor else str(int(major))


def inspect_archive(path: str) -> dict:
    """Directory-level look inside a ZIP/CBZ: chapter folders, image count and
    the first ComicInfo.xml / series.json (small, read into memory only)."""
    info = {"entries": 0, "images": 0, "executables": 0, "chapters": [], "volumes": [], "series": None,
            "language": None, "web_hosts": [], "source_url": None, "writer": None, "penciller": None,
            "remote_count": None, "json_title": None, "json_status": None, "error": None}
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            info["entries"] = len(names)
            chapters, vols = set(), set()
            for n in names:
                parts = [p for p in n.replace("\\", "/").split("/") if p]
                dirs = parts if n.endswith("/") else parts[:-1]
                ext = os.path.splitext(n)[1].lower()
                if not n.endswith("/"):
                    if ext in IMAGE_EXT:
                        info["images"] += 1
                    elif ext in EXECUTABLE_EXT:
                        info["executables"] += 1
                for d in dirs:
                    m = CHAPTER_DIR_RE.match(d)
                    if m:
                        chapters.add(chapter_key(m.group(1), m.group(2)))
                    mv = VOLUME_DIR_RE.match(d)
                    if mv:
                        vols.add(int(mv.group(1)))
            info["chapters"] = sorted(chapters, key=lambda c: tuple(int(x) for x in c.split(".")))
            info["volumes"] = sorted(vols)
            ci = next((n for n in names if n.lower().endswith("comicinfo.xml")), None)
            if ci and z.getinfo(ci).file_size < 1_000_000:
                root = ET.fromstring(z.read(ci))
                info["series"] = (root.findtext("Series") or "").strip() or None
                info["language"] = (root.findtext("LanguageISO") or "").strip() or None
                info["writer"] = (root.findtext("Writer") or "").strip() or None
                info["penciller"] = (root.findtext("Penciller") or "").strip() or None
                count = (root.findtext("Count") or "").strip()
                info["remote_count"] = int(count) if count.isdigit() else None
                web = (root.findtext("Web") or "").split()
                info["web_hosts"] = sorted({(urlparse(u).hostname or "").lower() for u in web if "://" in u} - {""})
            sj = next((n for n in names if n.rsplit("/", 1)[-1].lower() == "series.json"), None)
            if sj and z.getinfo(sj).file_size < 1_000_000:
                data = json.loads(z.read(sj).decode("utf-8", "replace"))
                if isinstance(data, dict):
                    info["json_title"] = data.get("title")
                    info["json_status"] = data.get("status")
                    src = data.get("sourceUrl") or data.get("url")
                    if isinstance(src, str) and "://" in src:
                        info["source_url"] = src
                        host = (urlparse(src).hostname or "").lower()
                        if host and host not in info["web_hosts"]:
                            info["web_hosts"].append(host)
                    if not info["language"] and isinstance(data.get("language"), str):
                        info["language"] = data["language"]
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, ET.ParseError, ValueError, RuntimeError,
            KeyError, EOFError) as err:
        info["error"] = f"{type(err).__name__}: {err}"
    return info


def name_numbers(filename: str) -> tuple[list[str], list[int]]:
    """Chapter / volume numbers stated in a file name (HaruNeko's '-part-NN'
    download chunks are NOT volumes and are ignored)."""
    stem = os.path.splitext(filename)[0]
    stem = re.sub(r"(?i)[-_ ]part[-_ ]?\d+(?:\s*\(\d+\))?$", "", stem)
    chapters = [chapter_key(*(m.group(1).split(".") + [None])[:2]) for m in FILE_CH_RE.finditer(stem)]
    vols = [int(m.group(1)) for m in FILE_VOL_RE.finditer(stem)] + [int(m.group(1)) for m in JP_VOL_RE.finditer(stem)]
    return chapters[:1], vols[:1]


def scan_vault(vault_root: str, index_path: str, *, hash_files: bool = True, checkpoint_every: int = 40) -> dict:
    prev_index = read_json(index_path) or {}
    prev = prev_index.get("files", {}) if prev_index.get("vault_root") in (None, vault_root) else {}
    files: dict[str, dict] = {}
    dirs: list[str] = []
    stats = {"files": 0, "bytes": 0, "hashed": 0, "hashed_bytes": 0, "reused": 0, "archives_inspected": 0,
             "errors": 0}
    started = time.monotonic()
    since_checkpoint = 0

    def checkpoint(partial: bool) -> dict:
        merged = dict(prev) if partial else {}
        merged.update(files)
        idx = {"schema": INDEX_SCHEMA, "vault_root": vault_root, "generated_at": now_iso(), "partial": partial,
               "stats": dict(stats, seconds=round(time.monotonic() - started, 1)), "dirs": dirs, "files": merged}
        write_json(index_path, idx)
        return idx

    for root, dnames, fnames in os.walk(vault_root):
        dnames.sort()
        rel_root = os.path.relpath(root, vault_root).replace("\\", "/")
        if rel_root != ".":
            dirs.append(rel_root)
        for fn in sorted(fnames):
            path = os.path.join(root, fn)
            rel = os.path.relpath(path, vault_root).replace("\\", "/")
            try:
                st = os.stat(path)
            except OSError as err:
                files[rel] = {"error": f"stat failed: {err}"}
                stats["errors"] += 1
                continue
            stats["files"] += 1
            stats["bytes"] += st.st_size
            old = prev.get(rel)
            ext = os.path.splitext(fn)[1].lower()
            if (old and old.get("size") == st.st_size and old.get("mtime_ns") == st.st_mtime_ns
                    and old.get("scan_version") == SCAN_VERSION and (old.get("sha256") or not hash_files)):
                files[rel] = old
                stats["reused"] += 1
                continue
            rec = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "ext": ext, "kind": KIND.get(ext, "other"),
                   "sha256": None, "scan_version": SCAN_VERSION, "archive": None}
            if hash_files:
                try:
                    rec["sha256"] = sha256_file(path)
                    stats["hashed"] += 1
                    stats["hashed_bytes"] += st.st_size
                except OSError as err:
                    rec["error"] = f"read failed: {err}"
                    stats["errors"] += 1
            elif old and old.get("size") == st.st_size and old.get("mtime_ns") == st.st_mtime_ns:
                rec["sha256"] = old.get("sha256")
            if ext in ARCHIVE_EXT:
                rec["archive"] = inspect_archive(path)
                stats["archives_inspected"] += 1
            files[rel] = rec
            since_checkpoint += 1
            if since_checkpoint >= checkpoint_every:
                since_checkpoint = 0
                checkpoint(partial=True)
                LOG.info("  scan: %d files, %.1f GB hashed so far (%.0fs)", stats["files"],
                         stats["hashed_bytes"] / 1e9, time.monotonic() - started)
    return checkpoint(partial=False)


def load_index(index_path: str) -> dict:
    return read_json(index_path) or {"files": {}, "dirs": []}


def duplicate_groups(index: dict) -> list[dict]:
    by_sha: dict[str, list[str]] = {}
    for rel, rec in index.get("files", {}).items():
        sha = rec.get("sha256")
        if sha and rec.get("size"):
            by_sha.setdefault(sha, []).append(rel)
    groups = [{"sha256": sha, "size": index["files"][paths[0]]["size"], "paths": sorted(paths)}
              for sha, paths in by_sha.items() if len(paths) > 1]
    return sorted(groups, key=lambda g: (-g["size"], g["paths"][0]))


def file_numbers(rel: str, rec: dict) -> tuple[set[str], set[int]]:
    arch = rec.get("archive") or {}
    chapters = set(arch.get("chapters") or [])
    vols = set(arch.get("volumes") or [])
    ch_name, vol_name = name_numbers(rel.rsplit("/", 1)[-1])
    if not chapters:
        chapters.update(ch_name)
    vols.update(vol_name)
    return chapters, vols


def is_foreign_payload(rec: dict) -> bool:
    """An archive that is clearly not media (e.g. a software installer)."""
    arch = rec.get("archive") or {}
    return rec.get("kind") == "executable" or (bool(arch) and arch.get("executables", 0) > 0
                                                and arch.get("images", 0) == 0)
