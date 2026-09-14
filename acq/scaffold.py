"""Scaffold: create the missing expected folders, and nothing else.

Dry run by default. With apply=True it creates folders with os.mkdir, one
level at a time: a missing material-class folder inside an EXISTING family
folder, then the work folder. It never creates family folders, never deletes,
moves, renames or writes a file in the Vault, and never creates a folder for
a work that is unofficial, low/medium confidence, under review, ambiguous or
in conflict.
"""
from __future__ import annotations

import json
import os

from . import taxonomy
from .util import LOG, now_iso, within

ACTIONS = ("CREATE", "EXISTS", "LEGACY_MAPPING", "CONFLICT", "AMBIGUOUS", "REVIEW_REQUIRED")
RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
INVALID = set('<>:"/\\|?*')


def valid_segment(name: str) -> bool:
    return (bool(name) and not any(c in INVALID or ord(c) < 32 for c in name) and not name.endswith((".", " "))
            and name.split(".")[0].lower() not in RESERVED)


def eligible(w: dict, family_exists: bool, path: str | None) -> tuple[bool, str]:
    if not family_exists:
        return False, "family folder missing (family folders are never auto-created)"
    if not path:
        return False, "no destination planned"
    if w.get("availability") == "not_confirmed":
        return False, "existence / availability not confirmed"
    if w.get("official") is not True:
        return False, "official status not confirmed"
    if w.get("relation") in taxonomy.NEVER_SCAFFOLD:
        return False, f"relation {w.get('relation')} is never scaffolded"
    if w.get("confidence") != "high" or w.get("review_status") != "ACCEPTED":
        return False, f"confidence {w.get('confidence')} / {w.get('review_status')}: needs review first"
    if len(path) > 240:
        return False, "path too long for Windows"
    return True, "official, high confidence"


def plan(cat, layout: dict) -> list[dict]:
    actions: list[dict] = []
    for f in layout["families"]:
        fam = cat.get_family(f["family_title"])
        fam_dir = cat.family_dir(fam)
        planned_parents: set[str] = set()
        for row in f["works"]:
            w = cat.get_work(fam, row["work_id"])
            st = row["layout_status"]
            base = {"family": fam["family"], "work": w["work"], "work_id": w["id"], "relation": w["relation"],
                    "material_class": w["material_class"], "path": row["local_path"],
                    "expected_path": row["expected_path"]}
            if st == "CONTAINED":
                inside = w.get("contained_in") or row.get("contained_candidate")
                actions.append(dict(base, action="EXISTS", reason=f"contained in '{inside}'; no folder needed"))
            elif st in ("FOUND", "MISSING_CONTENT"):
                actions.append(dict(base, action="EXISTS", reason="folder present" + ("" if st == "FOUND" else " (empty)")))
            elif st == "LEGACY_MAPPING":
                actions.append(dict(base, action="LEGACY_MAPPING", reason=f"{row['legacy_kind']}: kept at its current "
                                    f"path (scheme path would be {row['expected_path']}); nothing moved"))
            elif st == "CONFLICT":
                actions.append(dict(base, action="CONFLICT", reason="a file occupies the folder path"))
            elif st == "AMBIGUOUS":
                amb = w.get("layout_ambiguity") or {}
                actions.append(dict(base, action="AMBIGUOUS", reason=f"similar existing folder "
                                    f"{amb.get('similar_existing_folder')} ({amb.get('score')}): same work? not created"))
            elif st == "REVIEW_REQUIRED":
                actions.append(dict(base, action="REVIEW_REQUIRED", reason=(
                    "folder exists; relation/official status to review" if row["folder_exists"]
                    else f"not created until reviewed ({w.get('confidence')} confidence, {w.get('origin')})")))
            elif st == "MISSING_FOLDER":
                ok, why = eligible(w, f["folder_exists"], row["local_path"])
                segs = (w.get("vault_subpath") or "").split("/")
                if ok and not all(valid_segment(s) for s in segs):
                    ok, why = False, "folder name not valid on Windows"
                if ok and not within(row["local_path"], fam_dir):
                    ok, why = False, "destination outside the family folder"
                if not ok:
                    actions.append(dict(base, action="REVIEW_REQUIRED", reason=why))
                    continue
                cur = fam_dir
                for seg in segs:
                    cur = os.path.join(cur, seg)
                    if os.path.isdir(cur) or cur.lower() in planned_parents:
                        continue
                    if os.path.exists(cur):
                        actions.append(dict(base, action="CONFLICT", path=cur, reason="a file occupies this path"))
                        break
                    planned_parents.add(cur.lower())
                    kind = "work folder" if os.path.normcase(cur) == os.path.normcase(row["local_path"]) else "class folder"
                    actions.append(dict(base, action="CREATE", path=cur, kind=kind, reason=why))
            else:
                actions.append(dict(base, action="REVIEW_REQUIRED", reason=f"unhandled layout status {st}"))
    return actions


def apply(cat, actions: list[dict], log_path: str) -> list[dict]:
    results = []
    with open(log_path, "a", encoding="utf-8", newline="\n") as log:
        for a in actions:
            if a["action"] != "CREATE":
                continue
            path = a["path"]
            fam = cat.get_family(a["family"])
            if not (within(path, cat.vault_root) and within(path, cat.family_dir(fam))):
                results.append(dict(a, result="REFUSED (outside family folder)"))
                continue
            if os.path.exists(path):
                results.append(dict(a, result="EXISTS"))
                continue
            if not os.path.isdir(os.path.dirname(path)):
                results.append(dict(a, result="SKIPPED (parent missing)"))
                continue
            os.mkdir(path)
            entry = {"at": now_iso(), "action": "CREATE", "kind": a.get("kind"), "family": a["family"],
                     "work": a["work"], "path": path, "applied": True}
            log.write(json.dumps(entry, ensure_ascii=False) + "\n")
            results.append(dict(a, result="CREATED"))
            LOG.info("  created %s", path)
    return results


def read_log(log_path: str) -> list[dict]:
    if not os.path.exists(log_path):
        return []
    out = []
    with open(log_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return out
