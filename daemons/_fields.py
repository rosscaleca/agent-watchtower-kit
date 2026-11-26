"""Field helpers shared by the daemons that write event lines.

A person's name is the account's own text. Written raw into a ``by="..."`` or ``assignee="..."``
field it could hold a line separator or a double quote, end the field or the line, and write a
second, forged line into a log a reader trusts. Every daemon passes such a name through
``who_field`` before it reaches a line.

A sibling module, imported the way ``_degraded`` is: the daemons dir is ``sys.path[0]`` when a
daemon runs as a script.
"""
import re
import unicodedata

WHO_CAP = 80


#: The Unicode categories a terminal or a bidi-aware reader acts on but does not show: Cc, the
#: controls (C0 and C1: ESC, NUL, DEL, the 8-bit CSI ...), and Cf, the format characters (the
#: bidi marks, embeddings, overrides and isolates, zero-width spaces and joiners, the BOM).
#: Dropped by :func:`unshown_dropped` once whitespace is collapsed, except the two joiners of
#: :data:`_KEPT`.
_UNSHOWN_CATEGORIES = ("Cc", "Cf")

#: Two Cf characters kept: U+200C ZERO WIDTH NON-JOINER (Persian and Indic spelling) and U+200D
#: ZERO WIDTH JOINER (emoji sequences). They join or separate letters and move nothing on a
#: terminal line. Trade-off: every other Cf character is still dropped, the tag characters
#: (U+E0020..U+E007F) included, so a subdivision flag written as a tag sequence loses its tags.
_KEPT = ("\u200c", "\u200d")


#: The Default_Ignorable_Code_Point property, as ranges (lo, hi) of code points, copied from Unicode's
#: DerivedCoreProperties.txt (property Default_Ignorable_Code_Point): characters a renderer shows as
#: nothing. Python 3.9's ``unicodedata`` has no such property. Some are Cc or Cf and
#: :func:`unshown_dropped` already drops them; the rest (U+034F, U+115F, U+1160, U+17B4, U+17B5,
#: U+180B..U+180D, U+180F, U+3164, U+FE00..U+FE0F, U+FFA0 ...) are Mn or Lo and stay in a name. Used only to
#: tell whether a name has anything readable in it (:func:`who_field`).
_DEFAULT_IGNORABLE = (
    (0x00AD, 0x00AD), (0x034F, 0x034F), (0x061C, 0x061C), (0x115F, 0x1160), (0x17B4, 0x17B5),
    (0x180B, 0x180F), (0x200B, 0x200F), (0x202A, 0x202E), (0x2060, 0x206F), (0x3164, 0x3164),
    (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF), (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8), (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A), (0xE0000, 0xE0FFF),
)


def is_default_ignorable(c):
    """True when the character ``c`` has the Unicode property Default_Ignorable_Code_Point
    (:data:`_DEFAULT_IGNORABLE`): it shows as nothing."""
    cp = ord(c)
    return any(lo <= cp <= hi for lo, hi in _DEFAULT_IGNORABLE)


def unshown_dropped(s):
    """``s`` without the characters of :data:`_UNSHOWN_CATEGORIES`, except :data:`_KEPT`. A rule,
    not a list: every control and format character, whatever Unicode adds to the category."""
    return "".join(c for c in s
                   if c in _KEPT or unicodedata.category(c) not in _UNSHOWN_CATEGORIES)


def who_field(name):
    """A name as one field of an event line: whitespace (every line separator included)
    collapsed to a single space, control and format characters dropped (:func:`unshown_dropped`; the
    two joiners of :data:`_KEPT` stay inside a name), double quotes turned to single, capped at
    ``WHO_CAP`` characters; ``?`` when nothing readable is left (a name of only joiners,
    default-ignorable characters (:func:`is_default_ignorable`: U+3164, U+115F, U+034F, U+FE0F ...) and
    whitespace included; a default-ignorable beside a readable character stays). An ordinary
    name comes back unchanged."""
    s = unshown_dropped(re.sub(r"\s+", " ", str(name or ""))).strip()
    if not "".join(c for c in s if c not in _KEPT and not is_default_ignorable(c)).strip():
        return "?"
    if len(s) > WHO_CAP:
        s = s[: WHO_CAP - 1] + "…"
    return s.replace('"', "'")
