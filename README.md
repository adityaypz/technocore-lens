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
