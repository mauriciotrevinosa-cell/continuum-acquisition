"""Tests for the acquisition orchestrator. Fake Vaults in temp dirs ONLY.

Run:  python -m unittest discover -s C:\\ContinuumTools\\acquisition\\tests -v
"""
from __future__ import annotations

import functools
import hashlib
import http.server
import json
import os
import shutil
import socketserver
import sys
import tempfile
import threading
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from acq import (acquire, coverage, discover, ingest, layout, report, scaffold, sources, taxonomy as tx,  # noqa: E402
                 updates, util, vault_scan)
from acq.catalog import Catalog  # noqa: E402
from acq.http import HttpClient, ProviderUnavailable  # noqa: E402
from acq.providers.mangaupdates import MangaUpdates, summarize  # noqa: E402


# ---------------------------------------------------------------------------
def make_zip(path, chapters=(), series=None, web=None, lang="en", extra=None, salt="", count=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for ch in chapters:
            z.writestr(f"Ch{ch}/001.jpg", f"img{ch}{salt}".encode())
            if series:
                xml = f"<ComicInfo><Series>{series}</Series><LanguageISO>{lang}</LanguageISO>"
                if count is not None:
                    xml += f"<Count>{count}</Count>"
                if web:
                    xml += f"<Web>{web}</Web>"
                z.writestr(f"Ch{ch}/ComicInfo.xml", xml + "</ComicInfo>")
        for name, data in (extra or {}).items():
            z.writestr(name, data)


def write(path, data: bytes):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


def snapshot(root):
    out = {}
    for r, _d, fs in os.walk(root):
        for f in fs:
            p = os.path.join(r, f)
            st = os.stat(p)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, root)] = (st.st_size, st.st_mtime_ns, hashlib.sha256(fh.read()).hexdigest())
    return out


def dirs(root):
    return sorted(os.path.relpath(os.path.join(r, d), root) for r, ds, _ in os.walk(root) for d in ds)


W = lambda work, sub, role="main", **kw: dict({"work": work, "role": role, "vault_subpath": sub,  # noqa: E731
                                               "declared_status": None, "candidate_sources": [], "intake_aliases": [],
                                               "availability": "published", "notes": ""}, **kw)

FAMILIES = [
    {"order": 1, "family": "Alpha Saga", "vault_family_folder": "Alpha Saga", "medium": "manga", "category": "LIKE",
     "works": [W("Alpha Saga", "manga", candidate_sources=[{"source": "viz", "confidence": "likely"}],
                 intake_aliases=["Arufa Saga"])]},
    {"order": 2, "family": "Beta Days", "vault_family_folder": "Beta Days", "medium": "manga", "category": "FAVORITE",
     "works": [W("Beta Days", "manga/Beta Days"), W("Beta Holidays", "manga/Beta Holidays", "official spin-off")]},
    {"order": 3, "family": "Gamma", "vault_family_folder": "Gamma", "medium": "manga",
     "works": [W("Gamma Saga", "manga")]},
    {"order": 4, "family": "Delta Blocked", "vault_family_folder": "Delta Blocked", "medium": "manga",
     "works": [W("Delta Blocked", None, availability="not_confirmed")]},
]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="acqtest-")
        self.vault = os.path.join(self.tmp, "vault")
        self.data = os.path.join(self.tmp, "data")
        self.intake = os.path.join(self.tmp, "intake")
        os.makedirs(self.data)
        v = self.vault
        make_zip(f"{v}/Alpha Saga/manga/Alpha-Saga-part-01.zip", ["0001", "0002", "0003"], "Alpha Saga",
                 "https://comix.to/title/x/1")
        make_zip(f"{v}/Alpha Saga/manga/Alpha-Saga-part-02.zip", ["0005", "0006"], "Alpha Saga")
        os.makedirs(f"{v}/Alpha Saga/fan-art")
        os.makedirs(f"{v}/Alpha Saga/anime")
        make_zip(f"{v}/Beta Days/manga/Beta Days/b1.zip", ["0001", "0002"], "Beta Days")
        shutil.copy2(f"{v}/Beta Days/manga/Beta Days/b1.zip", f"{v}/Beta Days/manga/Beta Days/b1 copy.zip")
        make_zip(f"{v}/Beta Days/manga/Beta Days/holiday.zip", ["0001"], "Beta Holidays", salt="h")
        make_zip(f"{v}/Beta Days/manga/Beta Days Comic Anthology/anth.zip", ["0001"], "Beta Days Comic Anthology")
        os.makedirs(f"{v}/Beta Days/manga/Beta Holidays")
        make_zip(f"{v}/Gamma/manga/Gamma Saga MS/g.zip", ["0001"], "Gamma Saga")
        make_zip(f"{v}/Gamma/manga/XOXO/x.zip", ["0001"], "Gamma Saga XOXO")
        make_zip(f"{v}/Gamma/manga/installer.zip", extra={"app/thing.exe": b"MZ", "app/lib.dll": b"MZ"})
        os.makedirs(f"{v}/Delta Blocked/manga")
        make_zip(f"{v}/Alpha-Saga/stray.zip", ["0001"], "Alpha Saga", salt="stray")
        self.cat_path = os.path.join(self.data, "works-catalog.json")
        with open(self.cat_path, "w", encoding="utf-8") as fh:
            json.dump({"schema": "continuum.personal.acquisition-catalog/1", "vault_root": self.vault,
                       "intake_root": os.path.join(self.intake, "HaruNeko"), "families": FAMILIES,
                       "sources": {"viz": {"name": "VIZ", "url": "https://www.viz.com", "delivery": "x"}}}, fh)
        self.before = snapshot(self.vault)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def assertVaultFilesUntouched(self):
        now = snapshot(self.vault)
        for rel, meta in self.before.items():
            self.assertEqual(now.get(rel), meta, f"Vault file changed or vanished: {rel}")

    def state(self, cat=None):
        cat = cat or Catalog(self.cat_path)
        idx = vault_scan.scan_vault(self.vault, os.path.join(self.data, "vault-index.json"))
        tree = layout.Tree(self.vault, idx)
        adopted = layout.adopt_vault_families(cat, tree)
        adopted += layout.adopt_vault_folders(cat, tree)
        layout.harvest_aliases(cat, tree)
        layout.plan_paths(cat, tree)
        lay = layout.build_layout(cat, tree, unofficial_hosts=["comix.to"])
        cov = coverage.compute(cat, tree)
        return cat, idx, tree, lay, cov, adopted


