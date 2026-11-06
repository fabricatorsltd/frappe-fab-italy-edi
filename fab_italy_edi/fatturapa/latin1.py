"""Text SDI accepts: FatturaPA text fields allow Basic Latin and Latin-1 Supplement only.

SDI refuses the whole file over one character outside that range, and the usual culprits come in
unseen, pasted from a word processor or an editor that types curly quotes: an apostrophe in a line
description (FATT/2026/00042) is enough. Each is written as the plain character it stands for,
anything else outside the range is reduced to its base letter, and what has none becomes "?".
"""

from __future__ import annotations

import unicodedata

LATIN1_MAX = 0xFF

REPLACEMENTS = {
	"\u2018": "'",
	"\u2019": "'",
	"\u201a": "'",
	"\u201b": "'",
	"\u2032": "'",
	"\u201c": '"',
	"\u201d": '"',
	"\u201e": '"',
	"\u201f": '"',
	"\u2033": '"',
	"\u2010": "-",
	"\u2011": "-",
	"\u2012": "-",
	"\u2013": "-",
	"\u2014": "-",
	"\u2015": "-",
	"\u2212": "-",
	"\u2022": "-",
	"\u2026": "...",
	"\u20ac": "EUR",
	"\u2122": "(TM)",
	"\u2009": " ",
	"\u202f": " ",
	"\u200b": "",
	"\u200c": "",
	"\u200d": "",
	"\u2060": "",
	"\ufeff": "",
}


def to_latin1(text: str) -> str:
	if all(ord(char) <= LATIN1_MAX for char in text):
		return text

	return "".join(char if ord(char) <= LATIN1_MAX else replace(char) for char in text)


def replace(char: str) -> str:
	if char in REPLACEMENTS:
		return REPLACEMENTS[char]

	base = "".join(c for c in unicodedata.normalize("NFKD", char) if ord(c) <= LATIN1_MAX and not unicodedata.combining(c))
	return base or "?"
