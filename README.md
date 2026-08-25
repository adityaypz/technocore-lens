# technocore-lens

A read-only health and signal analyzer for [Technocore](https://technocore.chat) rooms.

Technocore, by [Flop Labs](https://flop.finance), is a public HTTP chat/notes
surface for AI agents. Because anyone can write with a single `GET`, the open
rooms fill quickly with low-effort `check-in` and `heartbeat` messages from
agents farming the `$FLOP` airdrop. That noise buries the rooms where real
discussion is happening.

**technocore-lens** reads the public endpoints only and scores every room, so
you can tell signal from farming noise at a glance. It never creates an
identity, never signs, and never writes. Everything it does is reproducible
with `curl`.

---

## What it measures

For each room it samples the recent message window and reports:

| Metric | Meaning |
|---|---|
| **Health score** | 0-100 composite: high signal + broad authorship + low spam |
| **Spam share** | fraction of messages matching boilerplate check-in/heartbeat patterns |
| **Signal share** | fraction of messages that carry real content (after stripping URLs/DIDs) |
| **Author concentration** | Herfindahl index in [0,1] — 1.0 means one DID dominates |
| **Cadence** | messages per minute over the sampled span |
| **Noisiest authors** | the DIDs posting the most in the window |

The health score is a transparent weighted blend:

```
health = 100 * (0.45*signal_share + 0.35*(1-spam_share) + 0.20*(1-concentration))
```

No magic, no model — you can read the formula and disagree with the weights.

---

## Install

No dependencies beyond the Python 3.10+ standard library.

```bash
git clone https://github.com/adityaypz/technocore-lens.git
cd technocore-lens
python3 technocore_lens.py --help
```

## Usage

```bash
# Rank every public room by health
python3 technocore_lens.py

# Deep-dive a single room
python3 technocore_lens.py lobby

# Bigger sample window (max 200, Technocore's read cap)
python3 technocore_lens.py lobby --limit 200

# Show the 10 noisiest authors in a room
python3 technocore_lens.py miner --top 10

# Machine-readable output for pipelines
python3 technocore_lens.py --json
python3 technocore_lens.py lobby --json
```

## Example

```
  ROOM                       HEALTH   SPAM  SIGNAL   MSGS  AUTHORS
  ------------------------------------------------------------------
  ai                             99     0%    100%     44       26
  defi                           98     0%    100%     30       20
  alpha                          96     0%    100%     30       10
  ...
  lobby                          84    12%     77%    150       46
  ...
  miner                          31    85%     13%    134      117
  welcome                         0   100%      0%      1        1
```

The `ai`, `defi`, and `alpha` rooms are carrying real discussion. The `miner`
and `welcome` rooms are almost pure farming noise. That is the call the tool
helps you make in one command.

---

## How it works

1. `GET /rooms?format=json` — the public room directory.
2. `GET /r/<room>?format=json&limit=<n>` — the recent message window per room.
3. Local classification of each message (spam vs substantive) and a few simple
   statistics (Herfindahl concentration, cadence).

A room that returns an error (some private/`d-` rooms return `HTTP 500` to
anonymous readers) is skipped, not fatal — a single bad room never aborts a
full-directory scan.

## What it deliberately does not do

- It does **not** read, create, or touch any `identity.pem` or private key.
- It does **not** sign or post anything.
- It makes only unauthenticated `GET` requests to `technocore.chat`.

It is a lens, not a client. If you want to create a DID and post signed
messages, use the official [`flop-labs/technocore-chat`](https://github.com/flop-labs/technocore-chat)
tooling.

## Spam heuristics

The classifier is intentionally conservative. A message is flagged as spam only
if it matches a known boilerplate pattern (`check-in`, `heartbeat`,
`Agent NNNN active`, bare DID dumps, `gm`/`wagmi`, one-word pings). A message is
counted as substantive only if, after removing URLs and DIDs, it still has at
least six real words. You may tune `SPAM_PATTERNS` in `technocore_lens.py` for
your own definition of noise.

## License

MIT. See [LICENSE](LICENSE).

---

## payload_check.py — pre-flight validator for signed writes

A second, standalone tool in this repo. It catches two ways a Technocore
signed write fails *silently*, before you sign:

**1. The normalization / silent-403 trap.**
The docs describe the invisible-character sweep loosely ("C0/C1 controls,
format characters, zero-width joiners, bidi overrides"), which reads like
"strip `Cc` + `Cf`". The reference client and server actually sweep six Unicode
categories to a space: `Cc, Cf, Cs, Co, Zl, Zp`. Verified live against
technocore.chat — `U+2028` (Zl), `U+2029` (Zp) and `U+E000` (Co) are each
stored as a single space. If you sign the raw text but the server stores the
normalized text, the Ed25519 signature no longer matches and you get an
unexplained `HTTP 403`.

> Credit: the spec-vs-enforcement gap and the CJK URL-budget limit were first
> written up publicly by [@superskie](https://x.com/superskie/status/2091906289031065833)
> and filed as [flop-labs/technocore-chat#75](https://github.com/flop-labs/technocore-chat/issues/75).
> `payload_check` is the preventative side of that finding: it flags the trap
> before signing rather than diagnosing it after.

**2. The URL-budget overflow.**
The signed GET lane spends part of the URL on the DID, signature and nonce, so
the usable text budget in bytes is smaller than the 4096-character ceiling
implies. CJK text percent-encodes to ~9 bytes/char and overflows the proxy long
before 4096 characters.

```bash
# Flag an invisible-character trap and print the text you should actually sign
python3 payload_check.py "Deploy is live<U+2028>check the repo"

# Read from stdin (useful for large or generated messages)
echo "your text" | python3 payload_check.py -

# JSON for pipelines; exit code is 0 when safe, 2 when the write would fail
python3 payload_check.py "text" --json
```

Example — a message containing `U+2028`:

```
  Pre-flight: OK
  Altered by sweep    yes
  Invisible characters swept to space (1):
    idx   14  U+2028   Zl line-separator       LINE SEPARATOR
  WARNING: sign the normalized text, not the raw text, or the server returns a silent 403
  Sign THIS normalized text:
    'Deploy is live check the repo'
```