# ---------------------------------------------------------------------------
class TestAdoptNewFamilies(Base):
    """A folder the user dropped in becomes a library entry, not a warning."""

    def test_a_new_top_level_folder_becomes_a_family(self):
        v = self.vault
        make_zip(f"{v}/Epsilon Tales/manga/Epsilon-Tales-part-01.zip", ["0001", "0002"], "Epsilon Tales")
        make_zip(f"{v}/Epsilon Tales/manga/Epsilon-Tales-part-02.zip", ["0003"], "Epsilon Tales")
        os.makedirs(f"{v}/Epsilon Tales/anime")
        cat, _idx, _tree, lay, _cov, adopted = self.state()

        fam = cat.get_family("Epsilon Tales")
        self.assertIsNotNone(fam, "a new Vault folder must enter the catalog")
        self.assertEqual(fam["vault_family_folder"], "Epsilon Tales", "the folder is never renamed")
        self.assertEqual(fam["origin"], "vault-adopted")
        self.assertEqual(fam["review_status"], "REVIEW", "adopted, but still the user's to confirm")
        self.assertIsNone(fam["category"], "a category is the user's judgement, not ours")
        self.assertTrue(any(c["action"] == "ADOPT_FAMILY" and c["family"] == "Epsilon Tales"
                            for c in adopted))
        self.assertTrue(fam["works"], "its works are adopted in the same pass")
        for w in fam["works"]:
            self.assertIsNone(w["official"], "nothing is declared official by assumption")
            self.assertEqual(w["review_status"], "REVIEW")
        paths = [g["path"] for g in lay["global_findings"] if g["type"] == "UNEXPECTED"]
        self.assertNotIn("Epsilon Tales", paths, "adopted, so no longer an unexplained folder")
        self.assertVaultFilesUntouched()

    def test_a_folder_that_looks_like_a_known_family_is_left_for_the_user(self):
        """Alpha-Saga next to Alpha Saga: a merge by guess buries one of them."""
        cat, _idx, _tree, lay, _cov, adopted = self.state()
        self.assertFalse([c for c in adopted if c["action"] == "ADOPT_FAMILY"
                          and c["family"] == "Alpha-Saga"])
        self.assertNotIn("Alpha-Saga", [f["vault_family_folder"] for f in cat.families])
        kinds = {g["type"] for g in lay["global_findings"] if g["path"] == "Alpha-Saga"}
        self.assertEqual(kinds, {"DUPLICATE_FAMILY_CANDIDATE"})

    def test_an_empty_or_private_folder_is_not_a_series(self):
        v = self.vault
        os.makedirs(f"{v}/Zeta Empty/manga")
        os.makedirs(f"{v}/_scratch/manga")
        make_zip(f"{v}/_scratch/manga/s.zip", ["0001"], "Scratch")
        cat, _idx, _tree, _lay, _cov, adopted = self.state()
        adopted_families = {c["family"] for c in adopted if c["action"] == "ADOPT_FAMILY"}
        self.assertNotIn("Zeta Empty", adopted_families, "no media: nothing to adopt")
        self.assertNotIn("_scratch", adopted_families, "a leading underscore is not a series")
        self.assertNotIn("_scratch", [f["vault_family_folder"] for f in cat.families])

    def test_the_spelling_on_disk_wins_and_the_real_title_becomes_an_alias(self):
        v = self.vault
        make_zip(f"{v}/Eta Chronicals/manga/e.zip", ["0001"], "Eta Chronicles")
        cat, _idx, _tree, lay, _cov, _adopted = self.state()
        fam = cat.get_family("Eta Chronicals")
        self.assertEqual(fam["family"], "Eta Chronicals", "the misspelling is the user's folder")
        self.assertIn("Eta Chronicles", fam["aliases"], "the real title is searchable anyway")
        # An adopted family is named after its folder, so the two names
        # collapse into one - the alias must survive that.
        row = next(f for f in lay["families"] if f["family_title"] == "Eta Chronicals")
        self.assertEqual(row["family_aliases"], ["Eta Chronicles"])
        self.assertVaultFilesUntouched()

    def test_adoption_scales_with_the_library_and_not_with_the_code(self):
        v = self.vault
        for n in range(12):
            make_zip(f"{v}/Series {n:02d} Unique/manga/s.zip", ["0001"], f"Series {n:02d} Unique")
        cat, _idx, _tree, _lay, _cov, adopted = self.state()
        self.assertEqual(len([c for c in adopted if c["action"] == "ADOPT_FAMILY"]), 12)
        ids = [f["id"] for f in cat.families]
        self.assertEqual(len(ids), len(set(ids)), "family ids stay unique")


