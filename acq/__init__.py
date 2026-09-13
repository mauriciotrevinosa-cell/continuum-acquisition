"""Continuum Acquisition Orchestrator - personal acquisition tooling.

External helper, NOT part of the Continuum runtime. Franchise knowledge lives
only in the personal data directory (C:\\ContinuumData\\acquisition); nothing in
this package names a franchise.

Hard rules enforced throughout:
  * the raw Vault is read, never modified: no delete, overwrite, rename, move,
    extraction or normalisation of existing files;
  * the only writes into the Vault are new directories (scaffold --apply) and
    new files copied from the intake (ingest --apply), never onto an existing
    name;
  * downloads happen only from sources whose policy explicitly permits them.
"""

__version__ = "1.0.0"
