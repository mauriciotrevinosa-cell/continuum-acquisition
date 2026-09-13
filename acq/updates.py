"""Update watch: new chapters, volumes, related works and Japanese books.

DETECTION ONLY. A new chapter, volume, spin-off, guidebook, anthology or
colour edition becomes an alert; nothing is ever downloaded because of it.
The first check of a work/family records a baseline and raises no alerts.
"""
from __future__ import annotations

import re

from . import taxonomy as tx
from .http import ProviderUnavailable
from .providers import ndl as ndl_mod
from .util import LOG, now_iso, norm, read_json

WATCH_SCHEMA = "continuum.personal.update-watch/2"


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def merge(cat, cov: dict, prev: dict | None) -> dict:
    """Rebuild the watch list from the catalog + coverage, keeping every
    remote observation, check time, baseline and alert from before."""
    prev = prev or {}
    old = {}
    for e in prev.get("works", []):
        old[e.get("work_id") or f"{e.get('family')}|{e.get('work')}"] = e
    works = []
    for fam, w in cat.iter_works():
        if w.get("official") is False or w.get("relation") == tx.FAN_WORK:
            continue
        c = cov.get(w["id"]) or {}
        if c.get("status") == "BLOCKED":
            continue
        o = old.get(w["id"]) or old.get(f"{fam['family']}|{w['work']}") or {}
        remote = w.get("remote") or {}
        latest_remote = o.get("latest_remote", o.get("latest_remote_observed_chapter"))
        if latest_remote is None:
            latest_remote = remote.get("latest_chapter")
        local = c.get("chapter_max")
        remote_vols = o.get("remote_volumes") or remote.get("volumes") or (remote.get("ndl") or {}).get("volumes")
        local_vols = len(c.get("local_volumes") or [])
        ua = None
        if _num(latest_remote) is not None and local is not None:
            ua = _num(latest_remote) > local
        elif remote_vols and local_vols:
            ua = remote_vols > local_vols
        ids = w.get("external_ids") or {}
        works.append({
            "family": fam["family"], "work": w["work"], "work_id": w["id"], "relation": w.get("relation"),
            "latest_local": local, "local_volumes": local_vols, "latest_remote": latest_remote,
            "remote_volumes": remote_vols, "remote_completed": o.get("remote_completed", remote.get("completed")),
            "last_checked": o.get("last_checked"),
            "source": o.get("source") or ("mangaupdates" if ids.get("mangaupdates") else "ndl" if ids.get("isbn") else None),
            "update_available": ua, "status": c.get("status"), "confidence": w.get("confidence"),
            "baseline": o.get("baseline") or {}, "alerts": o.get("alerts") or [],
        })
    return {"schema": WATCH_SCHEMA, "generated_at": now_iso(),
            "note": "Detection only. Alerts never trigger downloads. Remote fields, check times, baselines and "
                    "alerts survive every rebuild.",
            "works": works, "families": prev.get("families") or {}, "alerts": prev.get("alerts") or []}


def check_sources(adapter_list: list, watch: dict) -> list[dict]:
    """Ask every source that can track updates whether anything changed.

    The first observation of a source records a baseline and raises nothing.
    An alert never triggers a download: commercial material is still
    acquired deliberately, by the user.
    """
    states = watch.setdefault("sources", {})
    alerts: list[dict] = []
    stamp = now_iso()
    for adapter in adapter_list:
        if not adapter.supports("get_update_status"):
            continue
        state = states.get(adapter.id) or {}
        try:
            status = adapter.get_update_status(state)
        except Exception as err:  # an unreachable source is news, not a crash
            states[adapter.id] = {**state, "last_checked": stamp, "last_error": str(err)[:300]}
            LOG.warning("source %s could not be checked: %s", adapter.id, err)
            continue
        if status.changed:
            alerts.append({"at": stamp, "family": "", "work": None, "kind": "SOURCE_UPDATED",
                           "source": adapter.id,
                           "detail": f"{adapter.name}: {status.latest or status.detail}"})
        states[adapter.id] = {"fingerprint": status.fingerprint, "latest": status.latest,
                              "detail": status.detail, "last_checked": stamp, "last_error": None}
    return alerts


