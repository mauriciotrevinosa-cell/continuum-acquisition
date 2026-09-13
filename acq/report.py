"""Reports in the DATA directory (never in the Vault).

  vault-layout.json, vault-coverage.json      machine-readable state
  acquisition-queue.json/.csv, ACQUISITION_QUEUE.md
  LIBRARY_COVERAGE_REPORT.md                  per-family tree with icons
  MANUAL_ACQUISITION.md/.csv                  (acquire.write_manual)
  REVIEW_REQUIRED.md/.json                    everything a human must decide
  VAULT_STRUCTURE_AUDIT.md                    structure audit per family
  update-watch.json, UPDATE_WATCH.md
"""
from __future__ import annotations

import csv
import glob
import os
from collections import Counter, defaultdict

from . import taxonomy as tx
from .util import now_iso, read_json, write_json, write_text

ICON = {"COMPLETE": "✅", "PARTIAL": "⚠️", "MISSING": "❌", "NEEDS_MAPPING": "🧭", "UNKNOWN": "❓",
        "BLOCKED": "❓"}
CLASS_ORDER = {c: i for i, c in enumerate(tx.MATERIAL_CLASSES)}
LEGACY_V1_KEYS = {"order", "family", "work", "role", "medium", "acquisition_status", "vault_destination",
                  "destination_exists", "local_files", "coverage", "candidate_sources", "language",
                  "automatic_download_allowed", "automatic_download_reason", "manual_action_required",
                  "unofficial_sources", "notes"}


def esc(v) -> str:
    return str(v if v is not None else "").replace("|", "\\|").replace("\n", " ")


def cov_text(c: dict) -> str:
    if not c or not c.get("local_files"):
        return "no local files"
    if c.get("local_chapters"):
        s = f"Ch {c['chapter_min']}-{c['chapter_max']} ({c['local_chapters']} ch"
        s += f", gaps {c['gaps_text']})" if c.get("gaps") else ")"
    else:
        s = f"{c['local_files']} files"
    if c.get("remote_latest_chapter"):
        s += f" · latest known {c['remote_latest_chapter']}"
    elif c.get("remote_volumes"):
        s += f" · {c['remote_volumes']} vols known"
    return s


def fill_layout(layout: dict, cov: dict) -> dict:
    for f in layout["families"]:
        for r in f["works"]:
            c = cov.get(r["work_id"]) or {}
            r["coverage_status"] = c.get("status")
            r["coverage_reason"] = c.get("reason")
    return layout


def discovered_all(data_dir: str) -> list[dict]:
    out = []
    for p in sorted(glob.glob(os.path.join(data_dir, "discovered", "*.json"))):
        d = read_json(p)
        if d:
            out.append(d)
    return out


def freshness(index: dict | None, discovered: list[dict], catalog_data: dict | None) -> dict:
    """When each kind of knowledge was last renewed, stated rather than implied.

    Three clocks, because they go stale independently: the library scan (what
    is on disk), the catalogue refresh (what officially exists), and whether
    the scan hashed everything (duplicate detection trails a fast scan).
    """
    index = index or {}
    stamps = [d.get("generated_at") for d in discovered if d.get("generated_at")]
    stamps += [(catalog_data or {}).get("discovered_at")] if (catalog_data or {}).get("discovered_at") else []
    stats = index.get("stats") or {}
    return {
        "library_scanned_at": index.get("generated_at"),
        "library_scan_partial": bool(index.get("partial")),
        "library_files": stats.get("files"),
        "library_bytes": stats.get("bytes"),
        "unhashed_files": stats.get("unhashed"),
        "hashing": index.get("hashing"),
        "vault_root": index.get("vault_root"),
        "catalogue_refreshed_at": max(stamps) if stamps else None,
    }


def write_state(data_dir: str, layout: dict, cov: dict, fresh: dict | None = None) -> None:
    layout = fill_layout(layout, cov)
    if fresh is not None:
        layout["freshness"] = fresh
    write_json(os.path.join(data_dir, "vault-layout.json"), layout)
    doc = {"schema": "continuum.personal.vault-coverage/3", "generated_at": now_iso(), "works": cov}
    if fresh is not None:
        doc["freshness"] = fresh
    write_json(os.path.join(data_dir, "vault-coverage.json"), doc)


