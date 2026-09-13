"""Ingest: intake -> identify -> hash -> destination -> import.

DRY RUN BY DEFAULT; apply=True copies. Intake layout:

    <intake base>/<source>/<series folder>/<files...>   (HaruNeko, downloads)
    <intake base>/<source>/<loose file>                 (e.g. Manual/)

Identification, in order: intake-map.json -> exact title/alias of a catalogued
work -> ComicInfo/series.json title inside the archives -> the file name.
A merely SIMILAR title is never imported: it goes to REVIEW_REQUIRED.

Guarantees (unchanged from intake_import.py):
  * never overwrites: copy to a temporary name, verify SHA-256, os.rename()
    onto a name that does not exist (Windows refuses to replace a file);
  * same name + same bytes -> skipped; same name + different bytes -> BOTH
    kept, the incoming one as "<name> (intake <sha8>)<ext>";
  * same bytes already anywhere in the Vault (vault index) or in the
    destination folder under another name -> skipped;
  * never extracts, recompresses, renames or deletes; intake originals stay;
  * never creates family, class or work folders: only sub-folders mirrored
    from the intake, strictly beneath an existing work folder.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import uuid
from collections import Counter

from . import taxonomy as tx
from .catalog import work_titles
from .scaffold import valid_segment
from .util import norm, sha256_file, similarity, within
from .vault_scan import ARCHIVE_EXT, inspect_archive

INCOMPLETE = (".crdownload", ".part", ".partial", ".tmp", ".download")
TEMP_PREFIX = ".continuum-import-"
COLOR_RE = re.compile(r"カラー版|フルカラー|full[ _-]?colou?r|\bcolou?red\b|\(colou?r\)|\[colou?r\]", re.I)
LANG_RE = re.compile(r"[\[(](en|eng|english|jp|jpn|ja|japanese|es|esp|spa|spanish|español)[\])]", re.I)
LANG_MAP = {"en": "en", "eng": "en", "english": "en", "jp": "ja", "jpn": "ja", "ja": "ja", "japanese": "ja",
            "es": "es", "esp": "es", "spa": "es", "spanish": "es", "español": "es"}


def _clean_name(name: str) -> str:
    s = os.path.splitext(name)[0]
    s = re.sub(r"[\[(][^\])]*[\])]", " ", s)
    s = re.sub(r"(?i)[-_ ](?:part|vol(?:ume)?|v|ch(?:apter)?|c)[-_ .]?\d+(?:\.\d+)?.*$", "", s)
    s = re.sub(r"[-_ ]\d+(?:\.\d+)?$", "", s)
    return s.replace("_", " ").replace("-", " ").strip()


def _same_content_elsewhere(directory: str, sha: str, size: int) -> str | None:
    if not os.path.isdir(directory):
        return None
    for fn in os.listdir(directory):
        p = os.path.join(directory, fn)
        if os.path.isfile(p) and not fn.startswith(TEMP_PREFIX) and os.path.getsize(p) == size:
            if sha256_file(p) == sha:
                return p
    return None


def place(src: str, dest: str, sha: str, size: int, apply: bool):
    target, kind = dest, "imported"
    if os.path.exists(target):
        if os.path.isdir(target):
            return "blocked-directory-in-the-way", target
        if sha256_file(target) == sha:
            return "identical-already-present", target
        stem, ext = os.path.splitext(dest)
        target, n = f"{stem} (intake {sha[:8]}){ext}", 2
        while os.path.exists(target):
            if os.path.isfile(target) and sha256_file(target) == sha:
                return "identical-already-present", target
            target, n = f"{stem} (intake {sha[:8]} {n}){ext}", n + 1
        kind = "collision-preserved"
    else:
        dup = _same_content_elsewhere(os.path.dirname(dest), sha, size)
        if dup:
            return "identical-content-under-other-name", dup
    if not apply:
        return kind + " (dry-run)", target
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = os.path.join(os.path.dirname(target), f"{TEMP_PREFIX}{uuid.uuid4().hex}.partial")
    shutil.copy2(src, tmp)
    if sha256_file(tmp) != sha:
        os.remove(tmp)  # our own temporary copy, never a Vault original
        return "copy-verification-failed", target
    os.rename(tmp, target)
    return kind, target


class Identifier:
    def __init__(self, cat, mapping: dict):
        self.cat = cat
        self.mapping = mapping or {}
        self.index = cat.alias_index()
        self.titles = [(t, fam, w) for fam, w in cat.iter_works() for t in work_titles(w)]

    def resolve(self, family: str, work: str):
        fam = self.cat.get_family(family)
        if fam is None:
            return None
        return next(((fam, w) for w in fam["works"] if work in (w["work"], w["id"])), None)

    def by_title(self, text: str):
        hits = self.index.get(norm(text), set())
        if len(hits) == 1:
            fam_name, wid = next(iter(hits))
            fam = self.cat.get_family(fam_name)
            return (fam, self.cat.get_work(fam, wid)), None
        if len(hits) > 1:
            return None, f"ambiguous: {sorted(h[1] for h in hits)}"
        return None, None

    def identify(self, name: str, series: list[str], is_file: bool):
        if name in self.mapping:
            m = self.mapping[name]
            hit = self.resolve(m.get("family", ""), m.get("work", ""))
            return (hit, "intake-map") if hit else (None, "intake-map entry does not match the catalog")
        hit, amb = self.by_title(name)
        if hit:
            return hit, "alias"
        if amb:
            return None, amb
        bare = re.sub(r"[\[(]\s*[\])]", " ", COLOR_RE.sub(" ", name)).strip(" -_")
        if bare != name:  # "Series (Full Color)" -> "Series" (+ colour flag handled by the caller)
            hit, amb = self.by_title(bare)
            if hit:
                return hit, "alias (edition marker removed)"
        for s in series:
            hit, amb2 = self.by_title(s)
            if hit:
                return hit, f"comicinfo series '{s}'"
        if is_file:
            cleaned = _clean_name(name)
            hit, _ = self.by_title(cleaned)
            if hit:
                return hit, f"file name '{cleaned}'"
        return None, "unclassified"

    def suggest(self, texts: list[str]):
        best = (0.0, None, None)
        for text in texts:
            for t, fam, w in self.titles:
                s = similarity(text, t)
                if s > best[0]:
                    best = (s, fam, w)
        return best


def ensure_destination(cat, fam: dict, w: dict, dest_root: str, *, apply: bool) -> tuple[str | None, str]:
    """Create the folder an identified arrival belongs in.

    Creating folders is allowed; inventing structure is not. So this only
    ever creates BENEATH a family folder that already exists, only for a
    work the catalog already knows, and only through names Windows accepts.
    A file sitting where the folder should go stops it: that is a conflict
    for a human, not something to work around.
    """
    family_dir = cat.family_dir(fam)
    if not os.path.isdir(family_dir):
        return None, "the family folder does not exist (family folders are never auto-created)"
    if not within(dest_root, family_dir):
        return None, "the destination is outside the family folder"
    relative = os.path.relpath(dest_root, family_dir).replace("\\", "/")
    segments = [s for s in relative.split("/") if s and s != "."]
    if not segments or not all(valid_segment(s) for s in segments):
        return None, "the destination name is not valid on Windows"
    if not apply:
        return "would-create-destination-folder (dry-run)", ""
    current = family_dir
    for segment in segments:
        current = os.path.join(current, segment)
        if os.path.isfile(current):
            return None, f"a file occupies {current}"
        if not os.path.isdir(current):
            os.mkdir(current)
    return "created-destination-folder", ""


def _files(unit_path: str) -> list[str]:
    if os.path.isfile(unit_path):
        return [unit_path]
    out = []
    for root, dirs, files in os.walk(unit_path):
        dirs.sort()
        out += [os.path.join(root, f) for f in sorted(files)]
    return out


def run(cat, intake_dirs: list[str], *, mapping: dict | None = None, index: dict | None = None,
        apply: bool = False, log_path: str | None = None, unofficial_hosts=(),
        only_units: set[str] | None = None, create_folders: bool = False) -> dict:
    """`only_units` limits the run to named arrivals, so approving one thing
    imports that thing and nothing else."""
    ident = Identifier(cat, mapping or {})
    vault_sha = {}
    if index and os.path.normcase(index.get("vault_root") or "") == os.path.normcase(cat.vault_root):
        for rel, rec in index.get("files", {}).items():
            if rec.get("sha256"):
                vault_sha.setdefault(rec["sha256"], os.path.join(cat.vault_root, *rel.split("/")))
    unofficial = {h.lower() for h in unofficial_hosts}
    records, units, review, log = [], [], [], []
    stamp = lambda: dt.datetime.now().astimezone().isoformat(timespec="seconds")  # noqa: E731
    for src_dir in intake_dirs:
        if not os.path.isdir(src_dir):
            continue
        source = os.path.basename(os.path.normpath(src_dir))
        for entry in sorted(os.listdir(src_dir)):
            if only_units is not None and entry not in only_units:
                continue
            upath = os.path.join(src_dir, entry)
            is_file = os.path.isfile(upath)
            if is_file and (entry.lower().endswith(INCOMPLETE) or entry.startswith(TEMP_PREFIX)):
                records.append({"source": source, "unit": entry, "file": upath,
                                "action": "left-in-intake: incomplete download"})
                continue
            files = _files(upath)
            series, langs, hosts = Counter(), Counter(), Counter()
            for f in [x for x in files if os.path.splitext(x)[1].lower() in ARCHIVE_EXT][:8]:
                a = inspect_archive(f)
                for s in {a.get("series"), a.get("json_title")} - {None}:
                    series[s] += 1
                if a.get("language"):
                    langs[a["language"]] += 1
                for h in a.get("web_hosts") or []:
                    hosts[h] += 1
            for f in files:
                m = LANG_RE.search(os.path.basename(f))
                if m:
                    langs[LANG_MAP[m.group(1).lower()]] += 1
            hit, how = ident.identify(entry, [s for s, _ in series.most_common()], is_file)
            colored = bool(COLOR_RE.search(entry) or any(COLOR_RE.search(s) for s in series))
            unit = {"source": source, "unit": entry, "path": upath, "files": len(files), "classified_by": how,
                    "series": dict(series), "languages": dict(langs), "provenance_hosts": dict(hosts),
                    "unofficial_provenance": sorted(h for h in hosts if h in unofficial), "colored": colored}
            units.append(unit)
            if hit is None:
                score, sfam, sw = ident.suggest([entry, *series])
                if score >= 0.85 and sw is not None:
                    reason = f"similar to '{sfam['family']} / {sw['work']}' ({score:.2f}) but not an exact title"
                    review.append(dict(unit, reason=reason, suggestion=f"{sfam['family']} / {sw['work']}",
                                       fix="add the folder name to intake-map.json or to the work's intake_aliases"))
                    records.append({"source": source, "unit": entry, "file": upath,
                                    "action": "left-in-intake: REVIEW_REQUIRED (low-confidence match)"})
                else:
                    label = "file outside any series folder" if (is_file and how == "unclassified") else how
                    records.append({"source": source, "unit": entry, "file": upath, "action": f"left-in-intake: {label}"})
                continue
            fam, w = hit
            if colored and w.get("relation") != tx.COLORED_EDITION:
                ce = [x for x in fam["works"] if x.get("relation") == tx.COLORED_EDITION
                      and (x.get("edition_of") == w["id"] or similarity(x["work"], w["work"]) >= 0.8)]
                if len(ce) == 1:
                    w, how = ce[0], how + " + colour-edition marker"
                else:
                    review.append(dict(unit, reason=f"looks like a COLOUR edition of '{w['work']}' but no single "
                                                    f"colour-edition work is catalogued for it; not mixed with the "
                                                    f"standard edition", suggestion=w["work"]))
                    records.append({"source": source, "unit": entry, "file": upath,
                                    "action": "left-in-intake: REVIEW_REQUIRED (colour edition)"})
                    continue
            if w.get("relation") == tx.FAN_WORK or w.get("official") is False:
                review.append(dict(unit, reason="identified as fan material; fan material is not imported "
                                                "automatically", suggestion=w["work"]))
                records.append({"source": source, "unit": entry, "file": upath,
                                "action": "left-in-intake: REVIEW_REQUIRED (fan material)"})
                continue
            dest_root = cat.work_dest(fam, w)
            if not dest_root:
                records.append({"source": source, "unit": entry, "file": upath, "family": fam["family"],
                                "work": w["work"], "action": "left-in-intake: work has no destination"})
                continue
            if not os.path.isdir(dest_root):
                if not create_folders:
                    records.append({"source": source, "unit": entry, "file": upath, "family": fam["family"],
                                    "work": w["work"], "target": dest_root,
                                    "action": "left-in-intake: destination work folder missing "
                                              "(pass --create-folders, or run scaffold --apply)"})
                    continue
                made, why = ensure_destination(cat, fam, w, dest_root, apply=apply)
                if made is None:
                    records.append({"source": source, "unit": entry, "file": upath, "family": fam["family"],
                                    "work": w["work"], "target": dest_root,
                                    "action": f"left-in-intake: cannot create the destination - {why}"})
                    continue
                records.append({"source": source, "unit": entry, "file": upath, "family": fam["family"],
                                "work": w["work"], "action": made, "target": dest_root,
                                "classified_by": how})
            for src in files:
                fn = os.path.basename(src)
                if fn.lower().endswith(INCOMPLETE) or fn.startswith(TEMP_PREFIX):
                    records.append({"source": source, "unit": entry, "file": src,
                                    "action": "left-in-intake: incomplete download"})
                    continue
                rel = fn if is_file else os.path.relpath(src, upath)
                dest = os.path.join(dest_root, rel)
                if not within(dest, dest_root):
                    records.append({"source": source, "unit": entry, "file": src,
                                    "action": "left-in-intake: unsafe relative path"})
                    continue
                size = os.path.getsize(src)
                sha = sha256_file(src)
                elsewhere = vault_sha.get(sha)
                if elsewhere and os.path.exists(elsewhere) and os.path.normcase(elsewhere) != os.path.normcase(dest):
                    kind, target = "identical-already-in-vault", elsewhere
                else:
                    kind, target = place(src, dest, sha, size, apply)
                rec = {"source": source, "unit": entry, "file": src, "action": kind, "classified_by": how,
                       "family": fam["family"], "work": w["work"], "work_id": w["id"], "target": target,
                       "sha256": sha, "bytes": size, "colored": colored,
                       "language": (langs.most_common(1)[0][0] if langs else None),
                       "unofficial_provenance": unit["unofficial_provenance"]}
                records.append(rec)
                log.append({"at": stamp(), "action": kind, "classified_by": how, "family": fam["family"],
                            "work": w["work"], "source": src, "target": target, "sha256": sha, "bytes": size,
                            "applied": apply})
    if apply and log and log_path:
        os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)
        with open(log_path, "a", encoding="utf-8", newline="\n") as fh:
            for e in log:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    counts = Counter(r["action"] for r in records)
    return {"mode": "APPLY" if apply else "DRY RUN", "intake_dirs": intake_dirs, "records": records, "units": units,
            "review": review, "counts": dict(counts)}


def format_results(res: dict, limit: int = 50) -> str:
    lines = [f"mode: {res['mode']}   intake: {'; '.join(res['intake_dirs'])}"]
    if not res["records"]:
        lines.append("intake is empty - nothing to do")
    groups: dict[str, list[str]] = {}
    for r in res["records"]:
        tail = f" -> {r['target']}" if r.get("target") else ""
        groups.setdefault(r["action"], []).append(f"{r['file']}{tail}")
    for kind in sorted(groups):
        lines.append(f"[{kind}] {len(groups[kind])}")
        lines += [f"    {x}" for x in groups[kind][:limit]]
    flagged = [u for u in res["units"] if u["unofficial_provenance"]]
    if flagged:
        lines.append(f"[provenance] {len(flagged)} intake folder(s) carry metadata from hosts on your unofficial "
                     f"list: " + "; ".join(f"{u['unit']} ({', '.join(u['unofficial_provenance'])})" for u in flagged[:10]))
    return "\n".join(lines)
