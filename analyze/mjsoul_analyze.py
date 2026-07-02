#!/usr/bin/env python3
"""
mjsoul_analyze.py — play-style / efficiency / luck analysis across one or more replays.

Builds on mjsoul_turns.py (hand reconstruction + shanten/waits) and adds:
  * EFFICIENCY: for every free (non-forced) discard, is it acceptance(ukeire)-optimal?
    Reports match-rate and clear shanten-losing errors. (Pure-efficiency yardstick;
    some deviations are intentional value/defense trades — flagged, not condemned.)
  * LUCK: starting-hand shanten & dora, tenpai-reached rate, and useful-draw rate
    (draws that lowered shanten) — the "how good were my tiles" question.
  * STYLE: riichi rate, call/open rate, damaten, avg turns to tenpai.

Everything is computed for the marked player ("(you)" in the name list) and, for
context, pooled over the other three seats in the same games (the "field").

Usage:  python3 mjsoul_analyze.py file1.json [file2.json ...]
        python3 mjsoul_analyze.py *.json --json
"""
import sys, glob, json, statistics as st
import mjsoul_turns as M

# ---- acceptance (ukeire) + optimal-discard -------------------------------- #
def ukeire(c, melds):
    base = M.shanten(c[:], melds)
    tiles = kinds = 0
    for t in range(34):
        av = 4 - c[t]
        if av <= 0:
            continue
        c[t] += 1
        if M.shanten(c[:], melds) < base:
            kinds += 1; tiles += av
        c[t] -= 1
    return base, tiles

def best_discard(tiles, melds):
    """Over all discards from a (3k+2)-tile hand: min shanten, then max ukeire."""
    c = M.counts_of(tiles)
    best_sh, best_uk = 99, -1
    per = {}
    seen = set()
    for t in range(34):
        if c[t] == 0:
            continue
        c[t] -= 1
        sh, uk = ukeire(c, melds)
        c[t] += 1
        per[M.i2t(t)] = (sh, uk)
        if (sh, -uk) < (best_sh, -best_uk):
            best_sh, best_uk = sh, uk
    return best_sh, best_uk, per

def dora_of(ind):
    ind = M.norm(ind); n = int(ind[0]); s = ind[1]
    if s in "mps":
        return f"{1 if n == 9 else n+1}{s}"
    if n in (1, 2, 3):  return f"{n+1}z"   # E->S->W->N
    if n == 4:          return "1z"
    if n in (5, 6):     return f"{n+1}z"   # White->Green->Red
    return "5z"                            # Red->White

def parse_hand(hs):
    out = []
    for g in hs.split():
        for j in range(0, len(g), 2):
            out.append(g[j:j+2])
    return out

