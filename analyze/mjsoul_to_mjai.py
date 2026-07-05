#!/usr/bin/env python3
"""
Convert a Mahjong Soul -> Tenhou-6.0 *variant* replay (the pinyin-meld / dict-result
format that mjsoul_decode.py parses) into an **mjai JSONL event stream** that Mortal's
`libriichi.mjai.Bot` consumes.

This is a NEW, additive module. It does not modify any existing suite file. It reuses
mjsoul_decode.decode() for parsing and only adds the global-order reconstruction +
tile/event mapping that Mortal needs.

--- The hard part: global event order ---
The source stores per-seat draw/discard streams. mjai needs the true chronological
interleave, including calls that jump the turn. We rebuild it by simulating turn order:

  * Turn starts at the dealer (oya = kyoku % 4). The dealer's haipai has 14 tiles
    (the auto-drawn tile folded in); non-dealers have 13 and draw each turn.
  * A seat's next `draws` element that contains ',' is a CALL (c=chi, p=pon, m=daiminkan)
    made INSTEAD of drawing. The called tile is listed first; the source seat is not
    encoded, so we infer it: the call fires on the *most recent discard* whose tile
    matches (chi only from the caller's kamicha).
  * After a daiminkan the next `draws` element is the rinshan draw. Ankan ('a') / kakan
    ('k') appear in the `discards` stream, each followed by a rinshan draw in `draws`.

Every produced stream is meant to be VALIDATED by replaying it through libriichi's
PlayerState (see mjsoul_mortal.py / validate_stream). We do not trust it until it replays
clean for all four seats.

--- Tile mapping (verified against libriichi/src/tile.rs MJAI_PAI_STRINGS) ---
  number tiles 1-9 m/p/s : unchanged ("3m" -> "3m")
  red fives 0m/0p/0s     : -> "5mr"/"5pr"/"5sr"
  honors 1z..7z          : -> E,S,W,N (winds) then P,F,C (haku/hatsu/chun)
"""
import sys, json, re
from os import path

sys.path.insert(0, path.dirname(path.abspath(__file__)))
import mjsoul_decode

# Bump when the conversion logic changes in a way that could alter emitted events (and thus
# Mortal's inputs / precomputed sidecars). Recorded in each sidecar's provenance.
CONVERTER_VERSION = 1

HONOR_TO_MJAI = {'1z':'E', '2z':'S', '3z':'W', '4z':'N', '5z':'P', '6z':'F', '7z':'C'}
AKA_TO_MJAI   = {'0m':'5mr', '0p':'5pr', '0s':'5sr'}
BAKAZE = ['E', 'S', 'W', 'N']

# canonical mjai tile ordering (index == libriichi Tile id), for action-index math
MJAI_TILES = (
    [f'{n}{s}' for s in 'mps' for n in range(1, 10)]
    + ['E', 'S', 'W', 'N', 'P', 'F', 'C', '5mr', '5pr', '5sr']
)
TILE_TO_IDX = {t: i for i, t in enumerate(MJAI_TILES)}

# valid SOURCE tile codes (used to detect a/k kan prefixes on the discard side)
_KANNABLE = frozenset(
    [f'{n}{s}' for s in 'mps' for n in range(1, 10)]
    + [f'{n}z' for n in range(1, 8)]
    + ['0m', '0p', '0s']
)


def to_mjai_tile(t: str) -> str:
    """Map one source tile code (no prefix) to an mjai tile string."""
    if t in AKA_TO_MJAI:
        return AKA_TO_MJAI[t]
    if t in HONOR_TO_MJAI:
        return HONOR_TO_MJAI[t]
    return t  # 1m..9m / 1p..9p / 1s..9s already match


def deaka(mjai_tile: str) -> str:
    return {'5mr': '5m', '5pr': '5p', '5sr': '5s'}.get(mjai_tile, mjai_tile)


class ConversionError(Exception):
    pass


