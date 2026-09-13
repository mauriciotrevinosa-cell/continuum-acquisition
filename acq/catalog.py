"""Personal works catalog: load, migrate (v1 -> v2), query, save with backup.

The catalog is PERSONAL DATA (it lives in the data directory, never in this
package). v2 keeps every v1 field untouched and adds metadata:

  family: id, aliases, external_ids
  work:   id, titles{canonical,en,ja,romaji}, aliases, relation, material_class,
          medium, edition, edition_of, language, official, authority,
          confidence (high|medium|low), review_status (ACCEPTED|REVIEW),
          origin (curated|discovered|vault-adopted), external_ids, remote,
          provenance[], path_origin

Curated fields (work, role, vault_subpath, declared_status, candidate_sources,
intake_aliases, availability, notes, contained_in) are never overwritten by
automation. Discovery only ADDS works, aliases, ids and evidence.
"""
from __future__ import annotations

import os
import re

from . import taxonomy
from .util import LOG, backup, norm, now_iso, read_json, similarity, slug, tokens, write_json

SCHEMA = "continuum.personal.acquisition-catalog/2"
CURATED_FIELDS = ("work", "role", "vault_subpath", "declared_status", "candidate_sources", "intake_aliases",
                  "availability", "notes", "contained_in")
_STALE_NOTE = re.compile(r"\s*Vault folder NOT created yet:[^.]*\.")


def _uniq(seq):
    seen, out = set(), []
    for x in seq:
        k = norm(x)
        if x and k and k not in seen:
            seen.add(k)
            out.append(x)
    return out


def work_titles(w: dict) -> list[str]:
    t = w.get("titles") or {}
    return _uniq([w.get("work"), t.get("canonical"), t.get("en"), t.get("ja"), t.get("romaji"),
                  *(w.get("aliases") or []), *(w.get("intake_aliases") or [])])


def family_names(fam: dict) -> list[str]:
    return _uniq([fam.get("family"), fam.get("vault_family_folder"), *(fam.get("aliases") or [])])


def family_tokens(fam: dict) -> set[str]:
    """Words every title of the family tends to share (family names and main
    work titles). Removing them leaves what DISTINGUISHES a work."""
    toks: set[str] = set()
    for n in family_names(fam):
        toks |= tokens(n)
    for w in fam.get("works") or []:
        if w.get("relation") == taxonomy.MAIN_WORK:
            for t in work_titles(w):
                toks |= tokens(t)
    return toks


def distinct_similarity(a: str, b: str, ftoks: set[str]) -> float:
    """Similarity of the distinguishing part of two titles, so that
    "Series: Clayman Revenge" ~ "Clayman's Revenge" while "Series Gaiden" is
    NOT "Series". Two titles with nothing distinctive are the same title."""
    ra, rb = tokens(a) - ftoks, tokens(b) - ftoks
    if not ra and not rb:
        return 1.0
    if not ra or not rb:
        return 0.0
    ja, jb = " ".join(sorted(ra)), " ".join(sorted(rb))
    r = similarity(ja, jb)
    short, long_ = sorted((norm(ja), norm(jb)), key=len)
    if len(short) >= 4 and short in long_:  # "Guide" vs "Guidebook": suspicious, never a sure match
        r = max(r, 0.8)
    if re.findall(r"\d+", norm(ja)) != re.findall(r"\d+", norm(jb)):
        r = min(r, 0.7)
    return r


def migrate_work(fam: dict, w: dict) -> bool:
    changed = False

    def put(key, value):
        nonlocal changed
        if key not in w:
            w[key] = value
            changed = True

    sub = w.get("vault_subpath")
    first = sub.split("/")[0] if sub else None
    relation = w.get("relation") or taxonomy.role_to_relation(w.get("role"))
    medium = w.get("medium") or (first if first in taxonomy.STORY_CLASSES else (fam.get("medium") or "manga"))
    put("id", f"{slug(fam['family'])}/{slug(w['work'])}")
    put("relation", relation)
    put("medium", medium)
    put("material_class", first if first in taxonomy.MATERIAL_CLASSES else taxonomy.material_class(relation, medium))
    put("edition", taxonomy.edition_of([w.get("work")], relation))
    put("edition_of", None)
    put("language", None)
    put("origin", "curated")
    put("official", True)
    put("confidence", "high")
    put("review_status", "ACCEPTED")
    put("titles", {"canonical": w["work"], "en": None, "ja": None, "romaji": None})
    put("aliases", [])
    put("external_ids", {})
    put("remote", {})
    put("provenance", [])
    adaptation = "adaptation" in (w.get("role") or "").lower() and relation == taxonomy.MAIN_WORK
    put("authority", "ADAPTATION" if adaptation else taxonomy.authority(relation, medium, None))
    put("path_origin", "curated" if sub else None)
    if isinstance(w.get("notes"), str) and _STALE_NOTE.search(w["notes"]):
        w["notes"] = _STALE_NOTE.sub("", w["notes"]).strip()
        changed = True
    return changed


def migrate(data: dict) -> bool:
    changed = data.get("schema") != SCHEMA
    for fam in data.get("families", []):
        for key, value in (("id", slug(fam["family"])), ("aliases", []), ("external_ids", {})):
            if key not in fam:
                fam[key] = value
                changed = True
        for w in fam.get("works", []):
            changed |= migrate_work(fam, w)
    data["schema"] = SCHEMA
    return changed