# ---------------------------------------------------------------------------
class TestTaxonomy(unittest.TestCase):
    def test_keywords_and_relations(self):
        self.assertEqual(tx.classify(["デモアルファ アンソロジー"])[0], tx.OFFICIAL_ANTHOLOGY)
        self.assertEqual(tx.classify(["Demo Alpha Full Color"])[0], tx.COLORED_EDITION)
        self.assertEqual(tx.classify(["デモアルファ 公式キャラクターブック"])[0], tx.GUIDEBOOK)
        self.assertEqual(tx.classify(["Demo Alpha Illustrations"])[0], tx.ARTBOOK)
        self.assertEqual(tx.classify(["Demo Alpha dj - Fan Thing"], provider_type="Doujinshi")[0], tx.FAN_WORK)
        self.assertEqual(tx.classify(["デモアルファ 公式同人誌"], provider_type="Doujinshi")[0], tx.OFFICIAL_DOUJIN)
        self.assertEqual(tx.classify(["デモアルファ 同人版"])[0], tx.OFFICIAL_DOUJIN)
        self.assertEqual(tx.classify(["Side"], provider_relation="Side Story", relation_map=tx.MU_RELATION)[0],
                         tx.OFFICIAL_SPINOFF)
        self.assertEqual(tx.material_class(tx.OFFICIAL_ANTHOLOGY, "manga"), "anthology")
        self.assertEqual(tx.material_class(tx.SEQUEL, "manhwa"), "manhwa")
        self.assertEqual(tx.edition_of(["デモアルファ カラー版"]), "official-colored")
        self.assertTrue(tx.publisher_matches("集英社", ["Shueisha"]))
        self.assertFalse(tx.publisher_matches("謎出版", ["Shueisha"]))

    def test_names_and_similarity(self):
        self.assertEqual(util.safe_folder_name("Re:Zero? <x>"), "Re - Zero x")
        self.assertEqual(util.norm("Clayman's Revenge"), util.norm("Claymans Revenge"))
        self.assertTrue(util.norm("転生したら"))  # kana survive normalisation
        self.assertGreaterEqual(util.similarity("Demo Alpha Comic Anthology", "Demo Alpha Anthology Comic"), 0.99)
        self.assertLessEqual(util.similarity("Series 2", "Series"), 0.7)


