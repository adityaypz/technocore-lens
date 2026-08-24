#!/usr/bin/env python3
"""
technocore-lens - a read-only health & signal analyzer for Technocore rooms.

Technocore (https://technocore.chat, by Flop Labs) is a public HTTP chat/notes
surface for AI agents. Its open rooms fill quickly with low-effort "check-in"
and "heartbeat" spam from agents farming the $FLOP airdrop. That noise makes it
hard to tell whether a room carries real signal.

technocore-lens reads the PUBLIC endpoints only (no identity, no private key,
no writes) and reports, per room:

  - activity        : message volume and cadence
  - spam share      : fraction of messages that are boilerplate check-ins
  - author spread   : how concentrated posting is among a few DIDs (HHI)
  - signal ratio    : share of messages that look substantive
  - a 0-100 health score combining the above

It is a diagnostic lens, not a client. It never touches identity.pem, never
signs, and never posts. Everything it does, you can reproduce with curl.

Usage:
    python technocore_lens.py                     # summarize all public rooms
    python technocore_lens.py lobby               # deep-dive one room
    python technocore_lens.py lobby --limit 200   # bigger sample window
    python technocore_lens.py --json              # machine-readable output
    python technocore_lens.py lobby --top 10      # show top-N noisiest authors

License: MIT
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

BASE_URL = "https://technocore.chat"
DEFAULT_LIMIT = 200          # Technocore caps room reads at 200
REQUEST_TIMEOUT = 15.0
MAX_RESPONSE_BYTES = 8_000_000
USER_AGENT = "technocore-lens/1.0 (+https://github.com/adityaypz)"

# Boilerplate patterns that mark a message as low-effort farming noise.
# Kept deliberately conservative: these match the check-in/heartbeat spam that
# dominates the public rooms, not genuine short messages.
SPAM_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bcheck[\s\-]?in\b", re.I),
    re.compile(r"\bheartbeat\b", re.I),
    re.compile(r"\bagent\s+\d+\s+active\b", re.I),
    re.compile(r"\b(online|alive|active)\b.*\b(verified|registered|identity)\b", re.I),
    re.compile(r"\$FLOP\s+(check|agent|claim)", re.I),
    re.compile(r"\bgm\b|\bwagmi\b", re.I),
    re.compile(r"^\s*(hi|hello|hey|test|ping|pong)\s*[.!]?\s*$", re.I),
    re.compile(r"did:key:z6Mk[1-9A-HJ-NP-Za-km-z]{40,48}\s*$", re.I),  # bare DID dump
)

# A message is treated as "substantive" if, after stripping URLs and DIDs, it
# still carries enough real words to be a sentence or an idea.
DID_RE = re.compile(r"did:key:z6Mk[1-9A-HJ-NP-Za-km-z]{40,48}")
URL_RE = re.compile(r"https?://\S+")
WORD_RE = re.compile(r"[A-Za-z]{3,}")


# --------------------------------------------------------------------------- #
# HTTP (read-only)                                                            #
# --------------------------------------------------------------------------- #
class FetchError(RuntimeError):
    """A single endpoint failed. Callers decide whether to skip or abort."""


def _get_json(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    Perform one bounded GET against a public Technocore endpoint.

    Raises FetchError on any transport/decode failure so callers can choose to
    skip a single bad room without killing a whole-directory scan.
    """
    query = f"?{urlencode(params)}" if params else ""
    url = f"{BASE_URL}{path}{query}"
    request = Request(url, headers={"accept": "application/json", "user-agent": USER_AGENT})
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as error:
        raise FetchError(f"HTTP {error.code}")
    except URLError as error:
        raise FetchError(f"unreachable ({error.reason})")
    except TimeoutError:
        raise FetchError("timed out")
    if len(raw) > MAX_RESPONSE_BYTES:
        raise FetchError("response exceeded safety limit")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FetchError(f"invalid JSON ({error})")
    if not isinstance(payload, dict):
        raise FetchError("expected a JSON object")
    return payload


def fetch_rooms() -> list[dict[str, Any]]:
    """Return the public room directory."""
    payload = _get_json("/rooms", {"format": "json"})
    rooms = payload.get("rooms", [])
    return [r for r in rooms if isinstance(r, dict) and r.get("room")]


def fetch_messages(room: str, limit: int) -> list[dict[str, Any]]:
    """Return up to `limit` recent messages from a room, oldest first."""
    safe_room = quote(room, safe="")
    payload = _get_json(f"/r/{safe_room}", {"format": "json", "limit": limit})
    messages = payload.get("messages", [])
    return [m for m in messages if isinstance(m, dict)]


# --------------------------------------------------------------------------- #
# Analysis                                                                    #
# --------------------------------------------------------------------------- #
def is_spam(text: str) -> bool:
    """True if a message matches any low-effort boilerplate pattern."""
    stripped = text.strip()
    if not stripped:
        return True
    return any(pattern.search(stripped) for pattern in SPAM_PATTERNS)


def is_substantive(text: str) -> bool:
    """True if a message carries real content after removing URLs and DIDs."""
    if is_spam(text):
        return False
    core = DID_RE.sub("", URL_RE.sub("", text))
    return len(WORD_RE.findall(core)) >= 6


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def herfindahl(counts: Iterable[int]) -> float:
    """
    Author concentration index in [0, 1]. 1.0 means a single DID posts
    everything; values near 0 mean posting is spread across many DIDs.
    """
    values = [c for c in counts if c > 0]
    total = sum(values)
    if total == 0:
        return 0.0
    return sum((c / total) ** 2 for c in values)


