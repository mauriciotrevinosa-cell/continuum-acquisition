"""Acquisition planning, and the rare authorized automatic download.

For every official work that is missing or partial: all known legal channels,
the best one, what it costs the user (purchase / free reading / library), and
the exact title to search. A file is downloaded automatically ONLY when the
channel is registered with download_permitted=true (DRM-free, no login) AND
the work carries a direct-file link on that channel AND the host is not on
the unofficial list. Downloads land in <intake>/<source>/<work>/ (never in the
Vault), through a .partial file renamed only when complete; nothing is ever
overwritten.
"""
from __future__ import annotations

import csv
import json
import os
from urllib.parse import unquote, urlparse

from . import adapters as adapters_mod
from . import sources as src
from . import taxonomy
from .util import LOG, now_iso, safe_folder_name, sha256_file, write_text

MANUAL_COLUMNS = ["FAMILY", "WORK", "TYPE", "OFFICIAL?", "MISSING?", "BEST OFFICIAL SOURCE", "LANGUAGE",
                  "AVAILABLE VOLUMES", "AVAILABLE CHAPTERS", "DOWNLOADABLE?", "REQUIRES PURCHASE?",
                  "REQUIRES USER ACTION?", "EXACT SEARCH TITLE", "ALIASES", "URL", "NOTES"]
WANTED = ("MISSING", "PARTIAL", "BLOCKED")


def _purchase_text(cands: list[dict]) -> str:
    free = [c["name"] for c in cands if "FREE_OFFICIAL_WEB" in c["access"]]
    buy = [c for c in cands if {"DRM_EBOOK", "DRM_FREE_PURCHASE"} & set(c["access"])]
    lib = [c for c in cands if "LIBRARY_LENDING" in c["access"]]
    if buy and free:
        return f"to OWN files: yes; free official reading (no file) on {free[0]}"
    if buy:
        return "yes" + (" (or borrow via library)" if lib else "")
    if free:
        return f"no: free official reading on {free[0]} (no file download)"
    if any("STREAMING" in c["access"] for c in cands):
        return "subscription / free tier (streaming)"
    return "unknown"


def plan(cat, cov: dict, reg: dict) -> list[dict]:
    items = []
    for fam, w in cat.iter_works():
        if w.get("official") is False or w.get("relation") == taxonomy.FAN_WORK:
            continue
        c = cov.get(w["id"]) or {}
        if c.get("status") not in WANTED:
            continue
        cands = src.candidates(fam, w, reg)
        avail = src.availability(cands) if c.get("status") != "BLOCKED" else "BLOCKED"
        best = cands[0] if cands else None
        remote = w.get("remote") or {}
        ndl = remote.get("ndl") or {}
        titles = w.get("titles") or {}
        search = " / ".join(x for x in dict.fromkeys([titles.get("en") or w["work"], titles.get("ja")]) if x)
        aliases = [a for a in (w.get("aliases") or []) + (w.get("intake_aliases") or []) if a][:6]
        missing = c["status"] if c["status"] != "PARTIAL" else f"PARTIAL: {c.get('reason')}"
        notes = [w.get("notes") or ""]
        if w.get("review_status") != "ACCEPTED":
            notes.append(f"catalog: {w.get('confidence')} confidence, needs review")
        if c.get("missing_chapters_text"):
            notes.append(f"missing chapters {c['missing_chapters_text']}")
        items.append({
            "family": fam["family"], "family_order": fam.get("order", 999), "category": fam.get("category"),
            "work": w["work"], "work_id": w["id"], "type": w.get("relation"), "material_class": w.get("material_class"),
            "official": w.get("official"), "coverage_status": c["status"], "missing": missing,
            "best_source": best["name"] if best else "— none identified —", "best_access":
                src.ACCESS_TEXT.get(next((a for a in best["access"]), "UNKNOWN"), "") if best else "",
            "language": (best or {}).get("language") or w.get("language") or "",
            "available_volumes": remote.get("volumes") or ndl.get("volumes"),
            "available_chapters": remote.get("latest_chapter"), "remote_status": remote.get("status_text"),
            "downloadable": avail == "AVAILABLE_AUTHORIZED", "requires_purchase": _purchase_text(cands),
            "requires_user_action": avail != "AVAILABLE_AUTHORIZED", "search_title": search, "aliases": aliases,
            "url": (best or {}).get("url") or "", "notes": "; ".join(n for n in notes if n),
            "availability": avail, "candidates": cands, "priority": taxonomy.PRIORITY.get(w.get("relation"), 8),
            "review_status": w.get("review_status"), "confidence": w.get("confidence"),
        })
    items.sort(key=lambda i: (i["family_order"], i["priority"], i["work"].lower()))
    return items