class TestScanLayoutCoverage(Base):
    def test_scan_is_read_only_cached_and_detects(self):
        idx_path = os.path.join(self.data, "vault-index.json")
        idx = vault_scan.scan_vault(self.vault, idx_path)
        rec = idx["files"]["Alpha Saga/manga/Alpha-Saga-part-01.zip"]
        self.assertEqual(rec["archive"]["chapters"], ["1", "2", "3"])
        self.assertEqual(rec["archive"]["series"], "Alpha Saga")
        self.assertEqual(rec["archive"]["web_hosts"], ["comix.to"])
        self.assertEqual(len(rec["sha256"]), 64)
        dups = vault_scan.duplicate_groups(idx)
        self.assertEqual(len(dups), 1)
        self.assertEqual(len(dups[0]["paths"]), 2)
        self.assertTrue(vault_scan.is_foreign_payload(idx["files"]["Gamma/manga/installer.zip"]))
        again = vault_scan.scan_vault(self.vault, idx_path)
        self.assertEqual(again["stats"]["hashed"], 0)
        self.assertEqual(again["stats"]["reused"], again["stats"]["files"])
        self.assertVaultFilesUntouched()

    def test_layout_adopts_maps_and_flags(self):
        cat, idx, tree, lay, cov, adopted = self.state()
        acts = {(c["action"], c["work"]) for c in adopted}
        self.assertIn(("REPOINT", "Gamma Saga"), acts)
        self.assertIn(("ADOPT", "XOXO"), acts)
        self.assertIn(("ADOPT", "Beta Days Comic Anthology"), acts)
        rows = {r["work"]: r for f in lay["families"] for r in f["works"]}
        self.assertEqual(rows["Alpha Saga"]["layout_status"], "LEGACY_MAPPING")
        self.assertEqual(rows["Alpha Saga"]["legacy_kind"], "class-root")
        self.assertEqual(rows["Alpha Saga"]["unofficial_provenance"], ["comix.to"])
        self.assertEqual(rows["Gamma Saga"]["local_path"], os.path.join(self.vault, "Gamma", "manga", "Gamma Saga MS"))
        self.assertEqual(rows["Delta Blocked"]["local_path"], None)  # curated without destination stays so
        anth = rows["Beta Days Comic Anthology"]
        self.assertEqual(anth["relationship_type"], tx.OFFICIAL_ANTHOLOGY)
        self.assertEqual(anth["material_class"], "anthology")
        self.assertEqual(anth["layout_status"], "REVIEW_REQUIRED")
        self.assertTrue(anth["legacy_mapping"])
        self.assertEqual(rows["Beta Holidays"]["layout_status"], "MISSING_CONTENT")
        g = lay["global_findings"]
        self.assertTrue(any(x["type"] == "DUPLICATE_FAMILY_CANDIDATE" and x["path"] == "Alpha-Saga" for x in g))
        self.assertEqual(len(lay["proposed_moves"]), 1)
        self.assertIn("Beta Holidays", lay["proposed_moves"][0]["target"])
        gam = next(f for f in lay["families"] if f["family_title"] == "Gamma")
        self.assertTrue(any("installer" in x["detail"] or "not media" in x["detail"] for x in gam["findings"]))
        beta = next(f for f in lay["families"] if f["family_title"] == "Beta Days")
        self.assertTrue(any(x["type"] == "POSSIBLE_DUPLICATE" for x in beta["findings"]))
        # coverage
        by = {c["work"]: c for c in cov.values()}
        self.assertEqual(by["Alpha Saga"]["status"], "PARTIAL")
        self.assertEqual(by["Alpha Saga"]["gaps"], [4])
        self.assertEqual(by["Beta Holidays"]["status"], "MISSING")
        self.assertEqual(by["Delta Blocked"]["status"], "BLOCKED")
        self.assertIn("DUPLICATE_CANDIDATE", by["Beta Days"]["flags"])
        self.assertVaultFilesUntouched()

    def test_numbering_gaps_are_not_missing_chapters(self):
        """A skipped NUMBER is not a skipped chapter.

        Series skip integers and insert decimals, so counting is what
        separates "the numbering jumps" from "you are missing something".
        """
        make_zip(f"{self.vault}/Beta Days/manga/Beta Holidays/h1.zip", ["0001", "0002", "0005"],
                 "Beta Holidays", count=3)
        cat, idx, tree, lay, cov, _ = self.state()
        got = next(v for v in cov.values() if v["work"] == "Beta Holidays")
        self.assertEqual(got["status"], "COMPLETE")
        self.assertIn("NUMBERING_GAPS", got["flags"])
        self.assertEqual(got["gaps"], [3, 4], "the gap is still reported, it just is not a shortfall")
        self.assertIn("nothing is missing", got["reason"])

    def test_holding_fewer_chapters_than_the_release_lists_is_partial(self):
        make_zip(f"{self.vault}/Beta Days/manga/Beta Holidays/h1.zip", ["0001", "0002", "0005"],
                 "Beta Holidays", count=9)
        cat, idx, tree, lay, cov, _ = self.state()
        got = next(v for v in cov.values() if v["work"] == "Beta Holidays")
        self.assertEqual(got["status"], "PARTIAL")
        self.assertIn("3 held of 9", got["reason"])

    def test_contiguous_but_short_of_the_release_count(self):
        make_zip(f"{self.vault}/Beta Days/manga/Beta Holidays/h1.zip", ["0001", "0002", "0003"],
                 "Beta Holidays", count=12)
        cat, idx, tree, lay, cov, _ = self.state()
        got = next(v for v in cov.values() if v["work"] == "Beta Holidays")
        self.assertEqual(got["status"], "PARTIAL")
        self.assertIn("COUNT_FROM_FILE_METADATA", got["flags"])

    def test_coverage_against_remote(self):
        cat = Catalog(self.cat_path)
        fam = cat.get_family("Beta Days")
        fam["works"][0]["remote"] = {"latest_chapter": 5}
        cat2, idx, tree, lay, cov, _ = self.state(cat)
        c = cov[fam["works"][0]["id"]]
        self.assertEqual(c["status"], "PARTIAL")
        self.assertEqual(c["missing_chapters_text"], "3-5")
        fam["works"][0]["remote"] = {"latest_chapter": 2}
        self.assertEqual(coverage.compute(cat, tree)[fam["works"][0]["id"]]["status"], "COMPLETE")
        fam["works"][1]["contained_in"] = "Beta Days"
        self.assertEqual(coverage.compute(cat, tree)[fam["works"][1]["id"]]["status"], "COMPLETE")


# ---------------------------------------------------------------------------
def mu_series(sid, title, typ="Manga", authors=("Kenji Tanaka",), original="Shueisha", english=None,
              related=(), aliases=(), latest=None, status="3 Volumes (Ongoing)"):
    pubs = [{"publisher_name": original, "type": "Original", "notes": ""}] if original else []
    if english:
        pubs.append({"publisher_name": english, "type": "English", "notes": "3 Volumes; Ongoing"})
    return {"series_id": sid, "title": title, "type": typ, "year": "2020", "completed": False, "licensed": bool(english),
            "latest_chapter": latest, "status": status, "associated": [{"title": a} for a in aliases],
            "publishers": pubs, "publications": [], "url": f"https://mu.example/{sid}",
            "authors": [{"name": a, "type": "Author"} for a in authors],
            "related_series": [{"relation_type": r, "related_series_id": i, "related_series_name": n}
                               for r, i, n in related]}


class FakeMU(MangaUpdates):
    def __init__(self, series, searches):
        self.series, self.searches, self.calls = series, searches, 0

    def search(self, text, perpage=25):
        return self.searches.get(text, [])

    def node(self, sid, ttl_days=None):
        self.calls += 1
        s = self.series.get(sid)
        return summarize(s) if s else None


class FakeNDL:
    def __init__(self, recs):
        self.recs = recs

    def search_books(self, title, max_records=1000, ttl_days=None):
        return [r for r in self.recs if title in (r["title"] + (r.get("series_title") or ""))]


def ndl_rec(title, pub, isbn, series=None, vol=None):
    return {"title": title, "dc_title": title, "link": "https://ndl.example/" + isbn, "category": ["図書"],
            "creators": [], "publishers": [pub], "isbn": [isbn], "volume": vol, "series_title": series, "issued": "2021"}


