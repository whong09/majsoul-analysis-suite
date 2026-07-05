# mjsoul_mortal.py — value-aware discard review with Mortal (policy-only)

A **new, additive** tool for the suite. It runs the Mortal mahjong AI locally (CPU) over your
Mahjong Soul games and, for each of your decisions, reports Mortal's recommended action and the
**policy probability mass** it puts on the action you actually took — the value-aware replacement
for mjsoul_analyze's "live-acceptance-optimal" column. Existing suite modules are untouched.

Files added (all additive):
- `mjsoul_mortal.py`  — the reviewer (you-vs-field aggregates, disagreements, `--json` + table).
- `mjsoul_to_mjai.py` — converter from our MJS→Tenhou-6 variant to the **mjai** event stream Mortal consumes.

## Policy-only + hanchan-on-tonpuu caveats (by design)
- **Policy-only, no GRP.** Mortal is a value/Q model. We drive only its policy: the recommended
  action is `argmax` over the masked Q-values; the "probability mass" is a **softmax over those
  masked Q-values** (Mortal has no explicit policy head — this is the standard reviewer convention).
  The GRP/placement-value model is **not used and not required**, and **no placement-EV is computed**.
- **mortal-298k is a HANCHAN (南) model** run on your **TONPUU (East-only)** games. Its tile-level
  discard / safety / shape reads transfer and are trustworthy; anything **placement/endgame is out of
  scope** here by design. This line is printed on every report.

## Requirements / how to run
The reviewer needs `torch` + the built `libriichi` from the CPU dry-run. Run it with that arm64 venv:

```
~/mortal-dryrun/venv/bin/python mjsoul_mortal.py logs/<game>.json [more.json ...]
~/mortal-dryrun/venv/bin/python mjsoul_mortal.py --json logs/<game>.json
```

Paths are overridable via env vars (defaults shown):
- `MORTAL_DIR`        = `~/mortal-dryrun/Mortal/mortal`   (contains `libriichi.so`, `model.py`, `engine.py`)
- `MORTAL_CHECKPOINT` = `~/mortal-dryrun/models/mortal_298k.pth`  (public VoidShine/mortal-298k weights)
- `MORTAL_DEVICE`     = `cpu`  (set `mps` to probe Apple GPU; CPU is the validated path)

Output: a you-vs-field table (agreement% and avg policy mass on your chosen discard, you vs the mean of
the other three seats), your biggest disagreements (discards where Mortal put the least mass on your
action, `*` = not Mortal's pick), and — where cheap — a `<DEAL-IN` join marking disagreements that were
the tile you were ronned on, plus Mortal's average mass on your deal-in discards.

## Inference path used
`libriichi.mjai.Bot(MortalEngine(brain, dqn), player_id)` — the same interface `mortal.py` uses. We feed
the mjai event stream one event at a time; when the bot returns a reaction, that's a decision point for
that seat, and its `meta` (with `q_values` + `mask_bits`) gives the per-action distribution. We construct
the engine with `enable_quick_eval=False` so **every** decision runs the net and carries `meta` (quick-eval
would shortcut forced discards). We never touch `mortal.py`'s post-loop GRP block, so no GRP is loaded.
The actual action you took is the mjai reaction that immediately follows the trigger event (or a pass).

## The converter and its global-order reconstruction
Our source stores **per-seat** draw/discard streams; mjai needs the true chronological interleave including
calls. `mjsoul_to_mjai.py` rebuilds it by simulating turn order (dealer starts with 14 tiles; calls in a
seat's `draws` stream jump the turn; the called tile is matched to the most recent discard since the source
doesn't encode the caller's source seat; pon/kan outrank chi on the same tile; minkan/ankan/kakan pull a
rinshan draw; kan-dora appended in order).

**Validation gate (passed):** every converted game is replayed through libriichi's `PlayerState` for all four
seats, which errors on any illegal event (wrong tile-in-hand, illegal call, bad order). **17/17 bundled games
replay clean.** We do not score a game that fails this gate.

### Known limits / things not fully validated
- **Global order is a reconstruction.** It's validated by the PlayerState replay above (strong: all hands,
  calls, and kans reconcile), but it assumes standard call priority (pon/kan > chi) and infers each call's
  source seat from the last matching discard.
- **Dealer's first draw identity** is synthesized (the source folds the dealer's auto-draw into the 14-tile
  haipai without saying which tile it was). This only affects the dealer's very first discard's "you just
  drew X" observation, one decision per kyoku.