def manual_rows(items: list[dict]) -> list[dict]:
    rows = []
    for i in items:
        rows.append({
            "FAMILY": i["family"], "WORK": i["work"], "TYPE": f"{i['type']} ({i['material_class']})",
            "OFFICIAL?": {True: "yes", False: "no", None: "unverified"}[i["official"]], "MISSING?": i["missing"],
            "BEST OFFICIAL SOURCE": f"{i['best_source']} — {i['best_access']}".strip(" —"),
            "LANGUAGE": i["language"], "AVAILABLE VOLUMES": i["available_volumes"] or "",
            "AVAILABLE CHAPTERS": i["available_chapters"] or "", "DOWNLOADABLE?": "yes" if i["downloadable"] else "no",
            "REQUIRES PURCHASE?": i["requires_purchase"],
            "REQUIRES USER ACTION?": "yes" if i["requires_user_action"] else "no",
            "EXACT SEARCH TITLE": i["search_title"], "ALIASES": "; ".join(i["aliases"]), "URL": i["url"],
            "NOTES": i["notes"],
        })
    return rows


def write_manual(data_dir: str, items: list[dict]) -> None:
    rows = manual_rows(items)
    with open(os.path.join(data_dir, "MANUAL_ACQUISITION.csv"), "w", encoding="utf-8-sig", newline="") as fh:
        wr = csv.DictWriter(fh, fieldnames=MANUAL_COLUMNS)
        wr.writeheader()
        wr.writerows(rows)
    md = ["# Manual acquisition list", "", f"Generated {now_iso()}. Every row is an OFFICIAL work that is missing or "
          "partial in the Vault. Nothing here was downloaded: each row needs you (purchase, free official reader, "
          "library loan or streaming). After you get a file, drop it in `C:\\ContinuumIntake\\Manual` and run "
          "`ingest` (dry run) then `ingest --apply`.", "",
          f"**{len(rows)} items**, {sum(i['downloadable'] for i in items)} authorized automatic downloads.", "",
          "| " + " | ".join(MANUAL_COLUMNS) + " |", "|" + "---|" * len(MANUAL_COLUMNS)]
    for r in rows:
        cells = []
        for col in MANUAL_COLUMNS:
            v = str(r[col]).replace("|", "\\|").replace("\n", " ")
            if col == "URL" and v:
                v = f"[link]({v})"
            cells.append(v)
        md.append("| " + " | ".join(cells) + " |")
    md += ["", "## All legal channels per item", ""]
    for i in items:
        md.append(f"- **{i['family']} / {i['work']}** ({i['availability']})")
        for c in i["candidates"]:
            md.append(f"  - {c['name']} [{', '.join(c['access'])}] {c['url'] or ''} — {'; '.join(c['evidence'])}")
    write_text(os.path.join(data_dir, "MANUAL_ACQUISITION.md"), "\n".join(md) + "\n")


def search_sources(items: list[dict], adapter_list: list, *, per_source: int = 5,
                   limit: int | None = None) -> dict:
    """Ask every registered source that can search where the missing works are.

    This is the `user-provided source -> adapter -> search missing material`
    half of the flow. It only READS: a hit is a lead, not a download.
    """
    stats = {"sources": 0, "searched": 0, "hits": 0, "errors": 0}
    usable = [a for a in adapter_list if a.supports("search")]
    stats["sources"] = len(usable)
    for item in (items if limit is None else items[:limit]):
        hits = item.setdefault("source_hits", [])
        media = item.get("material_class")
        query = (item.get("search_title") or item["work"]).split(" / ")[0].strip()
        for adapter in usable:
            allowed = adapter.entry.get("media") or ()
            if media and allowed and media not in allowed:
                continue
            try:
                found = adapter.search(query, limit=per_source)
            except Exception as err:  # one broken source must not stop the sweep
                stats["errors"] += 1
                hits.append({"source": adapter.id, "error": str(err)[:300]})
                continue
            stats["searched"] += 1
            automatic = adapters_mod.AUTOMATIC_ACQUISITION in adapter.capabilities()
            for hit in found:
                stats["hits"] += 1
                hits.append({"source": adapter.id, "adapter": adapter.kind, "title": hit.title,
                             "url": hit.url, "score": hit.score, "kind": hit.kind, "automatic": automatic,
                             "capabilities": adapter.capabilities(), "detail": hit.detail})
        if any(h.get("automatic") for h in hits):
            item["availability"] = "AVAILABLE_AUTHORIZED"
            item["downloadable"] = True
            item["requires_user_action"] = False
    return stats


