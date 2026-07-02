#!/usr/bin/env python3
"""
mjsoul_turns.py — companion to mjsoul_decode.py.

Turn-by-turn reconstruction of every seat's concealed hand through a round, with
per-turn shanten and (at tenpai) the exact wait. Handles the alignment traps that
make naive hand-tracing wrong:

  * DEALER haipai is 14 tiles — the dealer's opening discard has no preceding draw.
  * CALLS in the draw stream (c/p/m) take the called tile from an opponent, not the
    wall: chi/pon are followed by a discard with no draw; a minkan draws a rinshan
    first (a plain entry in the draw stream) then discards.
  * KANS on the discard side (a = ankan, k = added-kan) are declarations, not
    discards: they consume a discard-stream slot, then draw a rinshan and discard.
  * A TSUMO win leaves a final draw with no following discard (draws = discards + 1).

Everything is walked with a two-pointer state machine that self-aligns from these
rules, so it degrades gracefully (and warns) rather than silently mis-pairing.

Usage:
    python3 mjsoul_turns.py replay.json                     # every round, every seat
    python3 mjsoul_turns.py replay.json --round 2           # only log index 2 (East-3)
    python3 mjsoul_turns.py replay.json --round 6 --seat 0  # one seat
    python3 mjsoul_turns.py replay.json --json              # structured events

--round takes the 0-based index into `log` (matches mjsoul_decode's idx). Seats are
0-3 (E/S/W/N at game start). Requires mjsoul_decode.py alongside it.

A "source streams don't reconcile" warning means that seat's recorded draw/discard
streams are internally inconsistent at that turn (a rare export quirk — e.g. a tile is
discarded that a prior call already consumed). The walker flags it and keeps going, but
that seat's hand is only trustworthy up to the flagged turn. Per-turn shanten/waits are
computed from the concealed hand plus called melds; red fives (0m/0p/0s) are treated as
5 for shape math but kept as-is for display.
"""
import json, sys, os, argparse, functools

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mjsoul_decode import load, decode, WINDS  # noqa: E402

# --------------------------------------------------------------------------- #
# tile <-> 34-index helpers  (red fives 0m/0p/0s normalise to 5 for counting)
# --------------------------------------------------------------------------- #
def strip_pref(t):
    return t.lstrip("rtak")

def norm(t):
    t = strip_pref(t)
    if t and t[0] == "0":
        t = "5" + t[1]
    return t

def t2i(t):
    t = norm(t)
    n, s = int(t[0]), t[1]
    return {"m": 0, "p": 9, "s": 18, "z": 27}[s] + (n - 1)

def i2t(i):
    if i < 27:
        return f"{i % 9 + 1}{'mps'[i // 9]}"
    return f"{i - 27 + 1}z"

def counts_of(tiles):
    c = [0] * 34
    for t in tiles:
        c[t2i(t)] += 1
    return c

# --------------------------------------------------------------------------- #
# agari / tenpai / shanten
# --------------------------------------------------------------------------- #
def _form_sets(c, need):
    """Can `need` melds (triplets/runs) be carved from counts c, leaving nothing?"""
    if need == 0:
        return not any(c)
    i = next((k for k in range(34) if c[k] > 0), None)
    if i is None:
        return False
    if c[i] >= 3:
        c[i] -= 3
        if _form_sets(c, need - 1):
            c[i] += 3
            return True
        c[i] += 3
    if i < 27 and i % 9 <= 6 and c[i + 1] and c[i + 2]:
        c[i] -= 1; c[i + 1] -= 1; c[i + 2] -= 1
        if _form_sets(c, need - 1):
            c[i] += 1; c[i + 1] += 1; c[i + 2] += 1
            return True
        c[i] += 1; c[i + 1] += 1; c[i + 2] += 1
    return False

def is_complete_std(c, melds):
    need = 4 - melds
    for p in range(34):
        if c[p] >= 2:
            c[p] -= 2
            ok = _form_sets(c[:], need)
            c[p] += 2
            if ok:
                return True
    return False

YAOCHU = [0, 8, 9, 17, 18, 26] + list(range(27, 34))

def is_complete_special(c):
    if sum(c) != 14:
        return False
    if all(c[i] in (0, 2) for i in range(34)) and sum(1 for i in range(34) if c[i] == 2) == 7:
        return True  # chiitoitsu
    if all(c[i] == 0 for i in range(34) if i not in YAOCHU):
        kinds = sum(1 for i in YAOCHU if c[i] > 0)
        pair = any(c[i] >= 2 for i in YAOCHU)
        if kinds == 13 and pair:
            return True  # kokushi
    return False

