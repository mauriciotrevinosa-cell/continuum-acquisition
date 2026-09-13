#!/usr/bin/env python3
"""Compatibility wrapper: build_queue.py -> acquisition_orchestrator.py coverage.

Same flags as before. Queue, coverage and update-watch are now produced by the
orchestrator (update-watch still MERGES: remote fields and check times are
never lost). --create-missing now means "scaffold --apply": only safe folders
for official, high-confidence works are created; nothing is moved or deleted.
The original script is kept in legacy/build_queue_v1.py.
"""
from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import acquisition_orchestrator as orch  # noqa: E402

DEFAULT_CATALOG = r"C:\ContinuumData\acquisition\works-catalog.json"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--catalog", default=DEFAULT_CATALOG)
    ap.add_argument("--vault", default=None)
    ap.add_argument("--out", default=None, help="output directory (default: next to the catalog)")
    ap.add_argument("--create-missing", action="store_true", help="= scaffold --apply (safe folders only)")
    a = ap.parse_args()
    base = ["--catalog", a.catalog, "--data-dir", a.out or os.path.dirname(os.path.abspath(a.catalog))]
    if a.vault:
        base += ["--vault", a.vault]
    rc = orch.main(base + ["coverage"])
    if rc == 0 and a.create_missing:
        rc = orch.main(base + ["scaffold", "--apply", "--no-rescan"])
    return rc


if __name__ == "__main__":
    sys.exit(main())
