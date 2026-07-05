#!/usr/bin/env python3
"""
mjsoul_value.py — the value layer the efficiency scorer is missing.

mjsoul_analyze.py optimizes shanten -> ukeire and nothing else, so it is blind to
*why* a hand is worth advancing: whether a tenpai can actually win (yaku), whether a
discard keeps dora/aka, how live the resulting wait is, and what the score situation
says the objective even is. That blindness makes it flag value-motivated and
game-state-motivated plays as "errors."

This module adds four things and re-labels the efficiency decisions accordingly:

  1. YAKU GATE   tenpai_has_yaku(): does a resting shape win, damaten, on >=1 wait?
                 A yakuless tenpai (closed, no riichi) cannot win — leaving it is NOT
                 a shanten error, it's shedding a hollow shape. Covers the common
                 damaten yaku (tanyao, yakuhai, pinfu, iipeiko, sanshoku, ittsu,
                 chanta/junchan, toitoi, honitsu, chinitsu, chiitoitsu). Riichi and
                 menzen-tsumo are excluded on purpose: they are *available* on any
                 closed tenpai, so counting them would make every tenpai "winnable"
                 and defeat the point of the gate.

  2. DORA COUNT  dora_aka(): dora + red-five count in a tile list, so a discard that
                 keeps value can be told apart from one that keeps a blank.

  3. WAIT LIVE   reuses mjsoul_luck's liveness idea (4 - visible copies) so a decision
                 can be judged on the quality of the tenpai it reaches, not just that
                 it reaches one.

  4. GAME STATE  standings(): rank + margin from the pre-round scores, and a coarse
                 objective hint (build-for-points vs protect-placement).

reclassify() takes analyze.seat_metrics' decisions and tags each off-optimal /
shanten-losing discard as one of: real_error, value_trade (kept dora/aka or created/
kept a yaku the "optimal" line drops), or hollow_exit (left a yakuless tenpai).

Usage:  python3 mjsoul_value.py file.json [file2 ...]        # per-decision re-read
        python3 mjsoul_value.py file.json --seat 3
"""
import sys, json
import mjsoul_turns as M
import mjsoul_analyze as A
from mjsoul_decode import WINDS, round_label

# --- 34-index helpers ------------------------------------------------------ #
DRAGONS = {31, 32, 33}                 # White, Green, Red
WINDS_IDX = {27, 28, 29, 30}           # E, S, W, N
YAOCHU = set(M.YAOCHU)

def dora_tile_idx(indicator):
    """34-index of the dora given an indicator tile code (mirrors analyze.dora_of)."""
    return M.t2i(A.dora_of(indicator))

def dora_aka(tiles, dora_idxs):
    """dora + aka count in a raw tile-code list."""
    n = sum(1 for t in tiles if t in ("0m", "0p", "0s"))
    n += sum(1 for t in tiles if M.t2i(t) in dora_idxs)
    return n

# --- standard-form decomposition (pair + 4 sets) --------------------------- #
def _carve(c, need, acc, out):
    if need == 0:
        if not any(c):
            out.append(list(acc))
        return
    i = next((k for k in range(34) if c[k] > 0), None)
    if i is None:
        return
    if c[i] >= 3:                                  # triplet
        c[i] -= 3; acc.append(("pon", i))
        _carve(c, need - 1, acc, out)
        acc.pop(); c[i] += 3
    if i < 27 and i % 9 <= 6 and c[i + 1] and c[i + 2]:   # run
        c[i] -= 1; c[i + 1] -= 1; c[i + 2] -= 1; acc.append(("chi", i))
        _carve(c, need - 1, acc, out)
        acc.pop(); c[i] += 1; c[i + 1] += 1; c[i + 2] += 1

def decompositions(counts):
    """All (pair_idx, sets) standard decompositions of a complete 14-tile count."""
    out = []
    for p in range(34):
        if counts[p] >= 2:
            counts[p] -= 2
            got = []
            _carve(counts[:], 4, [], got)
            for sets in got:
                out.append((p, sets))
            counts[p] += 2
    return out

# --- yaku / han scoring over a completed hand ------------------------------ #
def _suit(i): return i // 9 if i < 27 else 3
def _num(i):  return i % 9 if i < 27 else i - 27

