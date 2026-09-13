"""Relation taxonomy and generic classifiers.

Generic bibliographic knowledge only (relation types, common Japanese
publishing vocabulary, common publisher names). No franchise appears here.

OFFICIAL != MAIN CANON: relation says what a work IS; `authority` says how it
relates to the primary source. Neither asserts canon.
"""
from __future__ import annotations

import re

from .util import norm

MAIN_WORK = "MAIN_WORK"
SEQUEL = "SEQUEL"
PREQUEL = "PREQUEL"
OFFICIAL_SPINOFF = "OFFICIAL_SPINOFF"
PARALLEL_ADAPTATION = "PARALLEL_ADAPTATION"
ALTERNATE_ADAPTATION = "ALTERNATE_ADAPTATION"
OFFICIAL_ANTHOLOGY = "OFFICIAL_ANTHOLOGY"
OFFICIAL_DOUJIN = "OFFICIAL_DOUJIN"
ONE_SHOT = "ONE_SHOT"
SPECIAL_CHAPTER = "SPECIAL_CHAPTER"
GUIDEBOOK = "GUIDEBOOK"
FANBOOK_OFFICIAL = "FANBOOK_OFFICIAL"
ARTBOOK = "ARTBOOK"
COLORED_EDITION = "COLORED_EDITION"
DELUXE_OR_ALTERNATE_EDITION = "DELUXE_OR_ALTERNATE_EDITION"
VISUAL_REFERENCE = "VISUAL_REFERENCE"
CROSSOVER_OFFICIAL = "CROSSOVER_OFFICIAL"
IF_STORY = "IF_STORY"
PROMOTIONAL_OFFICIAL = "PROMOTIONAL_OFFICIAL"
OTHER_OFFICIAL = "OTHER_OFFICIAL"
FAN_WORK = "FAN_WORK"
UNKNOWN = "UNKNOWN"

RELATION_TYPES = [
    MAIN_WORK, SEQUEL, PREQUEL, OFFICIAL_SPINOFF, PARALLEL_ADAPTATION, ALTERNATE_ADAPTATION,
    OFFICIAL_ANTHOLOGY, OFFICIAL_DOUJIN, ONE_SHOT, SPECIAL_CHAPTER, GUIDEBOOK, FANBOOK_OFFICIAL,
    ARTBOOK, COLORED_EDITION, DELUXE_OR_ALTERNATE_EDITION, VISUAL_REFERENCE, CROSSOVER_OFFICIAL,
    IF_STORY, PROMOTIONAL_OFFICIAL, OTHER_OFFICIAL, FAN_WORK, UNKNOWN,
]

# Acquisition order requested: main -> sequel/prequel -> spin-off ->
# parallel/alternate -> supplemental -> colour/visual variants ->
# anthology/doujin -> other. Low priority is never hidden.
PRIORITY = {
    MAIN_WORK: 0, SEQUEL: 1, PREQUEL: 1, OFFICIAL_SPINOFF: 2, ONE_SHOT: 2, SPECIAL_CHAPTER: 2,
    IF_STORY: 2, CROSSOVER_OFFICIAL: 2, PARALLEL_ADAPTATION: 3, ALTERNATE_ADAPTATION: 3,
    GUIDEBOOK: 4, FANBOOK_OFFICIAL: 4, ARTBOOK: 4, VISUAL_REFERENCE: 4,
    COLORED_EDITION: 5, DELUXE_OR_ALTERNATE_EDITION: 5, OFFICIAL_ANTHOLOGY: 6, OFFICIAL_DOUJIN: 6,
    PROMOTIONAL_OFFICIAL: 7, OTHER_OFFICIAL: 7, UNKNOWN: 8, FAN_WORK: 9,
}

