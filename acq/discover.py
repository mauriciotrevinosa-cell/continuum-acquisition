"""Discovery: find every OFFICIAL work of a family in bibliographic sources.

  * MangaUpdates relation graph, walked from the curated works;
  * NDL (Japan's legal-deposit catalogue): guidebooks, art books, colour and
    deluxe editions, anthologies, novelisations, Japanese volume counts, each
    checked against the family's own publisher;
  * AniList when its API is available (anime adaptations, official links).

A wiki is never used, and never alone. OFFICIAL != MAIN CANON: anything
official is catalogued (low priority is fine, hidden is not). Fan works are
recorded as such and never catalogued. Low-confidence items go to review.
"""
from __future__ import annotations

import json
import os
import re
import time
from collections import deque

from . import taxonomy as tx
from .catalog import add_alias, distinct_similarity, family_names, family_tokens, work_titles
from .http import ProviderUnavailable
from .providers import ndl as ndl_mod
from .util import LOG, has_cjk, has_kana, norm, now_iso, read_json, similarity, write_json

STORY_MEDIA = ("manga", "manhwa", "manhua", "light-novel", "web-novel")
EN_WORDS = {"the", "of", "a", "an", "is", "my", "to", "and", "in", "with", "i", "you", "who", "as", "on", "for", "at",
            "from", "got", "it", "her", "his", "this", "that", "be", "was", "are", "love", "girl", "girls", "time",
            "days", "life", "story", "diary", "daily", "not", "can't", "don't", "really"}
NOVEL_SERIES = re.compile(r"文庫|ノベル|ノベライズ|小説|j books|jump j|novel", re.I)


def _person(name: str) -> str:
    return norm(re.sub(r"\s*\([^)]*\)\s*$", "", name or ""))


def _keep_alias(t: str) -> bool:
    return bool(t) and (t.isascii() or has_cjk(t) or bool(re.search(r"[가-힣]", t)))


def english_title(node: dict) -> str:
    best, score = node["title"], 0
    for t in [node["title"]] + list(node.get("aliases") or []):
        if not t or not t.isascii():
            continue
        s = sum(w in EN_WORDS for w in re.findall(r"[a-z']+", t.lower()))
        if s > score:
            best, score = t, s
    return best


KANJI = re.compile(r"[㐀-鿿]")
#: "Title -SUBTITLE-", "Title (reading)", "Title [x]": decoration a shop or a
#: database adds around the name. The name is what a catalogue indexes.
DECORATION = re.compile(
    r"[\-–—~～][^\-–—~～]{1,24}[\-–—~～]"          # Title -SUBTITLE-
    r"|[（(【\[][^）)】\]]{0,30}[）)】\]]"          # Title(reading), Title[x]: content too,
)                                                   # because a catalogue inlines the furigana


def japanese_candidates(node_or_titles) -> list[str]:
    """Every plausible Japanese title, best first.

    Picking one is a losing game: the kana form is often the READING (which
    no catalogue indexes), and a Han-only string may be the CHINESE title,
    which is indistinguishable by characters alone. So rank them and let the
    caller ask under more than one name.
    """
    aliases = (node_or_titles.get("aliases") or []) if isinstance(node_or_titles, dict) else list(node_or_titles)
    def rank(text: str) -> tuple[int, int]:
        kanji, kana = bool(KANJI.search(text)), bool(has_kana(text))
        score = 3 if (kanji and kana) else 2 if kana else 1  # mixed script reads as Japanese
        return (-score, len(text))
    return sorted({a for a in aliases if a and has_cjk(a)}, key=rank)


def ja_title(node: dict) -> str | None:
    candidates = japanese_candidates(node)
    return candidates[0] if candidates else None


def title_core(text: str | None) -> str:
    """A title with its decoration stripped, for comparison only."""
    return norm(DECORATION.sub(" ", text or ""))


def title_matches(query_core: str, *candidates: str | None) -> bool:
    """True when a record is plausibly the same title as the query.

    Containment runs BOTH ways: a catalogue often holds a shorter form than
    the decorated title a database carries, and vice versa. Three characters
    is the floor, so a one-character record cannot match everything.
    """
    if len(query_core) < 3:
        return False
    for candidate in candidates:
        core = title_core(candidate)
        if not core:
            continue
        if query_core in core or (len(core) >= 3 and core in query_core):
            return True
    return False


