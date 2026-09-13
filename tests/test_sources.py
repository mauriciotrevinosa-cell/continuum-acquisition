"""Source registry + adapters. Local fixtures only: no real site is contacted.

Run:  python -m unittest discover -s C:\\ContinuumTools\\acquisition\\tests -v
"""
from __future__ import annotations

import functools
import http.server
import json
import os
import shutil
import socketserver
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import acquisition_orchestrator as orch  # noqa: E402
from acq import adapters, sources  # noqa: E402
from acq.adapters import base  # noqa: E402
from acq.http import HttpClient  # noqa: E402

INDEX_HTML = """<html><head>
<title>Example Library</title>
<meta property="og:title" content="Example Library">
<meta property="og:description" content="A fixture site">
<link rel="alternate" type="application/rss+xml" href="/feed.xml">
</head><body>
<a href="/works/alpha">Alpha Chronicle</a>
<a href="/works/beta">Beta Journey</a>
<a href="/files/alpha-vol1.epub">Alpha Chronicle Volume 1 (EPUB)</a>
</body></html>"""

SEARCH_HTML = """<html><body>
<a href="/works/alpha">Alpha Chronicle</a>
<a href="/works/unrelated">Something Else Entirely</a>
</body></html>"""

WORK_HTML = """<html><head>
<title>Alpha Chronicle - Example Library</title>
<meta property="og:title" content="Alpha Chronicle">
<meta property="og:description" content="Volume 1 of a fixture series">
</head><body>
<a href="/works/alpha/ch-12">Chapter 12</a>
<a href="/files/alpha-vol1.epub">Alpha Chronicle Volume 1 (EPUB)</a>
</body></html>"""

FEED_V1 = """<?xml version="1.0"?><rss><channel>
<item><title>Alpha Chronicle Chapter 12</title><link>http://x/12</link><pubDate>Mon, 01 Jan 2026</pubDate></item>
</channel></rss>"""

FEED_V2 = """<?xml version="1.0"?><rss><channel>
<item><title>Alpha Chronicle Chapter 13</title><link>http://x/13</link><pubDate>Tue, 02 Jan 2026</pubDate></item>
<item><title>Alpha Chronicle Chapter 12</title><link>http://x/12</link><pubDate>Mon, 01 Jan 2026</pubDate></item>
</channel></rss>"""