# ---------------------------------------------------------------------------
def write_queue(data_dir: str, cat, cov: dict, layout: dict, items: list[dict]) -> dict:
    lrows = {r["work_id"]: r for f in layout["families"] for r in f["works"]}
    by_id = {i["work_id"]: i for i in items}
    prev = read_json(os.path.join(data_dir, "acquisition-queue.json")) or {}
    prev_rows = {}
    for r in prev.get("works", []):
        prev_rows[r.get("work_id") or f"{r.get('family')}|{r.get('work')}"] = r
    rows = []
    for fam, w in cat.iter_works():
        c = cov.get(w["id"]) or {}
        lr = lrows.get(w["id"]) or {}
        it = by_id.get(w["id"])
        row = {
            "order": fam.get("order"), "family": fam["family"], "category": fam.get("category"), "work": w["work"],
            "work_id": w["id"], "relation": w.get("relation"), "material_class": w.get("material_class"),
            "medium": w.get("medium"), "official": w.get("official"), "origin": w.get("origin"),
            "review_status": w.get("review_status"), "confidence": w.get("confidence"),
            "coverage_status": c.get("status"), "coverage_reason": c.get("reason"), "coverage": cov_text(c),
            "flags": c.get("flags") or [], "layout_status": lr.get("layout_status"),
            "vault_destination": lr.get("local_path"), "destination_exists": lr.get("folder_exists"),
            "local_files": c.get("local_files"), "best_source": (it or {}).get("best_source"),
            "availability": (it or {}).get("availability"),
            "automatic_download_allowed": bool(it and it.get("downloadable")),
            "manual_action_required": bool(it and it.get("requires_user_action")),
            "search_title": (it or {}).get("search_title"), "priority": tx.PRIORITY.get(w.get("relation"), 8),
            "notes": w.get("notes") or "",
            # what a source actually offered, and what it costs the user:
            # the queue is what the UI reads, so it carries both.
            "url": (it or {}).get("url") or "",
            "requires_purchase": (it or {}).get("requires_purchase") or "",
            "source_hits": (it or {}).get("source_hits") or [],
        }
        old = prev_rows.get(w["id"]) or prev_rows.get(f"{fam['family']}|{w['work']}") or {}
        for k, v in old.items():  # personal fields added by hand survive every rebuild
            if k not in row and k not in LEGACY_V1_KEYS:
                row[k] = v
        rows.append(row)
    pending = sorted((r for r in rows if r["coverage_status"] in ("MISSING", "PARTIAL", "BLOCKED")
                      and r["official"] is not False),
                     key=lambda r: (r["order"] or 999, r["priority"], r["work"].lower()))
    summary = Counter(r["coverage_status"] for r in rows)
    summary_d = {"families": len(cat.families), "works": len(rows), **{k.lower(): v for k, v in summary.items()},
                 "queue": len(pending), "automatic_downloads_allowed": sum(r["automatic_download_allowed"] for r in rows)}
    write_json(os.path.join(data_dir, "acquisition-queue.json"),
               {"schema": "continuum.personal.acquisition-queue/2", "generated_at": now_iso(), "summary": summary_d,
                "queue": [f"{r['family']} / {r['work']}" for r in pending], "works": rows})
    cols = ["order", "family", "category", "work", "relation", "material_class", "official", "review_status",
            "coverage_status", "coverage", "layout_status", "vault_destination", "best_source", "availability",
            "automatic_download_allowed", "manual_action_required", "search_title", "notes"]
    with open(os.path.join(data_dir, "acquisition-queue.csv"), "w", encoding="utf-8-sig", newline="") as fh:
        wr = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        wr.writeheader()
        wr.writerows(rows)
    md = ["# Acquisition queue", "", f"Generated {now_iso()}.", "",
          f"**{summary_d['works']} works** in {summary_d['families']} families · "
          + " · ".join(f"{ICON.get(k, '')} {k.lower()} {v}" for k, v in sorted(summary.items()))
          + f" · automatic downloads allowed: **{summary_d['automatic_downloads_allowed']}**", "",
          "Order: family order, then main → sequel/prequel → spin-off → parallel/alternate → supplemental → "
          "colour/visual → anthology/doujin → other. Details and every legal channel: `MANUAL_ACQUISITION.md`.", "",
          "| # | Family | Work | Type | Status | Best official source | Destination |", "|---|---|---|---|---|---|---|"]
    for i, r in enumerate(pending, 1):
        md.append(f"| {i} | {esc(r['family'])} | {esc(r['work'])}{' *(review)*' if r['review_status'] != 'ACCEPTED' else ''}"
                  f" | {r['relation']} | {ICON.get(r['coverage_status'], '')} {esc(r['coverage_reason'])} | "
                  f"{esc(r['best_source'])} | `{esc(r['vault_destination'])}` |")
    write_text(os.path.join(data_dir, "ACQUISITION_QUEUE.md"), "\n".join(md) + "\n")
    return summary_d


