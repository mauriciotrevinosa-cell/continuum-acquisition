#!/usr/bin/env python3
"""Build the personal acquisition queue from a works catalog and the raw Vault.

Franchise-agnostic tooling: every title lives in the catalog (personal data
under C:\\ContinuumData\\acquisition), never in this file.

Inputs
  works-catalog.json   family / work / Vault folder / candidate official sources
  the raw Vault        LISTED ONLY

Outputs (next to the catalog unless --out is given)
  acquisition-queue.json, acquisition-queue.csv, ACQUISITION_QUEUE.md
  vault-coverage.json  per-work files and chapter coverage
  update-watch.json    merged, never clobbered: remote/last-checked fields survive

Vault safety: directories are listed and each ZIP/CBZ's central directory is
read to find chapter folders. Nothing is extracted, hashed, renamed, moved or
opened for writing. With --create-missing, only a missing WORK folder beneath
an existing medium folder may be created (os.mkdir, never makedirs); family
and medium folders are never created.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
import zipfile

DEFAULT_CATALOG = r"C:\ContinuumData\acquisition\works-catalog.json"
CHAPTER_RE = re.compile(r"^(?:ch|chapter)[ _.-]?(\d+)(?:\.(\d+))?$", re.I)
ARCHIVES = (".zip", ".cbz")
NO_AUTO_REASON = (
    "Official distribution for this work is in-app reading or a DRM-protected ebook store; none grants "
    "a DRM-free direct-download right, so tooling must not download it. Mauricio acquires it himself."
)


def stamp() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def scan(path: str) -> dict:
    res = {"exists": os.path.isdir(path), "files": 0, "bytes": 0, "archives": 0, "extensions": {},
           "chapters": 0, "chapter_min": None, "chapter_max": None, "gaps": [],
           "chapters_in_multiple_archives": [], "unreadable_archives": []}
    if not res["exists"]:
        return res
    seen: dict[str, list[str]] = {}
    for root, _dirs, files in os.walk(path):
        for fn in files:
            p = os.path.join(root, fn)
            res["files"] += 1
            res["bytes"] += os.path.getsize(p)
            ext = os.path.splitext(fn)[1].lower()
            res["extensions"][ext] = res["extensions"].get(ext, 0) + 1
            if ext not in ARCHIVES:
                continue
            res["archives"] += 1
            try:
                with zipfile.ZipFile(p) as z:
                    tops = {n.split("/")[0] for n in z.namelist()}
            except (zipfile.BadZipFile, OSError):
                res["unreadable_archives"].append(os.path.relpath(p, path))
                continue
            for t in tops:
                if CHAPTER_RE.match(t):
                    seen.setdefault(t, []).append(os.path.relpath(p, path))
    ints = sorted({int(CHAPTER_RE.match(t).group(1)) for t in seen})
    res["chapters"] = len(seen)
    if ints:
        res["chapter_min"], res["chapter_max"] = ints[0], ints[-1]
        have = set(ints)
        res["gaps"] = [n for n in range(ints[0], ints[-1] + 1) if n not in have]
    res["chapters_in_multiple_archives"] = sorted(t for t, z in seen.items() if len(z) > 1)
    return res


def status_of(work: dict, dest: str | None, sc: dict) -> str:
    if work.get("contained_in"):
        # Acquired inside another work's files (e.g. extra chapters appended by
        # the source). Nothing is moved; the work is simply not pending.
        return "CONTAINED_IN_OTHER_WORK"
    if work.get("availability") == "not_confirmed":
        return "BLOCKED_NOT_CONFIRMED"
    if not dest:
        return "NO_DESTINATION"
    if not sc["exists"]:
        return "DESTINATION_MISSING"
    if work.get("declared_status") == "COMPLETE":
        return "COMPLETE" if sc["files"] else "DECLARED_COMPLETE_BUT_EMPTY"
    return "PRESENT_UNVERIFIED" if sc["files"] else "PENDING"


def coverage_text(sc: dict) -> str:
    if not sc["files"]:
        return ""
    if sc["chapters"]:
        gaps = f", {len(sc['gaps'])} gaps" if sc["gaps"] else ", no gaps"
        return f"Ch {sc['chapter_min']}-{sc['chapter_max']} ({sc['chapters']} chapter folders{gaps})"
    return f"{sc['files']} files (chapters not detectable)"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--catalog", default=DEFAULT_CATALOG)
    ap.add_argument("--vault", default=None, help="override vault_root from the catalog")
    ap.add_argument("--out", default=None, help="output directory (default: next to the catalog)")
    ap.add_argument("--create-missing", action="store_true",
                    help="create missing WORK folders beneath existing medium folders")
    args = ap.parse_args()

    with open(args.catalog, encoding="utf-8") as fh:
        cat = json.load(fh)
    vault = args.vault or cat["vault_root"]
    out = args.out or os.path.dirname(os.path.abspath(args.catalog))
    sources = cat.get("sources", {})
    if not os.path.isdir(vault):
        print(f"Vault root not found: {vault}", file=sys.stderr)
        return 2

    rows, coverage, issues, created = [], {}, [], []
    expected_top, expected_children = set(), {}

    for fam in cat["families"]:
        ffolder = os.path.join(vault, fam["vault_family_folder"])
        expected_top.add(fam["vault_family_folder"].lower())
        if not os.path.isdir(ffolder):
            issues.append(f"family folder missing (never auto-created): {ffolder}")
        for w in fam["works"]:
            sub = w.get("vault_subpath")
            dest = os.path.normpath(os.path.join(ffolder, *sub.split("/"))) if sub else None
            if sub and "/" in sub:
                medium_dir = os.path.join(ffolder, sub.split("/")[0])
                expected_children.setdefault(medium_dir.lower(), set()).add(sub.split("/", 1)[1].lower())
                if args.create_missing and os.path.isdir(medium_dir) and not os.path.exists(dest):
                    os.mkdir(dest)
                    created.append(dest)
            sc = scan(dest) if dest else scan("")
            st = status_of(w, dest, sc)
            cands = [dict(sources.get(c["source"], {"name": c["source"]}), confidence=c["confidence"])
                     for c in w.get("candidate_sources", [])]
            pending = st in ("PENDING", "PRESENT_UNVERIFIED", "DESTINATION_MISSING", "BLOCKED_NOT_CONFIRMED")
            row = {
                "order": fam["order"],
                "family": fam["family"],
                "work": w["work"],
                "role": w.get("role", ""),
                "medium": fam["medium"],
                "acquisition_status": st,
                "vault_destination": dest,
                "destination_exists": bool(dest and os.path.isdir(dest)),
                "local_files": sc["files"],
                "coverage": coverage_text(sc),
                "candidate_sources": cands,
                "language": cat.get("language_default", "en"),
                "automatic_download_allowed": False,
                "automatic_download_reason": NO_AUTO_REASON,
                "manual_action_required": pending,
                "unofficial_sources": "not surveyed by tooling",
                "notes": w.get("notes", ""),
            }
            rows.append(row)
            coverage[f"{fam['family']} / {w['work']}"] = dict(sc, vault_destination=dest)
            if st == "DECLARED_COMPLETE_BUT_EMPTY":
                issues.append(f"declared COMPLETE but no files: {dest}")
            if sc["gaps"]:
                issues.append(f"chapter gaps in {dest}: {sc['gaps'][:20]}")
            if sc["unreadable_archives"]:
                issues.append(f"unreadable archives in {dest}: {sc['unreadable_archives'][:5]}")

    # Files parked directly in a medium folder whose works use child folders.
    for fam in cat["families"]:
        subs = [w.get("vault_subpath") or "" for w in fam["works"]]
        if any("/" in s for s in subs):
            mdir = os.path.join(vault, fam["vault_family_folder"], fam["medium"])
            if os.path.isdir(mdir):
                loose = [f for f in os.listdir(mdir) if os.path.isfile(os.path.join(mdir, f))]
                if loose:
                    issues.append(f"{len(loose)} file(s) directly in {mdir} not inside any work folder")
                extra = [d for d in os.listdir(mdir) if os.path.isdir(os.path.join(mdir, d))
                         and d.lower() not in expected_children.get(mdir.lower(), set())]
                if extra:
                    issues.append(f"work folders not in catalog under {mdir}: {extra}")
    extra_top = [d for d in os.listdir(vault) if os.path.isdir(os.path.join(vault, d))
                 and d.lower() not in expected_top]
    if extra_top:
        issues.append(f"top-level Vault folders not in catalog: {extra_top}")

    pending_rows = [r for r in rows if r["manual_action_required"]]
    summary = {
        "families": len(cat["families"]),
        "works": len(rows),
        "complete": sum(r["acquisition_status"] == "COMPLETE" for r in rows),
        "pending": sum(r["acquisition_status"] == "PENDING" for r in rows),
        "present_unverified": sum(r["acquisition_status"] == "PRESENT_UNVERIFIED" for r in rows),
        "blocked": sum(r["acquisition_status"].startswith("BLOCKED") for r in rows),
        "destination_missing": sum(r["acquisition_status"] == "DESTINATION_MISSING" for r in rows),
        "automatic_downloads_allowed": sum(r["automatic_download_allowed"] for r in rows),
        "work_folders_created": created,
        "issues": issues,
    }
    generated = stamp()

    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "acquisition-queue.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump({"generated_at": generated, "catalog": os.path.abspath(args.catalog), "vault_root": vault,
                   "summary": summary, "queue": [r["family"] + " / " + r["work"] for r in pending_rows],
                   "works": rows}, fh, ensure_ascii=False, indent=2)
    with open(os.path.join(out, "vault-coverage.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump({"generated_at": generated, "vault_root": vault, "works": coverage}, fh, ensure_ascii=False, indent=2)

    cols = ["order", "family", "work", "role", "medium", "acquisition_status", "vault_destination",
            "destination_exists", "local_files", "coverage", "candidate_sources", "language",
            "automatic_download_allowed", "manual_action_required", "notes"]
    with open(os.path.join(out, "acquisition-queue.csv"), "w", encoding="utf-8-sig", newline="") as fh:
        wr = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        wr.writeheader()
        for r in rows:
            wr.writerow(dict(r, candidate_sources="; ".join(
                f"{c.get('name')} [{c.get('confidence')}]" for c in r["candidate_sources"])))

    # update-watch: merge so remote observations and check times are never lost.
    uw_path = os.path.join(out, "update-watch.json")
    prev = {}
    if os.path.exists(uw_path):
        with open(uw_path, encoding="utf-8") as fh:
            for e in json.load(fh).get("works", []):
                prev[(e["family"], e["work"])] = e
    watch = []
    for r in rows:
        if r["acquisition_status"].startswith("BLOCKED"):
            continue
        sc = coverage[f"{r['family']} / {r['work']}"]
        old = prev.get((r["family"], r["work"]), {})
        local = sc["chapter_max"] if sc["chapters"] else None
        remote = old.get("latest_remote_observed_chapter")
        watch.append({
            "family": r["family"],
            "work": r["work"],
            "vault_destination": r["vault_destination"],
            "latest_local_chapter": local,
            "local_chapter_folders": sc["chapters"],
            "latest_remote_observed_chapter": remote,
            "last_checked": old.get("last_checked"),
            "source": old.get("source") or (r["candidate_sources"][0]["name"] if r["candidate_sources"] else None),
            "update_available": (remote > local) if (remote is not None and local is not None) else None,
        })
    with open(uw_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump({"schema": "continuum.personal.update-watch/1", "generated_at": generated,
                   "note": "Acquisition tooling only. latest_remote_observed_chapter / last_checked / source "
                           "are preserved across rebuilds; fill them in by hand or with a future checker.",
                   "works": watch}, fh, ensure_ascii=False, indent=2)

    md = [f"# Acquisition queue", "", f"Generated {generated} from `{os.path.abspath(args.catalog)}`.", "",
          f"**{summary['works']} works** in {summary['families']} families: {summary['complete']} complete, "
          f"{summary['pending']} pending, {summary['present_unverified']} present-unverified, "
          f"{summary['blocked']} blocked. Automatic downloads allowed: **{summary['automatic_downloads_allowed']}**.",
          "", "Every pending work needs Mauricio to acquire it himself: no candidate source grants a "
          "DRM-free direct-download right. Unofficial sites were not surveyed.", "",
          "## Queue (in documented order)", "",
          "| # | Family | Work | Status | Vault destination | Official candidates |", "|---|---|---|---|---|---|"]
    for i, r in enumerate(pending_rows, 1):
        cands = "<br>".join(f"{c.get('name')} *({c.get('confidence')})*" for c in r["candidate_sources"]) or "— none identified —"
        md.append(f"| {i} | {r['family']} | {r['work']} | {r['acquisition_status']} | "
                  f"`{r['vault_destination'] or '(none)'}` | {cands} |")
    md += ["", "## Complete", "", "| Family | Work | Coverage |", "|---|---|---|"]
    for r in rows:
        if r["acquisition_status"] == "COMPLETE":
            md.append(f"| {r['family']} | {r['work']} | {r['coverage']} |")
    md += ["", "## Issues", ""] + ([f"- {x}" for x in issues] or ["- none"])
    with open(os.path.join(out, "ACQUISITION_QUEUE.md"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(md) + "\n")

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
