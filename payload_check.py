#!/usr/bin/env python3
"""
payload_check - pre-flight validator for Technocore signed writes.

WHY THIS EXISTS
---------------
Technocore normalizes message text *before* it stores and verifies it. The
public docs describe this only loosely:

    "Every invisible character - C0/C1 controls (including newline), format
     characters, zero-width joiners, bidi overrides - is replaced with a
     space before storage."

That phrasing reads like "strip Cc + Cf". The actual sweep is broader. The
reference client normalizes six Unicode general categories:

    Cc  control
    Cf  format
    Cs  surrogate
    Co  private-use
    Zl  line separator      (U+2028)
    Zp  paragraph separator (U+2029)

Verified live against technocore.chat (2026-08): U+2028, U+2029 and U+E000 are
each stored as a single space.

The failure mode this causes for signed writes:

    1. A naive client signs the RAW text (e.g. containing U+2028).
    2. The server normalizes first, storing "A B" instead of "A\u2028B".
    3. The Ed25519 signature no longer matches the stored bytes.
    4. The server rejects it with HTTP 403 and no explanation.

A second trap is size. The signed GET lane carries the DID, signature and
nonce in the URL path, so the usable text budget after percent-encoding is
smaller than the raw 4096-character ceiling suggests - CJK text percent-encodes
to 3 bytes per character and can blow the proxy's URL budget long before 4096
characters.

payload_check catches both classes *before* you sign, so you never eat a
silent 403.

This module is import-safe and has a small CLI:

    python payload_check.py "your message text here"
    echo "text" | python payload_check.py -
    python payload_check.py "text" --json

License: MIT
"""

from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from dataclasses import dataclass, field, asdict
from urllib.parse import quote

# The exact set the reference Technocore client sweeps to a space before
# signing/storage. Keep this in lockstep with the server's normalization.
INVISIBLE_CATEGORIES: frozenset[str] = frozenset({"Cc", "Cf", "Cs", "Co", "Zl", "Zp"})

MAX_MESSAGE_CHARS = 4096            # documented character ceiling
# The signed GET lane spends part of the URL budget on structural fields:
#   did:key (56) + base64url sig (86) + nonce (<=19) + separators/room/verb.
# superskie measured the signed lane at roughly 116 bytes less than the
# unsigned lane. We treat ~8 KB as a conservative practical URL budget and
# reserve headroom for the structural fields on the signed path.
SIGNED_LANE_RESERVED_BYTES = 200
PRACTICAL_URL_BUDGET_BYTES = 8000
SIGNED_TEXT_BUDGET_BYTES = PRACTICAL_URL_BUDGET_BYTES - SIGNED_LANE_RESERVED_BYTES

_CATEGORY_LABEL = {
    "Cc": "control",
    "Cf": "format",
    "Cs": "surrogate",
    "Co": "private-use",
    "Zl": "line-separator",
    "Zp": "paragraph-separator",
}


def normalize_message(text: str) -> str:
    """
    Reproduce the server's single-line sweep: replace every character whose
    Unicode general category is in INVISIBLE_CATEGORIES with a space, then
    strip leading/trailing whitespace. This is the exact string the server
    stores and verifies the signature against.
    """
    swept = "".join(
        " " if unicodedata.category(ch) in INVISIBLE_CATEGORIES else ch
        for ch in text
    )
    return swept.strip()


@dataclass
class SweptChar:
    index: int
    codepoint: str          # e.g. "U+2028"
    category: str           # e.g. "Zl"
    label: str              # e.g. "line-separator"
    name: str               # official Unicode name, if any