class _Handler(http.server.BaseHTTPRequestHandler):
    """Tiny fixture site: robots.txt, a page, a feed, a search page, a file."""

    robots = "User-agent: *\nAllow: /\n"
    feed = FEED_V1

    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        bodies = {
            "/robots.txt": (type(self).robots, "text/plain"),
            "/": (INDEX_HTML, "text/html"),
            "/index.html": (INDEX_HTML, "text/html"),
            "/feed.xml": (type(self).feed, "application/rss+xml"),
            "/search": (SEARCH_HTML, "text/html"),
            "/works/alpha": (WORK_HTML, "text/html"),
            "/files/alpha-vol1.epub": ("EPUB-BYTES", "application/epub+zip"),
            "/private/secret.epub": ("SECRET", "application/epub+zip"),
        }
        if path not in bodies:
            self.send_error(404)
            return
        body, ctype = bodies[path]
        payload = body.encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="acqsrc-")
        self.data = os.path.join(self.tmp, "data")
        self.intake = os.path.join(self.tmp, "intake")
        self.vault = os.path.join(self.tmp, "vault")
        for d in (self.data, self.intake, self.vault):
            os.makedirs(d)
        self.http = HttpClient(os.path.join(self.tmp, "cache"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def serve(self, robots=_Handler.robots, feed=FEED_V1):
        _Handler.robots = robots
        _Handler.feed = feed
        httpd = socketserver.TCPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    def policy(self, unofficial=()):
        return base.Policy(unofficial_hosts=tuple(unofficial), vault_root=self.vault, intake_root=self.intake)


# ---------------------------------------------------------------------------
class TestRegistry(Base):
    def test_seed_only_on_creation_and_removal_sticks(self):
        reg, changed = sources.load_registry(self.data)
        self.assertTrue(changed)
        self.assertGreater(len(reg["sources"]), 0, "starter list should seed a brand-new registry")
        sources.save_registry(self.data, reg)
        victim = sorted(reg["sources"])[0]
        sources.remove_source(reg, victim)
        sources.save_registry(self.data, reg)
        again, _ = sources.load_registry(self.data)
        self.assertNotIn(victim, again["sources"], "a removed source must not be re-seeded")

    def test_empty_registry_is_valid(self):
        reg, _ = sources.load_registry(self.data, seed=False)
        self.assertEqual(reg["sources"], {})
        sources.save_registry(self.data, reg)
        back, _ = sources.load_registry(self.data)
        self.assertEqual(back["sources"], {}, "an empty registry stays empty (project-agnostic)")

    def test_add_list_enable_remove_roundtrip(self):
        reg, _ = sources.load_registry(self.data, seed=False)
        entry = sources.add_source_url(reg, "example.org", name="Example", access=["FREE_OFFICIAL_WEB"],
                                       search="https://example.org/s?q={q}")
        self.assertEqual(entry["id"], "example")
        self.assertEqual(entry["url"], "https://example.org")
        self.assertEqual(entry["adapter"], "web")
        self.assertTrue(entry["enabled"])
        self.assertNotIn(base.AUTOMATIC_ACQUISITION, entry["capabilities"])
        sources.set_enabled(reg, "example", False)
        sources.save_registry(self.data, reg)
        back, _ = sources.load_registry(self.data)
        self.assertFalse(back["sources"]["example"]["enabled"])
        self.assertIsNotNone(sources.remove_source(back, "example"))
        self.assertIsNone(sources.get_source(back, "example"))

    def test_duplicate_id_needs_replace_and_bad_search_rejected(self):
        reg, _ = sources.load_registry(self.data, seed=False)
        sources.add_source_url(reg, "https://example.org")
        with self.assertRaises(ValueError):
            sources.add_source_url(reg, "https://example.org/other", source_id="example")
        with self.assertRaises(ValueError):
            sources.add_source_url(reg, "https://other.org", search="https://other.org/s?q=QUERY")
        sources.add_source_url(reg, "https://example.org/v2", source_id="example", replace=True)
        self.assertEqual(reg["sources"]["example"]["url"], "https://example.org/v2")

    def test_unofficial_hosts_are_refused_everywhere(self):
        reg, _ = sources.load_registry(self.data, seed=False)
        sources.add_unofficial_host(reg, "bad.example")
        with self.assertRaises(sources.RefusedSource):
            sources.add_source_url(reg, "https://bad.example/library")
        with self.assertRaises(sources.RefusedSource):
            sources.add_source_url(reg, "https://mirror.bad.example/library")
        with self.assertRaises(sources.RefusedSource):
            sources.add_link(reg, {"work": "W"}, "https://bad.example/title/1")

    def test_restart_does_not_corrupt_the_registry(self):
        reg, _ = sources.load_registry(self.data, seed=False)
        for i in range(5):
            sources.add_source_url(reg, f"https://s{i}.example", access=["FREE_OFFICIAL_WEB"])
            sources.save_registry(self.data, reg)
        path = os.path.join(self.data, "sources.json")
        for _ in range(3):  # simulate restarts: load -> mutate -> save
            reg, _ = sources.load_registry(self.data)
            sources.set_enabled(reg, "s2", False)
            sources.save_registry(self.data, reg)
            with open(path, encoding="utf-8") as fh:
                parsed = json.load(fh)
            self.assertEqual(len(parsed["sources"]), 5)
            self.assertEqual(parsed["schema"], sources.REGISTRY_SCHEMA)
        self.assertFalse(parsed["sources"]["s2"]["enabled"])
        backups = [f for f in os.listdir(self.data) if f.startswith("sources.json.bak-")]
        self.assertTrue(backups, "every save keeps a timestamped backup")

    def test_v1_registry_migrates_to_v2(self):
        legacy = {"schema": "continuum.personal.sources/1", "unofficial_hosts": [],
                  "sources": {"old": {"name": "Old", "url": "https://old.example", "access": ["DRM_EBOOK"],
                                      "download_permitted": False}}}
        with open(os.path.join(self.data, "sources.json"), "w", encoding="utf-8") as fh:
            json.dump(legacy, fh)
        reg, changed = sources.load_registry(self.data)
        self.assertTrue(changed)
        entry = reg["sources"]["old"]
        self.assertEqual(entry["id"], "old")
        self.assertEqual(entry["adapter"], "web")
        self.assertTrue(entry["enabled"])
        self.assertIn(base.MANUAL_ACQUISITION, entry["capabilities"])
        self.assertEqual(entry["access"], ["DRM_EBOOK"], "user values are never overwritten")


# ---------------------------------------------------------------------------
class TestWebAdapter(Base):
    def entry(self, url, **kw):
        reg, _ = sources.load_registry(self.data, seed=False)
        return sources.add_source_url(reg, url, **kw), reg

    def test_self_test_discovers_what_the_site_offers(self):
        url = self.serve()
        entry, reg = self.entry(url, search=None)
        adapter = adapters.build(entry, http=self.http, policy=self.policy())
        result = adapter.self_test()
        self.assertTrue(result["ok"], result)
        checks = {c["check"]: c for c in result["checks"]}
        self.assertTrue(checks["robots.txt"]["ok"])
        self.assertTrue(checks["reachable"]["ok"])
        self.assertTrue(checks["feed"]["ok"], "the fixture advertises an RSS feed")
        self.assertTrue(checks["metadata"]["ok"])
        self.assertFalse(checks["search"]["ok"], "no search template was given")
        self.assertIn(base.UPDATE_TRACKING, result["capabilities"])
        self.assertNotIn(base.AUTOMATIC_ACQUISITION, result["capabilities"])
        self.assertIn("get_update_status", result["operations"])

    def test_robots_disallow_refuses_the_source(self):
        url = self.serve(robots="User-agent: *\nDisallow: /\n")
        entry, _ = self.entry(url)
        adapter = adapters.build(entry, http=self.http, policy=self.policy())
        result = adapter.self_test()
        self.assertFalse(result["ok"])
        self.assertIn("robots", result["error"])
        with self.assertRaises(base.SourceRefused):
            adapter.get_work_metadata("/works/alpha")

    def test_search_and_metadata(self):
        url = self.serve()
        entry, _ = self.entry(url, search=None)
        entry["search"] = url + "/search?q={q}"
        adapter = adapters.build(entry, http=self.http, policy=self.policy())
        adapter.self_test()
        hits = adapter.search("Alpha Chronicle")
        self.assertTrue(hits)
        self.assertEqual(hits[0].title, "Alpha Chronicle")
        self.assertTrue(hits[0].url.endswith("/works/alpha"))
        meta = adapter.get_work_metadata("/works/alpha")
        self.assertEqual(meta["title"], "Alpha Chronicle")
        chapters = adapter.get_chapters("/works/alpha")
        self.assertEqual(chapters[0]["number"], "12")
        with self.assertRaises(base.SourceRefused):
            adapter.get_work_metadata("/works/missing")  # a 404 is never parsed as metadata

    def test_update_status_baseline_then_change(self):
        url = self.serve()
        entry, _ = self.entry(url)
        adapter = adapters.build(entry, http=self.http, policy=self.policy())
        adapter.self_test()
        first = adapter.get_update_status(None)
        self.assertFalse(first.changed, "the first observation is a baseline, never an alert")
        self.assertIn("Chapter 12", first.latest)
        _Handler.feed = FEED_V2
        fresh = HttpClient(os.path.join(self.tmp, "cache2"))
        adapter2 = adapters.build(entry, http=fresh, policy=self.policy())
        second = adapter2.get_update_status({"fingerprint": first.fingerprint})
        self.assertTrue(second.changed)
        self.assertIn("Chapter 13", second.latest)

    def test_download_only_when_permitted(self):
        url = self.serve()
        entry, _ = self.entry(url)
        adapter = adapters.build(entry, http=self.http, policy=self.policy())
        files = adapter.get_available_files("/")
        self.assertTrue(files)
        self.assertFalse(files[0].direct)
        with self.assertRaises(base.SourceRefused):
            adapter.acquire(files[0], os.path.join(self.intake, "x"))
        entry["download_permitted"] = True
        entry["capabilities"] = list(entry["capabilities"]) + [base.AUTOMATIC_ACQUISITION]
        adapter = adapters.build(entry, http=self.http, policy=self.policy())
        target_dir = os.path.join(self.intake, entry["id"])
        got = adapter.acquire(adapter.get_available_files("/")[0], target_dir)
        self.assertEqual(got["result"], "downloaded")
        self.assertTrue(got["path"].startswith(self.intake))
        again = adapter.acquire(adapter.get_available_files("/")[0], target_dir)
        self.assertEqual(again["result"], "already in intake", "never overwrite what is already there")

    def test_never_downloads_into_the_vault(self):
        url = self.serve()
        entry, _ = self.entry(url, download_permitted=True)
        adapter = adapters.build(entry, http=self.http, policy=self.policy())
        with self.assertRaises(base.SourceRefused):
            adapter.acquire(base.FileRef(name="x.epub", location=url + "/files/alpha-vol1.epub", direct=True),
                            os.path.join(self.vault, "Anything"))

    def test_unofficial_host_is_refused_by_the_adapter_too(self):
        url = self.serve()
        entry, _ = self.entry(url)
        host = sources.host_of(url)
        adapter = adapters.build(entry, http=self.http, policy=self.policy(unofficial=[host]))
        result = adapter.self_test()
        self.assertFalse(result["ok"])
        self.assertIn("unofficial", result["error"])


# ---------------------------------------------------------------------------
class TestLocalFolderAdapter(Base):
    def setUp(self):
        super().setUp()
        self.mine = os.path.join(self.tmp, "mine", "Series Alpha")
        os.makedirs(self.mine)
        for name, body in (("alpha-ch1.cbz", b"ONE"), ("alpha-ch2.cbz", b"TWO")):
            with open(os.path.join(self.mine, name), "wb") as fh:
                fh.write(body)
        reg, _ = sources.load_registry(self.data, seed=False)
        self.entry = sources.add_source_url(reg, os.path.join(self.tmp, "mine"), source_id="mine")
        self.adapter = adapters.build(self.entry, policy=self.policy())

    def test_detected_as_local_and_automatic(self):
        self.assertEqual(self.entry["adapter"], "local-folder")
        result = self.adapter.self_test()
        self.assertTrue(result["ok"], result)
        self.assertIn(base.AUTOMATIC_ACQUISITION, result["capabilities"])

    def test_search_files_and_acquire_into_intake(self):
        hits = self.adapter.search("Series Alpha")
        self.assertTrue(hits)
        files = self.adapter.get_available_files("Series Alpha")
        self.assertEqual(len(files), 2)
        dest = os.path.join(self.intake, "mine", "Series Alpha")
        got = self.adapter.acquire(files[0], dest)
        self.assertEqual(got["result"], "copied")
        self.assertTrue(os.path.isfile(got["path"]))
        with open(os.path.join(self.mine, "alpha-ch1.cbz"), "rb") as fh:
            self.assertEqual(fh.read(), b"ONE", "the source file is never moved or altered")
        self.assertEqual(self.adapter.acquire(files[0], dest)["result"], "already in intake")
        self.assertFalse(any(n.startswith(".continuum-import-") for n in os.listdir(dest)))

    def test_update_status_tracks_new_files(self):
        first = self.adapter.get_update_status(None)
        self.assertFalse(first.changed)
        with open(os.path.join(self.mine, "alpha-ch3.cbz"), "wb") as fh:
            fh.write(b"THREE")
        second = self.adapter.get_update_status({"fingerprint": first.fingerprint})
        self.assertTrue(second.changed)

    def test_folder_inside_the_vault_is_refused(self):
        reg, _ = sources.load_registry(self.data, seed=False)
        inside = os.path.join(self.vault, "Family")
        os.makedirs(inside)
        entry = sources.add_source_url(reg, inside, source_id="inside")
        adapter = adapters.build(entry, policy=self.policy())
        result = adapter.self_test()
        self.assertFalse(result["ok"])
        self.assertIn("Vault", result["error"])


# ---------------------------------------------------------------------------
class TestCapabilityGate(Base):
    def test_catalogue_source_can_never_acquire(self):
        reg, _ = sources.load_registry(self.data, seed=False)
        entry = sources.add_source_url(reg, "https://catalogue.example", adapter="bibliographic",
                                       download_permitted=True)
        entry["provider"] = "mangaupdates"
        adapter = adapters.build(entry, policy=self.policy())
        self.assertNotIn(base.AUTOMATIC_ACQUISITION, adapter.capabilities())
        self.assertFalse(adapter.supports("acquire"))
        with self.assertRaises(base.NotSupported):
            adapter.acquire(base.FileRef(name="x", location="y", direct=True), self.intake)

    def test_operation_requires_the_capability(self):
        reg, _ = sources.load_registry(self.data, seed=False)
        entry = sources.add_source_url(reg, "https://example.org")
        entry["capabilities"] = [base.DISCOVERY_ONLY]
        adapter = adapters.build(entry, http=self.http, policy=self.policy())
        self.assertFalse(adapter.supports("get_update_status"))
        with self.assertRaises(base.SourceRefused):
            adapter.get_update_status(None)


# ---------------------------------------------------------------------------
class TestAcquisitionFlow(Base):
    """source -> adapter -> search missing -> intake -> identify -> destination."""

    def setUp(self):
        super().setUp()
        from acq import acquire, catalog as catalog_mod, coverage, ingest, layout, vault_scan
        self.mods = {"acquire": acquire, "coverage": coverage, "ingest": ingest, "layout": layout,
                     "vault_scan": vault_scan, "catalog": catalog_mod}
        os.makedirs(os.path.join(self.vault, "Beta Days", "manga", "Beta Days"))
        self.mine = os.path.join(self.tmp, "mine", "Beta Days")
        os.makedirs(self.mine)
        for name in ("Beta Days ch1.cbz", "Beta Days ch2.cbz"):
            with open(os.path.join(self.mine, name), "wb") as fh:
                fh.write(name.encode())
        self.cat_path = os.path.join(self.data, "works-catalog.json")
        with open(self.cat_path, "w", encoding="utf-8") as fh:
            json.dump({"schema": "continuum.personal.acquisition-catalog/1", "vault_root": self.vault,
                       "intake_root": os.path.join(self.intake, "Manual"),
                       "families": [{"order": 1, "family": "Beta Days", "vault_family_folder": "Beta Days",
                                     "medium": "manga", "works": [
                                         {"work": "Beta Days", "role": "main",
                                          "vault_subpath": "manga/Beta Days", "declared_status": None,
                                          "candidate_sources": [], "intake_aliases": [],
                                          "availability": "published", "notes": ""}]}]}, fh)

    def test_missing_work_is_found_acquired_into_intake_and_identified(self):
        m = self.mods
        cat = m["catalog"].Catalog(self.cat_path)
        idx = m["vault_scan"].scan_vault(self.vault, os.path.join(self.data, "vault-index.json"))
        tree = m["layout"].Tree(self.vault, idx)
        cov = m["coverage"].compute(cat, tree)
        reg, _ = sources.load_registry(self.data, seed=False)
        sources.add_source_url(reg, os.path.join(self.tmp, "mine"), source_id="mine")

        items = m["acquire"].plan(cat, cov, reg)
        self.assertEqual([i["work"] for i in items], ["Beta Days"])
        self.assertEqual(items[0]["coverage_status"], "MISSING")

        adapter_list = adapters.for_registry(reg, http=self.http, policy=self.policy())
        stats = m["acquire"].search_sources(items, adapter_list)
        self.assertGreaterEqual(stats["hits"], 1)
        self.assertTrue(items[0]["downloadable"], "a folder the user owns can be acquired automatically")

        log = os.path.join(self.data, "acquire-log.jsonl")
        planned = m["acquire"].acquire_from_sources(items, adapter_list, self.intake, log, apply=False)
        self.assertTrue(planned and all(p["result"] == "would acquire" for p in planned))
        self.assertFalse(os.path.exists(os.path.join(self.intake, "mine")), "dry run copies nothing")

        done = m["acquire"].acquire_from_sources(items, adapter_list, self.intake, log, apply=True)
        copied = [d for d in done if d.get("result") == "copied"]
        self.assertEqual(len(copied), 2)
        for path in (d["path"] for d in copied):
            self.assertTrue(path.startswith(self.intake))
            self.assertFalse(path.startswith(self.vault), "acquisition never writes to the Vault")

        result = m["ingest"].run(cat, [os.path.join(self.intake, "mine")], index=idx, apply=False,
                                 log_path=os.path.join(self.data, "import-log.jsonl"))
        actions = {os.path.basename(r["file"]): r["action"] for r in result["records"]}
        self.assertEqual(set(actions.values()), {"imported (dry-run)"})
        targets = {r["target"] for r in result["records"]}
        self.assertTrue(all(os.path.join("Beta Days", "manga", "Beta Days") in t for t in targets))
        self.assertEqual(sorted(os.listdir(os.path.join(self.vault, "Beta Days", "manga", "Beta Days"))), [],
                         "the Vault is untouched until ingest --apply")


class TestCli(Base):
    def run_cli(self, *args):
        return orch.main(["--data-dir", self.data, *args])

    def test_add_list_remove_via_cli(self):
        folder = os.path.join(self.tmp, "mine")
        os.makedirs(folder, exist_ok=True)
        self.assertEqual(self.run_cli("sources", "add", folder, "--id", "mine", "--no-test"), 0)
        self.assertEqual(self.run_cli("sources", "list"), 0)
        self.assertEqual(self.run_cli("sources", "disable", "mine"), 0)
        reg, _ = sources.load_registry(self.data)
        self.assertFalse(reg["sources"]["mine"]["enabled"])
        self.assertEqual(self.run_cli("sources", "enable", "mine"), 0)
        self.assertEqual(self.run_cli("sources", "test", "mine"), 0)
        reg, _ = sources.load_registry(self.data)
        self.assertTrue(reg["sources"]["mine"]["last_test"]["ok"])
        self.assertEqual(self.run_cli("sources", "remove", "mine"), 0)
        reg, _ = sources.load_registry(self.data)
        self.assertNotIn("mine", reg["sources"])
        self.assertEqual(self.run_cli("sources", "remove", "mine"), 3, "removing twice is an error, not a crash")

    def test_cli_refuses_unofficial_host(self):
        self.assertEqual(self.run_cli("sources", "unofficial", "bad.example"), 0)
        self.assertEqual(self.run_cli("sources", "add", "https://bad.example", "--no-test"), 3)

    def test_works_without_a_catalog(self):
        """A fresh install has no library yet; the registry still works."""
        self.assertFalse(os.path.exists(os.path.join(self.data, "works-catalog.json")))
        self.assertEqual(self.run_cli("sources", "list", "--json"), 0)


if __name__ == "__main__":
    unittest.main()
