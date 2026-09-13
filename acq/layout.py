"""Vault layout: expected vs actual topology, legacy mappings, audit findings.

    <Family>/<material class>/<Work>/[<edition or release>/]<raw files>

Everything here is ANALYSIS plus catalog METADATA. Nothing in the Vault is
created, moved, renamed or deleted by this module; scaffold.py creates
folders, and moves are only ever PROPOSED (never applied automatically).

Legacy paths are first-class: a work whose raw files sit directly in its
class folder (e.g. <Family>/manga/) keeps that path; it is recorded as a
LEGACY_MAPPING instead of being migrated for aesthetics.
"""
from __future__ import annotations

import os
from collections import Counter, defaultdict

from . import taxonomy
from .catalog import Catalog, add_alias, distinct_similarity, family_names, family_tokens, work_titles
from .util import LOG, norm, now_iso, safe_folder_name, similarity, slug
from . import vault_scan

LAYOUT_SCHEMA = "continuum.personal.vault-layout/1"
#: Folder names that mean "this is the main story", used by some sources
#: instead of repeating the title. A naming convention, not a title.
MAIN_STORY_MARKERS = {"ms", "mainstory", "main", "mainseries"}
EQUIV_STRONG = 0.9
EQUIV_WEAK = 0.75
STATUSES = ("EXPECTED", "FOUND", "MISSING_FOLDER", "MISSING_CONTENT", "LEGACY_MAPPING", "UNEXPECTED",
            "AMBIGUOUS", "POSSIBLE_DUPLICATE", "REVIEW_REQUIRED", "CONFLICT", "CONTAINED")


class Tree:
    """Live directory listing (dirs) + the vault index (files)."""

    def __init__(self, vault: str, index: dict):
        self.vault = vault
        self.dirs: set[str] = set()
        for root, dnames, _ in os.walk(vault):
            for d in dnames:
                self.dirs.add(os.path.relpath(os.path.join(root, d), vault).replace("\\", "/"))
        self.lower = {d.lower(): d for d in self.dirs}
        self.files: dict[str, dict] = {rel: rec for rel, rec in index.get("files", {}).items()
                                       if os.path.exists(os.path.join(vault, rel))}
        self.children: dict[str, list[str]] = defaultdict(list)
        for d in sorted(self.dirs):
            parent, _, name = d.rpartition("/")
            self.children[parent].append(name)
        self.files_in: dict[str, list[str]] = defaultdict(list)
        for rel in self.files:
            self.files_in[rel.rpartition("/")[0]].append(rel)

    def actual(self, rel: str) -> str | None:
        return self.lower.get(rel.lower())

    def exists_dir(self, rel: str) -> bool:
        return rel.lower() in self.lower

    def is_file(self, rel: str) -> bool:
        return os.path.isfile(os.path.join(self.vault, *rel.split("/")))

    def kids(self, rel: str) -> list[str]:
        act = self.actual(rel)
        return list(self.children.get(act, [])) if act else []

    def files_under(self, rel: str) -> list[str]:
        prefix = rel.lower().rstrip("/") + "/"
        return [f for f in self.files if f.lower().startswith(prefix)]


def other_names(fam: dict) -> list[str]:
    """Names for this family besides its own and its folder's.

    Computed by exclusion rather than by position: when a family was adopted
    from a Vault folder, its name IS the folder name, the two collapse into
    one entry, and dropping a fixed two would swallow a real alias.
    """
    own = {norm(fam.get("family")), norm(fam.get("vault_family_folder"))}
    return [n for n in family_names(fam) if norm(n) not in own]


def krel_of(family_folder: str, cls: str, child: str) -> str:
    return f"{family_folder}/{cls}/{child}"


def wrel(fam: dict, w: dict) -> str | None:
    sub = w.get("vault_subpath")
    return f"{fam['vault_family_folder']}/{sub}" if sub else None


def canonical_subpath(w: dict) -> str:
    return f"{w['material_class']}/{safe_folder_name(w['work'])}"