# ---------------------------------------------------------------------------
def write_coverage_report(data_dir: str, cat, cov: dict, layout: dict, discovered: list[dict]) -> dict:
    lfam = {f["family_title"]: f for f in layout["families"]}
    fan_by_family = {d["family"]: d.get("fan_works") or [] for d in discovered}
    official = [(fam, w) for fam, w in cat.iter_works() if w.get("official") is not False]
    st = Counter((cov.get(w["id"]) or {}).get("status") for _, w in official)
    origins = Counter(w.get("origin") for _, w in official)
    review_n = sum(w.get("review_status") != "ACCEPTED" for _, w in official)
    unoff = sum(1 for f in layout["families"] for r in f["works"] if r.get("unofficial_provenance"))
    fans = sum(len(v) for v in fan_by_family.values())
    md = ["# Library coverage report", "", f"Generated {now_iso()} · Vault `{cat.vault_root}`", "",
          "✅ complete · ⚠️ partial · ❌ missing · ❓ unknown, blocked or needs review. OFFICIAL ≠ MAIN CANON: "
          "supplemental official material is listed (low priority, never hidden). Fan works are counted, not catalogued.",
          "", "## Summary", "", "| | Count |", "|---|---|",
          f"| Families | {len(cat.families)} |",
          f"| Official works known | {len(official)} (curated {origins.get('curated', 0)}, discovered "
          f"{origins.get('discovered', 0)}, adopted from Vault folders {origins.get('vault-adopted', 0)}) |",
          f"| ✅ Complete | {st.get('COMPLETE', 0)} |", f"| ⚠️ Partial | {st.get('PARTIAL', 0)} |",
          f"| ❌ Missing | {st.get('MISSING', 0)} |",
          f"| ❓ Unknown / blocked | {st.get('UNKNOWN', 0) + st.get('BLOCKED', 0)} |",
          f"| Works needing review | {review_n} |", f"| Fan works recorded (not catalogued) | {fans} |",
          f"| Local files | {layout['summary']['files']} ({layout['summary']['bytes'] / 1e9:.1f} GB) |",
          f"| Works whose local files carry metadata from hosts on your unofficial list | {unoff} |", "",
          "## Families", ""]
    for fam in sorted(cat.families, key=lambda f: f.get("order", 999)):
        works = [w for w in fam["works"] if w.get("official") is not False]
        done = sum((cov.get(w["id"]) or {}).get("status") == "COMPLETE" for w in works)
        md.append(f"### {fam.get('order', '')}. {fam['family']} — {fam.get('category') or ''} — {done}/{len(works)} complete")
        rows = {r["work_id"]: r for r in (lfam.get(fam["family"]) or {}).get("works", [])}
        by_class = defaultdict(list)
        for w in works:
            by_class[w.get("material_class") or "manga"].append(w)
        for cls in sorted(by_class, key=lambda c: CLASS_ORDER.get(c, 99)):
            md.append(f"- **{cls}**")
            for w in sorted(by_class[cls], key=lambda w: (tx.PRIORITY.get(w.get("relation"), 8), w["work"].lower())):
                c = cov.get(w["id"]) or {}
                r = rows.get(w["id"]) or {}
                icon = "❓" if w.get("review_status") != "ACCEPTED" and c.get("status") != "COMPLETE" else ICON.get(c.get("status"), "❓")
                bits = [w.get("relation") or "", cov_text(c)]
                if w.get("official") is None:
                    bits.append("official status unverified")
                if w.get("review_status") != "ACCEPTED":
                    bits.append(f"review ({w.get('confidence')}, {w.get('origin')})")
                if w.get("contained_in"):
                    bits.append(f"contained in {w['contained_in']}")
                if w.get("language") == "ja":
                    bits.append("Japanese release")
                if r.get("legacy_mapping"):
                    bits.append(f"legacy path ({r.get('legacy_kind')})")
                if c.get("flags"):
                    bits.append(", ".join(c["flags"]))
                md.append(f"  - {icon} {w['work']} · " + " · ".join(b for b in bits if b))
        fl = fan_by_family.get(fam["family"]) or []
        if fl:
            md.append(f"- *fan works recorded, not catalogued: {len(fl)}* — "
                      + "; ".join(x.get("title") or "?" for x in fl[:5]) + (" …" if len(fl) > 5 else ""))
        md.append("")
    write_text(os.path.join(data_dir, "LIBRARY_COVERAGE_REPORT.md"), "\n".join(md) + "\n")
    return {"official_works": len(official), **{k.lower(): v for k, v in st.items() if k}, "review": review_n,
            "fan_works": fans}