@functools.lru_cache(maxsize=None)
def _std_dfs(c, i, sets, part, pair, melds):
    """Min standard-form shanten for counts `c` (a 34-tuple) from tile index `i`.
    Memoized on the whole state — subproblems (same remaining counts + partial
    build) recur across different decomposition orders and across hands, so
    caching turns the naive exponential search into a fast polynomial one."""
    while i < 34 and c[i] == 0:
        i += 1
    if i == 34:
        m = sets + melds
        p = part
        if m + p > 4:
            p = 4 - m
        return 8 - 2 * m - p - (1 if pair else 0)
    cl = list(c)
    best = 8
    # triplet
    if cl[i] >= 3:
        cl[i] -= 3; best = min(best, _std_dfs(tuple(cl), i, sets + 1, part, pair, melds)); cl[i] += 3
    # run
    if i < 27 and i % 9 <= 6 and cl[i + 1] and cl[i + 2]:
        cl[i] -= 1; cl[i + 1] -= 1; cl[i + 2] -= 1
        best = min(best, _std_dfs(tuple(cl), i, sets + 1, part, pair, melds))
        cl[i] += 1; cl[i + 1] += 1; cl[i + 2] += 1
    # pair (as THE pair)
    if cl[i] >= 2 and not pair:
        cl[i] -= 2; best = min(best, _std_dfs(tuple(cl), i, sets, part, True, melds)); cl[i] += 2
    # partial: pair-as-taatsu (toward triplet)
    if cl[i] >= 2:
        cl[i] -= 2; best = min(best, _std_dfs(tuple(cl), i, sets, part + 1, pair, melds)); cl[i] += 2
    # partial: two-sided / closed run
    if i < 27 and i % 9 <= 7 and cl[i + 1]:
        cl[i] -= 1; cl[i + 1] -= 1; best = min(best, _std_dfs(tuple(cl), i, sets, part + 1, pair, melds)); cl[i] += 1; cl[i + 1] += 1
    if i < 27 and i % 9 <= 6 and cl[i + 2]:
        cl[i] -= 1; cl[i + 2] -= 1; best = min(best, _std_dfs(tuple(cl), i, sets, part + 1, pair, melds)); cl[i] += 1; cl[i + 2] += 1
    # float (discard this copy)
    cl[i] -= 1; best = min(best, _std_dfs(tuple(cl), i, sets, part, pair, melds)); cl[i] += 1
    return best

def _std_shanten(c, melds):
    return _std_dfs(tuple(c), 0, 0, 0, False, melds)

def _chiitoi_shanten(c):
    pairs = sum(1 for x in c if x >= 2)
    kinds = sum(1 for x in c if x > 0)
    return 6 - pairs + max(0, 7 - kinds)

def _kokushi_shanten(c):
    kinds = sum(1 for i in YAOCHU if c[i] > 0)
    pair = any(c[i] >= 2 for i in YAOCHU)
    return 13 - kinds - (1 if pair else 0)

@functools.lru_cache(maxsize=None)
def _shanten_cached(counts, melds):
    c = list(counts)
    s = _std_shanten(c[:], melds)
    if melds == 0:
        s = min(s, _chiitoi_shanten(c), _kokushi_shanten(c))
    return s

def shanten(concealed_counts, melds):
    # Memoized on the 34-count tuple: efficiency analysis re-evaluates the same
    # concealed shapes hundreds of times per decision (ukeire over every tile,
    # over every candidate discard), so caching turns a multi-minute run into a
    # sub-second one.
    return _shanten_cached(tuple(concealed_counts), melds)

def waits(concealed_counts, melds):
    """Tiles (34-index) that complete a 13-tile hand. Empty if not tenpai."""
    out = []
    for t in range(34):
        if concealed_counts[t] >= 4:
            continue
        concealed_counts[t] += 1
        total = sum(concealed_counts)
        done = is_complete_std(concealed_counts[:], melds)
        if not done and melds == 0 and total == 14:
            done = is_complete_special(concealed_counts[:])
        concealed_counts[t] -= 1
        if done:
            out.append(t)
    return out

