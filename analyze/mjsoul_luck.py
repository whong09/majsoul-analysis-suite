#!/usr/bin/env python3
"""
mjsoul_luck.py — the conversion / realized-luck analysis the rate metrics can't see.

Rate metrics (haipai shanten, ukeire, discard efficiency) average the dice out on
purpose, so they answer "how good were your inputs/decisions" and are structurally
blind to how the tiles actually fell. This measures the OUTPUT side:

  * tenpai -> WIN conversion  (did reaching tenpai actually pay?)
  * live wait width at tenpai (how many of your winning tiles were still live:
    4 - copies visible in all ponds + all melds + dora indicators + your own hand)
  * for tenpais that didn't win: were you OUTRACED (someone else won the hand),
    was it an exhaustive DRAW, or did you DEAL IN while pushing tenpai
  * a luck residual: expected wins (your tenpai count x field's win-per-tenpai)
    vs your actual wins.

If your waits are as live as the field's but your tenpais win far less often, that
gap is variance, not skill. Usage: python3 mjsoul_luck.py file.json [file2 ...]
"""
import sys, glob, json
from collections import Counter
import mjsoul_turns as M

def strip(t):
    return M.strip_pref(t)

def call_tiles(s):
    parts = s.split(",")
    return [parts[0][1:]] + parts[1:]

def gone_tiles(drd):
    """Every tile visible-and-out-of-the-wall this round: all discards, all called
    meld tiles, all kan tiles, and the dora indicators. Returns Counter over norm."""
    g = Counter()
    for p in range(4):
        s = drd["seats"][p]
        for d in s["discards"]:
            t = strip(d)
            if d[:1] in ("a", "k"):        # kan declaration: 4 tiles leave play
                g[M.norm(t)] += 4
            else:
                g[M.norm(t)] += 1
        for c in s.get("calls", []):        # chi/pon/minkan meld tiles
            for t in call_tiles(c):
                g[M.norm(t)] += 1
    for ind in drd.get("dora", []):
        g[M.norm(ind)] += 1
    return g

def final_tenpai(events):
    """Return (waits list, concealed hand tiles) at the last tenpai state, or None."""
    best = None
    for e in events:
        if e.get("tenpai"):
            best = (e["waits"], e["hand"])
        if "win" in e and best is None:
            # won straight off a shape we didn't log as 'tenpai' resting (rare)
            best = (e.get("waits", []), e["hand"])
    return best

def live_width(waits, own_hand_str, gone):
    from_hand = Counter(M.norm(t) for g in [own_hand_str.split()] for grp in g
                        for j in range(0, len(grp), 2) for t in [grp[j:j+2]])
    tot = 0
    for w in waits:
        nw = M.norm(w)
        vis = gone.get(nw, 0) + from_hand.get(nw, 0)
        tot += max(0, 4 - vis)
    return tot

def analyze(paths):
    agg = {"you": dict(tenpai=0, wins=0, width=[], outraced=0, draw=0, dealin=0),
           "field": dict(tenpai=0, wins=0, width=[], outraced=0, draw=0, dealin=0)}
    unlucky = []   # concrete wide-wait tenpais that lost
    for path in paths:
        doc = M.load(path)
        names = doc.get("name", [])
        me = next((i for i, n in enumerate(names) if "(you)" in n), None)
        _, brounds = M.build(doc)
        _, drounds = M.decode(doc)
        for brd, drd in zip(brounds, drounds):
            gone = gone_tiles(drd)
            res = drd["result"]
            winners = set()
            dealin_by = {}
            if isinstance(res, dict) and res.get("agari"):
                for a in res["agari"]:
                    winners.add(a["who"])
                    if not a.get("tsumo"):
                        dealin_by[a["fromWho"]] = a["who"]
            wind = M.WINDS[drd["kyoku"] // 4]; num = drd["kyoku"] % 4 + 1
            for p in range(4):
                ev = brd["seats"][p]["events"]
                ft = final_tenpai(ev)
                won = p in winners
                if not ft and not won:
                    continue
                bucket = "you" if p == me else "field"
                agg[bucket]["tenpai"] += 1
                waits, hand = ft if ft else ([], "")
                lw = live_width(waits, hand, gone) if waits else 0
                agg[bucket]["width"].append(lw)
                if won:
                    agg[bucket]["wins"] += 1
                else:
                    if p in dealin_by:
                        agg[bucket]["dealin"] += 1
                    elif winners:
                        agg[bucket]["outraced"] += 1
                    else:
                        agg[bucket]["draw"] += 1
                    if p == me and waits:
                        unlucky.append((path.split("/")[-1].split("_")[1], f"{wind}{num}",
                                        "/".join(waits), lw,
                                        "dealt-in" if p in dealin_by else
                                        ("outraced" if winners else "draw")))
    return agg, unlucky

def main():
    paths = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not paths:
        print("usage: mjsoul_luck.py file.json [...]"); return
    agg, unlucky = analyze(paths)
    def rate(d): return d["wins"] / d["tenpai"] if d["tenpai"] else float("nan")
    def awid(d): return sum(d["width"]) / len(d["width"]) if d["width"] else float("nan")
    y, f = agg["you"], agg["field"]
    if "--json" in sys.argv:
        print(json.dumps({"you": y, "field": f, "unlucky": unlucky}, ensure_ascii=False, indent=2)); return
    print(f"\n{'':32}{'YOU':>10}{'FIELD':>10}")
    print("-" * 52)
    print(f"{'tenpais reached':32}{y['tenpai']:>10}{f['tenpai']:>10}")
    print(f"{'  -> won':32}{y['wins']:>10}{f['wins']:>10}")
    print(f"{'tenpai -> WIN rate':32}{rate(y)*100:>9.0f}%{rate(f)*100:>9.0f}%")
    print(f"{'avg LIVE wait width at tenpai':32}{awid(y):>10.2f}{awid(f):>10.2f}")
    print(f"\n{'of tenpais that did NOT win:':32}")
    print(f"{'  outraced (someone else won)':32}{y['outraced']:>10}{f['outraced']:>10}")
    print(f"{'  exhaustive draw':32}{y['draw']:>10}{f['draw']:>10}")
    print(f"{'  dealt in while tenpai':32}{y['dealin']:>10}{f['dealin']:>10}")
    exp = y["tenpai"] * rate(f)
    print(f"\nLUCK RESIDUAL")
    print(f"  your tenpais: {y['tenpai']}   field win-per-tenpai: {rate(f)*100:.0f}%")
    print(f"  expected wins at field's rate: {exp:.1f}   actual wins: {y['wins']}   "
          f"gap: {y['wins']-exp:+.1f}")
    if unlucky:
        print(f"\nYour tenpais that lost, by wait liveness (live tiles = winners still out there):")
        for game, rnd, waits, lw, how in sorted(unlucky, key=lambda x: -x[3]):
            print(f"  {game:>6} {rnd:<7} wait {waits:<10} {lw} live  -> {how}")

if __name__ == "__main__":
    main()
