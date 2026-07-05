# majsoul-analysis-suite

Tools to pull a [Mahjong Soul](https://mahjongsoul.game.yo-star.com/) replay out of the
browser, convert it to [tenhou.net/6](https://tenhou.net/6/) JSON, and analyze your play —
efficiency, luck, and style — from the command line.

## What's here

| Dir | Contents |
|-----|----------|
| `extract/` | `majsoul_extract.py` — scrapes the currently-open replay from Chrome's WASM heap and saves it as tenhou6 JSON (now also reads the in-client **MAKA** ("Seer") AI review from the heap and folds per-round/per-decision ratings into the JSON). `bulk_extract.py` drives the whole replay list to extract every game (canvas UI automation via `mjs_ui.py`; can auto-trigger MAKA analysis on un-analyzed games). `seer_decode.py`/`seer_capture.py` decode the MAKA protobuf. `EXTRACTION.md` documents the heap layout / protobuf format. |
| `analyze/` | `mjsoul_decode.py` (round-by-round report + riichi detection), `mjsoul_turns.py` (turn-by-turn hand reconstruction with per-turn shanten/waits), `mjsoul_analyze.py` (efficiency / luck / style stats vs. the field), `mjsoul_luck.py` (realized luck: tenpai→win conversion, live wait width, outraced/deal-in outcomes), `mjsoul_value.py` (value-aware layer: yaku/dora/wait-liveness/game-state, re-labels efficiency flags so value- and placement-motivated plays aren't miscounted as errors), `mjsoul_mortal.py` (value-aware discard review with the local **Mortal** AI, policy-only — `mjsoul_to_mjai.py` converts to the mjai stream it consumes), `mjsoul_pack.py` (builds a self-contained "pack" of logs + precomputed Mortal sidecars to analyze elsewhere with no model). `mahjong_analysis_instructions.md` is a spec for the JSON format; `mjsoul_mortal.md` documents the Mortal reviewer + packs. |
| `chrome-mcp/` | A small CDP-based browser automation MCP server used to drive Chrome during extraction. |
| `examples/` | Sample extracted logs. |
| `docs/` | Development notes. |

## Extract a replay

Chrome must run with remote debugging on a **non-default** profile:

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9223 --user-data-dir="$HOME/.chrome-debug-profile"
```

Open a replay in that window, then:

```bash
pip3 install websockets
MJS_ACCOUNT_ID=<your account id> python3 extract/majsoul_extract.py
```

It finds the loaded game, auto-detects which seat is you, and writes
`{date}_{time}_{room}_{placement}_playerN.json` (default: `~/majsoul-logs/`).
Don't know your account id? Run it once anyway — it prints the game's account list
(seat → nickname → id) so you can read yours off and set `MJS_ACCOUNT_ID`.
Config: `MJS_ACCOUNT_ID`, `MJS_CDP_PORT` (default 9223), `MJS_OUT_DIR`, or `--room` / `--out`.

## Analyze

```bash
python3 analyze/mjsoul_decode.py  examples/*.json      # round-by-round report
python3 analyze/mjsoul_turns.py   examples/*.json --round 2   # turn-by-turn one round
python3 analyze/mjsoul_analyze.py examples/*.json      # efficiency / luck / style
python3 analyze/mjsoul_luck.py    examples/*.json      # realized luck / conversion
python3 analyze/mjsoul_value.py   examples/*.json --seat 3   # value-aware re-read of efficiency flags
```

Sample `mjsoul_analyze.py` output:

```
metric                                                YOU      FIELD
--- LUCK (starting tiles & draws) ---
avg haipai shanten (lower=better start)              3.38       3.42
reached tenpai                                        50%        38%
useful-draw rate (draws that advanced shanten)        51%        29%
--- EFFICIENCY (your decisions) ---
acceptance-optimal discard                            71%        66%
shanten-losing discards (clear errors)                  4         25
--- STYLE ---
riichi rate (per hand)                                12%        17%
open/call rate (per hand)                             75%        50%
```

## Bulk extraction & MAKA

Extract every game in your replay list in one pass (drives the canvas UI over CDP, so
it's viewport-agnostic), reading each game's MAKA ("Seer") AI review straight from the heap:

```bash
python3 extract/bulk_extract.py                 # extract all; free MAKA on already-analyzed games
python3 extract/bulk_extract.py --analyze 30    # also spend up to 30 daily MAKA attempts on un-analyzed games
```

Bulk needs UI classifier references (`list`, `replay`, `makapanel`) — these are screenshots
of **your** client, so they aren't shipped; generate your own once with
`python3 extract/mjs_ui.py ref <name>` from each screen. The single-game extractor picks up
MAKA automatically when a game has been analyzed (`maka` key in the JSON; `--no-maka` to skip).

## Mortal review & shareable packs

Run the local [Mortal](https://github.com/Equim-chan/Mortal) AI (policy-only) over your games
for a value-aware discard review — its recommended action and the probability mass on what you
actually did. Needs a local Mortal build + weights (see `analyze/mjsoul_mortal.md`):

```bash
python3 analyze/mjsoul_mortal.py logs/<game>.json          # you-vs-field table + biggest disagreements
python3 analyze/mjsoul_pack.py --out pack_out --zip --glob 'logs/*.json'   # bundle for a no-model machine
```

`mjsoul_pack.py` precomputes Mortal's per-decision numbers into a `<log>.mortal.json` sidecar
and assembles logs + sidecars + the pure-Python suite modules into a small pack. A no-model
machine (e.g. a browser Claude) runs `mjsoul_mortal.py --no-model` and reads the sidecars —
the model, weights, and Mortal repo never travel, and a safety sweep refuses to ship them.

## chrome-mcp

TypeScript MCP server (Node 18+). Build before use:

```bash
cd chrome-mcp && npm install && npm run build   # -> dist/index.js
```

Register it with your MCP client pointing at `dist/index.js`. It connects to Chrome
over CDP on port 9223 and exposes navigate / screenshot / evaluate / click tools.

## Notes

Reads only your own replays from your own browser session; it doesn't touch Mahjong
Soul's servers. Tile codes are standard tenhou form (`m`/`p`/`s`/`z`, `0` = red five).

## License

MIT
