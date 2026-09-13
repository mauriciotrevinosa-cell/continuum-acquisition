#!/usr/bin/env python3
"""Non-destructive import from the acquisition intake into the raw Vault.

    SOURCE -> intake -> inspect -> classify -> hash -> destination -> import

Franchise-agnostic: titles come only from the catalog (personal data).
DEFAULT IS A DRY RUN. Pass --apply to copy anything.

Intake layout expected (HaruNeko's):  <intake>\\<series title>\\<chapter file or folder>
The series title is classified to a catalog work by, in order:
  1. intake-map.json   {"<intake folder name>": {"family": "...", "work": "..."}}
  2. exact normalised match against each work's name and intake_aliases
     (single-work families also match their family name / Vault folder name).
Unclassified or ambiguous folders are reported and left alone.

Guarantees
  * never overwrites: the copy lands under a temporary name, is hash-verified,
    then os.rename()d onto a name that does not exist -- and on Windows
    os.rename refuses to replace an existing file;
  * same name, same bytes            -> skipped, nothing written;
  * same name, different bytes       -> BOTH kept; incoming gets "(intake <sha8>)";
  * same bytes under another name in the destination -> skipped, no duplicate;
  * never extracts, recompresses or re-encodes;
  * never deletes or modifies the intake original;
  * never creates family, medium or work folders -- only sub-folders mirrored
    from the intake, strictly beneath an existing work folder.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import unicodedata
import uuid
import datetime as dt

DEFAULT_CATALOG = r"C:\ContinuumData\acquisition\works-catalog.json"
DEFAULT_LOG = r"C:\ContinuumData\acquisition\import-log.jsonl"
INCOMPLETE = (".crdownload", ".part", ".partial", ".tmp", ".download")
TEMP_PREFIX = ".continuum-import-"


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).replace("×", "x").lower()
    return re.sub(r"[^0-9a-z]+", "", s)


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def within(child: str, parent: str) -> bool:
    child, parent = os.path.normcase(os.path.abspath(child)), os.path.normcase(os.path.abspath(parent))
    return os.path.commonpath([child, parent]) == parent


def build_index(cat: dict, vault: str):
    index: dict[str, set] = {}
    works: dict[tuple, str | None] = {}
    for fam in cat["families"]:
        ffolder = os.path.join(vault, fam["vault_family_folder"])
        single = len(fam["works"]) == 1
        for w in fam["works"]:
            sub = w.get("vault_subpath")
            dest = os.path.normpath(os.path.join(ffolder, *sub.split("/"))) if sub else None
            key = (fam["family"], w["work"])
            works[key] = dest
            names = [w["work"], *w.get("intake_aliases", [])]
            if single:
                names += [fam["family"], fam["vault_family_folder"]]
            for n in names:
                index.setdefault(norm(n), set()).add(key)
    return index, works


def classify(folder: str, mapping: dict, index: dict):
    if folder in mapping:
        m = mapping[folder]
        return (m["family"], m["work"]), "intake-map"
    hits = index.get(norm(folder), set())
    if len(hits) == 1:
        return next(iter(hits)), "alias"
    if len(hits) > 1:
        return None, f"ambiguous: {sorted(hits)}"
    return None, "unclassified"


def same_content_elsewhere(directory: str, sha: str, size: int) -> str | None:
    if not os.path.isdir(directory):
        return None
    for fn in os.listdir(directory):
        p = os.path.join(directory, fn)
        if os.path.isfile(p) and not fn.startswith(TEMP_PREFIX) and os.path.getsize(p) == size:
            if sha256(p) == sha:
                return p
    return None


def place(src: str, dest: str, sha: str, size: int, apply: bool):
    target, kind = dest, "imported"
    if os.path.exists(target):
        if os.path.isdir(target):
            return "blocked-directory-in-the-way", target
        if sha256(target) == sha:
            return "identical-already-present", target
        stem, ext = os.path.splitext(dest)
        target, n = f"{stem} (intake {sha[:8]}){ext}", 2
        while os.path.exists(target):
            if os.path.isfile(target) and sha256(target) == sha:
                return "identical-already-present", target
            target, n = f"{stem} (intake {sha[:8]} {n}){ext}", n + 1
        kind = "collision-preserved"
    else:
        dup = same_content_elsewhere(os.path.dirname(dest), sha, size)
        if dup:
            return "identical-content-under-other-name", dup
    if not apply:
        return kind + " (dry-run)", target
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = os.path.join(os.path.dirname(target), f"{TEMP_PREFIX}{uuid.uuid4().hex}.partial")
    shutil.copy2(src, tmp)
    if sha256(tmp) != sha:
        os.remove(tmp)  # our own temporary copy, never a Vault original
        return "copy-verification-failed", target
    os.rename(tmp, target)  # Windows: raises FileExistsError rather than overwrite
    return kind, target


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="actually copy (default: dry run)")
    ap.add_argument("--catalog", default=DEFAULT_CATALOG)
    ap.add_argument("--intake", default=None)
    ap.add_argument("--vault", default=None)
    ap.add_argument("--map", default=None, help="intake-map.json (default: next to the catalog)")
    ap.add_argument("--log", default=DEFAULT_LOG)
    args = ap.parse_args()

    with open(args.catalog, encoding="utf-8") as fh:
        cat = json.load(fh)
    vault = args.vault or cat["vault_root"]
    intake = args.intake or cat["intake_root"]
    map_path = args.map or os.path.join(os.path.dirname(os.path.abspath(args.catalog)), "intake-map.json")
    mapping = {}
    if os.path.exists(map_path):
        with open(map_path, encoding="utf-8") as fh:
            mapping = json.load(fh)
    index, works = build_index(cat, vault)

    results = {}
    log = []
    if not os.path.isdir(intake):
        print(f"intake not found: {intake}", file=sys.stderr)
        return 2
    for entry in sorted(os.listdir(intake)):
        path = os.path.join(intake, entry)
        if os.path.isfile(path):
            results.setdefault("left-in-intake: file outside any series folder", []).append(path)
            continue
        key, how = classify(entry, mapping, index)
        if key is None:
            results.setdefault(f"left-in-intake: {how}", []).append(path)
            continue
        dest_root = works.get(key)
        if not dest_root or not os.path.isdir(dest_root):
            results.setdefault("left-in-intake: destination work folder missing", []).append(f"{path} -> {dest_root}")
            continue
        for root, _dirs, files in os.walk(path):
            for fn in sorted(files):
                src = os.path.join(root, fn)
                if fn.lower().endswith(INCOMPLETE) or fn.startswith(TEMP_PREFIX):
                    results.setdefault("left-in-intake: incomplete download", []).append(src)
                    continue
                dest = os.path.join(dest_root, os.path.relpath(src, path))
                if not within(dest, dest_root):
                    results.setdefault("left-in-intake: unsafe relative path", []).append(src)
                    continue
                size = os.path.getsize(src)
                sha = sha256(src)
                kind, target = place(src, dest, sha, size, args.apply)
                results.setdefault(kind, []).append(f"{src} -> {target}")
                log.append({"at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                            "action": kind, "classified_by": how, "family": key[0], "work": key[1],
                            "source": src, "target": target, "sha256": sha, "bytes": size,
                            "applied": args.apply})

    if args.apply and log:
        os.makedirs(os.path.dirname(args.log), exist_ok=True)
        with open(args.log, "a", encoding="utf-8", newline="\n") as fh:
            for e in log:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")

    print(f"mode: {'APPLY' if args.apply else 'DRY RUN'}   intake: {intake}   vault: {vault}")
    if not results:
        print("intake is empty - nothing to do")
    for kind in sorted(results):
        print(f"[{kind}] {len(results[kind])}")
        for line in results[kind][:50]:
            print(f"    {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
