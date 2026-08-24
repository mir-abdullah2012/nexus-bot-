"""Word-filter normalisation.

Lifted verbatim from Nexus 1.x so filtering decisions do not change during the
refactor. Pure functions, no imports beyond `re`, so this stays trivially
testable and importable from anywhere (including the migration script).

Catches f.u.c.k, fuuck, f u c k, l33t, etc.
"""

import re

_LEET = {
    "4": "a", "@": "a", "3": "e", "1": "i", "!": "i",
    "0": "o", "$": "s", "5": "s", "7": "t",
}


def normalize_text(text: str) -> str:
    text = text.lower()
    text = "".join(_LEET.get(c, c) for c in text)
    text = re.sub(r"[^a-z0-9\s]", "", text)   # strip punctuation
    return text


def contains_banned(text: str, banned_set) -> bool:
    norm = normalize_text(text)
    collapsed = re.sub(r"(.)\1+", r"\1", norm)          # fuuuck -> fuck
    joined = norm.replace(" ", "")                       # f u c k -> fuck
    words = set(norm.split()) | set(collapsed.split())
    for bad in banned_set:
        if bad in words:
            return True
        # joined check only for longer tokens to avoid false positives on tiny words
        if len(bad) >= 4 and bad in joined:
            return True
    return False