def _flush_han(all_idx, closed):
    """0 / honitsu / chinitsu han from the tiles present (indices incl. melds)."""
    suits = {_suit(i) for i in all_idx if i < 27}
    has_honor = any(i >= 27 for i in all_idx)
    if len(suits) == 1 and not has_honor:
        return 6 if closed else 5            # chinitsu
    if len(suits) <= 1:
        return 3 if closed else 2            # honitsu (one suit + honors, or all honors)
    return 0

def _score_complete(counts, meld_sets, win_idx, round_w, seat_w, closed):
    """Best structural han (yaku only, NO dora/aka) for a specific 14-tile hand.
    Returns (han, yaku_names). Covers the common yaku + a few yakuman; approximate
    (see §8 limitations) — good for tiering, not an exact scorer."""
    # chiitoitsu (closed, no melds)
    chiitoi = (closed and not meld_sets and all(v in (0, 2) for v in counts)
               and sum(1 for v in counts if v == 2) == 7)
    all_idx_raw = [i for i in range(34) if counts[i] > 0] + [i for _, i in meld_sets]
    # kokushi
    if closed and not meld_sets and set(i for i in range(34) if counts[i]) <= YAOCHU \
       and sum(1 for i in YAOCHU if counts[i] > 0) == 13:
        return 13, ["kokushi (yakuman)"]

    best = (0, [])
    decomps = decompositions(counts[:])
    if chiitoi:
        han = 2 + _flush_han([i for i in range(34) if counts[i]], closed)
        best = max(best, (han, ["chiitoitsu"]), key=lambda x: x[0])

    for pair, sets in decomps:
        allsets = sets + list(meld_sets)
        pons = [i for t, i in allsets if t == "pon"]
        chis = [i for t, i in allsets if t == "chi"]
        yaku = []
        han = 0
        involved = set()
        for t, i in allsets:
            involved.update((i,) if t == "pon" else (i, i + 1, i + 2))
        involved.add(pair)
        idx_present = [i for i in range(34) if counts[i]] + [i for _, i in meld_sets]

        # --- yakuman first (cap at 13) ---
        dragon_pons = [i for i in pons if i in DRAGONS]
        wind_pons = [i for i in pons if i in WINDS_IDX]
        if len(dragon_pons) == 3:
            best = max(best, (13, ["daisangen (yakuman)"]), key=lambda x: x[0]); continue
        if len(wind_pons) == 4:
            best = max(best, (13, ["daisuushii (yakuman)"]), key=lambda x: x[0]); continue
        if not (set(idx_present) - set(range(27, 34))):
            best = max(best, (13, ["tsuuiisou (yakuman)"]), key=lambda x: x[0]); continue
        if closed and not meld_sets and len(pons) == 4:
            best = max(best, (13, ["suuankou (yakuman)"]), key=lambda x: x[0]); continue

        # --- normal yaku ---
        # yakuhai
        for i in dragon_pons:
            han += 1; yaku.append("yakuhai(dragon)")
        if round_w in pons:
            han += 1; yaku.append("yakuhai(round)")
        if seat_w in pons:
            han += 1; yaku.append("yakuhai(seat)")
        # tanyao
        if not (involved & YAOCHU):
            han += 1; yaku.append("tanyao")
        # toitoi
        if len(pons) == 4:
            han += 2; yaku.append("toitoi")
        # sanankou (approx: 3+ pon, closed-ish; ron-nuance ignored)
        if len(pons) >= 3 and closed:
            han += 2; yaku.append("sanankou~")
        # iipeiko / ryanpeikou (closed)
        if closed and not meld_sets:
            dup = len(chis) - len(set(chis))
            if dup >= 2 and len(set(chis)) <= 2:
                han += 3; yaku.append("ryanpeikou")
            elif dup >= 1:
                han += 1; yaku.append("iipeiko")
        # sanshoku doujun
        by_num = {}
        for i in chis:
            by_num.setdefault(_num(i), set()).add(_suit(i))
        if any(len(s) == 3 for s in by_num.values()):
            han += 2 if closed else 1; yaku.append("sanshoku")
        # ittsu
        starts = {}
        for i in chis:
            starts.setdefault(_suit(i), set()).add(_num(i))
        if any({0, 3, 6} <= s for s in starts.values()):
            han += 2 if closed else 1; yaku.append("ittsu")
        # chanta / junchan
        def touch_yao(t, i):
            return (i in YAOCHU) if t == "pon" else (i in YAOCHU or (i + 2) in YAOCHU)
        if pair in YAOCHU and all(touch_yao(t, i) for t, i in allsets):
            junchan = not any(i >= 27 for i in involved)
            han += (3 if closed else 2) if junchan else (2 if closed else 1)
            yaku.append("junchan" if junchan else "chanta")
        # pinfu (closed, all chi, non-yakuhai pair, ryanmen win)
        if closed and not meld_sets and len(chis) == 4 \
           and pair not in DRAGONS and pair != round_w and pair != seat_w:
            for i in chis:
                if win_idx in (i, i + 2):
                    edge = (win_idx == i and _num(i) == 0) or \
                           (win_idx == i + 2 and _num(i) == 6)
                    if not edge and _suit(i) < 3:
                        han += 1; yaku.append("pinfu"); break
        han += _flush_han(idx_present, closed)
        if _flush_han(idx_present, closed):
            yaku.append("honitsu/chinitsu")
        best = max(best, (han, yaku), key=lambda x: x[0])
    return best

