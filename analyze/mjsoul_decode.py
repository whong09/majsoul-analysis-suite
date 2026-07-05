#!/usr/bin/env python3
"""
Decoder for Mahjong Soul -> Tenhou-6.0 JSON replays (the pinyin-meld / dict-result variant).

Usage:
    python3 mjsoul_decode.py path/to/replay.json
    python3 mjsoul_decode.py path/to/replay.json --json   # machine-readable summary

Notes on the format this handles (see the accompanying instructions .md for full detail):
  * Round array indices: 0=meta, 1=dora indicators, 2=ura-dora indicators,
    3/6/9/12=haipai, 4/7/10/13=draws, 5/8/11/14=discards, last elem=result dict.
  * meta = [kyoku, honba, riichi_sticks, s0, s1, s2, s3]  (scores x100).
  * Discard prefixes:  r=tsumogiri (drew & threw it),  t=RIICHI declaration tile,
    a=ankan (closed kan),  (no prefix)=tedashi (thrown from hand).
  * Draw-stream calls:  c=chi, p=pon, m=minkan; string is "{type}{called},{own},{own}[,own]"
    with the called tile listed first.  (Source seat is not encoded in this variant.)
  * Result shapes (last element of a round):
      - agari:      {"agari":[...], "owari":[deltas], "sc":[scores]}
      - exhaustive: {"owari":[tenpai payments], "sc":[scores]}  (ryuukyoku, no "agari")
      - abortive:   a non-dict (e.g. []) — kyuushu kyuuhai, four riichi, four kans,
                    four winds, triple ron. No score change; scores carry from meta.
    Points are absolute yen; owari deltas are absolute yen.
"""
import json, sys, re

WINDS = ["East", "South", "West", "North"]

def round_label(kyoku, honba=0):
    """Canonical round label. Base hands are just wind-number (East-1, East-2); the
    honba component (honba+1) is appended only when the round carried a honba:
    East-1 (base), East-1-2 (1 honba), East-1-3 (2 honba)…"""
    base = f"{WINDS[kyoku // 4]}-{kyoku % 4 + 1}"
    return base if not honba else f"{base}-{honba + 1}"
HONOR = {"1z":"East","2z":"South","3z":"West","4z":"North",
         "5z":"White","6z":"Green","7z":"Red"}
CALL = {"c":"chi","p":"pon","m":"minkan","a":"ankan","k":"added-kan"}

# Display abbreviations for honors in the CLI reports: winds E/S/W/N, dragons Wh/G/R.
# (The tenhou6 JSON still encodes them as Nz; this is display-only.) Canonical here so
# every tool — decode, turns, luck — renders honors the same way.
HONOR_DISP = {"1z":"E","2z":"S","3z":"W","4z":"N","5z":"Wh","6z":"G","7z":"R"}

def dtile(t):
    """One tile in display form: honors -> E/S/W/N/Wh/G/R; red fives kept (0m/0p/0s).
    Tolerates discard/call prefixes (r/t/a/k)."""
    return HONOR_DISP.get(t.lstrip("rtak"), t.lstrip("rtak"))

def dtiles(s):
    """Convert every 2-char tile code appearing in a string (e.g. a meld like
    'shunzi(5z,5z,5z)') to display form."""
    return re.sub(r"[0-9][mpsz]", lambda m: dtile(m.group()), s)

def pretty(t):
    """Human label for a tile code, keeping red-five and prefix info."""
    return t

def is_terminal_or_honor(t):
    """True for 1/9 of a suit or any honor. Red fives (0m/0p/0s) are NOT terminals."""
    t = t.lstrip("rtak")            # tolerate discard prefixes; haipai has none
    if len(t) != 2:
        return False
    n, s = t[0], t[1]
    if s == "z":
        return n in "1234567"
    if s in "mps":
        return n in "19"
    return False

def abortive_reason(seats):
    """Best-effort label for why an abortive (non-dict) round ended. Never raises."""
    try:
        vals = list(seats.values())
        # four riichi
        if vals and all(s.get("riichi") for s in vals):
            return "four riichi (suucha riichi)"
        # nine terminals/honors in an opening hand (kyuushu kyuuhai)
        if any(len({t for t in s.get("haipai", []) if is_terminal_or_honor(t)}) >= 9
               for s in vals):
            return "nine terminals/honors (kyuushu kyuuhai)"
        # four kans: open kan in calls (m...) + closed/added kan on the discard side (a.../k...)
        kans = sum(1 for s in vals for c in s.get("calls", []) if c and c[0] in "m")
        kans += sum(1 for s in vals for d in s.get("discards", [])
                    if d.startswith("a") or d.startswith("k"))
        if kans >= 4:
            return "four kans (suukaikan)"
        # four winds discarded first turn (suufon renda)
        firsts = [s["discards"][0] for s in vals if s.get("discards")]
        if len(firsts) == 4:
            norm = [d.lstrip("rt") for d in firsts]
            if norm[0] in ("1z", "2z", "3z", "4z") and all(x == norm[0] for x in norm):
                return "four identical winds discarded (suufon renda)"
    except Exception:
        pass
    return "abortive draw"