class Catalog:
    def __init__(self, path: str):
        self.path = path
        data = read_json(path)
        if data is None:
            raise FileNotFoundError(f"catalog not found: {path}")
        self.data = data
        self.migrated = migrate(data)
        self.dirty = self.migrated
        self.vault_override: str | None = None  # --vault: in memory only, never saved

    # -- properties ---------------------------------------------------------
    @property
    def vault_root(self) -> str:
        return self.vault_override or self.data["vault_root"]

    @property
    def intake_root(self) -> str:
        return self.data.get("intake_root") or ""

    @property
    def families(self) -> list[dict]:
        return self.data["families"]

    @property
    def sources(self) -> dict:
        return self.data.setdefault("sources", {})

    # -- queries --------------------------------------------------------------
    def family_dir(self, fam: dict) -> str:
        return os.path.join(self.vault_root, fam["vault_family_folder"])

    def work_dest(self, fam: dict, w: dict) -> str | None:
        sub = w.get("vault_subpath")
        return os.path.normpath(os.path.join(self.family_dir(fam), *sub.split("/"))) if sub else None

    def iter_works(self):
        for fam in self.families:
            for w in fam["works"]:
                yield fam, w

    def get_family(self, key: str) -> dict | None:
        k = norm(key)
        for fam in self.families:
            if k in (norm(fam["family"]), norm(fam["vault_family_folder"]), norm(fam.get("id"))):
                return fam
        return None

    def families_matching(self, text: str) -> list[dict]:
        k = norm(text)
        return [f for f in self.families if k and k in {norm(n) for n in family_names(f)}]

    def get_work(self, fam: dict, work_id: str) -> dict | None:
        return next((w for w in fam["works"] if w["id"] == work_id), None)

    def find_work(self, fam: dict, titles, *, external_ids: dict | None = None, material_class: str | None = None,
                  threshold: float = 0.9):
        """Match an incoming item to an existing work: external id, then exact
        title (same material class), then a unique fuzzy match."""
        for key, value in (external_ids or {}).items():
            if value in (None, "", []):
                continue
            for w in fam["works"]:
                if w.get("external_ids", {}).get(key) == value:
                    return w, f"external id {key}", 1.0
        pool = [w for w in fam["works"] if material_class in (None, w.get("material_class"))]
        keys = {norm(t) for t in titles if norm(t)}
        exact = [w for w in pool if keys & {norm(t) for t in work_titles(w)}]
        if len(exact) == 1:
            return exact[0], "exact title", 1.0
        if len(exact) > 1:
            return None, f"ambiguous exact title: {[w['work'] for w in exact]}", 0.0
        scored = sorted(((max((similarity(a, b) for a in titles for b in work_titles(w)), default=0.0), w)
                         for w in pool), key=lambda x: -x[0])
        if scored and scored[0][0] >= threshold and (len(scored) == 1 or scored[1][0] < threshold):
            return scored[0][1], f"fuzzy {scored[0][0]:.2f}", scored[0][0]
        ft = family_tokens(fam)
        scored = sorted(((max((distinct_similarity(a, b, ft) for a in titles for b in work_titles(w)), default=0.0), w)
                         for w in pool), key=lambda x: -x[0])
        if scored and scored[0][0] >= 0.85 and (len(scored) == 1 or scored[1][0] < 0.85):
            return scored[0][1], f"distinctive title {scored[0][0]:.2f}", scored[0][0]
        return None, None, 0.0

    def add_work(self, fam: dict, w: dict) -> dict:
        base = f"{fam['id']}/{slug(w['work'])}"
        ids = {x["id"] for x in fam["works"]}
        wid, n = base, 2
        while wid in ids:
            wid, n = f"{base}-{n}", n + 1
        w["id"] = wid
        migrate_work(fam, w)
        fam["works"].append(w)
        self.dirty = True
        return w

    def story_works(self, fam: dict, material_class: str) -> list[dict]:
        return [w for w in fam["works"] if w.get("material_class") == material_class
                and w.get("relation") not in (taxonomy.FAN_WORK,)]

    def alias_index(self) -> dict[str, set]:
        """norm(title) -> {(family, work_id)}. A family's own names map to its
        main work only when that family has exactly one work in its primary
        class (otherwise a bare family name is ambiguous and must not route)."""
        index: dict[str, set] = {}
        for fam in self.families:
            primary = fam.get("medium") or "manga"
            story = [w for w in fam["works"] if w.get("material_class") == primary]
            for w in fam["works"]:
                if w.get("review_status") != "ACCEPTED" and w.get("origin") != "vault-adopted":
                    continue
                for t in work_titles(w):
                    index.setdefault(norm(t), set()).add((fam["family"], w["id"]))
            if len(story) == 1:
                for n in family_names(fam):
                    index.setdefault(norm(n), set()).add((fam["family"], story[0]["id"]))
        return index

    # -- persistence ----------------------------------------------------------
    def save(self, *, reason: str = "") -> str | None:
        """Write the catalog; the FIRST save of a process makes a timestamped
        backup so every run can be rolled back as a whole."""
        self.data["schema"] = SCHEMA
        self.data["updated_at"] = now_iso()
        bak = None
        if not getattr(self, "_backed_up", False):
            bak = backup(self.path)
            self._backed_up = True
            self.backup_path = bak
        write_json(self.path, self.data)
        self.dirty = False
        LOG.debug("catalog saved (%s); backup %s", reason, bak)
        return bak


def add_alias(target: dict, key: str, value: str) -> bool:
    """Append an alias if it is new (by normalised comparison)."""
    if not value or not norm(value):
        return False
    lst = target.setdefault(key, [])
    existing = {norm(x) for x in lst}
    if isinstance(target.get("titles"), dict):
        existing |= {norm(x) for x in target["titles"].values() if x}
    if "work" in target:
        existing.add(norm(target["work"]))
    if "family" in target:
        existing |= {norm(target["family"]), norm(target.get("vault_family_folder"))}
    if norm(value) in existing:
        return False
    lst.append(value)
    return True
