"""Local coverage per work versus remote bibliographic data.

    COMPLETE       local files reach the remote latest chapter / volume count,
                   the work is contained in another work's files, or it is
                   declared so
    PARTIAL        gaps, or local stops before the remote latest chapter
    MISSING        no local media for this work, AND none of this work's
                   kind of material sits unmapped in the family's folder
    NEEDS_MAPPING  no files are attributed to the work, but unattributed
                   media of its material class IS present in the family: the
                   material may well be here, and "missing" would be a lie
    UNKNOWN        files present but nothing remote to compare against
    BLOCKED        the work's existence/availability is not confirmed

MISSING is a claim of absence, so it is the status that must be earned: it is
only reported when nothing local could be the work.

A continuation (a sequel arc) is often released inside its parent work's
download parts, numbered on after the parent's final chapter (200.1, 200.2...)
rather than filed in a folder of its own. When a continuation has no files but
its completed parent holds chapters beyond that parent's final chapter, those
chapters are reported as the continuation's probable content: status UNKNOWN
(present, count unverified), flag CONTAINED_CANDIDATE - never MISSING.

DUPLICATE_CANDIDATE is a flag (the same chapter in several archives), not a
status: the files are reported, never removed.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

import datetime as _dt

from . import vault_scan
from .taxonomy import VIDEO_CLASSES
from .layout import Tree, attribute

STATUSES = ("COMPLETE", "PARTIAL", "MISSING", "NEEDS_MAPPING", "UNKNOWN", "BLOCKED")
#: Relations whose content may ship inside the parent work's own releases.
CONTINUATIONS = frozenset({"SEQUEL", "CONTINUATION"})
#: Relations that ARE the parent a continuation follows.
PARENTS = frozenset({"MAIN_WORK", "PREQUEL"})


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


def iso_ns(ns: int | None) -> str | None:
    """A nanosecond timestamp as UTC ISO-8601, or None."""
    if not ns:
        return None
    return _dt.datetime.fromtimestamp(ns / 1e9, tz=_dt.timezone.utc).isoformat(timespec="seconds")


def is_media(tree: Tree, rel: str) -> bool:
    rec = tree.files[rel]
    return rec.get("kind") in vault_scan.MEDIA_KINDS and not vault_scan.is_foreign_payload(rec)


def unmapped_by_class(cat, tree: Tree, owner: dict) -> dict[tuple[str, str], list[str]]:
    """(family id, class) -> media files under that class folder no work owns."""
    out: dict[tuple[str, str], list[str]] = defaultdict(list)
    for fam in cat.families:
        frel = tree.actual(fam["vault_family_folder"])
        if not frel:
            continue
        for cls in tree.kids(frel):
            for rel in tree.files_under(f"{frel}/{cls}"):
                if rel not in owner and is_media(tree, rel):
                    out[(fam["id"], cls.lower())].append(rel)
    return out


def episodes(tree: Tree, rels: list[str]) -> dict:
    """What the video files of a work say about themselves: seasons and episodes.

    Local facts only. Nothing here knows how many episodes a season HAS; that
    is remote knowledge the catalog may or may not carry.
    """
    seasons: dict[int | None, set[int]] = defaultdict(set)
    other = 0
    videos = [r for r in rels if tree.files[r].get("kind") == "video"]
    for r in videos:
        season, episode = vault_scan.episode_numbers(r.rsplit("/", 1)[-1])
        if episode is None:
            other += 1
        else:
            seasons[season].add(episode)
    # Archives are containers, not episodes. What they hold is counted from
    # their own directory; an archive not yet inventoried says so instead of
    # being counted as one episode.
    archives = [r for r in rels if tree.files[r].get("kind") == "archive"]
    bundles = [r for r in archives if (tree.files[r].get("archive") or {}).get("videos")]
    uninventoried = [r for r in archives
                     if "videos" not in (tree.files[r].get("archive") or {})
                     and not (tree.files[r].get("archive") or {}).get("images")]
    contained = 0
    for r in bundles:
        for entry in (tree.files[r].get("archive") or {}).get("video_entries") or []:
            contained += 1
            season, episode = vault_scan.entry_episode_numbers(str(entry.get("name") or ""))
            if episode is None:
                other += 1
            else:
                seasons[season].add(episode)
    rows = []
    for season in sorted(seasons, key=lambda s: (s is None, s or 0)):
        eps = sorted(seasons[season])
        gaps = [n for n in range(eps[0], eps[-1] + 1) if n not in seasons[season]] if eps else []
        rows.append({"season": season, "episodes": len(eps), "first": eps[0], "last": eps[-1],
                     "episodes_text": ranges(eps), "gaps": gaps, "gaps_text": ranges(gaps)})
    return {"videos": len(videos), "other_videos": other, "seasons": rows,
            "archives_with_video": len(bundles), "contained_videos": contained,
            "archives_not_inventoried": len(uninventoried)}


def units_of(tree: Tree, rels: list[str]) -> list[dict]:
    """Each file of a work as a unit a reader can open, labelled by what it holds.

    A video is an episode when its name says so; an archive of pages carries
    the chapter and volume numbers found in it; an archive of videos lists the
    episodes inside it. Nothing is invented: a file whose name and contents
    state no number stays unnumbered.
    """
    out = []
    for rel in rels:
        rec = tree.files[rel]
        name = rel.rsplit("/", 1)[-1]
        arch = rec.get("archive") or {}
        unit = {"file": rel, "kind": rec.get("kind"), "size": rec.get("size", 0)}
        if rec.get("kind") == "video":
            season, episode = vault_scan.episode_numbers(name)
            unit.update(season=season, episode=episode)
        elif rec.get("kind") == "archive":
            chapters, vols = vault_scan.file_numbers(rel, rec)
            majors = sorted({int(c.split(".")[0]) for c in chapters})
            unit.update(chapters=len(chapters), chapter_text=ranges(majors), volumes=sorted(vols),
                        first_chapter=majors[0] if majors else None,
                        pages=arch.get("images") or 0, contained_videos=arch.get("videos") or 0)
            if arch.get("video_entries"):
                inside = [vault_scan.entry_episode_numbers(str(e.get("name") or ""))
                          for e in arch["video_entries"]]
                unit["contained_episodes"] = [{"season": s, "episode": e} for s, e in inside]
        out.append(unit)

    def order(u: dict):
        if u.get("episode") is not None:
            return (0, u.get("season") if u.get("season") is not None else 999, u["episode"], u["file"])
        # Reading order: by the first chapter an archive holds (download
        # parts are not numbered in chapter order), then by volume, then name.
        first = u.get("first_chapter")
        volume = (u.get("volumes") or [None])[0]
        return (1, first if first is not None else 10**9, volume if volume is not None else 10**9,
                [int(x) if x.isdigit() else x.lower() for x in re.split(r"(\d+)", u["file"])])
    return sorted(out, key=order)


def compute(cat, tree: Tree) -> dict[str, dict]:
    owner = attribute(cat, tree)
    unmapped = unmapped_by_class(cat, tree, owner)
    by_work: dict[str, list[str]] = defaultdict(list)
    for rel, (_fam, w) in owner.items():
        by_work[w["id"]].append(rel)
    out: dict[str, dict] = {}
    chapters_by_work: dict[str, tuple[set[str], dict[str, list[str]]]] = {}
    for fam, w in cat.iter_works():
        rels = [r for r in by_work.get(w["id"], []) if is_media(tree, r)]
        # the classes this work could be filed under: what it is, and where it points
        sub = (w.get("vault_subpath") or "").split("/")[0].lower()
        own_classes = {c for c in ((w.get("material_class") or "").lower(), sub) if c}
        unmapped_here = sorted({r for c in own_classes for r in unmapped.get((fam["id"], c), [])})
        ep = episodes(tree, rels)
        video_like = ep["videos"] + ep["archives_with_video"] + (
            ep["archives_not_inventoried"] if (w.get("material_class") or "").lower() in VIDEO_CLASSES else 0)
        is_video = bool(rels) and video_like * 2 >= len(rels)
        season_gaps = [s for s in ep["seasons"] if s["gaps"]]
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
        elif not rels and unmapped_here:
            status = "NEEDS_MAPPING"
            reason = (f"{len(unmapped_here)} local file(s) of this kind are in the family folder but not "
                      f"mapped to any work")
        elif not rels:
            status, reason = "MISSING", "no local media"
        elif is_video and season_gaps:
            status = "PARTIAL"
            reason = "; ".join(
                (f"season {s['season']}" if s["season"] is not None else "episodes")
                + f": {s['gaps_text']} not present (held {s['episodes_text']})" for s in season_gaps)
            flags.append("EPISODE_GAPS")
        elif is_video and w.get("declared_status") != "COMPLETE":
            held = sum(s["episodes"] for s in ep["seasons"])
            status = "UNKNOWN"
            if ep["archives_not_inventoried"] and not held:
                what = (f"{ep['archives_not_inventoried']} archive file(s); episode inventory not "
                        f"indexed yet")
            elif ep["contained_videos"] and not ep["videos"]:
                what = f"{ep['contained_videos']} episode file(s) inside {ep['archives_with_video']} archive(s)"
            elif held:
                what = f"{held} episode file(s)"
            else:
                what = f"{ep['videos']} video file(s)"
            reason = f"{what} present; no episode count is known to compare against"
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
        chapters_by_work[w["id"]] = (chapters, ch_arch)
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
            "media": "video" if is_video else ("pages" if rels else None),
            "episodes": ep if (ep["videos"] or ep["archives_with_video"] or ep["archives_not_inventoried"] and is_video) else None,
            # Which files make up the work, so a reader can open them. Paths
            # relative to the Vault; the application resolves them itself.
            "media_files": sorted(rels),
            "units": units_of(tree, rels),
            "unmapped_local_files": len(unmapped_here),
            "last_added_at": iso_ns(max((tree.files[r].get("created_ns") or 0 for r in rels), default=0)),
        }
    _continuations_inside_parents(cat, out, chapters_by_work)
    return out


def _continuations_inside_parents(cat, out: dict[str, dict], chapters_by_work: dict) -> None:
    """A continuation with no files of its own, whose chapters sit after its parent's finale."""
    for fam, w in cat.iter_works():
        row = out.get(w["id"])
        if row is None or row["status"] != "MISSING":
            continue
        if str(w.get("relation") or "").upper() not in CONTINUATIONS:
            continue
        cls = (w.get("material_class") or "").lower()
        for parent in fam.get("works") or []:
            if parent is w or str(parent.get("relation") or "").upper() not in PARENTS:
                continue
            if (parent.get("material_class") or "").lower() != cls or parent.get("id") not in chapters_by_work:
                continue
            remote = parent.get("remote") or {}
            final = remote.get("latest_chapter")
            if not remote.get("completed") or not isinstance(final, (int, float)):
                continue
            chapters, ch_arch = chapters_by_work[parent["id"]]
            beyond = sorted((c for c in chapters if _ck(c) > (int(final),)), key=_ck)
            if not beyond:
                continue
            files = sorted({r for c in beyond for r in ch_arch.get(c, [])})
            text = beyond[0] if len(beyond) == 1 else f"{beyond[0]}-{beyond[-1]}"
            row.update(
                status="UNKNOWN",
                reason=(f"probably held inside the files of '{parent['work']}': {len(beyond)} "
                        f"chapter(s) {text} come after that work's final chapter {int(final)}"),
                flags=[*row["flags"], "CONTAINED_CANDIDATE"],
                local_chapters=len(beyond),
                media="pages",
                contained_candidate={"work_id": parent["id"], "work": parent["work"],
                                     "chapters": beyond, "chapters_text": text, "files": files},
            )
            break


def reconcile_layout(layout: dict, cov: dict[str, dict]) -> int:
    """No folder is planned for a continuation whose chapters its parent's files already hold.

    The layout is built before coverage knows what is contained; a MISSING_FOLDER
    row for a CONTAINED_CANDIDATE work becomes CONTAINED (pointing at the parent),
    so the structure audit and the scaffold plan do not propose an empty folder.
    Returns how many rows changed.
    """
    changed = 0
    for fam in layout.get("families") or []:
        for row in fam.get("works") or []:
            c = cov.get(row.get("work_id")) or {}
            candidate = c.get("contained_candidate")
            if candidate and row.get("layout_status") == "MISSING_FOLDER":
                row["layout_status"] = "CONTAINED"
                row["contained_candidate"] = candidate["work"]
                changed += 1
    if changed and isinstance(layout.get("summary"), dict):
        summary = layout["summary"]
        summary["missing_folder"] = summary.get("missing_folder", 0) - changed
        summary["contained"] = summary.get("contained", 0) + changed
    return changed