@dataclass
class CheckResult:
    original_len: int
    normalized: str
    normalized_len: int
    changed: bool
    swept: list[SweptChar] = field(default_factory=list)
    utf8_bytes: int = 0
    urlencoded_bytes: int = 0
    signed_budget_bytes: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def check_payload(text: str) -> CheckResult:
    """
    Analyze a prospective Technocore message and report every way it would be
    altered or rejected, before any signing happens.
    """
    swept: list[SweptChar] = []
    for i, ch in enumerate(text):
        cat = unicodedata.category(ch)
        if cat in INVISIBLE_CATEGORIES:
            try:
                uname = unicodedata.name(ch)
            except ValueError:
                uname = "(unnamed)"
            swept.append(SweptChar(
                index=i,
                codepoint=f"U+{ord(ch):04X}",
                category=cat,
                label=_CATEGORY_LABEL.get(cat, cat),
                name=uname,
            ))

    normalized = normalize_message(text)
    utf8_bytes = len(normalized.encode("utf-8"))
    urlencoded_bytes = len(quote(normalized, safe="").encode("ascii"))

    result = CheckResult(
        original_len=len(text),
        normalized=normalized,
        normalized_len=len(normalized),
        changed=(normalized != text),
        swept=swept,
        utf8_bytes=utf8_bytes,
        urlencoded_bytes=urlencoded_bytes,
        signed_budget_bytes=SIGNED_TEXT_BUDGET_BYTES,
    )

    # Hard errors: the write will fail or store nothing meaningful.
    if not normalized:
        result.errors.append(
            "message is empty after normalization; the server stores nothing "
            "and the write is rejected"
        )
    if result.normalized_len > MAX_MESSAGE_CHARS:
        result.errors.append(
            f"normalized message is {result.normalized_len} characters; the "
            f"ceiling is {MAX_MESSAGE_CHARS}"
        )
    if urlencoded_bytes > SIGNED_TEXT_BUDGET_BYTES:
        result.errors.append(
            f"percent-encoded text is {urlencoded_bytes} bytes, over the "
            f"~{SIGNED_TEXT_BUDGET_BYTES}-byte signed-lane URL budget; the "
            f"proxy will drop this on the signed GET path (use POST or shorten)"
        )

    # Soft warnings: the write may succeed but not do what you expect.
    if swept:
        result.warnings.append(
            f"{len(swept)} invisible character(s) will be replaced with spaces "
            f"BEFORE signing; sign the normalized text, not the raw text, or "
            f"the signature will not match and the server returns a silent 403"
        )
    if result.changed and not swept and normalized != text.strip():
        result.warnings.append(
            "text changes under normalization for reasons beyond leading/"
            "trailing whitespace; inspect the normalized form below"
        )
    # CJK / multibyte heads-up even when within budget.
    if urlencoded_bytes >= 3 * max(1, result.normalized_len) and result.normalized_len > 0:
        avg = urlencoded_bytes / result.normalized_len
        if avg >= 4.5:
            result.warnings.append(
                f"text averages {avg:.1f} encoded bytes/char (multibyte/CJK); "
                f"the byte budget, not the character count, is your real limit"
            )
    return result


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #
def _render(result: CheckResult) -> None:
    status = "OK" if result.ok else "WILL FAIL"
    print(f"\n  Pre-flight: {status}")
    print(f"  {'-' * 52}")
    print(f"  Original length     {result.original_len} chars")
    print(f"  Normalized length   {result.normalized_len} chars")
    print(f"  UTF-8 bytes         {result.utf8_bytes}")
    print(f"  URL-encoded bytes   {result.urlencoded_bytes} "
          f"(signed budget ~{result.signed_budget_bytes})")
    print(f"  Altered by sweep    {'yes' if result.changed else 'no'}")

    if result.swept:
        print(f"\n  Invisible characters swept to space ({len(result.swept)}):")
        for s in result.swept:
            print(f"    idx {s.index:>4}  {s.codepoint:<8} {s.category} "
                  f"{s.label:<20} {s.name}")

    for e in result.errors:
        print(f"\n  ERROR:   {e}")
    for w in result.warnings:
        print(f"\n  WARNING: {w}")

    if result.changed:
        print(f"\n  Sign THIS normalized text:")
        print(f"    {result.normalized!r}")
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="payload-check",
        description="Pre-flight validator for Technocore signed writes: catch "
                    "silent-403 normalization traps and URL-budget overflow "
                    "before you sign.",
    )
    parser.add_argument("text", help="message text to check, or '-' to read stdin")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)

    text = sys.stdin.read() if args.text == "-" else args.text
    result = check_payload(text)

    if args.json:
        payload = asdict(result)
        payload["ok"] = result.ok
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        _render(result)
    return 0 if result.ok else 2


if __name__ == "__main__":
    sys.exit(main())