# ---- per-seat metrics from a decoded round -------------------------------- #
def seat_metrics(bseat, dseat, dora_ind):
    ev = bseat["events"]
    info = bseat.get("riichi_info") or {}

    # starting-hand luck
    haipai = dseat["haipai"]
    hp_shanten = M.shanten(M.counts_of(haipai), 0) if haipai else None
    hp_dora = None
    if haipai and dora_ind:
        dt = dora_of(dora_ind)
        hp_dora = sum(1 for t in haipai if M.norm(t) == dt) + \
                  sum(1 for t in haipai if t in ("0m", "0p", "0s"))

    # riichi cutoff: discards at/after this turn are forced (excluded from efficiency)
    cutoff = 10**9
    if info.get("established"):
        cutoff = info["turn"] if info.get("via") == "tedashi" else (info.get("turn_min") or cutoff)

    decisions = []            # (optimal?, shanten_err?, ukeire_lost, turn, discard, best)
    useful = draws = 0
    last_rest = hp_shanten
    tenpai_turn = None
    for i, e in enumerate(ev):
        sh = e.get("shanten")
        nm = len(e["melds"])
        # A well-formed concealed hand has exactly 14-3*melds tiles pre-discard.
        # If the turn-walker flagged "streams don't reconcile", the reconstruction
        # can be oversized; feeding that to the shanten DFS blows up exponentially,
        # so we require the exact count before doing any shanten/ukeire work.
        tiles = parse_hand(e["hand"])
        well_formed = (len(tiles) == 14 - 3 * nm)
        # useful-draw accounting (wall draws only, before tenpai, before riichi)
        if "draw" in e and well_formed and last_rest is not None and last_rest > 0 and e["turn"] < cutoff:
            post = M.shanten(M.counts_of(tiles), nm)
            draws += 1
            if post < last_rest:
                useful += 1
        # efficiency decision: a well-formed pre-discard state followed by a discard
        is_pre = well_formed
        nxt = ev[i+1] if i+1 < len(ev) else None
        if is_pre and nxt and "discard" in nxt and e["turn"] < cutoff:
            bsh, buk, per = best_discard(tiles, len(e["melds"]))
            act = per.get(M.norm(nxt["discard"]))
            if act:
                ash, auk = act
                optimal = (ash == bsh and auk == buk)
                serr = ash > bsh
                decisions.append(dict(turn=nxt["turn"], discard=nxt["discard"],
                                      optimal=optimal, shanten_err=serr,
                                      uke_lost=(buk - auk if ash == bsh else None),
                                      best_sh=bsh, act_sh=ash))
        if sh is not None:
            last_rest = sh
            if sh == 0 and tenpai_turn is None:
                tenpai_turn = e["turn"]
        if "win" in e and tenpai_turn is None:
            tenpai_turn = e["turn"]

    return dict(hp_shanten=hp_shanten, hp_dora=hp_dora,
                reached_tenpai=(tenpai_turn is not None), tenpai_turn=tenpai_turn,
                useful=useful, draws=draws, decisions=decisions,
                riichi=bool(info.get("established")),
                opened=bool(dseat.get("calls")))

# ---- aggregation ---------------------------------------------------------- #
def analyze(paths):
    per_seat = {"you": [], "field": []}
    dealins = []   # (game, round_label, winner_had_riichi, winner_calls)
    for path in paths:
        doc = M.load(path)
        names = doc.get("name", [])
        me = next((i for i, n in enumerate(names) if "(you)" in n), None)
        _, brounds = M.build(doc)     # walker output (events)
        _, drounds = M.decode(doc)    # raw haipai / calls
        for brd, drd in zip(brounds, drounds):
            dora_ind = brd["dora"][0] if brd["dora"] else None
            for p in range(4):
                m = seat_metrics(brd["seats"][p], drd["seats"][p], dora_ind)
                (per_seat["you"] if p == me else per_seat["field"]).append(m)
        # deal-in visibility (light pond-reading proxy) for the user
        for rd in drounds:
            res = rd["result"]
            if isinstance(res, dict) and res.get("agari"):
                for a in res["agari"]:
                    if not a.get("tsumo") and a["fromWho"] == me:
                        w = a["who"]
                        winfo = rd["seats"][w].get("riichi_info") or {}
                        wcalls = len(rd["seats"][w].get("calls") or [])
                        dealins.append((path.split("/")[-1], rd["kyoku"],
                                        bool(winfo.get("established")), wcalls))
    return per_seat, dealins

