"""Metrics computed by code, without a model (EVAL-SET.md section 7): markup, shape, filler and boilerplate."""
import re
from statistics import median

# What the OLD interface shows as raw characters (white-space: pre-wrap, no markdown).
_OLD_VISIBLE = {
    "bold": re.compile(r"\*\*|__"),
    "heading": re.compile(r"(?m)^\s{0,3}#{1,6}\s"),
    "bullet": re.compile(r"(?m)^\s*[-*•]\s+\S"),
    "numbered": re.compile(r"(?m)^\s*\d{1,2}[.)]\s+\S"),
    "backtick": re.compile(r"`"),
    "table": re.compile(r"(?m)^\s*\|.*\|\s*$"),
    "rule": re.compile(r"(?m)^\s*(?:-{3,}|\*{3,}|_{3,})\s*$"),
}
# Constructions that even the NEW interface (narrow markdown subset) does not draw.
_NEW_UNSUPPORTED = {
    "table": re.compile(r"(?m)^\s*\|.*\|\s*$"),
    "code_block": re.compile(r"```"),
    "html": re.compile(r"</?[A-Za-z][^>]{0,80}>"),
    "image": re.compile(r"!\[[^\]]*\]\([^)]*\)"),
}
_LINK = re.compile(r"\[[^\]]+\]\([^)]*\)")

_INTRO = re.compile(r"^\W*(?:конечно|отличный вопрос|хороший вопрос|давайте разберёмся|давайте разберемся|безусловно|"
                    r"с удовольствием|разумеется|прекрасный вопрос)", re.IGNORECASE)
_OUTRO = re.compile(r"(?:если хотите|если хочешь|могу также|хотите, я|хочешь, я|дайте знать|обращайтесь|я могу также)",
                    re.IGNORECASE)
_SCIENCE = re.compile(r"(?:не является наук|не наука|научно не доказан|научно не подтвержд|не имеет научн|"
                      r"астрология не может доказать|не заменяет)", re.IGNORECASE)
_CHANCERY = re.compile(r"(?<!\w)(?:данн(?:ый|ая|ое|ые|ого|ому|ым|ой|ую|ых|ыми)|является|являются|осуществля\w*|"
                       r"важно отметить|стоит отметить|это может указывать на то, что)", re.IGNORECASE)
_EMOJI = re.compile("[\U0001F000-\U0001FAFF✨❤⭐✅❌⚡❗]")


def markup_flags(text):
    return {
        "old_visible": sorted(name for name, pattern in _OLD_VISIBLE.items() if pattern.search(text)),
        "new_unsupported": sorted([name for name, pattern in _NEW_UNSUPPORTED.items() if pattern.search(text)] +
                                  (["link"] if _LINK.search(text) else [])),
    }


def shape(text):
    paragraphs = [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    items = len(re.findall(r"(?m)^\s*(?:[-*•]|\d{1,2}[.)])\s+\S", text))
    return {"chars": len(text), "words": len(re.findall(r"\w+", text)), "paragraphs": len(paragraphs),
            "list_items": items}


def filler(text):
    letters = [c for c in text if c.isalpha()]
    latin = sum("a" <= c.lower() <= "z" for c in letters)
    words = re.findall(r"\w+", text.strip().lower())
    return {
        "intro": bool(_INTRO.search(text.strip())),
        "outro": bool(_OUTRO.search(text[-300:])),
        "science_hedge": len(_SCIENCE.findall(text)),
        "chancery": len(_CHANCERY.findall(text)),
        "emoji": len(_EMOJI.findall(text)),
        "latin_share": round(latin / len(letters), 4) if letters else 0.0,
        "start2": " ".join(words[:2]),
    }


def same_start_share(texts):
    """Share of answers whose first two words equal another answer's of the same configuration."""
    starts = [filler(t)["start2"] for t in texts if t.strip()]
    if len(starts) < 2:
        return 0.0
    return sum(starts.count(s) > 1 for s in starts) / len(starts)


def median_or_none(values):
    values = [v for v in values if v is not None]
    return median(values) if values else None