# ---------------------------------------------------------------------------
def collect_review(cat, layout: dict, discovered: list[dict], scaffold_actions: list[dict] | None,
                   ingest_last: dict | None) -> list[dict]:
    items = []
    for fam, w in cat.iter_works():
        if w.get("review_status") != "ACCEPTED":
            ev = (w.get("provenance") or [{}])[-1].get("evidence", "")
            items.append({"kind": "WORK_REVIEW", "family": fam["family"], "item": w["work"],
                          "detail": f"{w.get('origin')} · {w.get('relation')} · {w.get('material_class')} · "
                                    f"confidence {w.get('confidence')} · official {w.get('official')}. {ev}",
                          "action": "confirm (set review_status ACCEPTED, confidence high) or correct relation / "
                                    "official in works-catalog.json"})
        if w.get("_coverage_status") == "NEEDS_MAPPING":
            items.append({"kind": "NEEDS_MAPPING", "family": fam["family"], "item": w["work"],
                          "detail": w.get("_coverage_reason") or "local material of this kind is unmapped",
                          "action": "point the work's vault_subpath at the folder that holds it, or move the "
                                    "files into its folder yourself"})
        if w.get("layout_ambiguity"):
            a = w["layout_ambiguity"]
            items.append({"kind": "LAYOUT_AMBIGUOUS", "family": fam["family"], "item": w["work"],
                          "detail": f"existing folder {a['similar_existing_folder']} is similar ({a['score']})",
                          "action": "if it IS this work, set vault_subpath to it; otherwise leave as is"})
    for d in discovered:
        for r in d.get("review") or []:
            items.append({"kind": "DISCOVERY", "family": d["family"], "item": r.get("title"),
                          "detail": r.get("reason") + (f" · candidates: {r['candidates']}" if r.get("candidates") else "")
                          + (f" · {r['url']}" if r.get("url") else ""), "action": "verify with the publisher"})
    for f in layout["families"]:
        for x in f["findings"]:
            if x.get("review_required"):
                items.append({"kind": f"VAULT_{x['type']}", "family": f["family_title"], "item": x["path"],
                              "detail": x["detail"], "action": "decide manually; the tool never moves or deletes"})
    for x in layout.get("global_findings") or []:
        if x.get("review_required"):
            items.append({"kind": f"VAULT_{x['type']}", "family": "(Vault root)", "item": x["path"],
                          "detail": x["detail"], "action": "decide manually"})
    for m in layout.get("proposed_moves") or []:
        items.append({"kind": "PROPOSED_MOVE", "family": "", "item": m["source"],
                      "detail": f"→ {m['target']} · {m['reason']}" + (" · TARGET NAME EXISTS" if m["collision"] else ""),
                      "action": "move it yourself if you agree (never applied automatically)"})
    for a in scaffold_actions or []:
        if a["action"] in ("CONFLICT", "AMBIGUOUS"):
            items.append({"kind": f"SCAFFOLD_{a['action']}", "family": a["family"], "item": a["work"],
                          "detail": f"{a['path']} · {a['reason']}", "action": "resolve manually"})
    for r in (ingest_last or {}).get("review") or []:
        items.append({"kind": "INGEST", "family": "", "item": r["path"], "detail": r["reason"],
                      "action": r.get("fix") or "route it with intake-map.json"})
    return items