def remote_from(node: dict) -> dict:
    return {"provider": node["provider"], "id": node["id"], "url": node.get("url"),
            "latest_chapter": node.get("latest_chapter"), "volumes": node.get("volumes"),
            "completed": node.get("completed"), "status_text": node.get("status_text"), "year": node.get("year"),
            "licensed": node.get("licensed"), "type": node.get("type"), "publishers": node.get("publishers") or [],
            "publications": node.get("publications") or [], "authors": node.get("authors") or [],
            "official_urls": node.get("official_urls") or [], "fetched_at": now_iso()}


class FamilyRun:
    def __init__(self, cat, fam: dict, providers: dict, *, max_nodes: int = 60, max_depth: int = 3,
                 ndl_max: int = 1000):
        self.cat, self.fam = cat, fam
        self.mu = providers.get("mangaupdates")
        self.ndl = providers.get("ndl")
        self.anilist = providers.get("anilist")
        self.max_nodes, self.max_depth, self.ndl_max = max_nodes, max_depth, ndl_max
        self.nodes: dict = {}
        self.fan: list = []
        self.outside: list = []
        self.review: list = []
        self.other_edges: list = []
        self.ndl_groups: list = []
        self.provider_status: dict = {}
        self.outcomes: list = []
        self.family_publishers: set[str] = set(fam.get("publishers_original") or [])

    # -- helpers --------------------------------------------------------------
    def _review(self, title, reason, **extra):
        self.review.append({"family": self.fam["family"], "title": title, "reason": reason, **extra})

    def seeds(self) -> list[dict]:
        return [w for w in self.fam["works"] if w.get("origin") == "curated" and w.get("official") is not False
                and w.get("medium") in STORY_MEDIA and w.get("availability") != "not_confirmed"]

    def resolve_mu(self, w: dict):
        ids = w.setdefault("external_ids", {})
        if ids.get("mangaupdates"):
            return ids["mangaupdates"], "pinned"
        wanted = {norm(t) for t in work_titles(w)}
        pool: dict = {}
        for q in work_titles(w)[:4]:
            for r in self.mu.search(q):
                if r.get("id") and self.mu.type_compatible(r.get("type"), w["medium"]):
                    pool[r["id"]] = r
            if any(norm(r["title"]) in wanted or norm(r.get("hit_title")) in wanted for r in pool.values()):
                break
        exact = [r for r in pool.values() if norm(r["title"]) in wanted or norm(r.get("hit_title")) in wanted]
        if len(exact) > 1:
            exact = [r for r in exact if norm(r["title"]) in wanted] or exact
        if len(exact) == 1:
            return exact[0]["id"], f"exact title match '{exact[0]['title']}'"
        if len(exact) > 1:
            self._review(w["work"], "several MangaUpdates series match this title exactly; pin "
                                    "external_ids.mangaupdates in the catalog",
                         candidates=[f"{r['title']} ({r['year']}) id {r['id']}" for r in exact[:6]])
            return None, "ambiguous"
        scored = sorted(((max(similarity(t, x) for t in work_titles(w) for x in (r["title"], r.get("hit_title") or "")), r)
                         for r in pool.values()), key=lambda s: -s[0])
        if scored and scored[0][0] >= 0.93 and (len(scored) == 1 or scored[1][0] < 0.93):
            return scored[0][1]["id"], f"fuzzy {scored[0][0]:.2f} '{scored[0][1]['title']}'"
        self._review(w["work"], "no MangaUpdates series could be matched with confidence; pin "
                                "external_ids.mangaupdates if one exists",
                     candidates=[f"{r['title']} ({r['year']}, {r['type']}) id {r['id']}" for _, r in scored[:5]])
        return None, "unresolved"

    def child_relation(self, parent_rel: str, etype: str) -> str:
        base = tx.MU_RELATION.get(etype, tx.OTHER_OFFICIAL)
        if base in (tx.SEQUEL, tx.PREQUEL) and parent_rel not in (tx.MAIN_WORK, tx.SEQUEL, tx.PREQUEL):
            return parent_rel
        return base

    def membership(self, node: dict, depth: int, seed_authors: set, names: list[str]):
        if {_person(a) for a in node.get("authors") or []} & seed_authors:
            return True, "shares an author with the family"
        texts = [norm(t) for t in [node["title"]] + list(node.get("aliases") or [])]
        if any(len(n) >= 5 and n in t for n in names for t in texts):
            return True, "title contains a family title"
        if depth <= 1:
            return True, "directly related to a family work"
        return False, "no shared author or title (reached through another series)"

    # -- MangaUpdates -----------------------------------------------------------
    def walk(self):
        seeds = []
        for w in self.seeds():
            try:
                mu_id, how = self.resolve_mu(w)
            except ProviderUnavailable as err:
                self.provider_status["mangaupdates"] = f"unavailable: {err}"
                return
            if mu_id:
                seeds.append((w, mu_id, how))
        self.provider_status["mangaupdates"] = f"ok ({len(seeds)} of {len(self.seeds())} works resolved)"
        names = [norm(n) for n in family_names(self.fam)] + [norm(t) for w in self.seeds() for t in work_titles(w)]
        seed_authors: set[str] = set()
        q = deque((mu_id, w["relation"], 0, None, "seed", w, how) for w, mu_id, how in seeds)
        queued = {s[1] for s in seeds}
        while q and len(self.nodes) < self.max_nodes:
            nid, rel, depth, via, edge, seed_w, how = q.popleft()
            if nid in self.nodes:
                continue
            try:
                node = self.mu.node(nid)
            except ProviderUnavailable as err:
                self.provider_status["mangaupdates"] = f"partial: {err}"
                break
            if not node:
                continue
            rec = {"node": node, "relation": rel, "depth": depth, "via": via, "edge": edge, "seed": seed_w,
                   "resolved_by": how}
            if seed_w is not None:
                seed_authors |= {_person(a) for a in node.get("authors") or []}
                rec["member"], rec["member_why"] = True, "curated work"
                for p in node.get("publishers") or []:
                    if (p.get("type") or "").lower() == "original" and p.get("name"):
                        self.family_publishers.add(p["name"])
            else:
                rec["member"], rec["member_why"] = self.membership(node, depth, seed_authors, names)
            self.nodes[nid] = rec
            if not rec["member"]:
                self.outside.append({"id": nid, "title": node["title"], "via": via, "edge": edge,
                                     "why": rec["member_why"]})
                continue
            if depth >= self.max_depth:
                continue
            for etype, rid, rname in node.get("related") or []:
                if rid in self.nodes or rid in queued:
                    continue
                if etype not in tx.MU_TRAVERSE:
                    if etype == "Doujinshi":
                        self.fan.append({"provider": "mangaupdates", "id": rid, "title": rname,
                                         "via": node["title"], "why": "MangaUpdates 'Doujinshi' relation"})
                    else:
                        self.other_edges.append({"id": rid, "title": rname, "edge": etype, "via": node["title"]})
                    continue
                queued.add(rid)
                q.append((rid, self.child_relation(rel, etype), depth + 1, nid, etype, None, None))
        if q:
            self._review(self.fam["family"], f"relation walk stopped at {self.max_nodes} series; "
                                             f"{len(q)} related series not examined (raise --max-nodes)")

    def classify_node(self, rec: dict):
        node = rec["node"]
        ptype = (node.get("type") or "").lower()
        texts = [node["title"]] + list(node.get("aliases") or [])
        if ptype == "doujinshi":
            return tx.classify(texts, provider_type="Doujinshi")
        rel, why = tx.keyword_relation([node["title"]], tx.STRONG)
        if not rel:
            rel, why = tx.keyword_relation(node.get("aliases") or [],
                                           [x for x in tx.STRONG if x[0] != tx.DELUXE_OR_ALTERNATE_EDITION])
        if rel:
            return rel, why
        if ptype == "artbook":
            return tx.ARTBOOK, "MangaUpdates type Artbook"
        rel, why = rec["relation"], f"MangaUpdates relation '{rec['edge']}'"
        parent = self.nodes.get(rec["via"]) if rec["via"] else None
        if (rel in (tx.OFFICIAL_SPINOFF, tx.ALTERNATE_ADAPTATION) and parent
                and (parent["node"].get("type") or "").lower() == "novel" and ptype in ("manga", "manhwa", "manhua")
                and similarity(node["title"], parent["node"]["title"]) >= 0.9):
            return tx.PARALLEL_ADAPTATION, "manga with the same title as the novel it is related to"
        if rel == tx.OFFICIAL_SPINOFF:
            weak, why2 = tx.keyword_relation(texts, tx.WEAK[:1])
            if weak:
                return weak, why2
        return rel, why

    def _pub_match(self, names) -> bool:
        return any(tx.publisher_matches(n, self.family_publishers) for n in names)

    def merge_node(self, rec: dict) -> None:
        node = rec["node"]
        fam = self.fam
        if rec["seed"] is not None:
            self.update_existing(rec["seed"], node, rec["seed"]["relation"], True, "high", "curated seed", rec)
            self.outcomes.append(("seed", node["title"], rec["seed"]["work"]))
            return
        if not rec.get("member"):
            return
        rel, why = self.classify_node(rec)
        medium = node.get("medium") or "manga"
        if medium == "light-novel" and fam.get("medium") in ("manhwa", "manhua"):
            medium = "web-novel"
        orig = [p["name"] for p in node.get("publishers") or [] if (p.get("type") or "").lower() == "original"]
        if rel == tx.FAN_WORK:
            official = False
        elif orig or node.get("licensed"):
            official = True
        else:
            official = None
        if official and (rec["member_why"].startswith("shares an author") or self._pub_match(orig)) \
                and rel not in (tx.UNKNOWN, tx.OTHER_OFFICIAL):
            confidence = "high"
        elif official:
            confidence = "medium"
        else:
            confidence = "low"
        cls = tx.material_class(rel, medium)
        titles = [node["title"]] + list(node.get("aliases") or [])
        w, how, _ = self.cat.find_work(fam, titles, external_ids={"mangaupdates": node["id"]}, material_class=cls)
        if w is None:
            w2, how2, _ = self.cat.find_work(fam, titles, external_ids={"mangaupdates": node["id"]})
            if w2 is not None and (w2.get("origin") in ("vault-adopted", "discovered") or how2 == "external id mangaupdates"):
                w, how = w2, how2
        if w is not None:
            self.update_existing(w, node, rel, official, confidence, how, rec, medium=medium, why=why)
            self.outcomes.append(("updated", node["title"], w["work"]))
            return
        if rel == tx.FAN_WORK or official is False:
            self.fan.append({"provider": "mangaupdates", "id": node["id"], "title": node["title"],
                             "via": rec["via"], "why": why})
            return
        if confidence == "low":
            self._review(node["title"], f"official status unproven (no original publisher on record); {why}",
                         provider="mangaupdates", url=node.get("url"), suggested_relation=rel)
            return
        # near-duplicate guard, same material class only (a novel and its manga share a title on purpose)
        ft = family_tokens(fam)
        same = [x for x in fam["works"] if x.get("material_class") == cls]
        near = max(((max(distinct_similarity(a, b, ft), similarity(a, b) if similarity(a, b) >= 0.92 else 0.0),
                     x["work"]) for x in same for a in titles[:12] for b in work_titles(x)), default=(0.0, None))
        if near[0] >= 0.75:
            self._review(node["title"], f"similar to existing work '{near[1]}' ({near[0]:.2f}) but not matched "
                                        f"with certainty; not added to avoid a duplicate (pin "
                                        f"external_ids.mangaupdates={node['id']} on the right work)",
                         provider="mangaupdates", url=node.get("url"), suggested_relation=rel)
            return
        en = english_title(node)
        new = {"work": en, "role": None, "declared_status": None, "candidate_sources": [], "intake_aliases": [],
               "availability": "published", "notes": "", "origin": "discovered", "official": official,
               "confidence": confidence, "review_status": "ACCEPTED" if confidence == "high" else "REVIEW",
               "relation": rel, "medium": medium, "material_class": cls,
               "edition": tx.edition_of(titles[:3], rel),
               "titles": {"canonical": node["title"], "en": en, "ja": ja_title(node),
                          "romaji": node["title"] if node["title"].isascii() else None},
               "aliases": [a for a in titles if _keep_alias(a) and norm(a) != norm(en)][:25],
               "external_ids": {"mangaupdates": node["id"]}, "remote": remote_from(node),
               "authority": "PRIMARY_SOURCE" if rec["edge"] == "Adapted From" else tx.authority(rel, medium, None),
               "provenance": [{"at": now_iso(), "source": "mangaupdates", "url": node.get("url"),
                               "evidence": f"'{rec['edge']}' of '{self.nodes[rec['via']]['node']['title'] if rec['via'] in self.nodes else '?'}'"
                                           f"; {rec['member_why']}; classified by {why}"}]}
        if rel == tx.COLORED_EDITION and rec["via"] in self.nodes:
            base = self.cat.find_work(fam, [self.nodes[rec["via"]]["node"]["title"]])[0]
            new["edition_of"] = base["id"] if base else None
        self.cat.add_work(fam, new)
        self.outcomes.append(("added", node["title"], new["work"]))

    def update_existing(self, w, node, rel, official, confidence, how, rec, *, medium=None, why=""):
        ids = w.setdefault("external_ids", {})
        if node["provider"] == "mangaupdates" and not ids.get("mangaupdates"):
            ids["mangaupdates"] = node["id"]
        elif node["provider"] == "anilist" and not ids.get("anilist"):
            ids["anilist"] = node["id"]
        t = w.setdefault("titles", {"canonical": w["work"]})
        better_ja = ja_title(node)
        # Replace a stored READING with the real title: an earlier pass could
        # store the kana pronunciation, which no Japanese catalogue indexes.
        if better_ja and (not t.get("ja")
                          or (not KANJI.search(t["ja"] or "") and KANJI.search(better_ja))):
            t["ja"] = better_ja
        if not t.get("en"):
            t["en"] = english_title(node)
        if not t.get("romaji") and node["title"].isascii():
            t["romaji"] = node["title"]
        for a in [node["title"]] + list(node.get("aliases") or [])[:25]:
            if _keep_alias(a):
                add_alias(w, "aliases", a)
        if node["provider"] == "mangaupdates" or not w.get("remote"):
            ndl_keep = (w.get("remote") or {}).get("ndl")
            w["remote"] = remote_from(node)
            if ndl_keep:
                w["remote"]["ndl"] = ndl_keep
        prov = w.setdefault("provenance", [])
        if not any(p.get("source") == node["provider"] and p.get("url") == node.get("url") for p in prov):
            prov.append({"at": now_iso(), "source": node["provider"], "url": node.get("url"),
                         "evidence": f"matched by {how}" + (f"; {why}" if why else "")})
        if w.get("origin") in ("vault-adopted", "discovered"):
            if w.get("origin") == "vault-adopted" or w.get("relation") == tx.UNKNOWN:
                w["relation"] = rel
                w["material_class"] = tx.material_class(rel, medium or w.get("medium") or "manga")
                w["authority"] = tx.authority(rel, medium or w.get("medium"), None)
            if official is not None:
                w["official"] = official
            if confidence == "high" and (how.startswith("external id") or how == "exact title"):
                w["confidence"], w["review_status"] = "high", "ACCEPTED"
        if rec is not None and rec.get("seed") is not None and w["relation"] == tx.MAIN_WORK \
                and w.get("material_class") == (self.fam.get("medium") or "manga"):
            for a in [node["title"]] + list(node.get("aliases") or []):
                if _keep_alias(a):
                    add_alias(self.fam, "aliases", a)
        self.cat.dirty = True

    # -- NDL ----------------------------------------------------------------------
    def main_ja(self):
        primary = self.fam.get("medium") or "manga"
        ordered = sorted(self.fam["works"], key=lambda w: (w.get("relation") != tx.MAIN_WORK,
                                                           w.get("material_class") != primary))
        for w in ordered:
            ja = (w.get("titles") or {}).get("ja")
            if ja and w.get("official") is not False:
                return w, ja
        return None, None

    def ndl_pass(self):
        if not self.ndl:
            return
        main, ja = self.main_ja()
        if not ja:
            self.provider_status["ndl"] = "skipped (no Japanese title known yet)"
            return
        # Ask under every name the work is actually sold under: the Japanese
        # title, and the Latin one when the series carries a Latin name in
        # Japan too (plenty do).
        queries: list[str] = []
        others = [a for a in japanese_candidates(main.get("aliases") or []) if norm(a) != norm(ja)][:2]
        for candidate in (ja, *others, (main.get("titles") or {}).get("en"), main.get("work")):
            cleaned = re.sub(r"[\s　]+", " ", candidate or "").strip()
            if not cleaned or len(title_core(cleaned)) < 3:
                continue
            # Spacing is not cosmetic to a catalogue: Japanese titles are
            # written without spaces, and a database indexes the exact
            # string. "X -Y-" and "X-Y-" are different searches, so ask both
            # - which means comparing the literal text, not a normalised
            # form that would treat them as one.
            for variant in (cleaned, re.sub(r"[\s　]+", "", cleaned)):
                if variant not in queries and (variant == cleaned or has_cjk(variant)):
                    queries.append(variant)
        queries = queries[:4]
        recs: list[dict] = []
        seen_links: set[str] = set()
        for query in queries:
            try:
                found = self.ndl.search_books(query, max_records=self.ndl_max)
            except ProviderUnavailable as err:
                self.provider_status["ndl"] = f"unavailable: {err}"
                return
            for record in found:
                key = record.get("link") or json.dumps(record, sort_keys=True, default=str)
                if key not in seen_links:
                    seen_links.add(key)
                    recs.append(record)
        query = queries[0]
        books = [r for r in recs if ndl_mod.is_book(r)]
        cores = [title_core(q) for q in queries]
        groups: dict[str, list] = {}
        for r in books:
            base = ndl_mod.base_title(r)
            if not any(title_matches(core, base, r.get("series_title")) for core in cores):
                continue
            groups.setdefault(norm(base), []).append(r)
        added = attached = ignored = 0
        for items in groups.values():
            title = ndl_mod.base_title(items[0])
            pubs = sorted({p for r in items for p in r.get("publishers") or []})
            series = sorted({r["series_title"] for r in items if r.get("series_title")})
            vols = sorted({v for r in items for v in [ndl_mod.volume_number(r)] if v})
            issued = sorted(r["issued"] for r in items if r.get("issued"))
            info = {"title": title, "publishers": pubs, "series": series,
                    "isbns": sorted({i for r in items for i in r.get("isbn") or []}),
                    "volumes": len(vols) or len(items), "volume_numbers": vols, "items": len(items),
                    "first_issued": issued[0] if issued else None, "last_issued": issued[-1] if issued else None,
                    "links": [r["link"] for r in items[:3] if r.get("link")]}
            official_pub = self._pub_match(pubs)
            rel, why = tx.keyword_relation([title] + series, tx.STRONG)
            group = dict(info, official_publisher=official_pub, relation=rel, why=why)
            self.ndl_groups.append(group)
            target = self.match_ja(title, pubs)
            if target is not None and (not rel or target.get("relation") == rel):
                self.attach_ndl(target, info)
                group["outcome"] = f"attached to '{target['work']}'"
                attached += 1
                continue
            medium = "manga"
            note = ""
            if not rel:
                if not official_pub:
                    group["outcome"] = "ignored (not the family's publisher, no supplemental keyword)"
                    ignored += 1
                    continue
                if any(NOVEL_SERIES.search(s) for s in series):
                    rel, medium, note = tx.OFFICIAL_SPINOFF, "light-novel", "novelisation (NDL series: " + "; ".join(series) + ")"
                else:
                    rel, note = tx.OTHER_OFFICIAL, "Japanese book by the family's publisher; relation unclassified"
                confidence = "medium"
            else:
                confidence = "high" if official_pub else "low"
            if confidence == "low":
                self._review(title, f"{rel} by a publisher that is not the family's ({', '.join(pubs) or 'unknown'}): "
                                    f"possibly an unofficial third-party book", provider="ndl",
                             url=(info["links"] or [None])[0], suggested_relation=rel)
                group["outcome"] = "review (publisher mismatch)"
                continue
            cls = tx.material_class(rel, medium)
            w, how, _ = self.cat.find_work(self.fam, [title], material_class=cls)
            if w is not None:
                self.attach_ndl(w, info)
                group["outcome"] = f"attached to '{w['work']}'"
                attached += 1
                continue
            new = {"work": title, "role": None, "declared_status": None, "candidate_sources": [], "intake_aliases": [],
                   "availability": "published", "notes": note, "origin": "discovered", "official": True,
                   "confidence": confidence, "review_status": "ACCEPTED" if confidence == "high" else "REVIEW",
                   "relation": rel, "medium": medium, "material_class": cls, "edition": tx.edition_of([title], rel),
                   "titles": {"canonical": title, "en": None, "ja": title, "romaji": None}, "aliases": [],
                   "external_ids": {"isbn": info["isbns"][:50]}, "remote": {"ndl": dict(info, fetched_at=now_iso())},
                   "language": "ja",
                   "provenance": [{"at": now_iso(), "source": "ndl", "url": (info["links"] or [None])[0],
                                   "evidence": f"National Diet Library: {len(items)} record(s), publisher "
                                               f"{', '.join(pubs)}; classified by {why or note}"}]}
            if rel in (tx.COLORED_EDITION, tx.DELUXE_OR_ALTERNATE_EDITION) and main is not None:
                new["edition_of"] = main["id"]
            self.cat.add_work(self.fam, new)
            group["outcome"] = f"added ({confidence})"
            self.outcomes.append(("added", title, title))
            added += 1
        self.provider_status["ndl"] = (f"ok: {len(books)} books, {len(groups)} titles; {added} added, "
                                       f"{attached} attached, {ignored} ignored")

    def match_ja(self, title: str, pubs):
        k = norm(title)
        hits = [w for w in self.fam["works"]
                if any(norm(t) == k for t in work_titles(w) if has_cjk(t))]
        if len(hits) > 1:
            by_pub = [w for w in hits if any(tx.publisher_matches(p, [x["name"] for x in (w.get("remote") or {}).get(
                "publishers") or [] if (x.get("type") or "").lower() == "original"]) for p in pubs)]
            hits = by_pub if len(by_pub) == 1 else []
        return hits[0] if len(hits) == 1 else None

    def attach_ndl(self, w: dict, info: dict) -> None:
        remote = w.setdefault("remote", {})
        old = remote.get("ndl") or {}
        merged = dict(info)
        merged["isbns"] = sorted(set(old.get("isbns") or []) | set(info["isbns"]))
        merged["fetched_at"] = now_iso()
        remote["ndl"] = merged
        if info["isbns"]:
            ids = w.setdefault("external_ids", {})
            ids["isbn"] = sorted(set(ids.get("isbn") or []) | set(info["isbns"]))[:200]
        self.cat.dirty = True

    # -- AniList (optional) -------------------------------------------------------
    def anilist_pass(self):
        if not self.anilist:
            return
        main, _ = self.main_ja()
        if main is None:
            return
        try:
            hits = self.anilist.search(main["work"])
        except ProviderUnavailable as err:
            self.provider_status["anilist"] = f"unavailable: {err}"
            return
        wanted = {norm(t) for t in work_titles(main)}
        added = 0
        for h in hits:
            if tx.ANILIST_FORMAT_MEDIUM.get((h.get("type") or "").upper()) != "anime":
                continue
            if norm(h["title"]) not in wanted and norm(h.get("hit_title")) not in wanted:
                continue
            try:
                node = self.anilist.node(h["id"])
            except ProviderUnavailable as err:
                self.provider_status["anilist"] = f"partial: {err}"
                break
            if not node or node.get("not_yet_released"):
                continue
            titles = [node["title"]] + node["aliases"]
            w, how, _ = self.cat.find_work(self.fam, titles, external_ids={"anilist": node["id"]},
                                           material_class="anime")
            if w is not None:
                self.update_existing(w, node, w["relation"], True, "high", how, None)
                continue
            new = {"work": node["title"], "role": None, "declared_status": None, "candidate_sources": [],
                   "intake_aliases": [], "availability": "published", "notes": f"{node['type']}", "origin": "discovered",
                   "official": True, "confidence": "high", "review_status": "ACCEPTED",
                   "relation": tx.PARALLEL_ADAPTATION, "medium": "anime", "material_class": "anime",
                   "edition": "standard", "titles": {"canonical": node["title"], "en": node["title"],
                                                     "ja": ja_title(node), "romaji": None},
                   "aliases": [a for a in node["aliases"] if _keep_alias(a)],
                   "external_ids": {"anilist": node["id"]}, "remote": remote_from(node), "authority": "ADAPTATION",
                   "provenance": [{"at": now_iso(), "source": "anilist", "url": node.get("url"),
                                   "evidence": "anime with the main work's exact title"}]}
            self.cat.add_work(self.fam, new)
            added += 1
        self.provider_status["anilist"] = f"ok ({added} anime works added)"

    # -- run ------------------------------------------------------------------------
    def run(self) -> dict:
        started = time.monotonic()
        if self.mu:
            self.walk()
            for rec in list(self.nodes.values()):
                self.merge_node(rec)
            if self.family_publishers:
                self.fam["publishers_original"] = sorted(self.family_publishers)
        self.ndl_pass()
        self.anilist_pass()
        for e in self.other_edges:
            self._review(e["title"], f"MangaUpdates relation '{e['edge']}' of '{e['via']}' is not a known "
                                     f"relation type; not followed", provider="mangaupdates")
        return {
            "family": self.fam["family"], "family_id": self.fam["id"], "generated_at": now_iso(), "complete": True,
            "seconds": round(time.monotonic() - started, 1), "providers": self.provider_status,
            "family_publishers": sorted(self.family_publishers),
            "nodes": [{"id": k, "title": r["node"]["title"], "type": r["node"].get("type"),
                       "medium": r["node"].get("medium"), "relation_walk": r["relation"], "edge": r["edge"],
                       "depth": r["depth"], "member": r.get("member"), "member_why": r.get("member_why"),
                       "resolved_by": r.get("resolved_by"), "url": r["node"].get("url"),
                       "latest_chapter": r["node"].get("latest_chapter"), "status": r["node"].get("status_text")}
                      for k, r in self.nodes.items()],
            "outcomes": [{"outcome": o, "title": t, "work": w} for o, t, w in self.outcomes],
            "fan_works": self.fan, "outside_family": self.outside, "review": self.review,
            "ndl_groups": self.ndl_groups,
        }