# Material classes (second Vault level). Story works live under their real
# medium; supplemental and fan material get their own class folder, so the
# filesystem separates KIND of material while canon stays in metadata.
STORY_CLASSES = ("manga", "manhwa", "manhua", "light-novel", "web-novel", "anime")
#: Classes whose material is moving image, not pages.
VIDEO_CLASSES = frozenset({"anime"})
SUPPLEMENTAL_FOLDER = {
    GUIDEBOOK: "guidebook", FANBOOK_OFFICIAL: "fanbook", ARTBOOK: "art-book",
    VISUAL_REFERENCE: "visual-reference", COLORED_EDITION: "colored-edition",
    DELUXE_OR_ALTERNATE_EDITION: "special", OFFICIAL_ANTHOLOGY: "anthology",
    OFFICIAL_DOUJIN: "official-doujin", PROMOTIONAL_OFFICIAL: "special",
    OTHER_OFFICIAL: "other-official", FAN_WORK: "fan-work",
}
MATERIAL_CLASSES = STORY_CLASSES + ("anthology", "official-doujin", "guidebook", "fanbook", "art-book",
                                    "colored-edition", "visual-reference", "special", "other-official",
                                    "fan-art", "fan-work")
FAN_CLASSES = ("fan-art", "fan-work")
NEVER_SCAFFOLD = {FAN_WORK, UNKNOWN}


def material_class(relation: str, medium: str) -> str:
    return SUPPLEMENTAL_FOLDER.get(relation, medium if medium in STORY_CLASSES else "manga")


medium_folder = material_class  # backwards-compatible name

EDITION_PATTERNS = [
    ("official-colored", [r"カラー版", r"フルカラー", r"\bfull[- ]?colou?r\b", r"\bcolou?red\b", r"彩色版"]),
    ("kanzenban", [r"完全版"]), ("aizoban", [r"愛蔵版"]), ("shinsoban", [r"新装版"]),
    ("special-edition", [r"特装版", r"\bspecial edition\b", r"collector'?s edition"]), ("limited", [r"限定版"]),
    ("deluxe", [r"豪華版", r"\bdeluxe\b"]), ("omnibus", [r"\bomnibus\b"]), ("box-set", [r"\bbox ?set\b"]),
]


def edition_of(texts, relation: str | None = None) -> str:
    """Edition/variant label. WORK and EDITION are different things: a colour
    edition is the same work in another edition, tracked as metadata."""
    blob = " | ".join(str(t) for t in texts if t)
    for label, patterns in EDITION_PATTERNS:
        if any(re.search(p, blob, re.IGNORECASE) for p in patterns):
            return label
    return "official-colored" if relation == COLORED_EDITION else "standard"


OFFICIAL_MARKERS = [r"公式", r"\bofficial\b", r"オフィシャル"]

# Checked in order against titles only (never against user tags: a "full
# colour" TAG on a natively coloured webtoon must not turn it into an edition).
STRONG = [
    (OFFICIAL_DOUJIN, [r"公式同人", r"official doujin", r"同人版"]),
    (OFFICIAL_ANTHOLOGY, [r"アンソロジー", r"\banthology\b", r"アンソロ"]),
    (COLORED_EDITION, [r"カラー版", r"フルカラー", r"\bfull[- ]?colou?r\b", r"\bcolou?red\b", r"彩色版"]),
    (VISUAL_REFERENCE, [r"設定資料", r"\bsetting materials?\b", r"キャラクターデザイン", r"\bcharacter designs?\b"]),
    (ARTBOOK, [r"画集", r"イラスト集", r"イラストレーションズ", r"\billustrations?\b", r"\bart ?books?\b",
               r"\bart ?works\b", r"原画集", r"アートブック", r"アートワークス"]),
    (FANBOOK_OFFICIAL, [r"ファンブック", r"\bfan ?book\b", r"オフィシャルブック", r"\bofficial book\b"]),
    (GUIDEBOOK, [r"ガイドブック", r"公式ガイド", r"\bguide ?book\b", r"\bofficial guide\b", r"データブック",
                 r"\bdata ?book\b", r"キャラクターブック", r"\bcharacter book\b", r"図鑑", r"ズカン", r"\bdaizukan\b",
                 r"大全", r"百科", r"解体新書", r"読本", r"\bencyclopedia\b"]),
    (DELUXE_OR_ALTERNATE_EDITION, [r"完全版", r"愛蔵版", r"新装版", r"特装版", r"限定版", r"豪華版", r"\bdeluxe\b",
                                   r"\bomnibus\b", r"collector'?s edition", r"\bspecial edition\b", r"\bbox ?set\b"]),
]
WEAK = [
    (ONE_SHOT, [r"\bone[- ]?shot\b", r"読切", r"読み切り"]),
    (SPECIAL_CHAPTER, [r"\bspecial chapter\b", r"番外編", r"特別編", r"\bextra chapter\b"]),
    (IF_STORY, [r"\bif[- ]?stor(?:y|ies)\b", r"ifストーリー", r"IFルート"]),
    (PROMOTIONAL_OFFICIAL, [r"カレンダー", r"\bcalendar\b", r"ポスター", r"パンフレット", r"\bpamphlet\b"]),
]

