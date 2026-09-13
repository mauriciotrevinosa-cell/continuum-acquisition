"""Arrival preparation: give a download the shape the library expects.

Downloads arrive in whatever shape a site or a store felt like. The Vault
holds one shape: an archive whose top level is chapter folders

    Something-part-01.zip
        Ch0001/001.jpg ...
        Ch0002/001.jpg ...

This module reads an arrival, says what it would take to get there, and can
build a prepared copy. Three rules make that safe:

* the original is opened read-only and never modified, moved or deleted;
* the prepared copy is written somewhere else entirely (the intake), so the
  Vault never contains anything this module produced until the user approves
  it through the normal import;
* an archive member with an absolute path or ``..`` in it is refused, and an
  expansion that would blow past the size cap is refused, so a hostile
  archive cannot write outside its target or fill the disk.
"""
from __future__ import annotations

import os
import re
import zipfile

from .util import LOG, safe_folder_name
from .vault_scan import CHAPTER_DIR_RE, IMAGE_EXT

#: Archives we can open. RAR and 7z need tools this tool does not ship.
READABLE = {".zip", ".cbz"}
UNREADABLE = {".rar", ".cbr", ".7z", ".cb7"}
#: Refuse an expansion larger than this: a zip bomb is not a manga.
MAX_EXPANDED_BYTES = 8 * 1024 * 1024 * 1024
NUMBER = re.compile(r"(\d{1,4})(?:\.(\d{1,2}))?")

READY = "READY"
EXTRACT_NESTED = "EXTRACT_NESTED"
GROUP_IMAGES = "GROUP_IMAGES"
UNSUPPORTED = "UNSUPPORTED"
EMPTY = "EMPTY"


def _safe_member(name: str) -> bool:
    """Reject absolute paths and traversal before anything is written."""
    if not name or name.startswith(("/", "\\")) or ":" in name.split("/")[0]:
        return False
    return ".." not in name.replace("\\", "/").split("/")


def _chapter_label(text: str, fallback: int = 1) -> str:
    match = NUMBER.search(text or "")
    if not match:
        return f"Ch{fallback:04d}"
    major = int(match.group(1))
    return f"Ch{major:04d}.{match.group(2)}" if match.group(2) else f"Ch{major:04d}"


def inspect(path: str) -> dict:
    """What an arrival is made of, without changing it."""
    out = {"path": path, "kind": "folder" if os.path.isdir(path) else "file", "images": 0,
           "chapter_dirs": 0, "nested_archives": 0, "unreadable_archives": 0, "other_files": 0,
           "expanded_bytes": 0, "error": None}
    if os.path.isdir(path):
        for base, dirs, files in os.walk(path):
            for d in dirs:
                if CHAPTER_DIR_RE.match(d):
                    out["chapter_dirs"] += 1
            for name in files:
                ext = os.path.splitext(name)[1].lower()
                full = os.path.join(base, name)
                try:
                    out["expanded_bytes"] += os.path.getsize(full)
                except OSError:
                    pass
                if ext in IMAGE_EXT:
                    out["images"] += 1
                elif ext in READABLE:
                    out["nested_archives"] += 1
                elif ext in UNREADABLE:
                    out["unreadable_archives"] += 1
                else:
                    out["other_files"] += 1
        return out

    ext = os.path.splitext(path)[1].lower()
    if ext in UNREADABLE:
        out["unreadable_archives"] = 1
        return out
    if ext not in READABLE:
        out["other_files"] = 1
        return out
    try:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    top = info.filename.strip("/").split("/")[0]
                    if CHAPTER_DIR_RE.match(top):
                        out["chapter_dirs"] += 1
                    continue
                out["expanded_bytes"] += info.file_size
                member_ext = os.path.splitext(info.filename)[1].lower()
                parts = [p for p in info.filename.replace("\\", "/").split("/") if p]
                if len(parts) > 1 and CHAPTER_DIR_RE.match(parts[0]):
                    out["chapter_dirs"] = max(out["chapter_dirs"], 1)
                if member_ext in IMAGE_EXT:
                    out["images"] += 1
                elif member_ext in READABLE:
                    out["nested_archives"] += 1
                elif member_ext in UNREADABLE:
                    out["unreadable_archives"] += 1
                else:
                    out["other_files"] += 1
            tops = {n.replace("\\", "/").split("/")[0] for n in archive.namelist()}
            out["chapter_dirs"] = max(out["chapter_dirs"],
                                      sum(1 for t in tops if CHAPTER_DIR_RE.match(t)))
    except (zipfile.BadZipFile, OSError, RuntimeError) as err:
        out["error"] = f"{type(err).__name__}: {err}"
    return out