def run(cat, providers: dict, data_dir: str, *, families=None, refresh: bool = False, max_age_days: float = 7,
        max_nodes: int = 60, max_depth: int = 3) -> list[dict]:
    out_dir = os.path.join(data_dir, "discovered")
    results = []
    selected = [f for f in cat.families if not families or any(
        norm(x) in (norm(f["family"]), norm(f["vault_family_folder"]), norm(f["id"])) for x in families)]
    for i, fam in enumerate(selected, 1):
        path = os.path.join(out_dir, f"{fam['id']}.json")
        prev = read_json(path)
        if prev and prev.get("complete") and not refresh:
            age = time.time() - os.path.getmtime(path)
            if age < max_age_days * 86400:
                LOG.info("[%d/%d] %s: discovered %.1f h ago, reusing (use --refresh to redo)", i, len(selected),
                         fam["family"], age / 3600)
                results.append(prev)
                continue
        LOG.info("[%d/%d] %s: discovering...", i, len(selected), fam["family"])
        res = FamilyRun(cat, fam, providers, max_nodes=max_nodes, max_depth=max_depth).run()
        write_json(path, res)
        if cat.dirty:
            cat.save(reason=f"discover {fam['family']}")
        added = sum(o["outcome"] == "added" for o in res["outcomes"])
        LOG.info("      %s | added %d, review %d, fan works %d", "; ".join(f"{k}: {v}" for k, v in
                                                                      res["providers"].items()), added,
                 len(res["review"]), len(res["fan_works"]))
        results.append(res)
    return results