def claims(cat: Catalog) -> dict[str, tuple[dict, dict]]:
    out: dict[str, tuple[dict, dict]] = {}
    for fam, w in cat.iter_works():
        r = wrel(fam, w)
        if r:
            out.setdefault(r.lower(), (fam, w))
    return out


def attribute(cat: Catalog, tree: Tree) -> dict[str, tuple[dict, dict]]:
    """file rel -> (family, work) by the DEEPEST claimed folder containing it."""
    cl = claims(cat)
    owner = {}
    for rel in tree.files:
        parts = rel.split("/")
        for i in range(len(parts) - 1, 0, -1):
            hit = cl.get("/".join(parts[:i]).lower())
            if hit:
                owner[rel] = hit
                break
    return owner


def _series_counter(tree: Tree, rels) -> Counter:
    c = Counter()
    for rel in rels:
        arch = tree.files[rel].get("archive") or {}
        for s in {arch.get("series"), arch.get("json_title")}:
            if s:
                c[s] += 1
    return c


# ---------------------------------------------------------------------------
# metadata passes (catalog only)
# ---------------------------------------------------------------------------
def adopt_vault_families(cat: Catalog, tree: Tree) -> list[dict]:
    """Register a top-level Vault folder the catalog has never heard of.

    Adding a series is dropping a folder into the Vault. Until this pass
    existed, such a folder was only *reported* as UNEXPECTED, so a user who
    added five series saw five warnings and no library entries.

    What is adopted is what the folder states about itself:

    * the folder name is the family name, spelled exactly as on disk. It is
      never corrected, tidied or renamed - a misspelling the user typed is
      still their folder, and a rename is a destructive change this tool does
      not make;
    * series names found in the files become aliases, so the real spelling is
      searchable without the folder moving;
    * a relation is only ever what the taxonomy can *derive*. Nothing here
      declares a work official, canonical or main by assumption.

    Refused deliberately:

    * a folder that resembles an existing family (it is a possible duplicate,
      and merging by guess would bury one of them);
    * a folder with no media under a story class - an empty shell, a scratch
      directory, or a name beginning with "_" or "." that is plainly not a
      series.

    Everything adopted lands as review_status REVIEW: it enters the library
    where the user can see it, and stays flagged until they confirm it.
    """
    changes: list[dict] = []
    known = {f["vault_family_folder"].lower() for f in cat.families}
    known |= {norm(n) for f in cat.families for n in family_names(f)}
    for d in sorted(tree.children.get("", [])):
        if d.lower() in known or norm(d) in known:
            continue
        if d.startswith((".", "_")):
            continue
        # A near-match is a question for the user, not an answer for us.
        similar = max((similarity(d, n) for f in cat.families for n in family_names(f)), default=0.0)
        if cat.families_matching(d) or similar >= EQUIV_WEAK:
            continue
        classes = [c for c in tree.kids(d)
                   if c.lower() in taxonomy.MATERIAL_CLASSES and c.lower() not in taxonomy.FAN_CLASSES]
        media = [r for c in classes for r in tree.files_under(f"{d}/{c}")
                 if tree.files[r].get("kind") in vault_scan.MEDIA_KINDS
                 and not vault_scan.is_foreign_payload(tree.files[r])]
        if not media:
            continue
        series = _series_counter(tree, media)
        held = Counter(c.lower() for c in classes
                       for r in tree.files_under(f"{d}/{c}") if r in set(media))
        primary = held.most_common(1)[0][0] if held else "manga"
        fam = {"order": 900 + len(cat.families), "family": d, "vault_family_folder": d,
               "medium": primary if primary in taxonomy.STORY_CLASSES else "manga",
               "works": [], "notes": "Adopted from an existing Vault folder; not reviewed yet.",
               "category": None, "id": slug(d), "external_ids": {},
               "aliases": [s for s in series if norm(s) != norm(d)],
               "origin": "vault-adopted", "review_status": "REVIEW",
               "provenance": [{"at": now_iso(), "source": "vault-layout",
                               "evidence": f"top-level folder '{d}' holding {len(media)} media file(s) "
                                           f"under {', '.join(sorted(held)) or 'no class folder'}"}]}
        ids = {f.get("id") for f in cat.families}
        base, n = fam["id"], 2
        while fam["id"] in ids:
            fam["id"], n = f"{base}-{n}", n + 1
        cat.families.append(fam)
        cat.dirty = True
        known.add(d.lower())
        changes.append({"action": "ADOPT_FAMILY", "family": d, "work": "",
                        "path": d, "files": len(media), "classes": sorted(held)})
        # Files sitting straight in <Family>/<class>/ are the common shape of
        # a downloaded series. They get a work at THAT path (a legacy
        # mapping), because the alternative is moving the user's files.
        # Sub-folders are left to adopt_vault_folders, which runs next and
        # gives each one its own work.
        for cls in classes:
            crel, kind = f"{d}/{cls}", cls.lower()
            direct = [r for r in tree.files_in.get(tree.actual(crel) or crel, [])
                      if tree.files[r].get("kind") in vault_scan.MEDIA_KINDS
                      and not vault_scan.is_foreign_payload(tree.files[r])]
            if not direct:
                continue
            names = _series_counter(tree, direct)
            title = names.most_common(1)[0][0] if names else d
            relation, why = taxonomy.classify([title, d])
            cat.add_work(fam, {
                # vault_subpath keeps the folder's real spelling; the class is
                # the vocabulary term it names.
                "work": title, "role": None, "vault_subpath": cls, "declared_status": None,
                "candidate_sources": [], "intake_aliases": [], "availability": "unknown",
                "notes": "Adopted from an existing Vault folder; relation and official status "
                         "need review.",
                "origin": "vault-adopted", "official": None, "confidence": "medium",
                "review_status": "REVIEW", "relation": relation,
                "medium": kind if kind in taxonomy.STORY_CLASSES else fam.get("medium"),
                "material_class": (taxonomy.material_class(relation, kind)
                                   if relation != taxonomy.UNKNOWN else kind),
                "titles": {"canonical": title, "en": None, "ja": None, "romaji": None},
                "aliases": [s for s in names if norm(s) != norm(title)],
                "path_origin": "adopted",
                "provenance": [{"at": now_iso(), "source": "vault-layout",
                                "evidence": f"{len(direct)} file(s) directly in {crel}; "
                                            f"classified by {why}"}]})
            changes.append({"action": "ADOPT", "family": d, "work": title, "path": crel,
                            "relation": relation, "why": why})
    return changes