def fake_providers():
    S = {
        100: mu_series(100, "Beta Days", english="VIZ Media", aliases=["ベータデイズ"], latest=2,
                       related=[("Side Story", 101, "Beta Holidays"), ("Spin-Off", 103, "Beta Days Gaiden"),
                                ("Doujinshi", 900, "Beta Days dj - Fan"), ("Alternate Story", 104, "Crossover Party"),
                                ("Spin-Off", 107, "Beta Days Anthology Comic")]),
        101: mu_series(101, "Beta Holidays", related=[("Main Story", 100, "Beta Days")]),
        103: mu_series(103, "Beta Days Gaiden", authors=("Kenji Tanaka", "Sato"),
                       related=[("Sequel", 106, "Beta Days Gaiden 2")]),
        106: mu_series(106, "Beta Days Gaiden 2", authors=("Sato",)),
        104: mu_series(104, "Crossover Party", authors=("Other Person",), related=[("Main Story", 105, "Other Series")]),
        105: mu_series(105, "Other Series", authors=("Other Person",), original="Kodansha"),
        107: mu_series(107, "Beta Days Anthology Comic", authors=("Various",)),
    }
    searches = {"Beta Days": [{"id": 100, "title": "Beta Days", "type": "Manga", "year": "2020", "hit_title": "Beta Days"},
                              {"id": 900, "title": "Beta Days dj - Fan", "type": "Doujinshi", "year": "2021",
                               "hit_title": "Beta Days dj - Fan"}],
                "Beta Holidays": [{"id": 101, "title": "Beta Holidays", "type": "Manga", "year": "2021",
                                   "hit_title": "Beta Holidays"}]}
    recs = [ndl_rec("ベータデイズ 1", "集英社", "9784000000011", vol="1"),
            ndl_rec("ベータデイズ 2", "集英社", "9784000000028", vol="2"),
            ndl_rec("ベータデイズ 公式ファンブック", "集英社", "9784000000035"),
            ndl_rec("ベータデイズの謎 大全", "謎出版", "9784000000042"),
            ndl_rec("ベータデイズ ノベライズ", "集英社", "9784000000059", series="JUMP j BOOKS"),
            ndl_rec("ラーメン ベータデイズ店", "食堂社", "9784000000066")]
    return {"mangaupdates": FakeMU(S, searches), "ndl": FakeNDL(recs)}