def check(cat, watch: dict, providers: dict, *, families=None, fresh_days: float = 0.5) -> list[dict]:
    mu, ndl = providers.get("mangaupdates"), providers.get("ndl")
    by_id = {e["work_id"]: e for e in watch["works"]}
    known_mu = {str((w.get("external_ids") or {}).get("mangaupdates")) for _, w in cat.iter_works()}
    alerts: list[dict] = []
    stamp = now_iso()
    for fam in cat.families:
        if families and not any(norm(x) in (norm(fam["family"]), norm(fam["id"])) for x in families):
            continue
        for w in fam["works"]:
            e = by_id.get(w["id"])
            mu_id = (w.get("external_ids") or {}).get("mangaupdates")
            if e is None or not mu or not mu_id:
                continue
            try:
                node = mu.node(mu_id, ttl_days=fresh_days)
            except ProviderUnavailable as err:
                LOG.warning("MangaUpdates unavailable: %s", err)
                mu = None
                continue
            if not node:
                continue
            first = not e["baseline"].get("mu_checked")
            latest, vols = node.get("latest_chapter"), node.get("volumes")
            if not first:
                if _num(latest) and _num(e.get("latest_remote")) and _num(latest) > _num(e["latest_remote"]):
                    alerts.append({"at": stamp, "family": fam["family"], "work": w["work"], "kind": "NEW_CHAPTERS",
                                   "detail": f"latest chapter {e['latest_remote']} -> {latest}"})
                if vols and e.get("remote_volumes") and vols > e["remote_volumes"]:
                    alerts.append({"at": stamp, "family": fam["family"], "work": w["work"], "kind": "NEW_VOLUME",
                                   "detail": f"volumes {e['remote_volumes']} -> {vols}"})
                if node.get("completed") and not e.get("remote_completed"):
                    alerts.append({"at": stamp, "family": fam["family"], "work": w["work"], "kind": "COMPLETED",
                                   "detail": "series marked complete"})
            related = {str(rid): (etype, name) for etype, rid, name in node.get("related") or []}
            base_rel = set(e["baseline"].get("related_ids") or [])
            if not first:
                for rid, (etype, name) in related.items():
                    if rid not in base_rel and rid not in known_mu:
                        rel = tx.MU_RELATION.get(etype, tx.OTHER_OFFICIAL)
                        kind = "NEW_FAN_WORK" if rel == tx.FAN_WORK else "NEW_RELATED_WORK"
                        alerts.append({"at": stamp, "family": fam["family"], "work": w["work"], "kind": kind,
                                       "detail": f"{etype}: {name} (MangaUpdates id {rid}); run discover to catalogue"})
            e["baseline"]["related_ids"] = sorted(base_rel | set(related))
            e["baseline"]["mu_checked"] = True
            if latest is not None:
                e["latest_remote"] = latest
            if vols:
                e["remote_volumes"] = vols
            e["remote_completed"] = node.get("completed")
            e["last_checked"] = stamp
            e["source"] = "mangaupdates"
            if _num(e["latest_remote"]) is not None and e.get("latest_local") is not None:
                e["update_available"] = _num(e["latest_remote"]) > e["latest_local"]
        # Japanese books (guidebooks, anthologies, colour editions, new volumes)
        ja = next(((w.get("titles") or {}).get("ja") for w in fam["works"]
                   if w.get("relation") == tx.MAIN_WORK and (w.get("titles") or {}).get("ja")), None)
        if ndl and ja:
            try:
                recs = [r for r in ndl.search_books(re.sub(r"\s+", " ", ja), ttl_days=max(fresh_days, 1))
                        if ndl_mod.is_book(r)]
            except ProviderUnavailable as err:
                LOG.warning("NDL unavailable: %s", err)
                ndl = None
                continue
            fw = watch["families"].setdefault(fam["id"], {})
            seen = set(fw.get("ndl_isbns") or [])
            now_isbns = {i for r in recs for i in r.get("isbn") or []}
            if fw.get("ndl_checked"):
                for r in recs:
                    new = [i for i in r.get("isbn") or [] if i not in seen]
                    if not new:
                        continue
                    rel, _ = tx.keyword_relation([r.get("title"), r.get("series_title")], tx.STRONG)
                    alerts.append({"at": stamp, "family": fam["family"], "work": None,
                                   "kind": f"NEW_JP_BOOK{'_' + rel if rel else ''}",
                                   "detail": f"{r.get('title')} ({', '.join(r.get('publishers') or [])}) ISBN {new[0]}"})
            fw["ndl_isbns"] = sorted(seen | now_isbns)
            fw["ndl_checked"] = stamp
    for a in alerts:
        e = next((x for x in watch["works"] if x["family"] == a["family"] and x["work"] == a["work"]), None)
        if e is not None:
            e["alerts"] = (e.get("alerts") or [])[-19:] + [a]
    watch["alerts"] = (watch.get("alerts") or [])[-200:] + alerts
    watch["last_check"] = stamp
    return alerts