def adopt_vault_folders(cat: Catalog, tree: Tree) -> list[dict]:
    """Register what the user already built so nothing in the Vault is
    'unknown': (1) point an EMPTY curated class-root work at the single child
    folder that obviously is it; (2) adopt every other unclaimed work folder
    as a REVIEW work at its CURRENT path (legacy mapping, never moved)."""
    changes = []
    for fam in cat.families:
        frel = tree.actual(fam["vault_family_folder"])
        if not frel:
            continue
        for cls in tree.kids(frel):
            if cls.lower() not in taxonomy.MATERIAL_CLASSES or cls.lower() in taxonomy.FAN_CLASSES:
                continue
            crel = f"{frel}/{cls}"
            cl = {r: w for r, (f2, w) in claims(cat).items() if f2 is fam}
            kids = [k for k in tree.kids(crel) if f"{crel}/{k}".lower() not in cl]
            # A folder an earlier pass ADOPTED is a placeholder this tool
            # invented, not a decision the user made, so it stays a candidate
            # for "which of these is the main story?".
            placeholder_kids = [k for k in tree.kids(crel)
                                if (cl.get(f"{crel}/{k}".lower()) or {}).get("origin") == "vault-adopted"]
            root_w = cl.get(crel.lower())
            root_media = [r for r in tree.files_in.get(crel, [])
                          if tree.files[r].get("kind") in vault_scan.MEDIA_KINDS
                          and not vault_scan.is_foreign_payload(tree.files[r])]
            if (root_w is not None and root_w.get("origin") == "curated" and not root_media
                    and (kids or placeholder_kids)):
                candidates = kids + placeholder_kids
                scored = sorted(((max(similarity(k, t) for t in work_titles(root_w)), k) for k in candidates),
                                reverse=True)
                chosen = None
                why = ""
                if scored[0][0] >= 0.85 and (len(scored) == 1 or scored[1][0] < 0.85):
                    chosen = scored[0][1]
                    why = f"matches the work title ({scored[0][0]:.2f})"
                else:
                    marked = [k for k in candidates if norm(k) in MAIN_STORY_MARKERS]
                    if len(marked) == 1:
                        chosen = marked[0]
                        why = "named for the main story"
                if chosen is not None:
                    k = chosen
                    root_w.setdefault("vault_subpath_curated", root_w["vault_subpath"])
                    root_w["vault_subpath"] = f"{cls}/{k}"
                    root_w["path_origin"] = "legacy-matched"
                    root_w["provenance"].append({"at": now_iso(), "source": "vault-layout", "evidence":
                                                 f"class root '{cls}' held no files; its child folder "
                                                 f"'{k}' {why}"})
                    # An earlier pass may have adopted this folder as a work of
                    # its own. The curated work owns it now, so the placeholder
                    # goes: two works claiming one folder is worse than either.
                    duplicate = next((x for x in fam["works"]
                                      if x is not root_w and x.get("vault_subpath") == f"{cls}/{k}"
                                      and x.get("origin") == "vault-adopted"), None)
                    if duplicate is not None:
                        fam["works"].remove(duplicate)
                        changes.append({"action": "MERGE", "family": fam["family"],
                                        "work": duplicate["work"], "path": krel_of(frel, cls, k),
                                        "into": root_w["work"]})
                    if k in kids:
                        kids.remove(k)
                    cat.dirty = True
                    changes.append({"action": "REPOINT", "family": fam["family"], "work": root_w["work"],
                                    "from": crel, "to": f"{crel}/{k}"})
            for k in kids:
                krel = f"{crel}/{k}"
                series = _series_counter(tree, tree.files_under(krel))
                title = series.most_common(1)[0][0] if series else k
                relation, why = taxonomy.classify([k, title, *series])
                w = {"work": k, "role": None, "vault_subpath": f"{cls}/{k}", "declared_status": None,
                     "candidate_sources": [], "intake_aliases": [], "availability": "unknown",
                     "notes": "Adopted from an existing Vault folder; relation and official status need review.",
                     "origin": "vault-adopted", "official": None, "confidence": "medium", "review_status": "REVIEW",
                     "relation": relation, "medium": cls if cls in taxonomy.STORY_CLASSES else fam.get("medium"),
                     "material_class": taxonomy.material_class(relation, cls) if relation != taxonomy.UNKNOWN else cls,
                     "titles": {"canonical": title, "en": None, "ja": None, "romaji": None},
                     "aliases": [s for s in series if norm(s) != norm(k)], "path_origin": "adopted",
                     "provenance": [{"at": now_iso(), "source": "vault-layout",
                                     "evidence": f"existing folder {krel} ({len(tree.files_under(krel))} files); "
                                                 f"classified by {why}"}]}
                cat.add_work(fam, w)
                changes.append({"action": "ADOPT", "family": fam["family"], "work": k, "path": krel,
                                "relation": relation, "why": why})
    return changes