# --- point tiers ----------------------------------------------------------- #
def value_tier(han):
    """(label, representative non-dealer ron points) for a han total. Han-based
    and fu-approximate; dealer hands run ~1.5x. See §8."""
    if han <= 0:  return ("yakuless", 0)
    if han >= 13: return ("yakuman", 32000)
    if han >= 11: return ("sanbaiman", 24000)
    if han >= 8:  return ("baiman", 16000)
    if han >= 6:  return ("haneman", 12000)
    if han == 5:  return ("mangan", 8000)
    return ({1: "1 han", 2: "2 han", 3: "3 han", 4: "4 han"}[han],
            {1: 1300, 2: 2600, 3: 5200, 4: 8000}[han])

def _breakdown_str(yaku, yaku_han, dora, aka):
    """e.g. 'tanyao (1) + dora 2 + aka 1'. Empty components dropped."""
    parts = []
    if yaku:
        parts.append(f"{', '.join(yaku)} ({yaku_han})")
    if dora:
        parts.append(f"dora {dora}")
    if aka:
        parts.append(f"aka {aka}")
    return " + ".join(parts) if parts else "no yaku"

def hand_value(concealed_counts, meld_sets, round_w, seat_w, closed,
               dora_idxs, fixed_aka=0):
    """Best damaten value over this tenpai's waits (NO riichi assumed). Returns a
    dict with the value broken out explicitly:
        winnable, han (total), yaku_han, dora, aka, yaku (list), breakdown (str),
        tier, points, best_wait, riichi_bonus.
    dora and aka are called out separately from yaku han so a tier can be read as
    'came from the hand' vs 'came from dora' at a glance."""
    best = dict(winnable=False, han=0, yaku_han=0, dora=0, aka=0, yaku=[],
                breakdown="no yaku", tier="yakuless", points=0,
                best_wait=None, riichi_bonus=1 if closed else 0)
    for w in M.waits(concealed_counts[:], len(meld_sets)):
        if concealed_counts[w] >= 4:
            continue
        concealed_counts[w] += 1
        yhan, yaku = _score_complete(concealed_counts[:], meld_sets, w,
                                     round_w, seat_w, closed)
        dora = sum(concealed_counts[i] for i in dora_idxs)
        concealed_counts[w] -= 1
        if yhan <= 0:                       # no yaku -> can't score, dora don't count
            continue
        total = yhan + dora + fixed_aka
        if total > best["han"]:
            label, pts = value_tier(total)
            best = dict(winnable=True, han=total, yaku_han=yhan, dora=dora,
                        aka=fixed_aka, yaku=yaku,
                        breakdown=_breakdown_str(yaku, yhan, dora, fixed_aka),
                        tier=label, points=pts, best_wait=M.i2t(w),
                        riichi_bonus=1 if closed else 0)
    return best

def tenpai_has_yaku(concealed_counts, meld_sets, round_w, seat_w, closed):
    """Back-compat gate: does the shape win damaten on >=1 wait?"""
    return hand_value(concealed_counts, meld_sets, round_w, seat_w, closed,
                      set())["winnable"]

