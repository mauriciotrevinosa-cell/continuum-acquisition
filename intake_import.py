#!/usr/bin/env python3
"""Compatibility wrapper: intake_import.py -> acquisition_orchestrator.py ingest.

DEFAULT IS STILL A DRY RUN; pass --apply to copy. Same flags and guarantees
as before (never overwrite, collisions kept as "(intake <sha8>)", identical
content skipped, intake originals untouched, no family/class/work folders
created). New: ComicInfo/file-name identification, colour-edition and fan
material sent to review, duplicates checked against the whole Vault index.
Without --intake the catalog's intake_root is used, exactly as before; the
orchestrator's `ingest` (no --intake) covers every intake source folder.
The original script is kept in legacy/intake_import_v1.py.
"""
from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import acquisition_orchestrator as orch  # noqa: E402
from acq import util  # noqa: E402

DEFAULT_CATALOG = r"C:\ContinuumData\acquisition\works-catalog.json"
DEFAULT_LOG = r"C:\ContinuumData\acquisition\import-log.jsonl"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="actually copy (default: dry run)")
    ap.add_argument("--catalog", default=DEFAULT_CATALOG)
    ap.add_argument("--intake", default=None)
    ap.add_argument("--vault", default=None)
    ap.add_argument("--map", default=None, help="intake-map.json (default: next to the catalog)")
    ap.add_argument("--log", default=DEFAULT_LOG)
    a = ap.parse_args()
    intake = a.intake or (util.read_json(a.catalog) or {}).get("intake_root")
    argv = ["--catalog", a.catalog, "--data-dir", os.path.dirname(os.path.abspath(a.catalog))]
    if a.vault:
        argv += ["--vault", a.vault]
    argv += ["ingest", "--intake", intake, "--log", a.log]
    if a.map:
        argv += ["--map", a.map]
    if a.apply:
        argv.append("--apply")
    return orch.main(argv)


if __name__ == "__main__":
    sys.exit(main())
