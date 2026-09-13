"""Shared helpers. Nothing here knows about any franchise."""
from __future__ import annotations

import datetime as dt
import glob
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import unicodedata
from difflib import SequenceMatcher

LOG = logging.getLogger("acq")


def utf8_console() -> None:
    """Windows consoles default to cp1252 and die on Japanese titles."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def setup_logging(log_file: str | None = None, verbose: bool = False) -> logging.Logger:
    LOG.setLevel(logging.DEBUG if verbose else logging.INFO)
    LOG.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(message)s"))
    LOG.addHandler(console)
    if log_file:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        LOG.addHandler(fh)
    LOG.propagate = False
    return LOG


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def norm(value) -> str:
    """Comparison key: NFKC, lower case, letters and digits of ANY script only.

    Keeps kana/kanji/hangul so Japanese titles stay comparable; drops
    punctuation, spaces and apostrophes so "Clayman's" ~ "Claymans".
    """
    s = unicodedata.normalize("NFKC", str(value or "")).replace("×", "x").lower()
    return "".join(ch for ch in s if ch.isalnum())


def tokens(value) -> set[str]:
    s = unicodedata.normalize("NFKC", str(value or "")).replace("×", "x").lower()
    return {norm(t) for t in re.split(r"[^\w]+", s) if norm(t)}


_tokens = tokens


def similarity(a, b) -> float:
    """0..1 title similarity: the better of character similarity and word-set
    overlap (so "Comic Anthology" ~ "Anthology Comic"), capped when the two
    titles carry different numbers ("X 2" is not "X")."""
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    r = SequenceMatcher(None, na, nb).ratio()
    ta, tb = _tokens(a), _tokens(b)
    if ta and tb:
        r = max(r, 2 * len(ta & tb) / (len(ta) + len(tb)))
    if re.findall(r"\d+", na) != re.findall(r"\d+", nb):
        r = min(r, 0.7)
    return r


def has_kana(s) -> bool:
    return bool(re.search(r"[぀-ヿ]", s or ""))


def has_cjk(s) -> bool:
    return bool(re.search(r"[぀-ヿ㐀-鿿]", s or ""))


def slug(value) -> str:
    s = unicodedata.normalize("NFKC", str(value or "")).lower().replace("×", "x")
    s = re.sub(r"[^\w]+", "-", s).strip("-")
    return s or "untitled"


_INVALID = '<>:"/\\|?*'


def safe_folder_name(name, max_len: int = 120) -> str:
    """Windows-safe folder name that keeps the title recognisable.

    Only characters Windows forbids are replaced; ':' becomes ' - ' to match
    the convention already used across the Vault.
    """
    s = unicodedata.normalize("NFKC", str(name or "")).replace("×", "x").replace(":", " - ")
    s = "".join("" if (ch in _INVALID or ord(ch) < 32) else ch for ch in s)
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"(?:\s-\s)+", " - ", s).strip().rstrip(". ")
    return s[:max_len].rstrip(". ") or "Untitled"


def sha256_file(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def read_json(path: str, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default


def write_json(path: str, obj) -> None:
    """Atomic write for OUR OWN data files (never used on the Vault)."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2, default=str)
        fh.write("\n")
    os.replace(tmp, path)


def write_text(path: str, text: str, bom: bool = False) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8-sig" if bom else "utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)


def backup(path: str, keep: int = 15) -> str | None:
    """Timestamped copy of one of OUR data files; prunes only our own backups."""
    if not os.path.exists(path):
        return None
    dst = f"{path}.bak-{stamp()}"
    shutil.copy2(path, dst)
    olds = sorted(glob.glob(glob.escape(path) + ".bak-*"))
    for old in olds[:-keep]:
        os.remove(old)
    return dst


def within(child: str, parent: str) -> bool:
    c = os.path.normcase(os.path.abspath(child))
    p = os.path.normcase(os.path.abspath(parent))
    try:
        return os.path.commonpath([c, p]) == p
    except ValueError:
        return False


def first_int(text) -> int | None:
    m = re.search(r"\d+", str(text or ""))
    return int(m.group(0)) if m else None