# --- meld tiles from decode calls ------------------------------------------ #
def meld_sets_from_calls(calls):
    """Turn decode 'calls' strings into ('pon'/'chi'/'kan', base_idx) descriptors."""
    out = []
    for c in calls:
        typ = c[0]
        parts = c.split(",")
        tiles = [parts[0][1:]] + parts[1:]
        idxs = sorted(M.t2i(t) for t in tiles)
        if typ == "c":
            out.append(("chi", idxs[0]))
        elif typ in ("p",):
            out.append(("pon", idxs[0]))
        elif typ in ("m",):          # minkan counts as a pon for yaku purposes
            out.append(("pon", idxs[0]))
    return out

# --- game state ------------------------------------------------------------ #
def standings(start_scores, seat):
    """Rank (1=lead) and signed margin to the nearest rival for `seat`."""
    mine = start_scores[seat]
    others = [start_scores[p] for p in range(4) if p != seat]
    order = sorted(range(4), key=lambda p: (-start_scores[p], p))
    rank = order.index(seat) + 1
    above = [s for s in others if s > mine]
    # negative margin = deficit to the seat directly above; >=0 = lead over best rival
    margin_to_next = (mine - min(above)) if above else (mine - max(others))
    return rank, margin_to_next

def objective_hint(rank, margin, kyoku, all_east=True):
    """Coarse: are we playing for points or protecting placement?
    A clear leader late wants placement; everyone else wants points."""
    late = kyoku >= 3  # East-4 onward in a tonpuu game (nominally the last hand)
    if rank == 1 and margin > 0 and (late or margin >= 8000):
        # leading into/at the final hand, OR a solid mid-game cushion
        return "protect-placement (build reluctantly, fold readily)"
    return "build-for-points (value/tempo normal)"

# --- event/hand helpers ---------------------------------------------------- #
def _pre_hand_at(ev, discard_turn):
    """The 14-tile pre-discard hand for the decision at `discard_turn`.
    In the walker, a decision pairs a well-formed pre-discard event `e` with the
    next event `nxt` that carries the discard; nxt['turn'] == discard_turn."""
    for i in range(len(ev) - 1):
        nxt = ev[i + 1]
        if nxt.get("turn") == discard_turn and "discard" in nxt:
            tiles = A.parse_hand(ev[i]["hand"])
            nm = len(ev[i]["melds"])
            if len(tiles) == 14 - 3 * nm:
                return tiles
    return None

def _hand_minus(tiles, discard_code):
    """Remove one copy of the discarded tile (matched on normalized identity,
    tolerating the r/t prefix on the discard code)."""
    target = M.norm(discard_code)
    out, dropped = [], False
    for t in tiles:
        if not dropped and M.norm(t) == target:
            dropped = True
            continue
        out.append(t)
    return out