def agg(rows):
    def mean(xs):
        xs = [x for x in xs if x is not None]
        return st.mean(xs) if xs else float("nan")
    decs = [d for r in rows for d in r["decisions"]]
    n = len(decs)
    opt = sum(1 for d in decs if d["optimal"])
    serr = sum(1 for d in decs if d["shanten_err"])
    uloss = [d["uke_lost"] for d in decs if d["uke_lost"] not in (None,)]
    tenp = [r for r in rows if r["reached_tenpai"]]
    return dict(
        hands=len(rows),
        hp_shanten=mean([r["hp_shanten"] for r in rows]),
        hp_dora=mean([r["hp_dora"] for r in rows]),
        tenpai_rate=len(tenp) / len(rows) if rows else float("nan"),
        tenpai_turn=mean([r["tenpai_turn"] for r in tenp]),
        useful_rate=(sum(r["useful"] for r in rows) / sum(r["draws"] for r in rows)
                     if sum(r["draws"] for r in rows) else float("nan")),
        eff_decisions=n,
        eff_optimal=opt / n if n else float("nan"),
        shanten_errs=serr,
        shanten_err_rate=serr / n if n else float("nan"),
        avg_uke_lost=mean(uloss),
        riichi_rate=sum(1 for r in rows if r["riichi"]) / len(rows) if rows else float("nan"),
        open_rate=sum(1 for r in rows if r["opened"]) / len(rows) if rows else float("nan"),
    )

def fmt(x, pct=False, d=2):
    if x != x:  # nan
        return "—"
    return f"{x*100:.0f}%" if pct else f"{x:.{d}f}"

def main():
    paths = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not paths:
        print("usage: mjsoul_analyze.py file.json [...]"); return
    per_seat, dealins = analyze(paths)
    y, f = agg(per_seat["you"]), agg(per_seat["field"])
    if "--json" in sys.argv:
        print(json.dumps({"you": y, "field": f, "dealins": dealins},
                         ensure_ascii=False, indent=2)); return
    rows = [
        ("hands played",              str(y["hands"]),                 str(f["hands"])),
        ("--- LUCK (starting tiles & draws) ---", "", ""),
        ("avg haipai shanten (lower=better start)", fmt(y["hp_shanten"]), fmt(f["hp_shanten"])),
        ("avg haipai dora+aka",       fmt(y["hp_dora"]),               fmt(f["hp_dora"])),
        ("reached tenpai",            fmt(y["tenpai_rate"], 1),        fmt(f["tenpai_rate"], 1)),
        ("avg turn reaching tenpai",  fmt(y["tenpai_turn"], d=1),      fmt(f["tenpai_turn"], d=1)),
        ("useful-draw rate (draws that advanced shanten)", fmt(y["useful_rate"], 1), fmt(f["useful_rate"], 1)),
        ("--- EFFICIENCY (your decisions) ---", "", ""),
        ("free discards evaluated",   str(y["eff_decisions"]),         str(f["eff_decisions"])),
        ("acceptance-optimal discard", fmt(y["eff_optimal"], 1),       fmt(f["eff_optimal"], 1)),
        ("shanten-losing discards (clear errors)", str(y["shanten_errs"]), str(f["shanten_errs"])),
        ("  as rate of decisions",    fmt(y["shanten_err_rate"], 1),   fmt(f["shanten_err_rate"], 1)),
        ("avg ukeire lost on off-optimal", fmt(y["avg_uke_lost"], d=1), fmt(f["avg_uke_lost"], d=1)),
        ("--- STYLE ---", "", ""),
        ("riichi rate (per hand)",    fmt(y["riichi_rate"], 1),        fmt(f["riichi_rate"], 1)),
        ("open/call rate (per hand)", fmt(y["open_rate"], 1),          fmt(f["open_rate"], 1)),
    ]
    w = max(len(r[0]) for r in rows)
    print(f"\n{'metric':<{w}}   {'YOU':>8}   {'FIELD':>8}")
    print("-" * (w + 22))
    for a, b, c in rows:
        if a.startswith("---"):
            print(f"\n{a}")
        else:
            print(f"{a:<{w}}   {b:>8}   {c:>8}")
    if dealins:
        print("\n--- deal-in visibility (pond-reading proxy) ---")
        vis = sum(1 for d in dealins if d[2] or d[3] >= 2)
        print(f"your deal-ins: {len(dealins)}  |  into a visible threat "
              f"(winner had riichi'd or ≥2 calls): {vis}  |  into a quiet hand: {len(dealins)-vis}")

if __name__ == "__main__":
    main()