class TestDiscoverScaffold(Base):
    def run_discovery(self, cat):
        cat_, idx, tree, lay, cov, _ = self.state(cat)
        res = discover.run(cat, fake_providers(), self.data, families=["Beta Days"], refresh=True)
        return res[0]

    def test_discover_merges_official_only(self):
        cat = Catalog(self.cat_path)
        curated_before = json.dumps([{k: w.get(k) for k in ("work", "role", "vault_subpath", "notes")}
                                     for w in cat.get_family("Beta Days")["works"]], sort_keys=True)
        res = self.run_discovery(cat)
        fam = cat.get_family("Beta Days")
        works = {w["work"]: w for w in fam["works"]}
        self.assertEqual(works["Beta Days"]["external_ids"]["mangaupdates"], 100)
        self.assertEqual(works["Beta Holidays"]["external_ids"]["mangaupdates"], 101)
        self.assertEqual(works["Beta Days"]["titles"]["ja"], "ベータデイズ")
        self.assertEqual(works["Beta Days Gaiden"]["relation"], tx.OFFICIAL_SPINOFF)
        self.assertEqual(works["Beta Days Gaiden"]["review_status"], "ACCEPTED")
        self.assertEqual(works["Beta Days Gaiden 2"]["relation"], tx.OFFICIAL_SPINOFF)  # sequel of a spin-off
        anth = works["Beta Days Comic Anthology"]  # adopted folder, now confirmed by discovery
        self.assertEqual(anth["external_ids"]["mangaupdates"], 107)
        self.assertTrue(anth["official"])
        self.assertNotIn("Beta Days Anthology Comic", works)  # no duplicate of the adopted folder
        self.assertNotIn("Other Series", works)  # outside the family
        self.assertNotIn("Beta Days dj - Fan", works)  # fan work never catalogued
        self.assertTrue(any(f["id"] == 900 for f in res["fan_works"]))
        fanbook = works.get("ベータデイズ 公式ファンブック")
        self.assertIsNotNone(fanbook)
        self.assertEqual(fanbook["relation"], tx.FANBOOK_OFFICIAL)
        self.assertEqual(fanbook["confidence"], "high")
        self.assertTrue(any("謎" in r["title"] for r in res["review"]))  # third-party guide -> review
        self.assertNotIn("ベータデイズの謎 大全", works)
        novel = works.get("ベータデイズ ノベライズ")
        self.assertEqual(novel["medium"], "light-novel")
        self.assertEqual(novel["review_status"], "REVIEW")
        self.assertEqual(works["Beta Days"]["remote"]["ndl"]["volumes"], 2)
        self.assertFalse(any("ラーメン" in w for w in works))
        after = json.dumps([{k: w.get(k) for k in ("work", "role", "vault_subpath", "notes")}
                            for w in fam["works"] if w.get("origin") == "curated"], sort_keys=True)
        self.assertEqual(curated_before, after)
        n = len(fam["works"])
        discover.run(cat, fake_providers(), self.data, families=["Beta Days"], refresh=True)
        self.assertEqual(len(fam["works"]), n, "discovery must be idempotent")
        self.assertVaultFilesUntouched()

    def test_scaffold_dry_run_then_apply(self):
        cat = Catalog(self.cat_path)
        self.run_discovery(cat)
        fam_a = cat.get_family("Alpha Saga")
        os.makedirs(os.path.join(self.vault, "Alpha Saga", "guidebook", "Alpha Saga Guide"))
        cat.add_work(fam_a, {"work": "Alpha Saga Guidebook", "relation": tx.GUIDEBOOK, "medium": "manga",
                             "material_class": "guidebook", "official": True, "confidence": "high",
                             "review_status": "ACCEPTED", "origin": "discovered", "availability": "published"})
        self.before = snapshot(self.vault)
        dirs_before = dirs(self.vault)
        cat, idx, tree, lay, cov, _ = self.state(cat)
        actions = scaffold.plan(cat, lay)
        creates = [a for a in actions if a["action"] == "CREATE"]
        self.assertEqual(dirs(self.vault), dirs_before, "dry run created something")
        paths = {os.path.relpath(a["path"], self.vault) for a in creates}
        self.assertIn(os.path.join("Beta Days", "manga", "Beta Days Gaiden"), paths)
        self.assertIn(os.path.join("Beta Days", "fanbook"), paths)
        self.assertIn(os.path.join("Beta Days", "fanbook", "ベータデイズ 公式ファンブック"), paths)
        self.assertFalse(any("ノベライズ" in p for p in paths), "review work must not be scaffolded")
        self.assertFalse(any("Delta Blocked" in p for p in paths), "blocked / no-destination work never scaffolded")
        self.assertFalse(any("Guidebook" in p for p in paths), "ambiguous work must not be scaffolded")
        kinds = {a["work"]: a["action"] for a in actions}
        self.assertEqual(kinds["Alpha Saga Guidebook"], "AMBIGUOUS")
        self.assertEqual(kinds["Alpha Saga"], "LEGACY_MAPPING")
        log = os.path.join(self.data, "scaffold-log.jsonl")
        res = scaffold.apply(cat, actions, log)
        self.assertTrue(all(r["result"] == "CREATED" for r in res))
        for p in paths:
            self.assertTrue(os.path.isdir(os.path.join(self.vault, p)))
        self.assertVaultFilesUntouched()
        self.assertEqual(set(dirs(self.vault)) - set(dirs_before), paths)
        cat, idx, tree, lay, cov, _ = self.state(cat)
        again = [a for a in scaffold.plan(cat, lay) if a["action"] == "CREATE"]
        self.assertEqual(again, [], "second scaffold must be a no-op")
        # reports render and never touch the Vault
        items = acquire.plan(cat, cov, sources.load_registry(self.data)[0])
        report.write_state(self.data, lay, cov)
        report.write_queue(self.data, cat, cov, lay, items)
        report.write_coverage_report(self.data, cat, cov, lay, report.discovered_all(self.data))
        report.write_review(self.data, report.collect_review(cat, lay, report.discovered_all(self.data), actions, None))
        report.write_vault_audit(self.data, lay, actions, True)
        for f in ("vault-layout.json", "LIBRARY_COVERAGE_REPORT.md", "REVIEW_REQUIRED.md", "VAULT_STRUCTURE_AUDIT.md"):
            self.assertTrue(os.path.exists(os.path.join(self.data, f)), f)
        self.assertVaultFilesUntouched()

    def test_scaffold_conflict_when_file_in_the_way(self):
        cat = Catalog(self.cat_path)
        fam = cat.get_family("Beta Days")
        cat.add_work(fam, {"work": "Blocked Name", "relation": tx.OFFICIAL_SPINOFF, "medium": "manga",
                           "material_class": "manga", "official": True, "confidence": "high",
                           "review_status": "ACCEPTED", "origin": "discovered", "vault_subpath": "manga/Blocked Name"})
        write(os.path.join(self.vault, "Beta Days", "manga", "Blocked Name"), b"i am a file")
        cat, idx, tree, lay, cov, _ = self.state(cat)
        acts = [a for a in scaffold.plan(cat, lay) if a["work"] == "Blocked Name"]
        self.assertEqual(acts[0]["action"], "CONFLICT")