# --------------------------------------------------------------------------- #
# hand display
# --------------------------------------------------------------------------- #
def hand_str(tiles):
    """Group raw tiles by suit for compact reading, keeping red-five codes."""
    order = {"m": 0, "p": 1, "s": 2, "z": 3}
    def key(t):
        n = norm(t)
        return (order[n[1]], int(n[0]), t)
    groups = {"m": [], "p": [], "s": [], "z": []}
    for t in sorted(tiles, key=key):
        groups[norm(t)[1]].append(strip_pref(t))
    parts = ["".join(groups[s]) for s in "mps z".replace(" ", "") if groups[s]]
    return " ".join(parts) if parts else "-"

# --------------------------------------------------------------------------- #
# per-seat turn walk (the alignment state machine)
# --------------------------------------------------------------------------- #
def remove_tile(hand, t):
    """Remove one instance of tile t (raw), matching red-five by value if needed."""
    if t in hand:
        hand.remove(t); return True
    nt = norm(t)
    for h in hand:
        if norm(h) == nt:
            hand.remove(h); return True
    return False

def walk_seat(haipai, draws, sides, is_dealer):
    hand = list(haipai)
    melds = []               # list of (type, tiles)
    events = []
    di = si = 0
    warnings = []
    turn = 0

    def n_melds():
        return len(melds)

    def snapshot(tag, extra=None):
        c = counts_of(hand)
        # at a post-discard resting point the concealed count is 13-3*melds
        w = []
        sh = None
        if sum(c) % 3 == 1:  # resting shape -> can be tenpai
            w = waits(c[:], n_melds())
            sh = shanten(c[:], n_melds())
        ev = dict(turn=turn, action=tag, hand=hand_str(hand),
                  melds=[f"{m[0]}:{''.join(m[1])}" for m in melds])
        if sh is not None:
            ev["shanten"] = sh
            ev["tenpai"] = (sh == 0)
            ev["waits"] = [i2t(t) for t in w]
        if extra:
            ev.update(extra)
        events.append(ev)

    def do_dispose():
        """Consume one side-stream entry (discard or kan-declaration). Recurses on kan.
        Returns True if a normal discard ended the turn, False if the round ended."""
        nonlocal si, di
        if si >= len(sides):
            return False
        s = sides[si]; si += 1
        tile = strip_pref(s)
        pref = s[: len(s) - len(tile)]
        if pref[:1] in ("a", "k"):
            # This export's a/k prefix is NOT reliable for ankan-vs-added-kan (an
            # added-kan can show up as 'a'). Decide by whether a matching pon already
            # exists: if it does, this is an added-kan upgrading that pon; else ankan.
            jp = next((j for j, m in enumerate(melds)
                       if m[0] == "pon" and norm(m[1][0]) == norm(tile)), None)
            if jp is not None:                        # added-kan (kakan): +1 from hand
                if not remove_tile(hand, tile):
                    warnings.append(f"T{turn}: added-kan {tile} not in hand")
                melds[jp] = ("added-kan", melds[jp][1] + [tile])
                klabel = "added-kan"
            else:                                     # ankan: 4 from hand
                got = sum(1 for _ in range(4) if remove_tile(hand, tile))
                if got < 4:
                    warnings.append(f"T{turn}: ankan {tile} but only {got} in hand")
                melds.append(("ankan", [tile] * 4))
                klabel = "ankan"
            snapshot(f"{klabel} {tile}")
            if di < len(draws) and "," not in draws[di]:  # rinshan
                rin = draws[di]; di += 1
                hand.append(rin)
                if si >= len(sides):                  # rinshan tsumo
                    snapshot(f"rinshan {rin} — TSUMO", {"win": rin}); return False
                return do_dispose()
            warnings.append("kan without rinshan draw"); return False
        # normal discard
        kind = {"r": "tsumogiri", "t": "RIICHI", "": "tedashi"}.get(pref[:1], pref)
        if not remove_tile(hand, tile):
            warnings.append(f"T{turn}: discard {s} not in reconstructed hand — source "
                            f"streams don't reconcile here; trust this seat's trace only up to T{turn-1}")
        snapshot(f"discard {tile} [{kind}]",
                 {"discard": tile, "kind": kind, "riichi": kind == "RIICHI"})
        return True

    # dealer opening discard (14-tile haipai, no draw)
    if is_dealer and len(haipai) == 14:
        turn = 1
        do_dispose()

    while di < len(draws):
        entry = draws[di]; di += 1
        turn += 1
        if "," in entry:                              # a call meld
            typ = {"c": "chi", "p": "pon", "m": "minkan"}.get(entry[0], entry[0])
            parts = entry.split(",")
            called, owns = parts[0][1:], parts[1:]
            for o in owns:
                remove_tile(hand, o)
            melds.append((typ, [called] + owns))
            snapshot(f"call {typ} {called} (+{' '.join(owns)})")
            if entry[0] == "m" and di < len(draws) and "," not in draws[di]:
                rin = draws[di]; di += 1                # minkan rinshan
                hand.append(rin)
                if si >= len(sides):
                    snapshot(f"rinshan {rin} — TSUMO", {"win": rin}); break
            do_dispose()
        else:                                         # normal wall draw
            hand.append(entry)
            if si >= len(sides):                       # last draw, no discard -> tsumo
                snapshot(f"draw {entry} — TSUMO", {"win": entry}); break
            snapshot(f"draw {entry}", {"draw": entry})
            do_dispose()

    return dict(melds=[f"{m[0]}:{''.join(m[1])}" for m in melds],
                events=events, warnings=warnings)

