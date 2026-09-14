#!/usr/bin/env python3
"""Continuum Acquisition Orchestrator: personal library tooling (not runtime).

    scan            read-only Vault inventory: cached SHA-256, duplicates,
                    unidentified files, chapter/volume ranges
    discover        find all OFFICIAL material per family (MangaUpdates, NDL,
                    AniList when available) and merge it into the catalog
    coverage        compare local vs remote; rebuild queue and reports
    verify-vault    structure audit: EXPECTED / FOUND / MISSING_FOLDER / ...
    scaffold        plan missing folders (DRY RUN); --apply creates folders only
    acquire         legal sources per missing work + manual list (DRY RUN);
                    --apply downloads only authorized DRM-free direct files
    ingest          intake -> Vault (DRY RUN); --apply copies, never overwrites
    update-check    detect new chapters / volumes / related works / JP books
    run             scan + discover + coverage + scaffold plan + acquire plan
    add-link        attach an official link to a work
    add-source      register an official channel
    add-unofficial-host   mark a host as unofficial (never used as a source)
    status          one-screen summary

Hard rules: the Vault is RAW preservation. Nothing here deletes, overwrites,
moves, renames, extracts or re-encodes Vault files. No DRM/paywall/login/
anti-bot bypass; no scanlation/aggregator sources. Personal data lives in the
data directory, never in this tool.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from acq import (acquire, adapters, catalog as catalog_mod, coverage, discover, ingest, layout,  # noqa: E402
                 prepare, report, scaffold, sources, updates, util, vault_scan)
from acq.http import HttpClient  # noqa: E402
from acq.providers.anilist import AniList  # noqa: E402
from acq.providers.mangaupdates import MangaUpdates  # noqa: E402
from acq.providers.ndl import NDL  # noqa: E402

DEFAULT_DATA = r"C:\ContinuumData\acquisition"
LOG = util.LOG


class Ctx:
    def __init__(self, args):
        self.args = args
        self.catalog_path = args.catalog or os.path.join(args.data_dir, "works-catalog.json")
        self.data_dir = args.data_dir if args.catalog is None or args.data_dir != DEFAULT_DATA \
            else os.path.dirname(os.path.abspath(self.catalog_path))
        os.makedirs(self.data_dir, exist_ok=True)
        util.setup_logging(os.path.join(self.data_dir, "logs", f"orchestrator-{util.stamp()[:8]}.log"),
                           getattr(args, "verbose", False))
        # A fresh install has no catalog yet. Source registry commands still
        # work: registering where to look does not require a library.
        try:
            self.cat = catalog_mod.Catalog(self.catalog_path)
        except FileNotFoundError:
            self.cat = None
        if self.cat is not None and getattr(args, "vault", None):
            self.cat.vault_override = args.vault
        if self.vault_root and util.within(self.data_dir, self.vault_root):
            raise SystemExit("refusing to run: the data directory is inside the Vault")
        self.index_path = os.path.join(self.data_dir, "vault-index.json")
        self.reg, changed = sources.load_registry(
            self.data_dir, self.cat.data.get("sources") if self.cat else None)
        if changed:
            sources.save_registry(self.data_dir, self.reg)
        self._providers = None

    @property
    def vault_root(self) -> str:
        if self.cat is not None:
            return self.cat.vault_root
        return getattr(self.args, "vault", None) or ""

    def require_catalog(self):
        if self.cat is None:
            raise SystemExit(f"no works catalog at {self.catalog_path}. Register sources first "
                             f"(`sources add`), or create the catalog to track a library.")
        return self.cat

    def policy(self):
        return adapters.policy_from(self.reg, vault_root=self.vault_root or None,
                                    intake_root=self.intake_base() if self.cat else None)

    def adapter(self, entry: dict):
        self.providers()  # ensures self.http exists
        return adapters.build(entry, http=self.http, policy=self.policy(), providers=self._providers)

    # -- shared pieces ------------------------------------------------------------
    def providers(self) -> dict:
        if self._providers is None:
            http = HttpClient(os.path.join(self.data_dir, "cache", "http"),
                              offline=getattr(self.args, "offline", False))
            self._providers = {"mangaupdates": MangaUpdates(http), "ndl": NDL(http)}
            if not getattr(self.args, "no_anilist", False):
                self._providers["anilist"] = AniList(http)
            self.http = http
        return self._providers

    def index(self, rescan: bool = True) -> dict:
        if rescan or not os.path.exists(self.index_path):
            LOG.info("scanning %s (read-only; hashes cached by size+mtime)...", self.cat.vault_root)
            return vault_scan.scan_vault(self.cat.vault_root, self.index_path,
                                         hash_files=not getattr(self.args, "no_hash", False))
        return vault_scan.load_index(self.index_path)

    def state(self, rescan: bool = True):
        self.require_catalog()
        idx = self.index(rescan)
        tree = layout.Tree(self.cat.vault_root, idx)
        # Families first: a folder nobody has claimed becomes a family, and
        # only then can the pass below adopt the works inside it.
        changes = layout.adopt_vault_families(self.cat, tree)
        changes += layout.adopt_vault_folders(self.cat, tree)
        aliases = layout.harvest_aliases(self.cat, tree)
        planned = layout.plan_paths(self.cat, tree)
        if self.cat.dirty:
            self.cat.save(reason="state")
        log = scaffold.read_log(os.path.join(self.data_dir, "scaffold-log.jsonl"))
        lay = layout.build_layout(self.cat, tree, unofficial_hosts=self.reg.get("unofficial_hosts", []),
                                  scaffold_log=log)
        cov = coverage.compute(self.cat, tree)
        coverage.reconcile_layout(lay, cov)
        return {"index": idx, "tree": tree, "layout": lay, "coverage": cov, "adopted": changes,
                "aliases": aliases, "planned": planned}

    def source_adapters(self, capability: str | None = None) -> list:
        """Every ENABLED registered source, as an adapter."""
        self.providers()
        return adapters.for_registry(self.reg, http=self.http, policy=self.policy(),
                                     providers=self._providers, capability=capability)

    def write_all(self, st: dict, *, scaffold_actions=None, scaffold_applied=False, items=None) -> dict:
        items = acquire.plan(self.cat, st["coverage"], self.reg) if items is None else items
        disc = report.discovered_all(self.data_dir)
        fresh = report.freshness(st.get("index"), disc, self.cat.data)
        report.write_state(self.data_dir, st["layout"], st["coverage"], fresh)
        qsum = report.write_queue(self.data_dir, self.cat, st["coverage"], st["layout"], items)
        csum = report.write_coverage_report(self.data_dir, self.cat, st["coverage"], st["layout"], disc)
        acquire.write_manual(self.data_dir, items)
        if scaffold_actions is None:
            scaffold_actions = scaffold.plan(self.cat, st["layout"])
        # collect_review reads coverage from the works; the markers are removed
        # again before anything is saved, so they never reach the catalog.
        for _fam, w in self.cat.iter_works():
            c = st["coverage"].get(w["id"]) or {}
            w["_coverage_status"], w["_coverage_reason"] = c.get("status"), c.get("reason")
        try:
            review = report.collect_review(self.cat, st["layout"], disc, scaffold_actions,
                                           util.read_json(os.path.join(self.data_dir, "ingest-last.json")))
        finally:
            for _fam, w in self.cat.iter_works():
                w.pop("_coverage_status", None)
                w.pop("_coverage_reason", None)
        report.write_review(self.data_dir, review)
        report.write_vault_audit(self.data_dir, st["layout"], scaffold_actions, scaffold_applied)
        wpath = os.path.join(self.data_dir, "update-watch.json")
        watch = updates.merge(self.cat, st["coverage"], util.read_json(wpath))
        util.write_json(wpath, watch)
        report.write_watch_md(self.data_dir, watch)
        return {"items": items, "queue": qsum, "coverage": csum, "review": review, "watch": watch}


def _print_counts(title: str, counter: Counter) -> None:
    LOG.info("%s: %s", title, ", ".join(f"{k} {v}" for k, v in sorted(counter.items())) or "none")


# ---------------------------------------------------------------------------
def cmd_scan(ctx: Ctx) -> int:
    st = ctx.state(rescan=True)
    idx = st["index"]
    s = idx["stats"]
    LOG.info("files %d · %.1f GB · hashed %d (%.1f GB) · reused from cache %d · archives inspected %d · errors %d",
             s["files"], s["bytes"] / 1e9, s["hashed"], s["hashed_bytes"] / 1e9, s["reused"],
             s["archives_inspected"], s["errors"])
    dups = vault_scan.duplicate_groups(idx)
    LOG.info("byte-identical duplicate groups: %d%s", len(dups), " (reported only, nothing deleted)" if dups else "")
    for g in dups[:20]:
        LOG.info("  %s", " == ".join(g["paths"]))
    owner = layout.attribute(ctx.cat, st["tree"])
    unident = [r for r in st["tree"].files if r not in owner]
    LOG.info("files not attributed to any work: %d", len(unident))
    for r in unident[:30]:
        LOG.info("  %s", r)
    if st["adopted"]:
        LOG.info("existing Vault folders registered in the catalog (metadata only):")
        for c in st["adopted"]:
            LOG.info("  %s %s / %s  %s", c["action"], c["family"], c["work"], c.get("path") or c.get("to"))
    for fam, w in ctx.cat.iter_works():
        c = st["coverage"][w["id"]]
        if c["local_files"]:
            LOG.info("  %-60s %s", f"{fam['family'][:28]} / {w['work'][:30]}", report.cov_text(c))
    ctx.write_all(st)
    return 0


def cmd_discover(ctx: Ctx) -> int:
    st = ctx.state(rescan=not os.path.exists(ctx.index_path))
    res = discover.run(ctx.cat, ctx.providers(), ctx.data_dir, families=ctx.args.family, refresh=ctx.args.refresh,
                       max_nodes=ctx.args.max_nodes, max_depth=ctx.args.max_depth)
    ctx.cat.data["discovered_at"] = util.now_iso()
    ctx.cat.dirty = True
    if ctx.cat.dirty:
        ctx.cat.save(reason="discover")
    st = ctx.state(rescan=False)
    out = ctx.write_all(st)
    added = sum(o["outcome"] == "added" for r in res for o in r["outcomes"])
    LOG.info("discover: %d families · %d works added · %d review items · %d fan works recorded · http %s",
             len(res), added, sum(len(r["review"]) for r in res), sum(len(r["fan_works"]) for r in res),
             getattr(ctx, "http", None) and ctx.http.stats)
    LOG.info("coverage: %s", out["coverage"])
    return 0


def cmd_coverage(ctx: Ctx) -> int:
    st = ctx.state(rescan=not ctx.args.no_rescan)
    out = ctx.write_all(st)
    LOG.info("coverage: %s", out["coverage"])
    LOG.info("queue: %s", out["queue"])
    LOG.info("reports in %s", ctx.data_dir)
    return 0


def cmd_verify(ctx: Ctx) -> int:
    st = ctx.state(rescan=not ctx.args.no_rescan)
    out = ctx.write_all(st)
    lay = st["layout"]
    rows = [r for f in lay["families"] for r in f["works"]]
    finds = [x for f in lay["families"] for x in f["findings"]] + lay["global_findings"]
    c = Counter(r["layout_status"] for r in rows)
    s = lay["summary"]
    LOG.info("EXPECTED %d", s["expected"])
    for k in ("FOUND", "MISSING_FOLDER", "MISSING_CONTENT", "LEGACY_MAPPING", "CONTAINED", "REVIEW_REQUIRED",
              "AMBIGUOUS", "CONFLICT"):
        LOG.info("%-18s %d", k, c.get(k, 0))
    LOG.info("%-18s %d", "UNEXPECTED", s["unexpected"])
    LOG.info("%-18s %d", "POSSIBLE_DUPLICATE", s["possible_duplicate_groups"])
    LOG.info("%-18s %d (works + findings)", "REVIEW_REQUIRED*", s["review_required"])
    if ctx.args.details:
        for f in lay["families"]:
            for r in f["works"]:
                if r["layout_status"] not in ("FOUND", "CONTAINED"):
                    LOG.info("  [%s] %s / %s  %s", r["layout_status"], f["family_title"], r["work"], r["local_path"])
            for x in f["findings"]:
                LOG.info("  <%s> %s  %s", x["type"], x["path"], x["detail"])
    LOG.info("wrote VAULT_STRUCTURE_AUDIT.md and vault-layout.json (%d review items total)", len(out["review"]))
    return 0


def cmd_scaffold(ctx: Ctx) -> int:
    st = ctx.state(rescan=not ctx.args.no_rescan)
    actions = scaffold.plan(ctx.cat, st["layout"])
    counts = Counter(a["action"] for a in actions)
    LOG.info("scaffold %s", "APPLY" if ctx.args.apply else "DRY RUN (use --apply to create folders)")
    for kind in scaffold.ACTIONS:
        sel = [a for a in actions if a["action"] == kind]
        LOG.info("%s %d", kind, len(sel))
        if kind in ("CREATE", "CONFLICT", "AMBIGUOUS") or (ctx.args.details and kind != "EXISTS"):
            for a in sel[: ctx.args.limit]:
                LOG.info("    %s  (%s)", a["path"], a["reason"])
    applied = False
    if ctx.args.apply and counts.get("CREATE"):
        res = scaffold.apply(ctx.cat, actions, os.path.join(ctx.data_dir, "scaffold-log.jsonl"))
        _print_counts("result", Counter(r["result"] for r in res))
        applied = True
        st = ctx.state(rescan=False)
        actions = scaffold.plan(ctx.cat, st["layout"])
    ctx.write_all(st, scaffold_actions=actions, scaffold_applied=applied)
    return 0


def cmd_acquire(ctx: Ctx) -> int:
    st = ctx.state(rescan=not ctx.args.no_rescan)
    items = acquire.plan(ctx.cat, st["coverage"], ctx.reg)
    adapter_list = ctx.source_adapters()
    if not ctx.args.no_search and adapter_list:
        stats = acquire.search_sources(items, adapter_list, limit=ctx.args.search_limit)
        LOG.info("searched %d source(s) that support search: %d hit(s), %d error(s)",
                 stats["sources"], stats["hits"], stats["errors"])
    out = ctx.write_all(st, items=items)
    _print_counts("availability", Counter(i["availability"] for i in items))
    for i in items[: ctx.args.limit]:
        hits = len([h for h in i.get("source_hits") or [] if not h.get("error")])
        LOG.info("  %-11s %-55s %-28s %s", i["coverage_status"],
                 f"{i['family'][:25]} / {i['work'][:28]}", i["best_source"][:28],
                 f"{hits} source hit(s)" if hits else "")
    log_path = os.path.join(ctx.data_dir, "acquire-log.jsonl")
    got = acquire.acquire_from_sources(items, adapter_list, ctx.intake_base(), log_path, apply=ctx.args.apply)
    if ctx.args.apply:
        got += acquire.apply(items, HttpClient(os.path.join(ctx.data_dir, "cache", "http")),
                             ctx.intake_base(), ctx.reg, log_path)
        done = sum(r.get("result") in ("copied", "downloaded") for r in got)
        LOG.info("acquired into the intake: %d file(s). Nothing entered the Vault: run `ingest` "
                 "(dry run) then `ingest --apply`.", done)
        for r in got[:40]:
            LOG.info("  %s: %s", r.get("result"), r.get("path") or r.get("url") or r.get("name"))
    else:
        LOG.info("DRY RUN: %d file(s) an authorized source could hand over now; %d work(s) need you "
                 "(see MANUAL_ACQUISITION.md). Nothing was downloaded.",
                 len([r for r in got if r.get("result") == "would acquire"]),
                 sum(i["requires_user_action"] for i in items))
        for r in got[:20]:
            LOG.info("  would acquire %-40s from %s", r.get("name"), r.get("source"))
    return 0


def _intake_base(self) -> str:
    base = self.cat.data.get("intake_base")
    if not base:
        ir = self.cat.intake_root or r"C:\ContinuumIntake"
        base = os.path.dirname(ir) if os.path.basename(os.path.normpath(ir)).lower() != "continuumintake" else ir
    return base


def _intake_dirs(self, source_filter=None, single: str | None = None) -> list[str]:
    """Every source folder inside the intake base (or one explicit folder)."""
    if single:
        return [single]
    base = self.intake_base()
    if self.vault_root and util.within(base, self.vault_root):
        raise SystemExit("refusing: the intake is inside the Vault")
    os.makedirs(os.path.join(base, "Manual"), exist_ok=True)
    # A name starting with "_" is Continuum's own workspace inside the
    # intake (discarded arrivals, scratch). Ingesting those would re-import
    # exactly what the user threw away.
    dirs = [os.path.join(base, d) for d in sorted(os.listdir(base))
            if os.path.isdir(os.path.join(base, d)) and not d.startswith("_")]
    if source_filter:
        wanted = {s.lower() for s in source_filter}
        dirs = [d for d in dirs if os.path.basename(d).lower() in wanted]
    return dirs


Ctx.intake_base = _intake_base
Ctx.intake_dirs = _intake_dirs


def cmd_ingest(ctx: Ctx) -> int:
    dirs = ctx.intake_dirs(ctx.args.source, single=ctx.args.intake)
    map_path = ctx.args.map or os.path.join(os.path.dirname(os.path.abspath(ctx.catalog_path)), "intake-map.json")
    mapping = util.read_json(map_path) or {}
    idx = vault_scan.load_index(ctx.index_path) if os.path.exists(ctx.index_path) else None
    res = ingest.run(ctx.cat, dirs, mapping=mapping, index=idx, apply=ctx.args.apply,
                     log_path=ctx.args.log or os.path.join(ctx.data_dir, "import-log.jsonl"),
                     unofficial_hosts=ctx.reg.get("unofficial_hosts", []),
                     create_folders=ctx.args.create_folders)
    print(ingest.format_results(res))
    if not ctx.args.intake:
        util.write_json(os.path.join(ctx.data_dir, "ingest-last.json"),
                        {k: res[k] for k in ("mode", "intake_dirs", "counts", "review")}
                        | {"at": util.now_iso(),
                           "units": [dict(u, action=next((r["action"] for r in res["records"]
                                                          if r.get("unit") == u["unit"]), "pending"))
                                     for u in res["units"]]})
        if ctx.args.apply and any(not r["action"].startswith(("left", "identical")) for r in res["records"]):
            ctx.write_all(ctx.state(rescan=True))
    return 0


def cmd_update_check(ctx: Ctx) -> int:
    st = ctx.state(rescan=not ctx.args.no_rescan)
    wpath = os.path.join(ctx.data_dir, "update-watch.json")
    watch = updates.merge(ctx.cat, st["coverage"], util.read_json(wpath))
    alerts = updates.check(ctx.cat, watch, ctx.providers(), families=ctx.args.family)
    source_alerts = updates.check_sources(ctx.source_adapters(), watch)
    alerts += source_alerts
    watch["alerts"] = (watch.get("alerts") or [])[-200:] + source_alerts
    util.write_json(wpath, watch)
    report.write_watch_md(ctx.data_dir, watch, alerts)
    first = sum(1 for e in watch["works"] if e["baseline"].get("mu_checked"))
    LOG.info("update-check: %d works tracked, %d with a remote baseline, %d new alerts, %d with update available",
             len(watch["works"]), first, len(alerts), sum(1 for e in watch["works"] if e.get("update_available")))
    for a in alerts[:50]:
        LOG.info("  %s %s / %s: %s", a["kind"], a["family"], a.get("work") or "", a["detail"])
    LOG.info("nothing was downloaded (detection only)")
    return 0


def cmd_arrivals(ctx: Ctx) -> int:
    """New Arrivals: what is waiting, and your call on each one.

    Acquisition may fill this area by itself. Nothing leaves it for the Vault
    without an explicit approval, and nothing is ever deleted: discarding
    moves an arrival aside, where you can still change your mind.
    """
    action = ctx.args.arrivals_cmd
    base = ctx.intake_base()
    map_path = os.path.join(os.path.dirname(os.path.abspath(ctx.catalog_path)), "intake-map.json")
    mapping = util.read_json(map_path) or {}
    index = vault_scan.load_index(ctx.index_path) if os.path.exists(ctx.index_path) else None

    if action == "list":
        res = ingest.run(ctx.cat, ctx.intake_dirs(ctx.args.source), mapping=mapping, index=index,
                         apply=False, unofficial_hosts=ctx.reg.get("unofficial_hosts", []))
        print(ingest.format_results(res))
        return 0

    source, unit = ctx.args.source_name, ctx.args.unit
    src_dir = os.path.join(base, source)
    upath = os.path.join(src_dir, unit)
    if not util.within(upath, base) or not os.path.exists(upath):
        LOG.error("no arrival named %r in %r", unit, source)
        return 3

    if action == "prepare":
        out_dir = os.path.join(base, "Prepared", safe_unit := util.safe_folder_name(unit))
        decision = prepare.prepare(upath, out_dir, apply=ctx.args.apply)
        LOG.info("%s: %s", decision["action"], decision["reason"])
        LOG.info("  inside: %d image(s), %d chapter folder(s), %d packed archive(s)",
                 decision["images"], decision["chapter_dirs"], decision["nested_archives"])
        for output in decision["outputs"]:
            LOG.info("  %s %s", "wrote" if output.get("written") else "would write", output["output"])
        if decision["outputs"] and not ctx.args.apply:
            LOG.info("dry run: add --apply to build the prepared copy (your original is never touched)")
        elif decision["applied"]:
            LOG.info("prepared copy is in %s; it will be offered as the source 'Prepared'", out_dir)
        return 0

    if action == "approve":
        res = ingest.run(ctx.cat, [src_dir], mapping=mapping, index=index, apply=True,
                         log_path=os.path.join(ctx.data_dir, "import-log.jsonl"),
                         unofficial_hosts=ctx.reg.get("unofficial_hosts", []), only_units={unit},
                         create_folders=True)
        print(ingest.format_results(res))
        moved = [r for r in res["records"] if r["action"] in ("imported", "collision-preserved")]
        LOG.info("approved '%s': %d file(s) entered the Vault", unit, len(moved))
        if moved:
            ctx.write_all(ctx.state(rescan=True))
        return 0

    # discard: move aside, never delete. A discarded arrival stays on disk.
    parked = os.path.join(base, "_discarded", util.stamp(), source)
    os.makedirs(parked, exist_ok=True)
    target = os.path.join(parked, unit)
    shutil.move(upath, target)
    LOG.info("discarded '%s' -> %s (still on disk; delete it yourself when you are sure)", unit, target)
    return 0


def cmd_watch(ctx: Ctx) -> int:
    """One unattended pass: everything that can run without a human.

    Read the Vault, ask the sources what is new, take what an authorized
    source may hand over, and import whatever is waiting in the intake. With
    --apply the import actually happens; without it, nothing is written.

    What this deliberately cannot do: buy, log in, or download commercial
    material from a reader or store. Those need a person, so the pass ends
    by telling you what is waiting for you.
    """
    LOG.info("watch: %s", "APPLY (files may enter the intake and the Vault)" if ctx.args.apply
             else "DRY RUN (nothing is written)")
    st = ctx.state(rescan=True)
    items = acquire.plan(ctx.cat, st["coverage"], ctx.reg)
    adapter_list = ctx.source_adapters()

    # 1. where can the missing material legally come from
    if not ctx.args.no_search and adapter_list:
        stats = acquire.search_sources(items, adapter_list, limit=ctx.args.search_limit)
        LOG.info("  searched %d source(s): %d hit(s)", stats["sources"], stats["hits"])

    # 2. take what an authorized source can hand over (a folder you own, a
    #    DRM-free direct file). Everything lands in the intake, never the Vault.
    fetched = acquire.acquire_from_sources(items, adapter_list, ctx.intake_base(),
                                           os.path.join(ctx.data_dir, "acquire-log.jsonl"),
                                           apply=ctx.args.apply)
    if ctx.args.apply:
        fetched += acquire.apply(items, HttpClient(os.path.join(ctx.data_dir, "cache", "http")),
                                 ctx.intake_base(), ctx.reg,
                                 os.path.join(ctx.data_dir, "acquire-log.jsonl"))
    acquired = sum(r.get("result") in ("copied", "downloaded") for r in fetched)
    LOG.info("  acquired into the intake: %d", acquired)

    # 3. import whatever is waiting in the intake
    map_path = os.path.join(os.path.dirname(os.path.abspath(ctx.catalog_path)), "intake-map.json")
    res = ingest.run(ctx.cat, ctx.intake_dirs(ctx.args.source), mapping=util.read_json(map_path) or {},
                     index=vault_scan.load_index(ctx.index_path), apply=ctx.args.apply,
                     log_path=os.path.join(ctx.data_dir, "import-log.jsonl"),
                     unofficial_hosts=ctx.reg.get("unofficial_hosts", []), create_folders=True)
    imported = sum(1 for r in res["records"] if r["action"] in ("imported", "collision-preserved"))
    waiting = sum(1 for r in res["records"] if r["action"].startswith("left-in-intake"))
    LOG.info("  imported into the Vault: %d · still waiting in the intake: %d", imported, waiting)
    util.write_json(os.path.join(ctx.data_dir, "ingest-last.json"),
                    {k: res[k] for k in ("mode", "intake_dirs", "counts", "review")}
                    | {"at": util.now_iso(),
                       "units": [dict(u, action=next((r["action"] for r in res["records"]
                                                      if r.get("unit") == u["unit"]), "pending"))
                                 for u in res["units"]]})

    # 4. what changed remotely since last time
    wpath = os.path.join(ctx.data_dir, "update-watch.json")
    watch = updates.merge(ctx.cat, st["coverage"], util.read_json(wpath))
    alerts = updates.check(ctx.cat, watch, ctx.providers(), families=ctx.args.family)
    source_alerts = updates.check_sources(adapter_list, watch)
    alerts += source_alerts
    watch["alerts"] = (watch.get("alerts") or [])[-200:] + source_alerts
    util.write_json(wpath, watch)

    # 5. republish the reports over whatever actually changed
    st = ctx.state(rescan=bool(imported or acquired))
    out = ctx.write_all(st, items=acquire.plan(ctx.cat, st["coverage"], ctx.reg))
    report.write_watch_md(ctx.data_dir, watch, alerts)

    needs_you = [i for i in out["items"] if i["requires_user_action"]]
    LOG.info("watch done: %d new alert(s) · %d work(s) need you · coverage %s",
             len(alerts), len(needs_you), out["coverage"])
    for a in alerts[:20]:
        LOG.info("  %s %s %s: %s", a["kind"], a["family"], a.get("work") or "", a["detail"])
    if not ctx.args.apply:
        LOG.info("this was a dry run; add --apply to let it import automatically")
    return 0


def cmd_run(ctx: Ctx) -> int:
    ctx.args.no_rescan = False
    cmd_scan(ctx)
    if not ctx.args.skip_discover:
        cmd_discover(ctx)
    ctx.args.no_rescan = True
    ctx.args.apply = ctx.args.apply_scaffold
    cmd_scaffold(ctx)
    ctx.args.apply = False
    cmd_acquire(ctx)
    return cmd_verify(ctx)


def _find_work(ctx: Ctx, family: str, work: str):
    fam = ctx.cat.get_family(family) or next(iter(ctx.cat.families_matching(family)), None)
    if fam is None:
        raise SystemExit(f"family not found: {family}")
    w = next((x for x in fam["works"] if work in (x["work"], x["id"])), None)
    if w is None:
        w, how, _ = ctx.cat.find_work(fam, [work])
    if w is None:
        raise SystemExit(f"work not found in {fam['family']}: {work} (works: {[x['work'] for x in fam['works']]})")
    return fam, w


def cmd_add_link(ctx: Ctx) -> int:
    fam, w = _find_work(ctx, ctx.args.family, ctx.args.work)
    try:
        link = sources.add_link(ctx.reg, w, ctx.args.url, note=ctx.args.note or "", direct_file=ctx.args.direct_file)
    except sources.RefusedSource as err:
        LOG.error("refused: %s", err)
        return 3
    ctx.cat.save(reason="add-link")
    LOG.info("linked %s / %s -> %s (source: %s)", fam["family"], w["work"], link["url"], link["source"])
    if link["source"] == "user-link":
        LOG.info("host %s is not in the registry; register it with add-source to classify its access model",
                 link["host"])
    return 0


def cmd_add_source(ctx: Ctx) -> int:
    try:
        sources.add_source(ctx.reg, ctx.args.key, ctx.args.name, ctx.args.url, ctx.args.access,
                           languages=ctx.args.language, download_permitted=ctx.args.download_permitted,
                           notes=ctx.args.note or "")
    except (sources.RefusedSource, ValueError) as err:
        LOG.error("refused: %s", err)
        return 3
    sources.save_registry(ctx.data_dir, ctx.reg)
    LOG.info("registered source %s (%s)", ctx.args.key, ", ".join(ctx.args.access))
    return 0


def cmd_add_unofficial(ctx: Ctx) -> int:
    changed = [h for h in ctx.args.host if sources.add_unofficial_host(ctx.reg, h)]
    sources.save_registry(ctx.data_dir, ctx.reg)
    LOG.info("unofficial hosts: %s (added %s)", ", ".join(ctx.reg["unofficial_hosts"]), changed or "none")
    return 0


def _source_row(entry: dict) -> str:
    test = entry.get("last_test") or {}
    mark = "ok" if test.get("ok") else ("FAIL" if test else "untested")
    return (f"  {'on ' if entry.get('enabled', True) else 'off'} {entry.get('id', ''):<22} "
            f"{entry.get('adapter', ''):<13} {mark:<9} {','.join(entry.get('capabilities') or []) or '-':<62} "
            f"{entry.get('url') or ''}")


def cmd_sources(ctx: Ctx) -> int:
    """add / list / remove / test / enable / disable registered sources."""
    action = ctx.args.sources_cmd
    reg = ctx.reg
    entries = reg.setdefault("sources", {})

    if action == "list":
        rows = [e for e in entries.values() if ctx.args.all or e.get("enabled", True)]
        rows.sort(key=lambda e: (not e.get("enabled", True), e.get("id") or ""))
        if ctx.args.json:
            print(json.dumps({"schema": reg.get("schema"), "count": len(rows), "sources": rows,
                              "unofficial_hosts": reg.get("unofficial_hosts", [])},
                             ensure_ascii=False, indent=2))
            return 0
        LOG.info("%d source(s) in %s", len(rows), os.path.join(ctx.data_dir, "sources.json"))
        LOG.info("  %-3s %-22s %-13s %-9s %-62s %s", "", "id", "adapter", "test", "capabilities", "url")
        for e in rows:
            LOG.info("%s", _source_row(e))
        if reg.get("unofficial_hosts"):
            LOG.info("never used as sources: %s", ", ".join(reg["unofficial_hosts"]))
        return 0

    if action == "add":
        try:
            entry = sources.add_source_url(
                reg, ctx.args.url, source_id=ctx.args.id, name=ctx.args.name, adapter=ctx.args.adapter,
                access=ctx.args.access, languages=ctx.args.language or ["en"], search=ctx.args.search,
                watch_url=ctx.args.watch_url, download_permitted=ctx.args.download_permitted,
                notes=ctx.args.note or "", roles=ctx.args.role, replace=ctx.args.replace)
        except (sources.RefusedSource, ValueError) as err:
            LOG.error("refused: %s", err)
            return 3
        LOG.info("registered %s (%s) -> %s", entry["id"], entry["adapter"], entry["url"])
        if not ctx.args.no_test:
            result = ctx.adapter(entry).self_test()
            sources.record_test(entry, result)
            _print_test(result)
        sources.save_registry(ctx.data_dir, reg)
        LOG.info("%s", _source_row(entry))
        return 0

    if action == "remove":
        removed = sources.remove_source(reg, ctx.args.id)
        if removed is None:
            LOG.error("no source with id %r (see `sources list`)", ctx.args.id)
            return 3
        sources.save_registry(ctx.data_dir, reg)
        LOG.info("removed %s (%s). It will not come back: the starter list is only applied to a new registry.",
                 removed.get("id"), removed.get("url"))
        return 0

    if action in ("enable", "disable"):
        entry = sources.set_enabled(reg, ctx.args.id, action == "enable")
        if entry is None:
            LOG.error("no source with id %r", ctx.args.id)
            return 3
        sources.save_registry(ctx.data_dir, reg)
        LOG.info("%s is now %s", entry["id"], "enabled" if entry["enabled"] else "disabled")
        return 0

    if action == "unofficial":
        added = [h for h in ctx.args.host if sources.add_unofficial_host(reg, h)]
        sources.save_registry(ctx.data_dir, reg)
        LOG.info("never used as sources: %s (added %s)", ", ".join(reg["unofficial_hosts"]), added or "none")
        return 0

    # test
    wanted = ctx.args.id or []
    selected = [e for key, e in entries.items()
                if (not wanted and (ctx.args.all or e.get("enabled", True)))
                or key in wanted or (e.get("id") in wanted)]
    if not selected:
        LOG.info("no sources to test (add one with `sources add <URL>`)")
        return 0
    ok = 0
    for entry in selected:
        try:
            result = ctx.adapter(entry).self_test()
        except Exception as err:  # a broken source must not end the run
            result = {"at": util.now_iso(), "source": entry.get("id"), "adapter": entry.get("adapter"),
                      "ok": False, "checks": [], "capabilities": [], "operations": [], "error": str(err)}
        sources.record_test(entry, result)
        ok += bool(result.get("ok"))
        _print_test(result)
    sources.save_registry(ctx.data_dir, reg)
    LOG.info("%d/%d source(s) usable", ok, len(selected))
    return 0 if ok else 1


def _print_test(result: dict) -> None:
    LOG.info("[%s] %s  %s", "ok  " if result.get("ok") else "FAIL", result.get("source"),
             result.get("error") or "")
    for check in result.get("checks") or []:
        LOG.info("      %-22s %-4s %s", check.get("check"), "ok" if check.get("ok") else "no",
                 check.get("detail") or "")
    if result.get("ok"):
        LOG.info("      capabilities: %s", ", ".join(result.get("capabilities") or []) or "none")
        LOG.info("      operations:   %s", ", ".join(result.get("operations") or []) or "none")


def cmd_status(ctx: Ctx) -> int:
    for name in ("vault-layout.json", "acquisition-queue.json", "REVIEW_REQUIRED.json", "update-watch.json"):
        d = util.read_json(os.path.join(ctx.data_dir, name)) or {}
        LOG.info("%-24s %s  %s", name, d.get("generated_at", "missing"),
                 d.get("summary") or (f"{len(d.get('items', []))} items" if "items" in d else ""))
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default=DEFAULT_DATA)
    ap.add_argument("--catalog", default=None, help="default: <data-dir>/works-catalog.json")
    ap.add_argument("--vault", default=None, help="override the catalog's vault_root (not saved)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        p = sub.add_parser(name, help=help_)
        p.set_defaults(fn=fn)
        return p

    p = add("scan", cmd_scan, "read-only Vault inventory")
    p.add_argument("--no-hash", action="store_true", help="skip hashing new/changed files")
    for name, fn, h in (("discover", cmd_discover, "find official material"), ("run", cmd_run, "full pipeline")):
        p = add(name, fn, h)
        p.add_argument("--family", nargs="*", help="limit to these families")
        p.add_argument("--refresh", action="store_true", help="ignore recent discovery results")
        p.add_argument("--offline", action="store_true", help="use only cached HTTP responses")
        p.add_argument("--no-anilist", action="store_true")
        p.add_argument("--max-nodes", type=int, default=60)
        p.add_argument("--max-depth", type=int, default=3)
        p.add_argument("--limit", type=int, default=60)
        p.add_argument("--details", action="store_true")
        p.add_argument("--no-hash", action="store_true")
        if name == "run":
            p.add_argument("--skip-discover", action="store_true")
            p.add_argument("--apply-scaffold", action="store_true", help="also create safe folders")
    for name, fn, h in (("coverage", cmd_coverage, "local vs remote + reports"),
                        ("verify-vault", cmd_verify, "structure audit"),
                        ("scaffold", cmd_scaffold, "missing folders (dry run)"),
                        ("acquire", cmd_acquire, "legal sources + manual list (dry run)"),
                        ("update-check", cmd_update_check, "detect updates (no downloads)")):
        p = add(name, fn, h)
        p.add_argument("--no-rescan", action="store_true", help="reuse the last vault index")
        p.add_argument("--details", action="store_true")
        p.add_argument("--limit", type=int, default=80)
        p.add_argument("--no-hash", action="store_true")
        if name in ("scaffold", "acquire"):
            p.add_argument("--apply", action="store_true")
        if name == "acquire":
            p.add_argument("--no-search", action="store_true",
                           help="skip asking registered sources where the missing works are")
            p.add_argument("--search-limit", type=int, default=None,
                           help="only search for the first N missing works")
            p.add_argument("--offline", action="store_true")
            p.add_argument("--no-anilist", action="store_true")
        if name == "update-check":
            p.add_argument("--family", nargs="*")
            p.add_argument("--offline", action="store_true")
            p.add_argument("--no-anilist", action="store_true")
    p = add("ingest", cmd_ingest, "intake -> Vault (dry run)")
    p.add_argument("--apply", action="store_true", help="actually copy (default: dry run)")
    p.add_argument("--intake", default=None, help="one source folder (default: every folder in the intake base)")
    p.add_argument("--source", nargs="*", help="only these intake source folders (e.g. Manual HaruNeko)")
    p.add_argument("--map", default=None)
    p.add_argument("--log", default=None)
    p.add_argument("--create-folders", action="store_true",
                   help="create a work's folder when an identified arrival has nowhere to go")
    p = add("add-link", cmd_add_link, "attach an official link to a work")
    p.add_argument("--family", required=True)
    p.add_argument("--work", required=True)
    p.add_argument("--url", required=True)
    p.add_argument("--note")
    p.add_argument("--direct-file", action="store_true", help="the URL is a direct file on a DRM-free channel")
    p = add("add-source", cmd_add_source, "register an official channel")
    p.add_argument("--key", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--url", required=True)
    p.add_argument("--access", nargs="+", required=True, choices=sources.ACCESS_ORDER)
    p.add_argument("--language", nargs="*", default=["en"])
    p.add_argument("--download-permitted", action="store_true",
                   help="ONLY for DRM-free files you are entitled to, fetchable without login")
    p.add_argument("--note")
    p = add("add-unofficial-host", cmd_add_unofficial, "never use these hosts as sources")
    p.add_argument("host", nargs="+")

    # -- sources: the registry of places Continuum may look --------------------
    sp = sub.add_parser("sources", help="register, list, test, enable and remove sources")
    sp.set_defaults(fn=cmd_sources)
    ss = sp.add_subparsers(dest="sources_cmd", required=True)

    sa = ss.add_parser("add", help="register a source from a URL or a local folder path")
    sa.add_argument("url")
    sa.add_argument("--id", help="registry id (default: derived from the host or folder name)")
    sa.add_argument("--name")
    sa.add_argument("--adapter", choices=sorted(adapters.KINDS), help="default: detected from the URL")
    sa.add_argument("--search", help="search template containing {q}")
    sa.add_argument("--watch-url", dest="watch_url", help="page or feed to watch for updates")
    sa.add_argument("--access", nargs="*", choices=sources.ACCESS_ORDER, default=None)
    sa.add_argument("--language", nargs="*", default=["en"])
    sa.add_argument("--role", nargs="*", help=f"e.g. {sources.ROLE_STORE_EN}, {sources.ROLE_STORE_JA}, "
                                              f"{sources.ROLE_ANIME}")
    sa.add_argument("--download-permitted", action="store_true",
                    help="ONLY for DRM-free material you are entitled to, fetchable without a login")
    sa.add_argument("--note")
    sa.add_argument("--replace", action="store_true", help="overwrite an existing id")
    sa.add_argument("--no-test", action="store_true", help="skip the probe (no network)")
    sa.add_argument("--offline", action="store_true")

    sl = ss.add_parser("list", help="show the registry")
    sl.add_argument("--json", action="store_true")
    sl.add_argument("--all", action="store_true", help="include disabled sources")

    sr = ss.add_parser("remove", help="delete a source from the registry")
    sr.add_argument("id")

    st = ss.add_parser("test", help="probe sources and record what they can do")
    st.add_argument("id", nargs="*")
    st.add_argument("--all", action="store_true", help="include disabled sources")
    st.add_argument("--offline", action="store_true")

    se = ss.add_parser("enable", help="use this source again")
    se.add_argument("id")
    sd = ss.add_parser("disable", help="keep the entry but stop using it")
    sd.add_argument("id")
    su = ss.add_parser("unofficial", help="mark hosts that must never be used as sources")
    su.add_argument("host", nargs="+")

    p = add("watch", cmd_watch, "unattended pass: detect updates, acquire what is allowed, import")
    p.add_argument("--apply", action="store_true",
                   help="actually acquire and import (default: report only)")
    p.add_argument("--no-search", action="store_true")
    p.add_argument("--search-limit", type=int, default=None)
    p.add_argument("--family", nargs="*")
    p.add_argument("--source", nargs="*", help="only these intake folders")
    p.add_argument("--offline", action="store_true")
    p.add_argument("--no-anilist", action="store_true")
    p.add_argument("--no-hash", action="store_true")
    p.add_argument("--limit", type=int, default=40)
    p.add_argument("--details", action="store_true")

    ap_arr = sub.add_parser("arrivals", help="New Arrivals: review, approve or set aside")
    ap_arr.set_defaults(fn=cmd_arrivals)
    arr = ap_arr.add_subparsers(dest="arrivals_cmd", required=True)
    al = arr.add_parser("list", help="what is waiting and where it would go")
    al.add_argument("--source", nargs="*")
    for name, helptext in (("approve", "import one arrival into the Vault"),
                           ("discard", "move one arrival aside (never deleted)"),
                           ("prepare", "reshape an arrival into chapter folders, in the intake")):
        sp2 = arr.add_parser(name, help=helptext)
        sp2.add_argument("--source-name", required=True, help="intake folder it arrived in")
        sp2.add_argument("--unit", required=True, help="the arrival's folder or file name")
        sp2.add_argument("--source", nargs="*", help=argparse.SUPPRESS)
        if name == "prepare":
            sp2.add_argument("--apply", action="store_true",
                             help="build the prepared copy (default: say what it would do)")

    add("status", cmd_status, "summary of the last run")
    return ap


def main(argv=None) -> int:
    util.utf8_console()
    args = build_parser().parse_args(argv)
    ctx = Ctx(args)
    return args.fn(ctx)


if __name__ == "__main__":
    sys.exit(main())