# ---------------------------------------------------------------------------
class TestIngest(Base):
    def setUp(self):
        super().setUp()
        hn = os.path.join(self.intake, "HaruNeko")
        man = os.path.join(self.intake, "Manual")
        write(f"{self.vault}/Beta Days/manga/Beta Days/ch1.cbz", b"VAULT-ORIGINAL-1")
        write(f"{hn}/Beta Days/ch1.cbz", b"INCOMING-1")
        write(f"{hn}/Beta Days/ch3.cbz", b"NEW-3")
        write(f"{hn}/Beta Days/ch4.cbz.crdownload", b"HALF")
        write(f"{hn}/Beta Days/vol1/p001.jpg", b"PAGE")
        shutil.copy2(f"{self.vault}/Beta Days/manga/Beta Days/b1.zip", f"{hn}/Beta Days/renamed.zip")
        make_zip(f"{hn}/Odd Site Name/a.zip", ["0009"], "Beta Holidays", web="https://comix.to/t/1")
        write(f"{hn}/Beta Dayz/x.cbz", b"FUZZY")
        write(f"{hn}/Beta Days (Full Color)/c.cbz", b"COLOR")
        write(f"{hn}/Totally Unknown/y.cbz", b"UNKNOWN")
        write(f"{man}/Beta Holidays v02.cbz", b"MANUAL-FILE")
        self.before = snapshot(self.vault)
        self.intake_before = snapshot(self.intake)

    def run_ingest(self, apply):
        cat = Catalog(self.cat_path)
        idx = vault_scan.scan_vault(self.vault, os.path.join(self.data, "vault-index.json"))
        dirs_ = [os.path.join(self.intake, d) for d in sorted(os.listdir(self.intake))]
        return ingest.run(cat, dirs_, index=idx, apply=apply, log_path=os.path.join(self.data, "import-log.jsonl"),
                          unofficial_hosts=["comix.to"])

    def test_dry_run_then_apply(self):
        dry = self.run_ingest(False)
        self.assertEqual(snapshot(self.vault), self.before, "dry run wrote to the Vault")
        acts = {os.path.basename(r["file"]): r["action"] for r in dry["records"]}
        self.assertEqual(acts["ch3.cbz"], "imported (dry-run)")
        self.assertEqual(acts["renamed.zip"], "identical-already-in-vault")
        self.assertIn("REVIEW_REQUIRED", acts["Beta Dayz"])  # similar title: never imported
        self.assertIn("colour", acts["Beta Days (Full Color)"])  # colour edition without a colour work
        self.assertIn("unclassified", acts["Totally Unknown"])
        self.assertEqual(acts["Beta Holidays v02.cbz"], "imported (dry-run)")  # identified by file name
        self.assertEqual(acts["a.zip"], "imported (dry-run)")  # identified by ComicInfo series
        odd = next(u for u in dry["units"] if u["unit"] == "Odd Site Name")
        self.assertEqual(odd["unofficial_provenance"], ["comix.to"])
        res = self.run_ingest(True)
        main = f"{self.vault}/Beta Days/manga/Beta Days"
        with open(f"{main}/ch1.cbz", "rb") as fh:
            self.assertEqual(fh.read(), b"VAULT-ORIGINAL-1")
        coll = [f for f in os.listdir(main) if f.startswith("ch1 (intake ")]
        self.assertEqual(len(coll), 1)
        self.assertTrue(os.path.exists(f"{main}/ch3.cbz"))
        self.assertTrue(os.path.exists(f"{main}/vol1/p001.jpg"))
        self.assertFalse(os.path.exists(f"{main}/ch4.cbz.crdownload"))
        self.assertFalse(os.path.exists(f"{main}/renamed.zip"))
        self.assertTrue(os.path.exists(f"{self.vault}/Beta Days/manga/Beta Holidays/Beta Holidays v02.cbz"))
        self.assertFalse(any(".continuum-import-" in p for p in snapshot(self.vault)))
        self.assertEqual(snapshot(self.intake), self.intake_before, "intake originals must stay untouched")
        self.assertVaultFilesUntouched()
        self.assertTrue(os.path.exists(os.path.join(self.data, "import-log.jsonl")))
        snap = snapshot(self.vault)
        self.run_ingest(True)
        self.assertEqual(snapshot(self.vault).keys(), snap.keys(), "re-apply must be idempotent")


