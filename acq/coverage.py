"""Local coverage per work versus remote bibliographic data.

    COMPLETE  local files reach the remote latest chapter / volume count, the
              work is contained in another work's files, or it is declared so
    PARTIAL   gaps, or local stops before the remote latest chapter
    MISSING   no local media at all
    UNKNOWN   files present but nothing remote to compare against
    BLOCKED   the work's existence/availability is not confirmed

DUPLICATE_CANDIDATE is a flag (the same chapter in several archives), not a
status: the files are reported, never removed.
"""
from __future__ import annotations

from collections import Counter, defaultdict

from . import vault_scan
from .layout import Tree, attribute

STATUSES = ("COMPLETE", "PARTIAL", "MISSING", "UNKNOWN", "BLOCKED")


def ranges(nums) -> str:
    nums = sorted(set(nums))
    if not nums:
        return ""
    out, start, prev = [], nums[0], nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
            continue
        out.append(f"{start}-{prev}" if start != prev else str(start))
        start = prev = n
    out.append(f"{start}-{prev}" if start != prev else str(start))
    return ", ".join(out)


def _ck(c: str):
    return tuple(int(x) for x in c.split("."))


def compute(cat, tree: Tree) -> dict[str, dict]:
    owner = attribute(cat, tree)
    by_work: dict[str, list[str]] = defaultdict(list)
    for rel, (_fam, w) in owner.items():
        by_work[w["id"]].append(rel)
    out: dict[str, dict] = {}
    for fam, w in cat.iter_works():
        rels = [r for r in by_work.get(w["id"], [])
                if tree.files[r].get("kind") in vault_scan.MEDIA_KINDS
                and not vault_scan.is_foreign_payload(tree.files[r])]
        chapters: set[str] = set()
        vols: set[int] = set()
        ch_arch: dict[str, list[str]] = defaultdict(list)
        for r in rels:
            ch, v = vault_scan.file_numbers(r, tree.files[r])
            chapters |= ch
            vols |= v
            for c in ch:
                ch_arch[c].append(r)
        majors = sorted({int(c.split(".")[0]) for c in chapters})
        gaps = [n for n in range(majors[0], majors[-1] + 1) if n not in set(majors)] if majors else []
        # How many chapters the release itself says exist. Chapter numbering
        # is not dense: series skip integers and insert decimals (57.5), so a
        # missing NUMBER is not a missing chapter. Counting is the check that
        # distinguishes the two. This comes from the files' own metadata, so
        # it corroborates - it never outranks a publisher's record.
        counts = Counter(c for c in (((tree.files[r].get("archive") or {}).get("remote_count"))
                                     for r in rels) if c)
        source_count = counts.most_common(1)[0][0] if counts else None
        remote = w.get("remote") or {}
        ndl = remote.get("ndl") or {}
        r_latest = remote.get("latest_chapter")
        r_vols = remote.get("volumes") or ndl.get("volumes")
        local_max = majors[-1] if majors else None
        missing_tail = []
        flags = []
        dup = sorted((c for c, a in ch_arch.items() if len(a) > 1), key=_ck)
        if dup:
            flags.append("DUPLICATE_CANDIDATE")
        if w.get("contained_in"):
            status, reason = "COMPLETE", f"contained in the files of '{w['contained_in']}'"
        elif w.get("availability") == "not_confirmed":
            status, reason = "BLOCKED", "existence / availability not confirmed"
        elif not rels:
            status, reason = "MISSING", "no local media"
        elif gaps and source_count and len(chapters) >= source_count:
            flags.append("NUMBERING_GAPS")
            status = "COMPLETE"
            reason = (f"{len(chapters)} chapters held and the release lists {source_count}: "
                      f"the numbering skips {ranges(gaps)}, nothing is missing")
        elif gaps:
            status, reason = "PARTIAL", f"chapter gaps {ranges(gaps)}"
            if source_count:
                reason += f" ({len(chapters)} held of {source_count} listed)"
            if r_latest and local_max is not None and local_max < r_latest:
                missing_tail = list(range(local_max + 1, int(r_latest) + 1))
                reason += f"; local stops at {local_max}, latest known {r_latest}"
        elif r_latest and local_max is not None:
            if local_max >= r_latest:
                status, reason = "COMPLETE", f"local reaches chapter {local_max} (latest known {r_latest})"
            else:
                missing_tail = list(range(local_max + 1, int(r_latest) + 1))
                status, reason = "PARTIAL", f"local stops at chapter {local_max}; latest known {r_latest}"
        elif r_vols and vols:
            status, reason = (("COMPLETE", f"{len(vols)} local volumes, {r_vols} known") if len(vols) >= r_vols
                              else ("PARTIAL", f"{len(vols)} of {r_vols} volumes"))
        elif w.get("declared_status") == "COMPLETE":
            status, reason = "COMPLETE", "declared complete in the catalog"
        elif source_count and chapters and len(chapters) < source_count:
            status = "PARTIAL"
            reason = (f"{len(chapters)} chapters held; the release itself lists {source_count}")
            flags.append("COUNT_FROM_FILE_METADATA")
        else:
            status, reason = "UNKNOWN", "files present; no remote chapter/volume data to compare"
        out[w["id"]] = {
            "family": fam["family"], "work": w["work"], "status": status, "reason": reason, "flags": flags,
            "local_files": len(rels), "local_bytes": sum(tree.files[r].get("size", 0) for r in rels),
            "local_chapters": len(chapters), "chapter_min": majors[0] if majors else None, "chapter_max": local_max,
            "gaps": gaps, "gaps_text": ranges(gaps), "local_volumes": sorted(vols),
            "remote_latest_chapter": r_latest, "remote_volumes": r_vols,
            "source_chapter_count": source_count,
            "remote_completed": remote.get("completed"), "remote_status": remote.get("status_text"),
            "missing_chapters_text": ranges(missing_tail), "chapters_in_multiple_archives": dup[:200],
            "languages": sorted({(tree.files[r].get("archive") or {}).get("language") for r in rels} - {None}),
        }
    return out