def convert_round(rd, oya_names=None):
    """Return a list of mjai event dicts for one decoded round (no start_game/end_game)."""
    kyoku = rd['kyoku']
    honba = rd['honba']
    sticks = rd['sticks']
    start = rd['start']
    dora = [to_mjai_tile(d) for d in rd['dora']]
    oya = kyoku % 4
    seats = rd['seats']

    # per-seat mutable stream state
    draws = {p: list(seats[p]['draws']) for p in range(4)}
    discs = {p: list(seats[p]['discards']) for p in range(4)}
    haipai = {p: list(seats[p]['haipai']) for p in range(4)}
    riichi_info = {p: seats[p].get('riichi_info', {}) for p in range(4)}

    # dealer: split 14-tile haipai into 13 tehai + a synthesized first draw
    first_draw = {p: None for p in range(4)}
    tehais = {}
    if len(haipai[oya]) == 14:
        d0 = discs[oya][0] if discs[oya] else None
        d0_tile = d0.lstrip('rtak') if d0 else None
        # pick a drawn tile != first discard (unless first discard is tsumogiri)
        drawn = None
        if d0 and d0.startswith('r') and d0_tile in haipai[oya]:
            drawn = d0_tile                      # tsumogiri first discard: drawn == discarded
        else:
            for cand in reversed(haipai[oya]):   # prefer a tile that isn't the (tedashi) first discard
                if cand != d0_tile:
                    drawn = cand
                    break
            if drawn is None:
                drawn = haipai[oya][-1]
        h = list(haipai[oya])
        h.remove(drawn)
        tehais[oya] = h
        first_draw[oya] = drawn
    elif len(haipai[oya]) == 13:
        tehais[oya] = list(haipai[oya])
    else:
        raise ConversionError(f'dealer haipai has {len(haipai[oya])} tiles (expected 13/14)')
    for p in range(4):
        if p == oya:
            continue
        if len(haipai[p]) != 13:
            raise ConversionError(f'seat {p} haipai has {len(haipai[p])} tiles (expected 13)')
        tehais[p] = list(haipai[p])

    events = []
    events.append({
        'type': 'start_kyoku',
        'bakaze': BAKAZE[kyoku // 4],
        'dora_marker': dora[0],
        'kyoku': kyoku % 4 + 1,
        'honba': honba,
        'kyotaku': sticks,
        'oya': oya,
        'scores': list(start),
        'tehais': [[to_mjai_tile(t) for t in tehais[p]] for p in range(4)],
    })

    dora_ptr = 1                 # next kan-dora index into `dora`
    last_drawn = {p: None for p in range(4)}   # mjai tile each seat most recently drew
    pon_meld = {p: {} for p in range(4)}       # base tile -> the 3 mjai tiles of an open pon
    last_discard = None          # (seat, mjai_tile)
    dp = {p: 0 for p in range(4)}  # discard pointer
    wp = {p: 0 for p in range(4)}  # draw/call pointer

    def reveal_kan_dora():
        nonlocal dora_ptr
        if dora_ptr < len(dora):
            events.append({'type': 'dora', 'dora_marker': dora[dora_ptr]})
            dora_ptr += 1

    def parse_call(s):
        typ = s[0]
        parts = s[1:].split(',')
        return typ, [to_mjai_tile(t) for t in parts]

    def do_discard_phase(p):
        """Emit p's discard, handling leading ankan/kakan (+rinshan draw) entries.
        Returns the mjai tile p discarded, or None if the round ended here (e.g. a
        rinshan-kaihou tsumo win off the kan-replacement draw, so no discard follows)."""
        nonlocal last_discard
        while dp[p] < len(discs[p]):
            entry = discs[p][dp[p]]
            if entry[0] in ('a', 'k') and entry[1:] in _KANNABLE:
                # Closed/added kan on the discard side. The a/k prefix in this export is
                # NOT reliable (seen 'a' on added-kans and 'k' on closed-kans), so decide
                # by whether p already ponned this tile (-> kakan) or not (-> ankan).
                tile = to_mjai_tile(entry[1:])
                base = deaka(tile)
                if base in ('5m', '5p', '5s'):
                    full = [base + 'r', base, base, base]   # the four physical tiles
                else:
                    full = [base, base, base, base]
                if base in pon_meld[p]:                      # KAKAN (added to a pon)
                    from collections import Counter
                    pon3 = pon_meld[p].pop(base)
                    diff = list((Counter(full) - Counter(pon3)).elements())
                    pai = diff[0] if diff else base
                    events.append({'type': 'kakan', 'actor': p, 'pai': pai,
                                   'consumed': list(pon3)})
                else:                                        # ANKAN (closed)
                    events.append({'type': 'ankan', 'actor': p, 'consumed': full})
                dp[p] += 1
                reveal_kan_dora()
                rin = to_mjai_tile(draws[p][wp[p]]); wp[p] += 1
                last_drawn[p] = rin
                events.append({'type': 'tsumo', 'actor': p, 'pai': rin})
                if dp[p] >= len(discs[p]):
                    return None  # rinshan kaihou (won on kan replacement)
                continue
            # normal / tsumogiri / riichi discard
            if entry.startswith('t'):   # riichi declaration tile
                tile = to_mjai_tile(entry[1:])
                events.append({'type': 'reach', 'actor': p})
                tsumogiri = (tile == last_drawn[p])
                events.append({'type': 'dahai', 'actor': p, 'pai': tile, 'tsumogiri': tsumogiri})
                dp[p] += 1
                last_discard = (p, tile)
                # accept the riichi unless it was nullified (ronned on the tile)
                if riichi_info[p].get('established'):
                    events.append({'type': 'reach_accepted', 'actor': p})
                return tile
            tsumogiri = entry.startswith('r')
            tile = to_mjai_tile(entry.lstrip('r'))
            events.append({'type': 'dahai', 'actor': p, 'pai': tile, 'tsumogiri': tsumogiri})
            dp[p] += 1
            last_discard = (p, tile)
            return tile
        return None  # no discard left -> round ended (terminal tsumo)

    def pick_called(mjai_tiles, discard_tile):
        """Called tile = the one matching the discard (this variant does NOT always
        list it first — e.g. red-five chi 'c4s,6s,0s' on a 5sr discard). The remaining
        tiles are the caller's own consumed tiles."""
        if discard_tile in mjai_tiles:
            idx = mjai_tiles.index(discard_tile)
        else:
            d = deaka(discard_tile)
            idx = next((i for i, t in enumerate(mjai_tiles) if deaka(t) == d), None)
            if idx is None:
                return None, None
        return mjai_tiles[idx], mjai_tiles[:idx] + mjai_tiles[idx + 1:]

    def find_caller(discarder, tile):
        base = deaka(tile)
        cands = []
        for s in range(4):
            if s == discarder:
                continue
            if wp[s] >= len(draws[s]):
                continue
            nxt = draws[s][wp[s]]
            if ',' not in str(nxt):
                continue
            typ, tiles = parse_call(nxt)
            if not any(deaka(t) == base for t in tiles):
                continue
            if typ == 'c' and s != (discarder + 1) % 4:
                continue  # chi only from kamicha (discarder must be caller's kamicha)
            cands.append((s, typ, tiles))
        if not cands:
            return None
        # call priority: daiminkan/pon outrank chi (multiple claimants on the same tile)
        prio = {'m': 0, 'p': 1, 'c': 2}
        cands.sort(key=lambda c: prio.get(c[1], 3))
        return cands[0]

    def do_call(s, discarder, typ, tiles):
        """Emit the call event (+rinshan for minkan) then s's discard."""
        disc_tile = last_discard[1]
        pai, consumed = pick_called(tiles, disc_tile)
        if pai is None:
            raise ConversionError(f'call {tiles} does not contain discard {disc_tile}')
        if typ == 'c':
            events.append({'type': 'chi', 'actor': s, 'target': discarder,
                           'pai': pai, 'consumed': consumed})
            wp[s] += 1
        elif typ == 'p':
            events.append({'type': 'pon', 'actor': s, 'target': discarder,
                           'pai': pai, 'consumed': consumed})
            pon_meld[s][deaka(pai)] = [pai] + consumed   # for a later kakan
            wp[s] += 1
        elif typ == 'm':
            events.append({'type': 'daiminkan', 'actor': s, 'target': discarder,
                           'pai': pai, 'consumed': consumed})
            wp[s] += 1
            reveal_kan_dora()
            rin = to_mjai_tile(draws[s][wp[s]]); wp[s] += 1
            last_drawn[s] = rin
            events.append({'type': 'tsumo', 'actor': s, 'pai': rin})
            if dp[s] >= len(discs[s]):
                return None  # rinshan kaihou off a daiminkan replacement
        else:
            raise ConversionError(f'unknown call type {typ}')
        return do_discard_phase(s)

    # generic call-chain resolver
    TERMINAL = object()

    def resolve_reactions(discarder, tile):
        """After `discarder` discards `tile`, chain any calls; return the seat whose
        discard was NOT called (normal turn advances from it), or TERMINAL if a called
        turn ended the round (rinshan kaihou)."""
        while True:
            hit = find_caller(discarder, tile)
            if hit is None:
                return discarder
            s, typ, tiles = hit
            tile = do_call(s, discarder, typ, tiles)
            if tile is None:
                return TERMINAL
            discarder = s

    # ---- main turn simulation ----
    terminated = False
    # dealer's first turn: synthesized draw (if 14-tile haipai) then discard
    turn = oya
    if first_draw[oya] is not None:
        fd = to_mjai_tile(first_draw[oya])
        last_drawn[oya] = fd
        events.append({'type': 'tsumo', 'actor': oya, 'pai': fd})
        discarded = do_discard_phase(oya)
        if discarded is None:
            terminated = True
        else:
            last = resolve_reactions(oya, discarded)
            if last is TERMINAL:
                terminated = True
            else:
                turn = (last + 1) % 4

    guard = 0
    while not terminated:
        guard += 1
        if guard > 400:
            raise ConversionError('turn simulation exceeded guard (likely order bug)')
        p = turn
        # p out of draws -> the round already ended (win/exhaustion on the last discard)
        if wp[p] >= len(draws[p]):
            break
        nxt = draws[p][wp[p]]
        if ',' in str(nxt):
            import os
            if os.environ.get('CONV_DEBUG'):
                print('--- STUCK: seat', p, 'pending call', nxt, file=sys.stderr)
                print('last_discard', last_discard, file=sys.stderr)
                for q in range(4):
                    front = draws[q][wp[q]] if wp[q] < len(draws[q]) else '(none)'
                    print(f'  seat{q} wp={wp[q]}/{len(draws[q])} front={front!r} dp={dp[q]}/{len(discs[q])}', file=sys.stderr)
                print('last 14 events:', file=sys.stderr)
                for e in events[-14:]:
                    print('   ', json.dumps(e, ensure_ascii=False), file=sys.stderr)
            raise ConversionError(
                f'seat {p} has a pending call at natural turn — order reconstruction gap')
        draw = to_mjai_tile(nxt); wp[p] += 1
        last_drawn[p] = draw
        events.append({'type': 'tsumo', 'actor': p, 'pai': draw})
        if dp[p] >= len(discs[p]):
            break  # no discard follows this draw -> terminal tsumo (win / rinshan kaihou)
        discarded = do_discard_phase(p)
        if discarded is None:
            break
        last = resolve_reactions(p, discarded)
        if last is TERMINAL:
            break
        turn = (last + 1) % 4

    # terminal event(s)
    res = rd['result']
    kind = rd['kind']
    # Terminal events. `deltas`/scores in the source are a 12-element MJS structure, not
    # mjai's [i32;4]; they are optional and irrelevant to the pre-terminal decisions we
    # score, so we omit them (Mortal's Bot only needs the terminal to close the kyoku).
    if kind == 'agari' and isinstance(res, dict) and res.get('agari'):
        for a in res['agari']:
            events.append({'type': 'hora', 'actor': a['who'], 'target': a['fromWho']})
    elif kind in ('exhaustive', 'abortive'):
        events.append({'type': 'ryukyoku'})
    events.append({'type': 'end_kyoku'})
    return events


def convert_game(doc, strict=False):
    """Return (names, list_of_mjai_event_dicts, skipped) for a whole game.

    Each kyoku is independent for the bot (start_kyoku resets per-hand state), so a round
    whose source data can't be cleanly reconstructed (e.g. a malformed abortive round) is
    SKIPPED with a warning rather than risk emitting a wrong event order. `skipped` lists
    (round_index, reason). Pass strict=True to raise instead of skipping."""
    names, rounds = mjsoul_decode.decode(doc)
    events = [{'type': 'start_game', 'names': list(names)}]
    skipped = []
    for i, rd in enumerate(rounds):
        try:
            events.extend(convert_round(rd))
        except ConversionError as e:
            if strict:
                raise
            skipped.append((i, str(e)))
            print(f'[mjsoul_to_mjai] WARNING: skipping round {i} '
                  f'({mjsoul_decode.round_label(rd["kyoku"], rd["honba"])}): {e}',
                  file=sys.stderr)
    events.append({'type': 'end_game'})
    return names, events, skipped


def main():
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(1)
    doc = json.load(open(sys.argv[1]))
    names, events, skipped = convert_game(doc)
    for ev in events:
        print(json.dumps(ev, ensure_ascii=False))


if __name__ == '__main__':
    main()