def harvest_aliases(cat: Catalog, tree: Tree) -> list[dict]:
    """ComicInfo/series.json titles become aliases of the work whose folder
    holds them, but only when the folder is consistent (one series) and the
    title does not already route to another work."""
    owner = attribute(cat, tree)
    per_work: dict[str, list[str]] = defaultdict(list)
    ref: dict[str, tuple[dict, dict]] = {}
    for rel, (fam, w) in owner.items():
        per_work[w["id"]].append(rel)
        ref[w["id"]] = (fam, w)
    index = cat.alias_index()
    added = []
    for wid, rels in per_work.items():
        fam, w = ref[wid]
        names = list({norm(s): s for s in _series_counter(tree, rels)}.values())
        if not names or not _one_cluster(names):
            continue
        for s in names:
            routes = index.get(norm(s), set())
            if routes and routes != {(fam["family"], wid)}:
                continue
            if add_alias(w, "aliases", s):
                added.append({"family": fam["family"], "work": w["work"], "alias": s})
                cat.dirty = True
            primary = fam.get("medium") or "manga"
            if w.get("relation") == taxonomy.MAIN_WORK and w.get("material_class") == primary:
                add_alias(fam, "aliases", s)
    return added


def _one_cluster(names: list[str]) -> bool:
    """ComicInfo Series and series.json title of one release often differ only
    in punctuation; a folder is consistent when all its names are that close."""
    return all(similarity(names[0], n) >= 0.9 for n in names[1:])