@dataclass
class RoomReport:
    room: str
    sampled: int
    spam: int
    substantive: int
    unique_authors: int
    concentration: float           # Herfindahl [0,1]
    msgs_per_min: float
    span_minutes: float
    top_authors: list[tuple[str, int]] = field(default_factory=list)

    @property
    def spam_share(self) -> float:
        return self.spam / self.sampled if self.sampled else 0.0

    @property
    def signal_share(self) -> float:
        return self.substantive / self.sampled if self.sampled else 0.0

    @property
    def health(self) -> int:
        """
        0-100 room-health score. High signal and broad authorship push it up;
        heavy spam and single-author domination push it down.
        """
        if self.sampled == 0:
            return 0
        signal = self.signal_share                    # want high
        clean = 1.0 - self.spam_share                 # want high
        spread = 1.0 - self.concentration             # want high
        score = 100.0 * (0.45 * signal + 0.35 * clean + 0.20 * spread)
        return max(0, min(100, round(score)))


def analyze_room(room: str, limit: int, top_n: int = 5) -> RoomReport:
    messages = fetch_messages(room, limit)
    sampled = len(messages)
    spam = sum(1 for m in messages if is_spam(str(m.get("text", ""))))
    substantive = sum(1 for m in messages if is_substantive(str(m.get("text", ""))))

    authors = Counter(str(m.get("from", "?")) for m in messages)
    concentration = herfindahl(authors.values())

    times = sorted(t for t in (_parse_ts(m.get("ts")) for m in messages) if t)
    span_minutes = 0.0
    if len(times) >= 2:
        span_minutes = (times[-1] - times[0]).total_seconds() / 60.0
    msgs_per_min = sampled / span_minutes if span_minutes > 0 else 0.0

    def short(did: str) -> str:
        return did if len(did) <= 24 else f"{did[:20]}...{did[-4:]}"

    top_authors = [(short(did), n) for did, n in authors.most_common(top_n)]

    return RoomReport(
        room=room,
        sampled=sampled,
        spam=spam,
        substantive=substantive,
        unique_authors=len(authors),
        concentration=round(concentration, 4),
        msgs_per_min=round(msgs_per_min, 2),
        span_minutes=round(span_minutes, 1),
        top_authors=top_authors,
    )


# --------------------------------------------------------------------------- #
# Rendering                                                                   #
# --------------------------------------------------------------------------- #
def _bar(fraction: float, width: int = 20) -> str:
    fraction = max(0.0, min(1.0, fraction))
    filled = round(fraction * width)
    return "#" * filled + "." * (width - filled)


def print_room_report(report: RoomReport) -> None:
    print(f"\n  Room: {report.room}")
    print(f"  {'-' * 52}")
    print(f"  Health score      {report.health:>3d} / 100")
    print(f"  Messages sampled  {report.sampled}")
    print(f"  Time span         {report.span_minutes} min "
          f"({report.msgs_per_min} msg/min)")
    print(f"  Unique authors    {report.unique_authors}")
    print(f"  Spam share        {report.spam_share:5.1%}  [{_bar(report.spam_share)}]")
    print(f"  Signal share      {report.signal_share:5.1%}  [{_bar(report.signal_share)}]")
    print(f"  Author concentr.  {report.concentration:.3f}  "
          f"(0 = spread out, 1 = one DID dominates)")
    if report.top_authors:
        print("  Noisiest authors:")
        for did, n in report.top_authors:
            print(f"    {n:>4d}  {did}")


def print_directory(reports: list[RoomReport]) -> None:
    reports = sorted(reports, key=lambda r: r.health, reverse=True)
    print(f"\n  {'ROOM':<26} {'HEALTH':>6} {'SPAM':>6} {'SIGNAL':>7} {'MSGS':>6} {'AUTHORS':>8}")
    print(f"  {'-' * 66}")
    for r in reports:
        print(f"  {r.room[:26]:<26} {r.health:>6d} {r.spam_share:>6.0%} "
              f"{r.signal_share:>7.0%} {r.sampled:>6d} {r.unique_authors:>8d}")
    print(f"\n  {len(reports)} room(s). Higher health = more real signal, less farming noise.")


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="technocore-lens",
        description="Read-only health & signal analyzer for Technocore rooms.",
    )
    parser.add_argument("room", nargs="?", help="room to deep-dive (omit to list all rooms)")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help=f"messages to sample per room (1-200, default {DEFAULT_LIMIT})")
    parser.add_argument("--top", type=int, default=5,
                        help="how many noisiest authors to show (deep-dive only)")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    limit = max(1, min(200, args.limit))

    if args.room:
        try:
            report = analyze_room(args.room, limit, top_n=max(1, args.top))
        except FetchError as error:
            print(f"error: could not read room '{args.room}': {error}")
            return 1
        if args.json:
            print(json.dumps({**asdict(report),
                              "spam_share": report.spam_share,
                              "signal_share": report.signal_share,
                              "health": report.health}, indent=2))
        else:
            print_room_report(report)
        return 0

    rooms = fetch_rooms()
    if not rooms:
        print("no public rooms found")
        return 1
    reports: list[RoomReport] = []
    skipped: list[tuple[str, str]] = []
    for r in rooms:
        name = str(r["room"])
        try:
            reports.append(analyze_room(name, limit, top_n=max(1, args.top)))
        except FetchError as error:
            skipped.append((name, str(error)))
    if not reports:
        print("error: every room failed to load")
        return 1
    if args.json:
        print(json.dumps(
            [{**asdict(r), "spam_share": r.spam_share,
              "signal_share": r.signal_share, "health": r.health} for r in reports],
            indent=2))
    else:
        print_directory(reports)
        if skipped:
            print(f"\n  skipped {len(skipped)} unreadable room(s): "
                  + ", ".join(f"{n} ({why})" for n, why in skipped))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        sys.exit(130)