class TestCreateDestination(Base):
    """An identified arrival with nowhere to go gets a home - carefully."""

    def add_work(self, family, work, subpath):
        with open(self.cat_path, encoding="utf-8") as fh:
            data = json.load(fh)
        fam = next(f for f in data["families"] if f["family"] == family)
        fam["works"].append(W(work, subpath, "official spin-off"))
        with open(self.cat_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def arrival(self, name):
        write(os.path.join(self.intake, "Manual", name, "e1.cbz"), b"ARRIVED")
        return [os.path.join(self.intake, "Manual")]

    def run_ingest(self, dirs, **kw):
        return ingest.run(Catalog(self.cat_path), dirs, index=None,
                          log_path=os.path.join(self.data, "import-log.jsonl"), **kw)

    def test_without_the_flag_it_waits(self):
        self.add_work("Beta Days", "Beta Extras", "manga/Beta Extras")
        res = self.run_ingest(self.arrival("Beta Extras"), apply=True)
        actions = [r["action"] for r in res["records"]]
        self.assertTrue(any("destination work folder missing" in a for a in actions), actions)
        self.assertFalse(os.path.exists(os.path.join(self.vault, "Beta Days", "manga", "Beta Extras")))

    def test_dry_run_creates_nothing(self):
        self.add_work("Beta Days", "Beta Extras", "manga/Beta Extras")
        res = self.run_ingest(self.arrival("Beta Extras"), apply=False, create_folders=True)
        actions = [r["action"] for r in res["records"]]
        self.assertIn("would-create-destination-folder (dry-run)", actions)
        self.assertFalse(os.path.exists(os.path.join(self.vault, "Beta Days", "manga", "Beta Extras")))
        self.assertVaultFilesUntouched()

    def test_apply_creates_the_folder_and_imports(self):
        self.add_work("Beta Days", "Beta Extras", "manga/Beta Extras")
        res = self.run_ingest(self.arrival("Beta Extras"), apply=True, create_folders=True)
        destination = os.path.join(self.vault, "Beta Days", "manga", "Beta Extras")
        self.assertTrue(os.path.isdir(destination))
        self.assertTrue(os.path.isfile(os.path.join(destination, "e1.cbz")))
        self.assertIn("created-destination-folder", [r["action"] for r in res["records"]])
        self.assertVaultFilesUntouched()

    def test_a_missing_family_folder_is_never_created(self):
        with open(self.cat_path, encoding="utf-8") as fh:
            data = json.load(fh)
        data["families"].append({"order": 9, "family": "Ghost Family", "medium": "manga",
                                 "vault_family_folder": "Ghost Family",
                                 "works": [W("Ghost Work", "manga/Ghost Work")]})
        with open(self.cat_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        res = self.run_ingest(self.arrival("Ghost Work"), apply=True, create_folders=True)
        actions = [r["action"] for r in res["records"]]
        self.assertTrue(any("family folder does not exist" in a for a in actions), actions)
        self.assertFalse(os.path.exists(os.path.join(self.vault, "Ghost Family")))

    def test_a_file_in_the_way_is_a_conflict_not_a_workaround(self):
        write(os.path.join(self.vault, "Beta Days", "manga", "Beta Blocked"), b"i am a file")
        self.before = snapshot(self.vault)
        self.add_work("Beta Days", "Beta Blocked", "manga/Beta Blocked")
        res = self.run_ingest(self.arrival("Beta Blocked"), apply=True, create_folders=True)
        actions = [r["action"] for r in res["records"]]
        self.assertTrue(any("a file occupies" in a for a in actions), actions)
        self.assertVaultFilesUntouched()


# ---------------------------------------------------------------------------
class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


class TestSourcesAcquire(Base):
    def test_unofficial_hosts_refused(self):
        reg, _ = sources.load_registry(self.data)
        sources.add_unofficial_host(reg, "comix.to")
        cat = Catalog(self.cat_path)
        w = cat.get_family("Beta Days")["works"][0]
        with self.assertRaises(sources.RefusedSource):
            sources.add_link(reg, w, "https://comix.to/title/abc")
        with self.assertRaises(sources.RefusedSource):
            sources.add_source(reg, "bad", "Bad", "https://sub.comix.to", ["DIRECT_DOWNLOAD_AUTHORIZED"])
        link = sources.add_link(reg, w, "https://www.viz.com/beta-days")
        self.assertEqual(link["source"], "viz")

    def test_manual_list_and_authorized_download_only(self):
        served = os.path.join(self.tmp, "served")
        write(os.path.join(served, "free-vol1.epub"), b"DRM-FREE-EPUB")
        handler = functools.partial(_Quiet, directory=served)
        with socketserver.TCPServer(("127.0.0.1", 0), handler) as httpd:
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            url = f"http://127.0.0.1:{httpd.server_address[1]}/free-vol1.epub"
            cat, idx, tree, lay, cov, _ = self.state()
            reg, _ = sources.load_registry(self.data)
            w = cat.get_family("Beta Days")["works"][1]  # Beta Holidays: MISSING
            sources.add_link(reg, w, url, direct_file=True)  # host not registered -> not downloadable
            items = acquire.plan(cat, cov, reg)
            it = next(i for i in items if i["work"] == "Beta Holidays")
            self.assertFalse(it["downloadable"])
            self.assertTrue(it["requires_user_action"])
            sources.add_source(reg, "local_drm_free", "Local DRM-free shop", url.rsplit("/", 1)[0],
                               ["DIRECT_DOWNLOAD_AUTHORIZED"], download_permitted=True)
            w["links"] = []
            sources.add_link(reg, w, url, direct_file=True)
            items = acquire.plan(cat, cov, reg)
            it = next(i for i in items if i["work"] == "Beta Holidays")
            self.assertTrue(it["downloadable"])
            http = HttpClient(os.path.join(self.data, "cache"))
            res = acquire.apply(items, http, self.intake, reg, os.path.join(self.data, "acq.jsonl"))
            got = [r for r in res if r["result"] == "downloaded"]
            self.assertEqual(len(got), 1)
            self.assertTrue(got[0]["path"].startswith(self.intake))
            self.assertFalse(got[0]["path"].startswith(self.vault))
            res2 = acquire.apply(items, http, self.intake, reg, os.path.join(self.data, "acq.jsonl"))
            self.assertEqual(res2[0]["result"], "already in intake")
            httpd.shutdown()
        acquire.write_manual(self.data, items)
        with open(os.path.join(self.data, "MANUAL_ACQUISITION.csv"), encoding="utf-8-sig") as fh:
            header = fh.readline().strip().split(",")
        self.assertEqual(header, acquire.MANUAL_COLUMNS)
        self.assertVaultFilesUntouched()


class TestUpdatesHttp(Base):
    def test_baseline_then_alerts(self):
        provs = fake_providers()
        cat = Catalog(self.cat_path)
        discover.run(cat, provs, self.data, families=["Beta Days"], refresh=True)
        cat, idx, tree, lay, cov, _ = self.state(cat)
        watch = updates.merge(cat, cov, None)
        self.assertEqual(updates.check(cat, watch, provs, families=["Beta Days"]), [], "first check = baseline")
        provs["mangaupdates"].series[100]["latest_chapter"] = 9
        provs["mangaupdates"].series[100]["related_series"].append(
            {"relation_type": "Spin-Off", "related_series_id": 555, "related_series_name": "Beta Days Brand New"})
        provs["ndl"].recs.append(ndl_rec("ベータデイズ アンソロジー", "集英社", "9784000000073"))
        alerts = updates.check(cat, watch, provs, families=["Beta Days"])
        kinds = {a["kind"] for a in alerts}
        self.assertIn("NEW_CHAPTERS", kinds)
        self.assertIn("NEW_RELATED_WORK", kinds)
        self.assertIn("NEW_JP_BOOK_OFFICIAL_ANTHOLOGY", kinds)
        watch2 = updates.merge(cat, cov, watch)
        e = next(x for x in watch2["works"] if x["work"] == "Beta Days")
        self.assertEqual(e["latest_remote"], 9)
        self.assertTrue(e["baseline"]["mu_checked"])

    def test_http_cache_and_offline(self):
        http = HttpClient(os.path.join(self.data, "cache"), offline=True)
        with self.assertRaises(ProviderUnavailable):
            http.request("GET", "https://example.invalid/x")


if __name__ == "__main__":
    unittest.main()
