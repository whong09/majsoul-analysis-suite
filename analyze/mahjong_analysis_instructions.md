# Mahjong replay analysis — instructions for Claude

Pass this file to Claude together with one or more `*.json` Mahjong Soul replay logs
(the Tenhou-6.0 export variant with pinyin meld names and a dict-shaped result).
It tells Claude exactly how the format works so it can analyze quickly and correctly.

---

## 1. How to read the file efficiently (do this, not trial-and-error)

1. These logs are small (tens of KB). **Load the whole file once** and parse the JSON — do
   not `head`/peek repeatedly.
2. **Use the decoder in Section 4** (or reproduce its logic). Run it once; don't
   re-derive the format each time.
3. Watch two gotchas that are easy to get wrong:
   - The **result object is the *last* element of each round array, not always index 15.**
     Abortive/short rounds have fewer elements.
   - **Score units differ.** In `meta` (element 0) scores are in units of 100
     (multiply by 100). In the result dict, `sc` and `owari` are already **absolute
     points** — do **not** multiply those.

If the user just wants a summary, produce the report in Section 5. If they ask for
something specific (a single round's turn-by-turn, tsumogiri/tedashi reads, riichi
timing, deal-in analysis), pull it from the decoded structure.

---

## 2. Top-level structure

```json
{
  "title": ["Mahjong Soul", "4-Player East"],
  "name":  ["Player0","Player1","Player2","Player3"],   // seats E,S,W,N at start
  "rule":  {"disp": "East", "aka": 1},                   // aka:1 = red fives on
  "log":   [ <round>, <round>, ... ]                     // one entry per hand played
}
```

## 3. A single round (element of `log`) — array layout

| Index | Contents |
|------:|----------|
| 0 | `meta` = `[kyoku, honba, riichi_sticks, s0, s1, s2, s3]` — scores ×100 |
| 1 | dora indicators (list of tiles) |
| 2 | ura-dora indicators (list; empty if no winning riichi) |
| 3 | P0 haipai (13 starting tiles) |
| 4 | P0 draws (self-draws **and** calls, in order) |
| 5 | P0 discards |
| 6–8 | P1 haipai / draws / discards |
| 9–11 | P2 haipai / draws / discards |
| 12–14 | P3 haipai / draws / discards |
| **last** | result dict (see §3.4) |

- `kyoku`: 0 = East-1, 1 = East-2, 2 = East-3, 3 = East-4, 4 = South-1, …
  **Dealer (oya) seat = `kyoku % 4`.** An "East" (tonpuu) game can spill into South-1+
  as sudden death if nobody has ≥30,000 at the end of East-4.

### 3.1 Tile codes
- `Nm` man (characters), `Np` pin (circles), `Ns` sou (bamboo), N = 1–9.
- `0m / 0p / 0s` = **red five** of that suit.
- Honors `Nz`: `1z`=East, `2z`=South, `3z`=West, `4z`=North, `5z`=White,
  `6z`=Green, `7z`=Red.

### 3.2 Discard prefixes (elements 5 / 8 / 11 / 14)
- `r` → **tsumogiri**: the just-drawn tile was discarded (e.g. `r5z`).
- `t` → **riichi declaration made on a *tedashi***: the from-hand tile on which the
  player declared riichi. Every discard *after* it by that player is `r` (forced
  tsumogiri). At most one `t` per player per round.
- `a` → **ankan** (closed kan) declared on that turn (e.g. `a7s`). Not a normal discard.
- *no prefix* → **tedashi**: a tile discarded from hand (not the drawn tile).

> ⚠️ **The `t` prefix does NOT capture every riichi.** It only marks a riichi
> declared on a *tedashi*. A **tsumogiri riichi** (declaring on the tile you just
> drew) is written with the same plain `r` as any other tsumogiri, so it is
> **invisible to a `t`-scan**. Conversely, a `t` declaration whose tile is
> immediately ronned is **nullified** (the player pays no stick), so `t` can also
> *over*-report. Do **not** detect riichi from discards alone — use the score
> reconciliation in §3.4. (This variant does not separately encode which tile a
> tsumogiri riichi was declared on; you can only bound it to the trailing run of
> `r` discards.)