def plan_paths(cat: Catalog, tree: Tree) -> list[dict]:
    """Give every catalogued official work without a path its destination:
    <class>/<Work>. An existing unclaimed folder that clearly IS the work is
    reused (mapping, no new folder); a merely similar one is AMBIGUOUS."""
    out = []
    cl = claims(cat)
    taken = set(cl)
    for fam in cat.families:
        frel = fam["vault_family_folder"]
        # every existing work-level folder of the family, with the titles that describe it
        existing = []
        for c in tree.kids(frel):
            for k in tree.kids(f"{frel}/{c}"):
                rel = f"{frel}/{c}/{k}"
                owner = cl.get(rel.lower())
                existing.append((rel, [k] + (work_titles(owner[1]) if owner else [])))
        ftoks = family_tokens(fam)
        for w in fam["works"]:
            if (w.get("vault_subpath") or w.get("official") is False or w.get("relation") in taxonomy.NEVER_SCAFFOLD
                    or w.get("origin") == "curated" or w.get("availability") == "not_confirmed"):
                continue  # a curated work without a path has no destination ON PURPOSE
            cls = w["material_class"]
            crel = f"{frel}/{cls}"
            free = [k for k in tree.kids(crel) if f"{crel}/{k}".lower() not in taken]
            scored = sorted(((max(similarity(k, t) for t in work_titles(w)), k) for k in free), reverse=True)
            target = f"{cls}/{safe_folder_name(w['work'])}"
            w.pop("layout_ambiguity", None)
            if scored and scored[0][0] >= EQUIV_STRONG and (len(scored) == 1 or scored[1][0] < EQUIV_STRONG):
                target, origin = f"{cls}/{scored[0][1]}", "legacy-matched"
            else:
                origin = "planned"
                # a similar folder ANYWHERE in the family (any class, claimed or not) makes the new
                # folder ambiguous: it may be the same work filed differently by the user
                near = sorted(((max(distinct_similarity(a, b, ftoks) for a in work_titles(w) for b in names), rel)
                               for rel, names in existing), reverse=True)
                if near and near[0][0] >= EQUIV_WEAK:
                    w["layout_ambiguity"] = {"similar_existing_folder": near[0][1], "score": round(near[0][0], 2)}
            w["vault_subpath"] = target
            w["path_origin"] = origin
            taken.add(f"{frel}/{target}".lower())
            cat.dirty = True
            out.append({"family": fam["family"], "work": w["work"], "subpath": target, "origin": origin})
    return out


