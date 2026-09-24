import re
from dataclasses import dataclass
from typing import Iterable, Optional

_STOPWORDS = frozenset("""
a an the of to in on for and or with by at from as is are was were be been it its this that
these those your you we our their his her how what why when who new now just also into over after about
than more most can will says said say has have had not no vs via up out get gets
ai model
""".split())

_WORD_RE = re.compile(r"[a-z0-9]+(?:[.\-][a-z0-9]+)*")
_CJK_RE = re.compile(r"[一-鿿]")

TITLE_NEAR_IDENTICAL = 0.9
TITLE_OVERLAP_MIN = 0.6
SUMMARY_OVERLAP_MIN = 0.3


@dataclass(frozen=True)
class StoryFingerprint:
    item_id: int
    title_tokens: frozenset
    summary_grams: frozenset


def title_tokens(title: Optional[str]) -> frozenset:
    words = _WORD_RE.findall((title or "").lower().replace("’", "'"))
    out = set()
    for w in words:
        if w in _STOPWORDS or (len(w) < 2 and not w.isdigit()):
            continue
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.add(w)
    return frozenset(out)


def summary_grams(text: Optional[str]) -> frozenset:
    text = (text or "").lower()
    grams = set(_WORD_RE.findall(text))
    cjk = _CJK_RE.findall(text)
    grams.update(cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1))
    return frozenset(grams)


def fingerprint(item_id: int, title: Optional[str], one_liner: Optional[str]) -> StoryFingerprint:
    return StoryFingerprint(item_id, title_tokens(title), summary_grams(one_liner))


def _overlap(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def is_same_story(a: StoryFingerprint, b: StoryFingerprint) -> bool:
    if _jaccard(a.title_tokens, b.title_tokens) >= TITLE_NEAR_IDENTICAL:
        return True
    return (
        _overlap(a.title_tokens, b.title_tokens) >= TITLE_OVERLAP_MIN
        and _overlap(a.summary_grams, b.summary_grams) >= SUMMARY_OVERLAP_MIN
    )


def find_duplicate(candidate: StoryFingerprint, seen: Iterable[StoryFingerprint]) -> Optional[StoryFingerprint]:
    for other in seen:
        if other.item_id != candidate.item_id and is_same_story(candidate, other):
            return other
    return None
