# Continuum Acquisition Orchestrator

Personal library tooling for [Continuum](https://github.com/mauriciotrevinosa-cell/Continuum).
It answers three questions about a manga / light-novel / anime library:

1. **What do I have?** A read-only scan of the vault: cached SHA-256, chapter
   and volume ranges read from archive directories, duplicates, provenance.
2. **What officially exists?** Discovery across bibliographic sources
   (MangaUpdates relation graph, Japan's National Diet Library, AniList),
   classified by relation - main work, sequel, spin-off, anthology, guidebook,
   art book, colour edition - because OFFICIAL is not the same as MAIN CANON.
3. **Where may the rest legally come from?** A registry of sources the user
   adds, each probed for what it can actually do, and never used beyond that.

## Hard rules

The vault is RAW preservation. Nothing here deletes, overwrites, moves,
renames, extracts or re-encodes a file already in it. Imports copy to a
temporary name, verify the hash, and rename onto a name that does not exist.

No DRM, paywall, login or anti-bot circumvention, ever. robots.txt is
honoured. A host on the user unofficial list is refused as a source. An
automatic download requires a source explicitly marked as DRM-free material
the user is entitled to - which in practice means their own files.

Personal data (the catalog, coverage, the registry) lives in a data
directory outside this repository. Nothing here names a franchise: titles
come from the catalog at runtime, and the tests use invented ones.

## Layout

    acq/adapters/   one interface, many kinds of source (web, local folder,
                    bibliographic); capabilities gate what each may be asked
    acq/providers/  bibliographic APIs
    acq/            scan, layout, coverage, discovery, ingest, scaffold,
                    acquire, updates, reports
    seed/           an optional starter list of legal channels: data, not code
    tests/          56 tests over throwaway vaults in temp directories

## Commands

    python acquisition_orchestrator.py scan
    python acquisition_orchestrator.py discover
    python acquisition_orchestrator.py coverage
    python acquisition_orchestrator.py sources add "<url or folder>"
    python acquisition_orchestrator.py sources test
    python acquisition_orchestrator.py arrivals list
    python acquisition_orchestrator.py arrivals prepare --source-name X --unit Y
    python acquisition_orchestrator.py arrivals approve --source-name X --unit Y
    python acquisition_orchestrator.py ingest            # dry run by default
    python acquisition_orchestrator.py watch             # unattended pass

Destructive-looking verbs are dry runs until `--apply`. Standard library
only: no third-party dependencies.

    python -m unittest discover -s tests
