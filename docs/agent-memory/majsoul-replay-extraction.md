# Development notes — replay extraction

Reverse-engineering notes for pulling the current Mahjong Soul replay out of Chrome's
WASM heap and converting it to tenhou6 JSON. (This project was built with Claude Code;
these are the working notes it kept.)

## Picking the right game out of the heap

The replay viewer decodes the **whole** game record into the WASM heap up front, so
playback position doesn't matter. But the heap also holds *stale* copies of previously
viewed games. The extractor:

1. Scans for all 4–8-round `RecordNewRound` clusters.
2. Groups them by game (East-1 signature: dora + seat-0 haipai).
3. Picks the **most-copied** game — the actively-viewed replay is re-decoded on every
   navigation, so it accumulates many copies, while stale games linger as single copies.
   (An earlier "just take the last cluster" heuristic grabbed stale games once several
   were cached.)
4. Finds that game's head (sits immediately *before* the record cluster, though only
   before *some* copies — so it tries copies until one parses), reads the account list,
   and auto-detects which seat is you by matching your account id.

Output name: `{YYYY-MM-DD}_{HHMM}_{room}_{Nth-place}_playerN.json`. Date/time come from
the head `start_time`; placement from the last round's `new_scores`; seat 0=East, 1=South,
2=West, 3=North at East 1.

## Format contracts the analysis tools depend on

- Result dict needs **both** `owari` (per-seat deltas) **and** `sc` (running totals).
  `owari` is required for the decoder's authoritative riichi test
  `owari[p] - (sc[p] - start[p]) == 1000` (verified on real data); without it, riichi
  detection falls back to a `t`-scan that misses tsumogiri riichi.
- Discard prefixes: `r` = tsumogiri, `t` = tedashi riichi. In the raw
  `.lq.RecordDiscardTile`, **field 3 = is_liqi (riichi), field 5 = moqie (tsumogiri)**.
- The final-round slice must extend well past the last `RecordNewRound` (≈40 KB), or the
  final round's win record gets truncated and the game looks unfinished.

## Chrome setup

CDP requires a **non-default** `--user-data-dir` (the default profile blocks remote
debugging). Launch Chrome with `--remote-debugging-port=9223 --user-data-dir=<some dir>`,
open a replay, then run the extractor. It talks to CDP directly via the Python
`websockets` library.