def plan(path: str) -> dict:
    """What would have to happen for this arrival to match the library."""
    facts = inspect(path)
    name = os.path.basename(os.path.normpath(path))
    if facts["error"]:
        return dict(facts, action=UNSUPPORTED, reason=f"cannot be read: {facts['error']}")
    if facts["unreadable_archives"] and not facts["images"] and not facts["chapter_dirs"]:
        return dict(facts, action=UNSUPPORTED,
                    reason="RAR/7z needs a tool this does not ship; extract it yourself into the intake")
    if facts["expanded_bytes"] > MAX_EXPANDED_BYTES:
        return dict(facts, action=UNSUPPORTED,
                    reason=f"expands to {facts['expanded_bytes'] / 1e9:.1f} GB, over the safety cap")
    if facts["chapter_dirs"] and facts["kind"] == "file":
        return dict(facts, action=READY, reason="already an archive of chapter folders")
    if facts["nested_archives"]:
        return dict(facts, action=EXTRACT_NESTED,
                    reason=f"{facts['nested_archives']} archive(s) packed inside; each becomes its own file")
    if facts["images"]:
        target = "one chapter folder" if not facts["chapter_dirs"] else f"{facts['chapter_dirs']} chapter folders"
        return dict(facts, action=GROUP_IMAGES,
                    reason=f"{facts['images']} loose image(s) to pack into {target} as {name}.zip")
    if facts["chapter_dirs"]:
        return dict(facts, action=GROUP_IMAGES, reason="chapter folders to pack into one archive")
    return dict(facts, action=EMPTY, reason="nothing readable to prepare")


def _extract_nested(path: str, out_dir: str, apply: bool) -> list[dict]:
    """Lift packed archives out, one file each. Originals stay put."""
    results: list[dict] = []
    if os.path.isdir(path):
        for base, _dirs, files in os.walk(path):
            for name in sorted(files):
                if os.path.splitext(name)[1].lower() not in READABLE:
                    continue
                source = os.path.join(base, name)
                target = os.path.join(out_dir, safe_folder_name(name))
                results.append({"output": target, "from": source, "written": False})
                if apply and not os.path.exists(target):
                    os.makedirs(out_dir, exist_ok=True)
                    with open(source, "rb") as src, open(target, "wb") as dst:
                        while chunk := src.read(1 << 20):
                            dst.write(chunk)
                    results[-1]["written"] = True
        return results

    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if info.is_dir() or os.path.splitext(info.filename)[1].lower() not in READABLE:
                continue
            if not _safe_member(info.filename):
                LOG.warning("refusing unsafe archive member %r in %s", info.filename, path)
                continue
            target = os.path.join(out_dir, safe_folder_name(os.path.basename(info.filename)))
            results.append({"output": target, "from": f"{path}!{info.filename}", "written": False})
            if apply and not os.path.exists(target):
                os.makedirs(out_dir, exist_ok=True)
                with archive.open(info) as src, open(target, "wb") as dst:
                    while chunk := src.read(1 << 20):
                        dst.write(chunk)
                results[-1]["written"] = True
    return results


def _group_images(path: str, out_dir: str, apply: bool) -> list[dict]:
    """Pack loose images into one archive of chapter folders."""
    name = os.path.basename(os.path.normpath(path))
    target = os.path.join(out_dir, f"{safe_folder_name(name)}.zip")
    entries: list[tuple[str, str]] = []  # (member name, source file)

    if os.path.isdir(path):
        for base, dirs, files in os.walk(path):
            dirs.sort()
            relative = os.path.relpath(base, path)
            folder = "" if relative == "." else relative.replace("\\", "/").split("/")[0]
            chapter = folder if CHAPTER_DIR_RE.match(folder or "") else _chapter_label(folder or name)
            for image in sorted(files):
                if os.path.splitext(image)[1].lower() in IMAGE_EXT:
                    entries.append((f"{chapter}/{image}", os.path.join(base, image)))
    result = {"output": target, "from": path, "members": len(entries), "written": False}
    if apply and entries and not os.path.exists(target):
        os.makedirs(out_dir, exist_ok=True)
        partial = target + ".partial"
        with zipfile.ZipFile(partial, "w", zipfile.ZIP_STORED) as out:
            for member, source in entries:
                out.write(source, member)
        os.rename(partial, target)
        result["written"] = True
    return [result]


def prepare(path: str, out_dir: str, *, apply: bool = False) -> dict:
    """Plan an arrival and, with apply, build the prepared copy."""
    decision = plan(path)
    outputs: list[dict] = []
    if decision["action"] == EXTRACT_NESTED:
        outputs = _extract_nested(path, out_dir, apply)
    elif decision["action"] == GROUP_IMAGES:
        outputs = _group_images(path, out_dir, apply)
    return {**decision, "out_dir": out_dir, "outputs": outputs, "applied": bool(apply and outputs)}