# --- re-label the efficiency decisions ------------------------------------- #
def reclassify(paths, want_seat=None):
    rows = []
    for path in paths:
        doc = M.load(path)
        names = doc.get("name", [])
        me = next((i for i, n in enumerate(names) if "(you)" in n), 0)
        seat = want_seat if want_seat is not None else me
        _, brounds = M.build(doc)
        _, drounds = M.decode(doc)
        for brd, drd in zip(brounds, drounds):
            kyoku = drd["kyoku"]
            dealer = kyoku % 4
            round_w = 27 + kyoku // 4
            seat_w = 27 + ((seat - dealer) % 4)
            dora_idxs = {dora_tile_idx(x) for x in drd["dora"]}
            calls = drd["seats"][seat].get("calls") or []
            closed = len(calls) == 0
            meld_sets = meld_sets_from_calls(calls)
            rank, margin = standings(drd["start"], seat)
            obj = objective_hint(rank, margin, kyoku)
            gone_at = A.make_gone_at(drd, seat)
            m = A.seat_metrics(brd["seats"][seat], drd["seats"][seat],
                               brd["dora"][0] if brd["dora"] else None, gone_at)
            ev = brd["seats"][seat]["events"]
            n_melds = len(meld_sets)
            own_disc = _disc_ordinals(ev)
            for d in m["decisions"]:
                if d["optimal"]:
                    continue
                pre_tiles = _pre_hand_at(ev, d["turn"])   # 14-tile pre-discard shape
                gone = gone_at(own_disc.get(d["turn"], 1))
                label = "sub_optimal"
                left_yaku = None
                left_tier = None
                left_breakdown = None
                kept_dora = opt_dora = None

                if d["shanten_err"] and d["best_sh"] == 0 and pre_tiles:
                    # we left a TENPAI: value it (winnable? how much, damaten?)
                    rest = _hand_minus(pre_tiles, d["discard"])
                    cc = M.counts_of(rest)
                    aka = sum(1 for t in rest if t in ("0m", "0p", "0s"))
                    hv = hand_value(cc, meld_sets, round_w, seat_w, closed,
                                    dora_idxs, aka)
                    left_yaku = hv["winnable"]
                    left_tier = hv["tier"] if hv["winnable"] else "yakuless"
                    left_breakdown = hv["breakdown"]
                    label = "real_error" if left_yaku else "hollow_exit"
                elif d["shanten_err"]:
                    label = "shanten_regression"   # dropped shanten but not from tenpai

                # dora/aka retention vs the LIVE-optimal line
                if pre_tiles and label in ("sub_optimal", "shanten_regression"):
                    kept_dora = dora_aka(_hand_minus(pre_tiles, d["discard"]), dora_idxs)
                    bsh, buk, per = A.best_discard(pre_tiles, n_melds, gone)
                    opt_tiles = [t for t, (sh, uk) in per.items()
                                 if (sh, uk) == (bsh, buk)]
                    opt_dora = max((dora_aka(_hand_minus(pre_tiles, t), dora_idxs)
                                    for t in opt_tiles), default=kept_dora)
                    if kept_dora > opt_dora:
                        label = "value_trade"
                rows.append(dict(
                    path=path.split("/")[-1], seat=seat,
                    round=round_label(kyoku, drd["honba"]), turn=d["turn"],
                    discard=d["discard"], best_sh=d["best_sh"], act_sh=d["act_sh"],
                    uke_lost=d["uke_lost"], label=label, left_winnable_tenpai=left_yaku,
                    left_tier=left_tier, left_breakdown=left_breakdown,
                    rank=rank, margin=margin, objective=obj))
    return rows

def _disc_ordinals(ev):
    """Map each discard's turn -> its 1-based ordinal among this seat's discards."""
    out, k = {}, 0
    for e in ev:
        if "discard" in e:
            k += 1
            out[e["turn"]] = k
    return out

def main():
    seat = None
    skip = set()
    if "--seat" in sys.argv:
        i = sys.argv.index("--seat")
        seat = int(sys.argv[i + 1])
        skip = {i, i + 1}
    paths = [a for j, a in enumerate(sys.argv[1:], start=1)
             if not a.startswith("--") and j not in skip]
    if not paths:
        print("usage: mjsoul_value.py file.json [--seat N]"); return
    rows = reclassify(paths, seat)
    if "--json" in sys.argv:
        print(json.dumps(rows, ensure_ascii=False, indent=2)); return
    print(f"\n{'rnd':<5}{'T':>3}  {'disc':<5}{'shanten':>9}  {'label':<12} {'note'}")
    print("-" * 78)
    for r in rows:
        sh = f"{r['best_sh']}->{r['act_sh']}"
        note = ""
        if r["label"] == "hollow_exit":
            note = "left a YAKULESS tenpai — not a real loss"
        elif r["label"] == "real_error":
            note = (f"left a WINNABLE {r.get('left_tier','?')} tenpai "
                    f"[{r.get('left_breakdown','?')}] — genuine loss")
        elif r["label"] == "shanten_regression":
            note = f"dropped shanten (not from tenpai), ukeire lost {r['uke_lost']}"
        elif r["label"] == "value_trade":
            note = "off-optimal but kept more dora/aka — value trade"
        elif r["label"] == "sub_optimal":
            note = f"live-ukeire lost {r['uke_lost']}"
        print(f"{r['round']:<5}{r['turn']:>3}  {r['discard']:<5}{sh:>9}  "
              f"{r['label']:<12} {note}")
    # game-state banner per round (dedup)
    seen = set()
    print("\ngame-state context (pre-round):")
    for r in rows:
        k = (r["path"], r["round"])
        if k in seen:
            continue
        seen.add(k)
        print(f"  {r['round']:<5} rank {r['rank']}  margin {r['margin']:+6}  -> {r['objective']}")

if __name__ == "__main__":
    main()