# ---------------------------------------------------------------------------
# layout + audit
# ---------------------------------------------------------------------------
def build_layout(cat: Catalog, tree: Tree, *, unofficial_hosts=(), scaffold_log: list | None = None) -> dict:
    owner = attribute(cat, tree)
    cl = claims(cat)
    dup_groups = vault_scan.duplicate_groups({"files": tree.files})
    index = cat.alias_index()
    unofficial = {h.lower() for h in unofficial_hosts}
    fam_by_folder = {f["vault_family_folder"].lower(): f for f in cat.families}
    created_by_family: dict[str, list[str]] = defaultdict(list)
    for e in scaffold_log or []:
        if e.get("applied") and e.get("action") == "CREATE":
            created_by_family[e.get("family", "")].append(e.get("path"))

    global_findings, proposed_moves, families_out = [], [], []

    # top level
    for d in sorted(tree.children.get("", [])):
        if d.lower() in fam_by_folder:
            continue
        similar = sorted(((max(similarity(d, n) for n in family_names(f)), f["family"]) for f in cat.families),
                         reverse=True)[:1]
        exact_alias = cat.families_matching(d)
        if exact_alias or (similar and similar[0][0] >= EQUIV_WEAK):
            target = exact_alias[0]["family"] if exact_alias else similar[0][1]
            global_findings.append({"type": "DUPLICATE_FAMILY_CANDIDATE", "path": d, "detail":
                                    f"looks like family '{target}' (alias/similarity); NOT merged automatically",
                                    "review_required": True})
        else:
            global_findings.append({"type": "UNEXPECTED", "path": d, "detail": "top-level folder not in catalog",
                                    "review_required": True})
    for rel in tree.files_in.get("", []):
        global_findings.append({"type": "UNEXPECTED", "path": rel, "detail": "file directly in the Vault root",
                                "review_required": True})

    for fam in sorted(cat.families, key=lambda f: f.get("order", 999)):
        frel_expected = fam["vault_family_folder"]
        frel = tree.actual(frel_expected)
        findings = []
        rows = []
        fam_files = tree.files_under(frel) if frel else []
        # classes present / expected
        present = {c.lower(): c for c in (tree.kids(frel) if frel else [])}
        expected_classes = sorted({w["material_class"] for w in fam["works"]
                                   if w.get("official") is not False and w.get("relation") != taxonomy.FAN_WORK})
        class_info = {}
        for c in sorted(set(present.values()) | set(expected_classes), key=str.lower):
            crel = f"{frel_expected}/{present.get(c.lower(), c)}"
            exists = c.lower() in present
            n_files = len(tree.files_under(crel)) if exists else 0
            class_info[c] = {"exists": exists, "files": n_files, "work_folders": len(tree.kids(crel)) if exists else 0,
                             "known_class": c.lower() in taxonomy.MATERIAL_CLASSES}
            if exists and c.lower() not in taxonomy.MATERIAL_CLASSES:
                near = sorted(((similarity(c, k), k) for k in taxonomy.MATERIAL_CLASSES), reverse=True)[0]
                findings.append({"type": "AMBIGUOUS" if near[0] >= EQUIV_WEAK else "UNEXPECTED", "path": crel,
                                 "detail": f"class folder not in the schema" +
                                           (f" (looks like '{near[1]}')" if near[0] >= EQUIV_WEAK else ""),
                                 "review_required": True})
            if exists and n_files == 0:
                findings.append({"type": "INFO", "path": crel, "detail": "empty class folder (kept; not deleted)",
                                 "review_required": False})
            if exists:
                root_claimed = crel.lower() in cl
                loose = tree.files_in.get(tree.actual(crel) or crel, [])
                if loose and not root_claimed:
                    findings.append({"type": "REVIEW_REQUIRED", "path": crel, "detail":
                                     f"{len(loose)} file(s) directly in a class folder that no work claims",
                                     "review_required": True})
                if root_claimed and tree.kids(crel):
                    findings.append({"type": "INFO", "path": crel, "detail":
                                     "legacy layout: a work's files sit in the class root while other works use "
                                     "child folders; files are attributed to the deepest claimed folder",
                                     "review_required": False})
        if frel:
            for rel in tree.files_in.get(frel, []):
                findings.append({"type": "UNEXPECTED", "path": rel, "detail": "file directly in the family folder",
                                 "review_required": True})
        else:
            findings.append({"type": "MISSING_FOLDER", "path": frel_expected, "detail":
                             "family folder missing (family folders are never auto-created)",
                             "review_required": True})

        for w in fam["works"]:
            r = wrel(fam, w)
            canon = f"{frel_expected}/{canonical_subpath(w)}"
            exists = bool(r and tree.exists_dir(r))
            conflict = bool(r and not exists and tree.is_file(r))
            wfiles = [rel for rel, (f2, w2) in owner.items() if w2 is w]
            media = [rel for rel in wfiles if tree.files[rel].get("kind") in vault_scan.MEDIA_KINDS]
            legacy = bool(r and exists and r.lower() != canon.lower())
            legacy_kind = None
            if legacy:
                sub = w["vault_subpath"]
                if "/" not in sub:
                    legacy_kind = "class-root"
                elif sub.split("/")[0].lower() != w["material_class"]:
                    legacy_kind = "class-differs"
                else:
                    legacy_kind = "folder-name-differs"
            if w.get("contained_in"):
                status = "CONTAINED"
            elif conflict:
                status = "CONFLICT"
            elif w.get("review_status") != "ACCEPTED":
                status = "REVIEW_REQUIRED"
            elif not r:
                status = "MISSING_FOLDER"
            elif not exists:
                status = "AMBIGUOUS" if w.get("layout_ambiguity") else "MISSING_FOLDER"
            elif legacy:
                status = "LEGACY_MAPPING"
            elif media:
                status = "FOUND"
            else:
                status = "MISSING_CONTENT"
            langs = Counter((tree.files[x].get("archive") or {}).get("language") for x in wfiles)
            hosts = Counter(h for x in wfiles for h in ((tree.files[x].get("archive") or {}).get("web_hosts") or []))
            rows.append({
                "family_id": fam["id"], "family_title": fam["family"], "family_aliases": other_names(fam),
                "medium": w.get("medium"), "work_id": w["id"], "canonical_title": w["titles"].get("canonical") or w["work"],
                "work": w["work"], "aliases": work_titles(w)[1:], "relationship_type": w.get("relation"),
                "material_class": w.get("material_class"), "edition": w.get("edition"),
                "edition_of": w.get("edition_of"), "official_status": w.get("official"),
                "authority_status": w.get("authority"), "origin": w.get("origin"),
                "expected_path": os.path.join(cat.vault_root, *canon.split("/")),
                "local_path": os.path.join(cat.vault_root, *r.split("/")) if r else None,
                "folder_exists": exists, "local_content_exists": bool(media), "local_files": len(wfiles),
                "local_bytes": sum(tree.files[x].get("size", 0) for x in wfiles),
                "languages": sorted(k for k in langs if k), "provenance_hosts": dict(hosts),
                "unofficial_provenance": sorted(h for h in hosts if h in unofficial),
                "coverage_status": None, "confidence": w.get("confidence"),
                "review_required": status in ("REVIEW_REQUIRED", "AMBIGUOUS", "CONFLICT"),
                "legacy_mapping": legacy, "legacy_kind": legacy_kind, "path_origin": w.get("path_origin"),
                "layout_status": status, "contained_in": w.get("contained_in"),
                "layout_ambiguity": w.get("layout_ambiguity"),
            })
            if conflict:
                findings.append({"type": "CONFLICT", "path": r, "detail": "a FILE exists where the work folder should be",
                                 "review_required": True})

        # misplaced content: archive whose series routes uniquely to ANOTHER work
        for rel in fam_files:
            rec = tree.files[rel]
            if vault_scan.is_foreign_payload(rec):
                findings.append({"type": "REVIEW_REQUIRED", "path": rel, "detail":
                                 "not media (software/installer archive) inside the Vault; left untouched — "
                                 "delete it yourself if it is a stray copy", "review_required": True})
                continue
            arch = rec.get("archive") or {}
            s = arch.get("series")
            if not s or rel not in owner:
                continue
            routes = index.get(norm(s), set())
            here = owner[rel]
            if len(routes) == 1:
                fam2, wid2 = next(iter(routes))
                if wid2 != here[1]["id"]:
                    f2 = cat.get_family(fam2)
                    w2 = cat.get_work(f2, wid2) if f2 else None
                    if w2 and wrel(f2, w2):
                        target = f"{wrel(f2, w2)}/{rel.rsplit('/', 1)[-1]}"
                        proposed_moves.append({"source": os.path.join(cat.vault_root, *rel.split("/")),
                                               "target": os.path.join(cat.vault_root, *target.split("/")),
                                               "reason": f"ComicInfo series '{s}' identifies work '{w2['work']}', "
                                                         f"but the file sits in '{here[1]['work']}'",
                                               "collision": tree.is_file(target) or target in tree.files,
                                               "applied": False})
                        findings.append({"type": "REVIEW_REQUIRED", "path": rel, "detail":
                                         f"possibly misplaced: series '{s}' belongs to '{w2['work']}' "
                                         f"(move PROPOSED only, never applied automatically)",
                                         "review_required": True})
        # duplicates within this family
        fam_set = {x.lower() for x in fam_files}
        for g in dup_groups:
            inside = [p for p in g["paths"] if p.lower() in fam_set]
            if len(inside) > 1:
                findings.append({"type": "POSSIBLE_DUPLICATE", "path": inside[0], "detail":
                                 f"{len(inside)} byte-identical files ({g['size'] / 1e6:.1f} MB each): "
                                 + "; ".join(inside) + " — reported only, nothing deleted",
                                 "review_required": False})
        families_out.append({
            "family_id": fam["id"], "family_title": fam["family"], "family_aliases": other_names(fam),
            "category": fam.get("category"), "family_path": os.path.join(cat.vault_root, frel_expected),
            "folder_exists": bool(frel), "primary_medium": fam.get("medium"), "classes": class_info,
            "expected_classes": expected_classes, "works": rows, "findings": findings,
            "folders_created": created_by_family.get(fam["family"], []),
            "files": len(fam_files), "bytes": sum(tree.files[x].get("size", 0) for x in fam_files),
        })

    # cross-family duplicates
    fam_of = {}
    for f in families_out:
        root = f["family_path"]
        fam_of[f["family_title"]] = os.path.relpath(root, cat.vault_root).replace("\\", "/").lower() + "/"
    for g in dup_groups:
        fams = {t for p in g["paths"] for t, pre in fam_of.items() if p.lower().startswith(pre)}
        if len(fams) > 1:
            global_findings.append({"type": "POSSIBLE_DUPLICATE", "path": g["paths"][0], "detail":
                                    "identical bytes in different families: " + "; ".join(g["paths"]),
                                    "review_required": True})

    all_rows = [r for f in families_out for r in f["works"]]
    all_find = [x for f in families_out for x in f["findings"]] + global_findings
    summary = {
        "total_families": len(families_out),
        "total_works": len(all_rows),
        "expected": sum(r["layout_status"] not in ("REVIEW_REQUIRED",) and r["official_status"] is not False
                        for r in all_rows),
        "found": sum(r["layout_status"] == "FOUND" for r in all_rows),
        "legacy_paths_mapped": sum(r["legacy_mapping"] for r in all_rows),
        "missing_folder": sum(r["layout_status"] == "MISSING_FOLDER" for r in all_rows),
        "missing_content": sum(r["layout_status"] == "MISSING_CONTENT" for r in all_rows),
        "contained": sum(r["layout_status"] == "CONTAINED" for r in all_rows),
        "unexpected": sum(x["type"] == "UNEXPECTED" for x in all_find),
        "ambiguous": sum(r["layout_status"] == "AMBIGUOUS" for r in all_rows) + sum(x["type"] == "AMBIGUOUS" for x in all_find),
        "possible_duplicate_groups": sum(x["type"] == "POSSIBLE_DUPLICATE" for x in all_find),
        "duplicate_family_candidates": sum(x["type"] == "DUPLICATE_FAMILY_CANDIDATE" for x in all_find),
        "conflicts": sum(r["layout_status"] == "CONFLICT" for r in all_rows),
        "review_required": sum(r["review_required"] for r in all_rows) + sum(x.get("review_required", False) for x in all_find),
        "proposed_moves": len(proposed_moves),
        "new_folders_created": sum(len(f["folders_created"]) for f in families_out),
        "files": len(tree.files), "bytes": sum(r.get("size", 0) for r in tree.files.values()),
    }
    return {"schema": LAYOUT_SCHEMA, "generated_at": now_iso(), "vault_root": cat.vault_root,
            "summary": summary, "families": families_out, "global_findings": global_findings,
            "proposed_moves": proposed_moves}
