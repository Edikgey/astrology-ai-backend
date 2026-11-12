"""Chart facts from the SAVED calculation rows (charts.json) and the code check of an answer (EVAL-SET.md section 7).

Nothing here recalculates a chart: facts are read from the rows the product stored (sign and house as saved,
degree within the sign by dropping, orbs from the saved aspect strings). Claims in an answer are extracted by
fixed rules and checked against those facts: confirmed / contradicted / unverifiable.
Sentences with hedges are skipped (spec: 'если бы', 'обычно', 'как правило', 'например'); this tool also skips
negations and general statements ('люди с ...'), because they do not assert a fact about this chart.
"""
import math
import re

SIGNS = ["Овен", "Телец", "Близнецы", "Рак", "Лев", "Дева", "Весы", "Скорпион", "Стрелец", "Козерог", "Водолей", "Рыбы"]

ASPECT_SYMBOLS = {  # saved symbol -> (code, Russian name, kind)
    "☌": ("conj", "соединение", "major"), "⚹": ("sext", "секстиль", "major"), "□": ("sq", "квадрат", "major"),
    "△": ("tri", "трин", "major"), "☍": ("opp", "оппозиция", "major"),
    "⚻": ("quincunx", "квинконс", "minor"), "⚺": ("semisextile", "полусекстиль", "minor"),
    "∠": ("semisquare", "полуквадрат", "minor"), "∠∠": ("sesquisquare", "полутороквадрат", "minor"),
    "Q": ("quintile", "квинтиль", "minor"), "bQ": ("biquintile", "биквинтиль", "minor"),
    "N": ("novile", "новиль", "minor"), "S": ("septile", "септиль", "minor"),
}
_ORB_LINE = re.compile(r"^(\S+) (.+) орбис: (\d+)° (\d+)'$")


def degree_in_sign(longitude):
    """Whole degrees and minutes within the sign, dropped (not rounded): 29.5667 is 29 deg 34 min, not 30."""
    within = longitude % 30
    total = math.floor(within * 60 + 1e-6)
    return total // 60, total % 60


def degree_text(longitude):
    degrees, minutes = degree_in_sign(longitude)
    return "%d°%02d′" % (degrees, minutes)