# --------------------------------------------------------------------------- #
# assembly over a decoded doc
# --------------------------------------------------------------------------- #
def build(doc, only_round=None, only_seat=None):
    names, rounds = decode(doc)
    out = []
    for idx, rd in enumerate(rounds):
        if only_round is not None and idx != only_round:
            continue
        dealer = rd["kyoku"] % 4
        wind = WINDS[rd["kyoku"] // 4]; num = rd["kyoku"] % 4 + 1
        seats_out = {}
        for p in range(4):
            if only_seat is not None and p != only_seat:
                continue
            s = rd["seats"][p]
            seats_out[p] = walk_seat(s["haipai"], s["draws"], s["discards"], dealer == p)
            seats_out[p]["riichi_info"] = s.get("riichi_info")
        out.append(dict(idx=idx, label=f"{wind} {num}" + (f" ({rd['honba']} honba)" if rd["honba"] else ""),
                        dealer=names[dealer], dora=rd["dora"], kind=rd["kind"],
                        scores=dict(zip(names, rd["scores"])) if rd.get("scores") else None,
                        names=names, seats=seats_out))
    return names, out

def render(names, built):
    L = []
    for rd in built:
        L.append("=" * 78)
        L.append(f"[idx {rd['idx']}] {rd['label']}   dealer: {rd['dealer']}   "
                 f"dora ind: {', '.join(rd['dora']) or '—'}")
        for p, seat in rd["seats"].items():
            ri = seat.get("riichi_info") or {}
            rlabel = ""
            if ri.get("established"):
                rlabel = (f"  [riichi on {ri['tile']} @T{ri['turn']}]" if ri.get("via") == "tedashi"
                          else f"  [tsumogiri riichi @T≥{ri['turn_min']}]")
            elif ri.get("declared") and ri.get("via") == "tedashi":
                rlabel = f"  [riichi on {ri['tile']} NULLIFIED]"
            L.append(f"\n  --- {names[p]}{' (dealer)' if names[p]==rd['dealer'] else ''}{rlabel} ---")
            if seat["melds"]:
                L.append(f"      melds: {', '.join(seat['melds'])}")
            for e in seat["events"]:
                tag = ""
                if e.get("tenpai"):
                    tag = f"   >> TENPAI  wait {'/'.join(e['waits'])}"
                elif "shanten" in e:
                    tag = f"   ({e['shanten']}-shanten)"
                if "win" in e:
                    tag = "   << WIN"
                L.append(f"      T{e['turn']:>2} {e['action']:<26} | {e['hand']:<34}{tag}")
            if seat["warnings"]:
                L.append(f"      ! warnings: {'; '.join(seat['warnings'])}")
        L.append("")
    return "\n".join(L)

def main():
    ap = argparse.ArgumentParser(description="Turn-by-turn Mahjong Soul replay parser.")
    ap.add_argument("path")
    ap.add_argument("--round", type=int, default=None, help="0-based log index (see mjsoul_decode)")
    ap.add_argument("--seat", type=int, default=None, help="seat 0-3 (E/S/W/N at start)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    doc = load(a.path)
    names, built = build(doc, a.round, a.seat)
    if a.json:
        print(json.dumps({"names": names, "rounds": built}, ensure_ascii=False, indent=2))
    else:
        print(render(names, built))

if __name__ == "__main__":
    main()