def classify_result(res, start, seats):
    """Return (kind, deltas, scores, reason) for any result shape. Defensive.

    kind is one of: 'agari', 'exhaustive', 'abortive', 'unknown'.
    scores is always a 4-list of absolute points (carried from `start` when the
    result records no new scores, e.g. an abortive draw)."""
    if isinstance(res, dict):
        if res.get("agari"):
            return "agari", res.get("owari"), res.get("sc") or list(start), None
        if res.get("sc") is not None or res.get("owari") is not None:
            return "exhaustive", res.get("owari"), res.get("sc") or list(start), None
        return "unknown", None, list(start), None
    # non-dict (empty list, etc.): abortive draw, nobody's score changes
    return "abortive", [0, 0, 0, 0], list(start), abortive_reason(seats)

def riichi_status(discards, start, owari, sc):
    """Per-seat riichi detection for one round. Returns a 4-list of dicts:
        {established, declared, tile, via, turn, turn_min, note}

    Why this exists: the export marks a *tedashi* riichi with a 't' prefix and
    gives the exact tile, but a riichi declared on a *drawn* tile (tsumogiri
    riichi) is written with the same 'r' as any tsumogiri, so a 't'-only scan
    misses it entirely. Worse, a 't' declaration whose tile is immediately ronned
    is NULLIFIED (no stick paid), so 't' can also over-report.

    The authoritative signal is the paid riichi stick, recovered from scores:
        owari[p] - (sc[p] - start[p]) == 1000   <=>  seat p established riichi.
    (`owari` reports each seat's change with its OWN stick added back; `sc` is the
    true running total. The 1000 gap is exactly the stick.) This needs both owari
    and sc; on abortive rounds (neither present) we fall back to the 't' marker.
    """
    out = []
    can_score = owari is not None and sc is not None and start is not None
    for p in range(4):
        d = discards[p]
        t_idx = next((i for i, x in enumerate(d) if x.startswith("t")), None)
        t_tile = d[t_idx][1:] if t_idx is not None else None
        paid = ((owari[p] - (sc[p] - start[p])) == 1000) if can_score else None

        info = dict(established=False, declared=False, tile=None, via=None,
                    turn=None, turn_min=None, note=None)

        if t_tile is not None:
            # A tedashi declaration: exact tile known.
            info.update(declared=True, tile=t_tile, via="tedashi", turn=t_idx + 1)
            if paid is False:
                info["note"] = ("declaration ronned on this tile — riichi nullified, "
                                "no stick paid")
                info["established"] = False
            else:
                info["established"] = bool(paid) if paid is not None else True
        elif paid:
            # Established a stick with no 't' -> tsumogiri riichi. Exact tile is
            # not encoded; bound the turn by the trailing all-tsumogiri run.
            tail = 0
            for x in reversed(d):
                if x.startswith("r"):
                    tail += 1
                else:
                    break
            info.update(established=True, declared=True, via="tsumogiri",
                        turn_min=len(d) - tail + 1,
                        note="tsumogiri riichi — exact declaration tile not encoded in this export")
        out.append(info)
    return out


def load(path):
    with open(path) as f:
        return json.load(f)

def decode(doc):
    log = doc["log"]
    names = doc.get("name", [f"Player{i}" for i in range(4)])
    rounds = []
    for r in log:
        meta = r[0]
        kyoku, honba, sticks = meta[0], meta[1], meta[2]
        start = [s*100 for s in meta[3:7]]
        dora = r[1]
        ura  = r[2]
        seats = {}
        all_discs = []
        for p in range(4):
            hi, wi, di = 3+3*p, 4+3*p, 5+3*p
            # tolerate short/malformed rounds rather than crashing
            haipai = r[hi] if hi < len(r) else []
            draws  = r[wi] if wi < len(r) else []
            discs  = r[di] if di < len(r) else []
            calls = [d for d in draws if "," in d]
            seats[p] = dict(haipai=haipai, draws=draws, discards=discs, calls=calls)
            all_discs.append(discs)
        res = r[-1]
        kind, deltas, scores, reason = classify_result(res, start, seats)

        # Authoritative riichi detection (score reconciliation + 't' for the tile).
        rstat = riichi_status(all_discs, start, deltas, scores)
        for p in range(4):
            info = rstat[p]
            seats[p]["riichi_info"] = info
            # Back-compat 'riichi' field: exact tile if an established tedashi
            # riichi, True for an established tsumogiri riichi, else None.
            if info["established"]:
                seats[p]["riichi"] = info["tile"] if info["tile"] else True
            else:
                seats[p]["riichi"] = None
        rounds.append(dict(kyoku=kyoku, honba=honba, sticks=sticks,
                           start=start, dora=dora, ura=ura, seats=seats, result=res,
                           kind=kind, deltas=deltas, scores=scores, reason=reason))
    return names, rounds