# MangaUpdates relation_type -> family relation (describes the RELATED series).
MU_RELATION = {
    "Sequel": SEQUEL, "Prequel": PREQUEL, "Side Story": OFFICIAL_SPINOFF, "Spin-Off": OFFICIAL_SPINOFF,
    "Main Story": MAIN_WORK, "Adapted From": MAIN_WORK, "Alternate Story": ALTERNATE_ADAPTATION,
    "Alternate Version": PARALLEL_ADAPTATION, "Doujinshi": FAN_WORK,
}
MU_TRAVERSE = {"Sequel", "Prequel", "Side Story", "Spin-Off", "Main Story", "Adapted From",
               "Alternate Story", "Alternate Version"}

# AniList relationType (version 2) -> family relation.
ANILIST_RELATION = {
    "SEQUEL": SEQUEL, "PREQUEL": PREQUEL, "SIDE_STORY": OFFICIAL_SPINOFF, "SPIN_OFF": OFFICIAL_SPINOFF,
    "ALTERNATIVE": ALTERNATE_ADAPTATION, "ADAPTATION": MAIN_WORK, "SOURCE": MAIN_WORK, "PARENT": MAIN_WORK,
    "SUMMARY": OTHER_OFFICIAL, "COMPILATION": DELUXE_OR_ALTERNATE_EDITION, "CONTAINS": SPECIAL_CHAPTER,
    "CHARACTER": CROSSOVER_OFFICIAL, "OTHER": OTHER_OFFICIAL,
}
ANILIST_TRAVERSE = {"SEQUEL", "PREQUEL", "SIDE_STORY", "SPIN_OFF", "ALTERNATIVE", "ADAPTATION", "SOURCE",
                    "PARENT", "SUMMARY", "COMPILATION", "CONTAINS"}

MU_TYPE_MEDIUM = {"manga": "manga", "manhwa": "manhwa", "manhua": "manhua", "novel": "light-novel",
                  "oel": "manga", "doujinshi": "manga", "artbook": "manga"}
ANILIST_FORMAT_MEDIUM = {"MANGA": "manga", "ONE_SHOT": "manga", "NOVEL": "light-novel", "TV": "anime",
                         "TV_SHORT": "anime", "MOVIE": "anime", "SPECIAL": "anime", "OVA": "anime", "ONA": "anime"}

# Common Japanese publishers: romanised name -> name as printed in Japanese
# bibliographic records. Used to prove a Japanese book came from the franchise's
# own publisher (official) rather than a third party (possible unofficial "謎本").
PUBLISHER_JA = {
    "shueisha": ["集英社"], "kodansha": ["講談社"], "shogakukan": ["小学館"],
    "square enix": ["スクウェア・エニックス", "スクウェアエニックス"], "kadokawa": ["KADOKAWA", "角川"],
    "kadokawa shoten": ["角川書店", "KADOKAWA"], "media factory": ["メディアファクトリー", "KADOKAWA"],
    "enterbrain": ["エンターブレイン", "KADOKAWA"], "ascii media works": ["アスキー・メディアワークス", "KADOKAWA"],
    "fujimi shobo": ["富士見書房", "KADOKAWA"], "micro magazine": ["マイクロマガジン"], "houbunsha": ["芳文社"],
    "ichijinsha": ["一迅社"], "futabasha": ["双葉社"], "overlap": ["オーバーラップ"],
    "sb creative": ["SBクリエイティブ", "ソフトバンククリエイティブ"], "hobby japan": ["ホビージャパン"],
    "alphapolis": ["アルファポリス"], "takarajimasha": ["宝島社"], "akita shoten": ["秋田書店"],
    "hakusensha": ["白泉社"], "takeshobo": ["竹書房"], "shonen gahosha": ["少年画報社"],
    "tokuma shoten": ["徳間書店"], "shinchosha": ["新潮社"], "gentosha": ["幻冬舎"], "mag garden": ["マッグガーデン"],
    "coamix": ["コアミックス"], "tobooks": ["TOブックス"], "earth star entertainment": ["アース・スター"],
    "gc novels": ["マイクロマガジン"], "kadokawa sneaker bunko": ["KADOKAWA", "角川"],
}


