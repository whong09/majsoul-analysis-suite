#!/usr/bin/env python3
"""
seer_decode.py — pure decoder for the Mahjong Soul MAKA ("Seer") analysis protobuf.

No dependencies (no CDP, no websockets) so both the extractor (heap path) and
seer_capture (websocket path) can share it without a circular import.

MAKA is internally "Seer". Its per-decision analysis is fetched via
`.lq.Lobby.fetchSeerReport` on every replay open; the raw response protobuf also
persists in the WASM heap, so it can be read either off the wire or out of the heap.

Wire shape (reverse-engineered — the .proto is undocumented):
  Res    { f2: Report }
  Report { f1: uuid(str)
           f2: repeated Decision { f1,f2: action indices into the game record
                                   f3: Detail { f1: seat 0-3 (absent => 0)
                                                f2: repeated Candidate { f1: tile_code
                                                                         f2: score 0-100 } } }
           f3: repeated Round    { f2: round_idx
                                   f4: repeated { f1: seat, f2: score 0-100 } } }
Tile codes are MJS-internal: man 110+r / pin 120+r / sou 130+r / honor 140+r (0=red5);
small codes and 200+ are calls/actions (left as raw '#code').
"""


# ---- protobuf wire-format reader (schema-free) ------------------------------
def _rv(b, i):
    r = 0; s = 0
    while True:
        x = b[i]; r |= (x & 0x7f) << s; i += 1
        if not x & 0x80:
            break
        s += 7
    return r, i

def _fields(b):
    """Return [(field_no, wire, value)]; value is int for varint, bytes for len-delimited."""
    i = 0; n = len(b); o = []
    while i < n:
        try:
            tag, i = _rv(b, i)
        except IndexError:
            break
        f = tag >> 3; wt = tag & 7
        if wt == 0:
            v, i = _rv(b, i); o.append((f, 0, v))
        elif wt == 2:
            ln, i = _rv(b, i)
            if i + ln > n:
                break
            o.append((f, 2, b[i:i+ln])); i += ln
        elif wt == 5:
            o.append((f, 5, b[i:i+4])); i += 4
        elif wt == 1:
            o.append((f, 1, b[i:i+8])); i += 8
        else:
            break
    return o

def _first(fs, num, wire=None):
    for f, w, v in fs:
        if f == num and (wire is None or w == wire):
            return v
    return None

def _all(fs, num):
    return [v for f, w, v in fs if f == num]


# ---- tile decoding ----------------------------------------------------------
def tile_name(code):
    """MJS-internal tile code -> human string (e.g. 133 -> '3s', 145 -> '5z')."""
    for base, suit in ((110, "m"), (120, "p"), (130, "s"), (140, "z")):
        if base <= code <= base + 9:
            return f"{code - base}{suit}"      # 0 = red five
    return f"#{code}"                            # small codes / calls: raw action code


# ---- report building --------------------------------------------------------
def _build(uuid, rf):
    decisions = []
    for dc in _all(rf, 2):
        ff = _fields(dc)
        detail = _first(ff, 3, 2)
        cand = []; seat = 0
        if detail is not None:
            df = _fields(detail)
            s = _first(df, 1, 0)                 # f1 = seat (0-3); absent => 0
            seat = s if s is not None else 0
            for cc in _all(df, 2):
                kv = _fields(cc)
                cand.append({"tile": tile_name(_first(kv, 1, 0) or 0),
                             "code": _first(kv, 1, 0), "score": _first(kv, 2, 0)})
        decisions.append({
            "a": _first(ff, 1, 0), "b": _first(ff, 2, 0),   # action indices into the record
            "seat": seat,
            "candidates": cand,                              # MAKA-rated options (0-100 each)
            "best": max((c["score"] or 0) for c in cand) if cand else None,
        })
    rounds = []
    for sc in _all(rf, 3):
        sf = _fields(sc)
        seats = {}
        for x in _all(sf, 4):
            xf = _fields(x)
            s = _first(xf, 1, 0); seats[s if s is not None else 0] = _first(xf, 2, 0)
        rounds.append({"round": _first(sf, 2, 0), "scores": seats})   # {seat: 0-100}
    return {"uuid": uuid, "decisions": decisions, "rounds": rounds}


def decode_report(raw):
    """raw = full ws frame (incl. 3-byte MJS wrapper) OR the bare Res payload.
    Returns a dict, or None if it doesn't look like a seer report."""
    for start in (3, 0):
        try:
            res = _fields(raw[start:])
            wrap = _first(res, 2, 2)
            if wrap is None:
                continue
            report = _first(_fields(wrap), 2, 2)
            if report is None:
                continue
            rf = _fields(report)
            uuid = _first(rf, 1, 2)
            if uuid and b"-" in uuid and len(uuid) > 20:
                return _build(uuid.decode("utf-8", "replace"), rf)
        except (IndexError, ValueError):
            continue
    return None


def decode_bare_report(win):
    """win = heap bytes starting at the Report's field1 ('0a <len> <uuid> ...').
    Returns a dict, or None."""
    try:
        rf = _fields(win)
        uuid = _first(rf, 1, 2)
        if uuid and b"-" in uuid and len(uuid) > 20:
            return _build(uuid.decode("utf-8", "replace"), rf)
    except (IndexError, ValueError):
        pass
    return None


# ---- condensed summary ------------------------------------------------------
def maka_summary(report):
    """Per-game + per-seat MAKA numbers. Per-seat rating = mean of that seat's
    per-round scores (0-100)."""
    if not report or not report.get("rounds"):
        return None
    seat_scores = {}
    for rd in report["rounds"]:
        for seat, sc in rd["scores"].items():
            if sc is not None:
                seat_scores.setdefault(seat, []).append(sc)
    per_seat = {s: round(sum(v) / len(v), 1) for s, v in seat_scores.items()}
    return {
        "uuid": report["uuid"],
        "rounds": len(report["rounds"]),
        "decisions": len(report["decisions"]),
        "seat_rating": per_seat,                       # {seat: mean 0-100}
        "round_scores": [rd["scores"] for rd in report["rounds"]],
    }