def acquire_from_sources(items: list[dict], adapter_list: list, intake_base: str, log_path: str, *,
                         apply: bool = False, limit_per_item: int = 200) -> list[dict]:
    """Copy/download what an AUTOMATIC_ACQUISITION source can legally give us.

    Everything lands in the intake, never in the Vault, so the normal
    identify / hash / duplicate-check / import step still decides what
    actually enters the library.
    """
    by_id = {a.id: a for a in adapter_list}
    results: list[dict] = []
    for item in items:
        for hit in item.get("source_hits") or []:
            if hit.get("error") or not hit.get("automatic"):
                continue
            adapter = by_id.get(hit.get("source"))
            if adapter is None or not adapter.supports("acquire"):
                continue
            ref = hit.get("url") or ""
            if adapter.kind == "local-folder" and ref:
                try:
                    ref = os.path.relpath(ref, adapter.root)
                except ValueError:
                    ref = ""
            try:
                files = adapter.get_available_files(ref)[:limit_per_item]
            except Exception as err:
                results.append({"work": item["work"], "source": adapter.id, "result": f"listing failed: {err}"})
                continue
            dest = os.path.join(intake_base, safe_folder_name(adapter.id), safe_folder_name(item["work"]))
            for file_ref in files:
                if not apply:
                    results.append({"work": item["work"], "source": adapter.id, "result": "would acquire",
                                    "name": file_ref.name, "bytes": file_ref.bytes, "path": dest})
                    continue
                try:
                    outcome = adapter.acquire(file_ref, dest)
                except Exception as err:
                    outcome = {"result": f"failed: {err}", "source": adapter.id}
                entry = {"at": now_iso(), "family": item.get("family"), "work": item["work"], **outcome}
                results.append(entry)
                if outcome.get("result") in ("copied", "downloaded"):
                    with open(log_path, "a", encoding="utf-8", newline="\n") as fh:
                        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
                    LOG.info("  %s %s -> %s", outcome["result"], file_ref.name, outcome.get("path"))
    return results


def apply(items: list[dict], http, intake_base: str, reg: dict, log_path: str) -> list[dict]:
    results = []
    for it in items:
        if it["availability"] != "AVAILABLE_AUTHORIZED":
            continue
        cand = next((c for c in it["candidates"] if c["download_permitted"] and c.get("download_url")
                     and not src.is_unofficial(c["download_url"], reg)), None)
        if cand is None:
            continue
        url = cand["download_url"]
        name = safe_folder_name(unquote(os.path.basename(urlparse(url).path)) or "download.bin")
        dest_dir = os.path.join(intake_base, safe_folder_name(cand["source"]), safe_folder_name(it["work"]))
        target = os.path.join(dest_dir, name)
        if os.path.exists(target):
            results.append({"work": it["work"], "url": url, "result": "already in intake", "path": target})
            continue
        os.makedirs(dest_dir, exist_ok=True)
        partial = target + ".partial"
        try:
            size = http.download(url, partial)
            os.rename(partial, target)  # Windows refuses to replace an existing file
        except FileExistsError:
            os.remove(partial)
            results.append({"work": it["work"], "url": url, "result": "target appeared meanwhile; kept it"})
            continue
        except Exception as err:  # network errors: leave nothing half-written behind
            if os.path.exists(partial):
                os.remove(partial)
            results.append({"work": it["work"], "url": url, "result": f"failed: {err}"})
            continue
        entry = {"at": now_iso(), "family": it["family"], "work": it["work"], "source": cand["source"], "url": url,
                 "path": target, "bytes": size, "sha256": sha256_file(target)}
        with open(log_path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        results.append(dict(entry, result="downloaded"))
        LOG.info("  downloaded %s", target)
    return results