def keyword_relation(texts, table) -> tuple[str | None, str | None]:
    blob = " | ".join(str(t) for t in texts if t)
    for relation, patterns in table:
        for pat in patterns:
            if re.search(pat, blob, re.IGNORECASE):
                return relation, f"title keyword /{pat}/"
    return None, None


def classify(texts, *, provider_relation: str | None = None, relation_map: dict | None = None,
             provider_type: str | None = None) -> tuple[str, str]:
    """Return (relation, reason). Titles decide supplemental types first,
    the provider's relation decides story relations, weak keywords last."""
    texts = [t for t in texts if t]
    ptype = (provider_type or "").lower()
    if ptype == "doujinshi":
        official, _ = keyword_relation(texts, [(OFFICIAL_DOUJIN, OFFICIAL_MARKERS)])
        return (OFFICIAL_DOUJIN, "doujinshi with an official marker") if official else \
               (FAN_WORK, "doujinshi without an official marker")
    rel, why = keyword_relation(texts, STRONG)
    if rel:
        return rel, why
    if ptype == "artbook":
        return ARTBOOK, "provider type Artbook"
    mapped = (relation_map or {}).get(provider_relation or "")
    if mapped and mapped != OTHER_OFFICIAL:
        if mapped == OFFICIAL_SPINOFF:
            weak, why2 = keyword_relation(texts, WEAK[:1])  # one-shot refinement only
            if weak:
                return weak, why2
        return mapped, f"provider relation '{provider_relation}'"
    weak, why = keyword_relation(texts, WEAK)
    if weak:
        return weak, why
    return (mapped or UNKNOWN), ("provider relation '%s'" % provider_relation if mapped else "no evidence")


def authority(relation: str, medium: str, origin_medium: str | None) -> str:
    if relation == MAIN_WORK:
        return "PRIMARY_SOURCE" if (origin_medium is None or medium == origin_medium) else "ADAPTATION"
    if relation in (SEQUEL, PREQUEL):
        return "CONTINUATION"
    if relation in (OFFICIAL_SPINOFF, ONE_SHOT, SPECIAL_CHAPTER, IF_STORY, CROSSOVER_OFFICIAL):
        return "SPINOFF_OFFICIAL"
    if relation in (PARALLEL_ADAPTATION, ALTERNATE_ADAPTATION):
        return "ADAPTATION"
    if relation == FAN_WORK:
        return "FAN"
    if relation == UNKNOWN:
        return "UNVERIFIED"
    return "SUPPLEMENTAL_OFFICIAL"


def publisher_matches(printed: str, known_romanised) -> bool:
    p = norm(printed)
    if not p:
        return False
    for name in known_romanised or ():
        key = str(name).lower().strip()
        candidates = [name] + PUBLISHER_JA.get(key, [])
        for c in candidates:
            n = norm(c)
            if n and (n in p or p in n):
                return True
    return False


ROLE_TO_RELATION = {
    "main": MAIN_WORK, "manga adaptation": MAIN_WORK, "sequel": SEQUEL, "prequel": PREQUEL,
    "prequel spin-off": PREQUEL, "post-main continuation": SEQUEL, "official spin-off": OFFICIAL_SPINOFF,
    "parody spin-off": OFFICIAL_SPINOFF, "4-koma gag spin-off": OFFICIAL_SPINOFF, "side story": OFFICIAL_SPINOFF,
    "official side story": OFFICIAL_SPINOFF, "alternate-timeline spin-off": IF_STORY,
    "crossover parody": CROSSOVER_OFFICIAL, "parallel adaptation": PARALLEL_ADAPTATION,
}


def role_to_relation(role: str | None) -> str:
    return ROLE_TO_RELATION.get((role or "main").strip().lower(), OFFICIAL_SPINOFF if role else MAIN_WORK)