def write_review(data_dir: str, items: list[dict]) -> None:
    write_json(os.path.join(data_dir, "REVIEW_REQUIRED.json"),
               {"schema": "continuum.personal.review/1", "generated_at": now_iso(), "items": items})
    by_kind = defaultdict(list)
    for i in items:
        by_kind[i["kind"]].append(i)
    md = ["# Review required", "", f"Generated {now_iso()}. **{len(items)} items.** Nothing below was changed "
          "automatically: every item waits for your decision.", ""]
    md += [f"- {k}: {len(v)}" for k, v in sorted(by_kind.items())]
    for k, v in sorted(by_kind.items()):
        md += ["", f"## {k} ({len(v)})", "", "| Family | Item | Detail | What to do |", "|---|---|---|---|"]
        md += [f"| {esc(i['family'])} | {esc(i['item'])} | {esc(i['detail'])} | {esc(i['action'])} |" for i in v]
    write_text(os.path.join(data_dir, "REVIEW_REQUIRED.md"), "\n".join(md) + "\n")


# ---------------------------------------------------------------------------
def write_vault_audit(data_dir: str, layout: dict, scaffold_actions: list[dict] | None, applied: bool) -> None:
    s = layout["summary"]
    acts = defaultdict(list)
    for a in scaffold_actions or []:
        acts[a["family"]].append(a)
    creates = sum(a["action"] == "CREATE" for a in scaffold_actions or [])
    md = ["# Vault structure audit", "", f"Generated {now_iso()} · `{layout['vault_root']}`", "",
          "Scheme: `<Source Family>\\<material class>\\<Work>\\[<edition/release>\\]<raw files>`. Legacy paths are "
          "mapped, not migrated. Nothing was moved, renamed, overwritten or deleted.", "",
          "## Global summary", "", "| | Count |", "|---|---|",
          f"| TOTAL FAMILIES | {s['total_families']} |", f"| TOTAL WORKS | {s['total_works']} |",
          f"| FOUND (content present at scheme path) | {s['found']} |",
          f"| LEGACY PATHS MAPPED | {s['legacy_paths_mapped']} |",
          f"| MISSING FOLDER | {s['missing_folder']} |", f"| MISSING CONTENT (folder empty) | {s['missing_content']} |",
          f"| CONTAINED IN ANOTHER WORK | {s['contained']} |",
          f"| NEW FOLDERS CREATED (all runs) | {s['new_folders_created']} |",
          f"| FOLDERS TO CREATE (scaffold plan{' — applied' if applied else ', dry run'}) | {creates} |",
          f"| DUPLICATE FAMILY CANDIDATES | {s['duplicate_family_candidates']} |",
          f"| POSSIBLE DUPLICATE FILE GROUPS | {s['possible_duplicate_groups']} |",
          f"| UNEXPECTED | {s['unexpected']} |", f"| AMBIGUOUS ITEMS | {s['ambiguous']} |",
          f"| CONFLICTS | {s['conflicts']} |", f"| PROPOSED MOVES (never applied) | {s['proposed_moves']} |",
          f"| REVIEW REQUIRED | {s['review_required']} |",
          f"| Files / size | {s['files']} / {s['bytes'] / 1e9:.1f} GB |", ""]
    if layout.get("global_findings"):
        md += ["### Vault root findings", ""] + [f"- **{x['type']}** `{x['path']}` — {x['detail']}"
                                                 for x in layout["global_findings"]] + [""]
    md += ["## Families", ""]
    for f in layout["families"]:
        rows = f["works"]
        md += [f"### {f['family_title']}", "",
               f"- **Current path:** `{f['family_path']}` — {'exists' if f['folder_exists'] else 'MISSING'}, "
               f"{f['files']} files, {f['bytes'] / 1e9:.2f} GB",
               f"- **Aliases:** {', '.join(f['family_aliases'][:8]) or '—'}", "- **Expected structure:**", "", "```",
               os.path.basename(f["family_path"]) + "\\"]
        by_cls = defaultdict(list)
        for r in rows:
            if r["official_status"] is not False:
                by_cls[r["material_class"]].append(r)
        classes = sorted(set(by_cls) | {c for c, i in f["classes"].items() if i["exists"]},
                         key=lambda c: CLASS_ORDER.get(c, 99))
        for c in classes:
            info = f["classes"].get(c) or {"exists": False}
            md.append(f"  {c}\\" + ("" if info.get("exists") else "   (to create)"))
            for r in sorted(by_cls.get(c, []), key=lambda r: r["work"].lower()):
                exp = os.path.relpath(r["expected_path"], f["family_path"])
                loc = os.path.relpath(r["local_path"], f["family_path"]) if r["local_path"] else "—"
                tail = f"   → local {loc}" if r["legacy_mapping"] else ""
                md.append(f"    {os.path.basename(exp)}\\   [{r['layout_status']}]{tail}")
        md += ["```", "", "| Work | Relation | Class | Official | Layout | Coverage | Local path |",
               "|---|---|---|---|---|---|---|"]
        for r in rows:
            md.append(f"| {esc(r['work'])} | {r['relationship_type']} | {r['material_class']} | "
                      f"{r['official_status']} | {r['layout_status']} | {r.get('coverage_status') or ''} | "
                      f"`{esc(os.path.relpath(r['local_path'], f['family_path']) if r['local_path'] else '')}` |")
        legacy = [r for r in rows if r["legacy_mapping"]]
        md += ["", "**Legacy mappings:** " + ("; ".join(
            f"{r['work']} → `{os.path.relpath(r['local_path'], f['family_path'])}` ({r['legacy_kind']})" for r in legacy)
                                            or "none")]
        fa = acts.get(f["family_title"], [])
        created_now = [a for a in fa if a["action"] == "CREATE"]
        md.append("**Folders created:** " + ("; ".join(f"`{os.path.relpath(p, f['family_path'])}`" for p in
                                                       f["folders_created"]) or "none")
                  + (f" · planned now ({'applied' if applied else 'dry run'}): "
                     + "; ".join(f"`{os.path.relpath(a['path'], f['family_path'])}`" for a in created_now)
                     if created_now else ""))
        issues = [x for x in f["findings"] if x["type"] in ("UNEXPECTED", "POSSIBLE_DUPLICATE", "CONFLICT", "INFO",
                                                            "MISSING_FOLDER")]
        amb = [x for x in f["findings"] if x["type"] == "AMBIGUOUS"] + \
              [{"path": r["work"], "detail": f"similar folder {r['layout_ambiguity']}"} for r in rows if r.get("layout_ambiguity")]
        rev = [x for x in f["findings"] if x["type"] == "REVIEW_REQUIRED"] + \
              [{"path": r["work"], "detail": f"{r['origin']}, {r['confidence']} confidence"} for r in rows
               if r["layout_status"] == "REVIEW_REQUIRED"]
        md.append("**Issues:** " + ("; ".join(f"{x['type']}: `{x['path']}` — {x['detail']}" for x in issues) or "none"))
        md.append("**Ambiguities:** " + ("; ".join(f"`{x['path']}` — {x['detail']}" for x in amb) or "none"))
        md.append("**Review required:** " + ("; ".join(f"`{x['path']}` — {x['detail']}" for x in rev) or "none"))
        md.append("")
    write_text(os.path.join(data_dir, "VAULT_STRUCTURE_AUDIT.md"), "\n".join(md) + "\n")


def write_watch_md(data_dir: str, watch: dict, new_alerts: list[dict] | None = None) -> None:
    ua = [e for e in watch["works"] if e.get("update_available")]
    md = ["# Update watch", "", f"Generated {now_iso()} · last remote check: {watch.get('last_check') or 'never'}", "",
          "Detection only: nothing is downloaded because an update appeared.", "",
          f"## New alerts this check ({len(new_alerts or [])})", ""]
    md += [f"- **{a['kind']}** {a['family']}{' / ' + a['work'] if a.get('work') else ''}: {a['detail']}"
           for a in new_alerts or []] or ["- none"]
    md += ["", f"## Works with an update available ({len(ua)})", "", "| Family | Work | Local | Remote | Checked |",
           "|---|---|---|---|---|"]
    md += [f"| {esc(e['family'])} | {esc(e['work'])} | {e.get('latest_local')} | {e.get('latest_remote')} | "
           f"{e.get('last_checked') or ''} |" for e in ua]
    write_text(os.path.join(data_dir, "UPDATE_WATCH.md"), "\n".join(md) + "\n")