class ChartFacts:
    """Facts of one synthetic chart, from a charts.json entry."""

    def __init__(self, entry):
        data = entry["data"]
        self.bodies = {}
        for symbol, body in data["bodies_for_circle"].items():
            degrees, minutes = degree_in_sign(body["degree"])
            self.bodies[symbol] = {"symbol": symbol, "name": body["label"], "sign": body["sign"],
                                   "sign_from_longitude": SIGNS[int(body["degree"] // 30) % 12],
                                   "deg": degrees, "min": minutes, "house": body.get("house"),
                                   "retro": bool(body.get("retrograde")), "longitude": body["degree"]}
        self.by_name = {b["name"]: s for s, b in self.bodies.items()}
        self.house_system = data.get("house_system")
        self.cusps = []
        for row in data.get("houses") or []:
            degrees, minutes = degree_in_sign(row["degree"])
            self.cusps.append({"house": int(row["symbol"]), "sign": SIGNS[int(row["degree"] // 30) % 12],
                               "deg": degrees, "min": minutes})
        self.aspects = self._aspects(data.get("aspects_structured") or {})
        self.patterns = [{"type": p["type"], "bodies": [b["label"] for b in p["bodies"]]}
                         for p in data.get("patterns_data") or []]

    def _aspects(self, structured):
        names = sorted(self.by_name, key=len, reverse=True)
        rows = []
        for kind in ("major", "minor"):
            for line in structured.get(kind) or []:
                match = _ORB_LINE.match(line)
                if not match or match.group(1) not in ASPECT_SYMBOLS:
                    continue
                symbol, middle = match.group(1), match.group(2)
                first = next((n for n in names if middle.startswith(n + " ")), None)
                second = next((n for n in names if middle.endswith(" " + n)), None)
                if first is None or second is None:
                    continue
                code, russian, aspect_kind = ASPECT_SYMBOLS[symbol]
                rows.append({"a": self.by_name[first], "b": self.by_name[second], "code": code, "name": russian,
                             "kind": aspect_kind, "orb_deg": round(int(match.group(3)) + int(match.group(4)) / 60, 2)})
        return rows

    def aspect_codes(self, a, b):
        return {r["code"] for r in self.aspects if {r["a"], r["b"]} == {a, b}}

    def for_judge(self):
        """Compact facts for the blind judge (names, not glyphs; exact degrees; orbs from saved strings)."""
        return {
            "placements": [[b["name"], b["sign"], "%d°%02d′" % (b["deg"], b["min"]), b["house"], b["retro"]]
                           for b in self.bodies.values()],
            "placement_columns": ["body", "sign", "degree_in_sign", "house", "retrograde"],
            "house_system": self.house_system,
            "cusps": [[c["house"], c["sign"], "%d°%02d′" % (c["deg"], c["min"])] for c in self.cusps],
            "aspects": [[self.bodies[r["a"]]["name"], r["name"], self.bodies[r["b"]]["name"], r["orb_deg"], r["kind"]]
                        for r in self.aspects],
            "aspect_columns": ["body_a", "aspect", "body_b", "orb_deg", "kind"],
            "patterns": [[p["type"], p["bodies"]] for p in self.patterns],
            "note": "No asteroids are calculated. No transits exist in this data.",
        }


# ---------------------------------------------------------------- claim extraction (Russian answers)
def _norm(text):
    text = text.lower().replace("ё", "е")
    return re.sub(r"[′’ʹ`]", "'", text)


_EDGE = r"(?<![\w])(?:%s)(?![\w])"
_BODY_PATTERNS = {
    "☉": r"солнц(?:е|а|у|ем)",
    "☽": r"(?<!черная )(?<!белая )лун(?:а|ы|е|у|ой|ою)",
    "☿": r"меркури(?:й|я|ю|ем|и)",
    "♀": r"венер(?:а|ы|е|у|ой|ою)",
    "♂": r"марс(?:а|у|е|ом)?",
    "♃": r"юпитер(?:а|у|е|ом)?",
    "♄": r"сатурн(?:а|у|е|ом)?",
    "♅": r"уран(?:а|у|е|ом)?",
    "♆": r"нептун(?:а|у|е|ом)?",
    "♇": r"плутон(?:а|у|е|ом)?",
    "AS": r"асцендент(?:а|у|е|ом)?|асц|asc",
    "MC": r"mc|мс|(?:сред|серед)ин(?:а|ы|е|у|ой)\s+неба",
    "☊": r"северн\w+\s+узел|северн\w+\s+узл\w*",
    "☋": r"южн\w+\s+узел|южн\w+\s+узл\w*",
}
_BODY_RE = {symbol: re.compile(_EDGE % pattern) for symbol, pattern in _BODY_PATTERNS.items()}
_ANY_BODY = re.compile("|".join("(?:%s)" % (_EDGE % p) for p in _BODY_PATTERNS.values()))
_SIGN_PATTERNS = {
    "Овен": r"овен|овн(?:а|у|е|ом)", "Телец": r"телец|тельц(?:а|у|е|ом)", "Близнецы": r"близнец(?:ы|ов|ам|ах|ами)",
    "Рак": r"рак(?:а|у|е|ом)?", "Лев": r"лев|льв(?:а|у|е|ом)", "Дева": r"дев(?:а|ы|е|у|ой)",
    "Весы": r"вес(?:ы|ов|ам|ах|ами)", "Скорпион": r"скорпион(?:а|у|е|ом)?", "Стрелец": r"стрелец|стрельц(?:а|у|е|ом)",
    "Козерог": r"козерог(?:а|у|е|ом)?", "Водолей": r"водол(?:ей|ея|ею|ее|еем)", "Рыбы": r"рыб(?:ы|ах|ам|ами)",
}
_SIGN_ALT = "|".join("(?P<s%d>%s)" % (i, p) for i, p in enumerate(_SIGN_PATTERNS.values()))
_SIGN_NAMES = list(_SIGN_PATTERNS)
_BODY_ALT = "|".join(_BODY_PATTERNS.values())
_ORDINALS = {"первом": 1, "втором": 2, "третьем": 3, "четвертом": 4, "пятом": 5, "шестом": 6, "седьмом": 7,
             "восьмом": 8, "девятом": 9, "десятом": 10, "одиннадцатом": 11, "двенадцатом": 12}
_ASPECT_WORDS = {
    "conj": r"соединени(?:е|я|ю|ем|и)|конъюнкци(?:я|и|ю|ей)",
    "sext": r"секстил(?:ь|я|ю|е|ем|и|ей)",
    "sq": r"квадрат(?:а|у|ом|е|ы)?",
    "tri": r"трин(?:а|у|ом|е|ы)?",
    "opp": r"оппозици(?:я|и|ю|ей)|противостояни(?:е|я|ю|ем|и)",
}
_ASPECT_RE = {code: re.compile(_EDGE % pattern) for code, pattern in _ASPECT_WORDS.items()}

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?;])\s+|\n+|\s+[-•]\s+")
_SKIP = re.compile(r"(?<!\w)(?:если|обычно|как правило|например|не|нет|ни|люди|людям|людей|тех)(?!\w)")
_SIGN_CLAIM = re.compile(r"(?<!\w)(?P<body>%s)(?!\w)(?P<mid>(?:\s+[\w'-]+,?){0,4}?)\s+(?:в|во)\s+(?:%s)(?![\w])" % (_BODY_ALT, _SIGN_ALT))
_HOUSE_CLAIM = re.compile(
    r"(?<!\w)(?P<body>%s)(?!\w)(?P<mid>(?:\s+[\w'-]+,?){0,6}?)\s+(?:в|во)\s+(?:(?P<n>\d{1,2})(?:-?(?:м|й|ом|ый|го))?|(?P<w>%s))\s+дом(?:е|а|у)?(?![\w])"
    % (_BODY_ALT, "|".join(_ORDINALS)))
_DEGREE_CLAIM = re.compile(
    r"(?<!\w)(?P<body>%s)(?!\w)(?P<mid>(?:[^\d!?;]|(?<=\s)\d{1,2}(?:-?(?:м|й|ом))?(?=\s+дом)){0,50}?)(?<![\d.,])(?P<d>\d{1,2})\s*(?:°|градус\w*)(?:\s*(?P<m>\d{1,2})\s*(?:'|мин\w*))?"
    % _BODY_ALT)
_DEGREE_NOT_POSITION = re.compile(r"^\w*\s*(?:от|до|между|с орбис)")


def split_sentences(text):
    text = re.sub(r"[*_#`>|]+", " ", text)
    return [part.strip() for part in _SENTENCE_SPLIT.split(text) if part and part.strip()]


def is_russian(text, share=0.5):
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and sum("а" <= c.lower() <= "я" or c.lower() == "ё" for c in letters) / len(letters) >= share


def _symbol_of(fragment):
    for symbol, pattern in _BODY_RE.items():
        if pattern.fullmatch(fragment) or pattern.search(fragment):
            return symbol
    return None


def _bodies_in(text):
    found = []
    for symbol, pattern in _BODY_RE.items():
        if pattern.search(text):
            found.append(symbol)
    return found


def _other_body_inside(mid):
    return bool(_ANY_BODY.search(mid))


def extract_claims(answer):
    """Claims about this chart made by an answer: sign, house, degree, aspect. Each carries its sentence."""
    claims = []
    for original in split_sentences(answer):
        sentence = _norm(original)
        if _SKIP.search(sentence):
            continue
        for match in _SIGN_CLAIM.finditer(sentence):
            if _other_body_inside(match.group("mid")):
                continue
            index = next(i for i in range(len(_SIGN_NAMES)) if match.group("s%d" % i))
            claims.append({"kind": "sign", "body": _symbol_of(match.group("body")), "value": _SIGN_NAMES[index],
                           "sentence": original})
        for match in _HOUSE_CLAIM.finditer(sentence):
            if _other_body_inside(match.group("mid")):
                continue
            number = int(match.group("n")) if match.group("n") else _ORDINALS[match.group("w")]
            if 1 <= number <= 12:
                claims.append({"kind": "house", "body": _symbol_of(match.group("body")), "value": number,
                               "sentence": original})
        for match in _DEGREE_CLAIM.finditer(sentence):
            if _other_body_inside(match.group("mid")) or "орбис" in match.group("mid"):
                continue
            if _DEGREE_NOT_POSITION.match(sentence[match.end():match.end() + 16]):
                continue
            claims.append({"kind": "degree", "body": _symbol_of(match.group("body")),
                           "value": [int(match.group("d")), int(match.group("m")) if match.group("m") else None],
                           "sentence": original})
        bodies = _bodies_in(sentence)
        codes = [code for code, pattern in _ASPECT_RE.items() if pattern.search(sentence)]
        if len(bodies) == 2 and len(codes) == 1:
            claims.append({"kind": "aspect", "body": bodies[0], "body2": bodies[1], "value": codes[0],
                           "sentence": original})
    return claims


def verify_claims(claims, facts):
    """Adds 'verdict' (confirmed/contradicted/unverifiable) and 'expected' to each claim."""
    result = []
    for claim in claims:
        item = dict(claim)
        body = facts.bodies.get(claim["body"])
        if body is None:
            item.update(verdict="unverifiable", expected=None)
        elif claim["kind"] == "sign":
            item.update(verdict="confirmed" if body["sign"] == claim["value"] else "contradicted", expected=body["sign"])
        elif claim["kind"] == "house":
            if body["house"] is None:
                item.update(verdict="unverifiable", expected=None)
            else:
                item.update(verdict="confirmed" if body["house"] == claim["value"] else "contradicted",
                            expected=body["house"])
        elif claim["kind"] == "degree":
            degrees, minutes = claim["value"]
            ok = degrees == body["deg"] and (minutes is None or abs(minutes - body["min"]) <= 1)
            item.update(verdict="confirmed" if ok else "contradicted", expected="%d°%02d′" % (body["deg"], body["min"]))
        else:
            other = facts.bodies.get(claim["body2"])
            if other is None:
                item.update(verdict="unverifiable", expected=None)
            else:
                present = facts.aspect_codes(claim["body"], claim["body2"])
                item.update(verdict="confirmed" if claim["value"] in present else "contradicted",
                            expected=",".join(sorted(present)) or "none")
        result.append(item)
    return result


def check_answer(answer, facts):
    """Counts for one answer. A contradiction carries the sentence for manual verification."""
    verified = verify_claims(extract_claims(answer), facts)
    return {"confirmed": sum(c["verdict"] == "confirmed" for c in verified),
            "contradicted": sum(c["verdict"] == "contradicted" for c in verified),
            "unverifiable": sum(c["verdict"] == "unverifiable" for c in verified),
            "contradictions": [{"kind": c["kind"], "body": c["body"], "claimed": c.get("body2") and
                                [c["value"], c["body2"]] or c["value"], "expected": c["expected"],
                                "sentence": c["sentence"][:300]} for c in verified if c["verdict"] == "contradicted"]}