def fmt_call(c):
    typ = CALL.get(c[0], c[0])
    tiles = [dtile(t) for t in c[1:].split(",")]
    return f"{typ}({tiles[0]}←, {' '.join(tiles[1:])})"

def report(doc):
    names, rounds = decode(doc)
    out = []
    disp = doc.get("rule", {}).get("disp", "?")
    aka  = doc.get("rule", {}).get("aka", 0)
    title = doc.get("title", [])
    room = title[1] if len(title) > 1 else None
    out.append(f"{' vs '.join(names)}")
    if room: out.append(f"Room: {room}")
    out.append(f"Ruleset: {disp}  |  red-fives: {'on' if aka else 'off'}\n")
    for rd in rounds:
        oya = rd["kyoku"] % 4
        hdr = round_label(rd["kyoku"], rd["honba"]) + (f" ({rd['honba']} honba)" if rd["honba"] else "")
        out.append(f"=== {hdr} ===  dealer: {names[oya]}"
                   + (f"  |  {rd['sticks']} riichi stick(s) carried" if rd["sticks"] else ""))
        out.append(f"  dora indicator: {', '.join(dtile(x) for x in rd['dora']) or '—'}")
        # riichi declarations (established sticks + nullified declarations)
        ri, nulled = [], []
        for p, s in rd["seats"].items():
            info = s.get("riichi_info", {})
            if info.get("established"):
                if info.get("via") == "tedashi":
                    ri.append(f"{names[p]} (on {dtile(info['tile'])}, turn {info['turn']})")
                else:
                    ri.append(f"{names[p]} (tsumogiri riichi, declared turn \u2265{info['turn_min']}; "
                              f"exact tile not encoded)")
            elif info.get("declared") and info.get("via") == "tedashi":
                nulled.append(f"{names[p]} (declared on {dtile(info['tile'])}, ronned before establishing)")
        if ri:     out.append("  riichi: " + "; ".join(ri))
        if nulled: out.append("  riichi (nullified): " + "; ".join(nulled))
        # calls
        for p, s in rd["seats"].items():
            if s["calls"]:
                out.append(f"  {names[p]} calls: " + ", ".join(fmt_call(c) for c in s["calls"]))
        res = rd["result"]
        if rd["kind"] == "agari":
            for a in res["agari"]:
                who = names[a["who"]]
                how = "tsumo" if a.get("tsumo") else f"ron off {names[a['fromWho']]}"
                melds = ("  melds: " + ", ".join(dtiles(m) for m in a["melds"])) if a.get("melds") else ""
                out.append(f"  WIN: {who} — {a['points']} pts ({how}), wait {dtile(a['machi'])}{melds}")
            if rd["deltas"]: out.append(f"  deltas: {dict(zip(names, rd['deltas']))}")
            if rd["scores"]: out.append(f"  scores: {dict(zip(names, rd['scores']))}")
        elif rd["kind"] == "exhaustive":
            out.append("  EXHAUSTIVE DRAW (ryuukyoku)")
            if rd["deltas"]: out.append(f"  tenpai payments: {dict(zip(names, rd['deltas']))}")
            if rd["scores"]: out.append(f"  scores: {dict(zip(names, rd['scores']))}")
        elif rd["kind"] == "abortive":
            out.append(f"  ABORTIVE DRAW — {rd['reason']} (no score change)")
            if rd["scores"]: out.append(f"  scores: {dict(zip(names, rd['scores']))}")
        else:
            out.append(f"  UNRECOGNIZED result; raw: {res}")
        out.append("")
    # final standings: last round that carries concrete scores (skips trailing draws
    # that record none, though abortive rounds now carry the unchanged scores forward)
    final = next((rd["scores"] for rd in reversed(rounds) if rd.get("scores")), None)
    if final:
        rank = sorted(zip(names, final), key=lambda x: -x[1])
        out.append("FINAL STANDINGS:")
        for i, (n, s) in enumerate(rank, 1):
            out.append(f"  {i}. {n}: {s}")
    return "\n".join(out)

def main():
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(1)
    doc = load(sys.argv[1])
    if "--json" in sys.argv:
        names, rounds = decode(doc)
        print(json.dumps({"names": names, "rounds": rounds}, ensure_ascii=False, indent=2))
    else:
        print(report(doc))

if __name__ == "__main__":
    main()