- **Kan-dora reveal timing** is approximated (emitted right after the kan's replacement draw, rather than
  tenhou's delayed reveal for daiminkan/kakan). Minor effect on dora-count observation between the kan and
  the following discard.
- **The `a`/`k` kan prefix in this export is unreliable** (seen `a` on added-kans and `k` on closed-kans),
  so ankan-vs-kakan is decided by whether the seat already ponned that tile, not by the prefix.
- **Malformed rounds are skipped** with a warning rather than guessed (e.g. one abortive round in
  `2026-07-04_2358_...` whose source result is corrupt and whose call order is internally inconsistent). Each
  kyoku is independent for the bot, so skipping one loses only that round.
- **"Policy mass" is softmax-over-Q**, not a true trained policy distribution (Mortal is value-based).

## Setup runbook (from the CPU dry-run this reuses)
Built once under `~/mortal-dryrun`: arm64 venv (`/opt/homebrew/bin/python3.11` — the `/usr/local` Python is
x86_64 and has no torch wheel), `cargo build -p libriichi --lib --release` then copy
`target/release/libriichi.dylib` → `mortal/libriichi.so` (macOS emits `.dylib`; Python needs `.so`), and the
public `mortal_298k.pth` (v4, 192ch/40blk, strict-loadable). Device used here: **CPU**.

---

# Shareable packs (`mjsoul_pack.py`) — precomputed Mortal sidecars

Mortal inference needs local weights that can't be shared. To discuss these analyses somewhere with
no model (e.g. a browser Claude), the Mortal-derived numbers travel **precomputed** in a per-log
sidecar; everything else stays cheap-recomputable from the raw logs.

## Sidecar: `<logbasename>.mortal.json`
One sidecar per log, written next to it. Compact, sorted-key JSON (deterministic bytes for a given
log+weights+code). Schema:

```
{ "schema_version": 1,
  "uuid": "<title[2]>",
  "provenance": {
     "model_id": "mortal-298k",
     "weights_sha256": "<sha256 of the .pth actually used>",
     "converter_version": <int>,          # mjsoul_to_mjai.CONVERTER_VERSION
     "mortal_module_version": <int>,       # mjsoul_mortal.MORTAL_MODULE_VERSION
     "generated_utc": "<ISO>",
     "device_used": "cpu",
     "source_log": { "uuid": "<title[2]>", "sha256": "<sha256 of the raw log file>" } },
  "names": [...], "you": <seat|null>, "skipped": [...],
  "records": [ { "decision_id","seat","kyoku","turn","kind",
                 "my_action","mortal_action","matched","prob_of_my_action","shanten" }, ... ] }
```

Records store **only what the report consumes** — Mortal's recommendation (`mortal_action`), the
policy mass on your action (`prob_of_my_action`, rounded 6dp), and `matched` — never the full
46-action tensors. ~50–90 KB/game.

## Keying scheme (decision_id)
The stable id is derived from existing record data as **`seat:kyoku_label:turn#occ`**. Base
`(seat, kyoku_label, turn)` is **not** unique on its own — `turn` (=junme) counts *discards*, so a
discard and the same-go-around call/pass decisions share it (verified: up to 3 collisions). The
`#occ` chronological tiebreak (0-based count within that base key) makes it unique and stable under
deterministic inference. Load fails loudly on any duplicate `decision_id` rather than silently
dropping.

## Staleness / validation on load
`get_analysis(log, allow_inference=…)` prefers a valid sidecar. It's **stale** if
`source_log.uuid != title[2]`, `source_log.sha256 != sha256(raw log)`, or the schema version
differs. In no-model mode a stale/missing sidecar **fails loud (exit 2)**, naming the mismatch — it
never serves wrong or guessed data. In inference mode it regenerates. Pack-build additionally treats
a sidecar as needing regen if the converter/module version or `weights_sha256` changed (idempotency:
otherwise it's left untouched).

## The no-model load path is dependency-free
`import mjsoul_mortal` pulls only stdlib + the pure-Python `mjsoul_decode`/`mjsoul_to_mjai`. `torch`,
`model`, `engine`, and `libriichi` are imported **lazily** inside `build_engine()`/`analyze_seat()`,
reached only on the inference path. Verified: on system `python3` (no torch/libriichi) with a bogus
`MORTAL_DIR`, `--no-model` renders the full report and `sys.modules` contains none of
torch/libriichi/numpy.

## Building a pack
```
~/mortal-dryrun/venv/bin/python mjsoul_pack.py --out pack_out --zip <logs...>
# or: --glob 'logs/2026-07-04_*.json'   (--refresh to force-regen, --force to overwrite --out)
```
Build (this machine, has the weights): ensures an up-to-date sidecar per log → copies **only** raw
logs + sidecars + the pure-Python suite modules (`decode, turns, analyze, luck, value, mortal`, plus
`mjsoul_to_mjai` which `mjsoul_mortal` imports) + `mahjong_analysis_instructions.md` → writes
`MANIFEST.json` (per-game provenance, file hashes/sizes) + `README.md`. A **safety sweep** aborts if
any `.pth/.pt/.ckpt`, a weights/checkpoint-looking file, or anything > 5 MB would ship. The weights
`sha256` is recorded but the `.pth` never travels. Recipient runs
`python mjsoul_mortal.py --no-model logs/<game>.json` (reads the sidecar) and the other scripts
recompute from the raw logs.