### 3.3 Calls in the draw stream (elements 4 / 7 / 10 / 13)
A called meld appears where the draw would be, as a comma-string
`"{type}{called},{own},{own}[,{own}]"`, **called tile listed first**:
- `c…` = chi (run), e.g. `c4m,6m,5m` → called 4m, completes 4-5-6m.
- `p…` = pon (triplet), e.g. `p3z,3z,3z`.
- `m…` = minkan (open/大明 kan), 4 tiles, e.g. `m4z,4z,4z,4z`.
- (`a…` ankan and added-kan appear on the **discard** side, see above.)
- ⚠️ This variant does **not** encode which seat the tile was called from.

### 3.4 Result dict (last element)
```json
{ "agari": [ { "who": 3, "fromWho": 3, "tsumo": true,
              "hand": [...13 tiles...], "melds": ["kezi(7p,7p,7p)", ...],
              "machi": "0p", "points": 12000 } ],
  "owari": [-6000,-3000,-3000,13000],   // per-round point change per seat (absolute)
  "sc":    [19000,22000,22000,37000] }  // running scores after the round (absolute)
```
- `who` = winner seat, `fromWho` = discarder seat (equals `who` on tsumo).
- Multiple entries in `agari` = multiron (e.g. double ron).
- Meld names are pinyin: `kezi`=triplet/pon, `shunzi`=run, `minggang`=open kan,
  `angang`=closed kan, `jiagang`=added kan, `chi`=chi.
- An exhaustive draw has no `agari` (just `owari`/`sc`, tenpai payments).

**`owari` vs `sc` and the riichi stick (important):**
`sc` is the true running total after the round (sum of changes = 0). `owari` gives
each seat's change **with its own riichi stick added back** — i.e. `owari` omits the
1000 a declarer paid. So `owari` sums to `+1000 × (number of established riichi)`,
while `sc − start` sums to 0. The gap is exactly the paid stick, which gives the
authoritative riichi test:

```
established_riichi(p)  ⇔  owari[p] − (sc[p] − start[p]) == 1000
```

where `start = [s*100 for s in meta[3:7]]`. This is the correct way to detect riichi:
it catches tsumogiri riichi (which `t` misses), catches winners who riichi'd, and
excludes nullified declarations (which `t` wrongly includes). For the exact tile, use
the `t` discard when present; a tsumogiri riichi's tile is not encoded (bound it to the
trailing `r` run). On abortive rounds there is no `owari`/`sc`, so fall back to `t`
(and accept that a tsumogiri riichi in a four-riichi abort can't be pinpointed).

### 3.5 What the format does **not** contain
No yaku names, no han/fu breakdown, no dora count per hand — only final `points`.
So Claude can report *what* won and for how much, hands, waits, melds, riichi timing,
and discard reads (tsumogiri vs tedashi), but should not invent yaku/han unless it
derives them itself from the hand and states it's doing so.

---

## 4. Decoder script

A ready-to-run decoder (`mjsoul_decode.py`) accompanies these instructions:

```
python3 mjsoul_decode.py replay.json          # human-readable report
python3 mjsoul_decode.py replay.json --json    # machine-readable decoded structure
```

If only this markdown is available, Claude can reproduce the logic from §3. The key
functions: split each round by the index table, read the result from `round[-1]`,
detect **established** riichi via the score reconciliation in §3.4 (not the `t`-scan
alone — see the warning in §3.2), and take final standings from the last round's `sc`.
The decoder's `riichi_status()` returns per seat `{established, via, tile, turn/turn_min,
note}`, distinguishing tedashi riichi (exact tile), tsumogiri riichi (tile not encoded,
turn bounded), and nullified declarations (ronned before the stick was paid).

---

## 5. Default analysis to produce

Unless the user asks for something else, give a concise report:

1. **Header** — players, ruleset (East/South), red-fives on/off.
2. **Per round** — wind+number (+honba), dealer, dora indicator; who declared riichi
   and on what tile; any calls; the win(s) with winner, tsumo/ron (and off whom),
   points, wait, and melds; the score deltas and running scores. Mark exhaustive draws.
3. **Final standings** — ranked, with final scores, from the last round's `sc`.

Keep it readable prose/tables, not raw JSON. Offer deeper dives on request:
turn-by-turn for a round, efficiency/deal-in reads, riichi-vs-damaten timing,
who fed whom, red-five usage, etc.

## 6. Batch / large-file notes
- Multiple logs: decode each, then a short cross-game summary (placements, average
  score, win/deal-in tallies) if asked.
- If a log is unusually large or malformed, decode defensively (result from `round[-1]`,
  tolerate missing keys) and report what couldn't be parsed rather than guessing.