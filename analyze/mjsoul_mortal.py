#!/usr/bin/env python3
"""
mjsoul_mortal.py — value-aware discard review of your Mahjong Soul games using Mortal.

This is the policy-only, value-aware replacement for mjsoul_analyze's "live-acceptance-
optimal" column. For each of YOUR decisions it asks the Mortal policy net (the public
hanchan `mortal-298k` checkpoint) what it would do, and how much probability mass it puts
on the action you actually took, then aggregates you-vs-field exactly like mjsoul_analyze
aggregates ukeire-optimality.

    POLICY-ONLY BY DESIGN. Mortal is a value/Q model; we drive only its policy (argmax over
    Q, with a softmax over the masked Q-values used as the "probability mass"). The GRP /
    placement-value model is NOT used and NOT required — no placement-EV is computed.

    CAVEAT (shown in every report): mortal-298k is a HANCHAN (南) model run on your TONPUU
    (East-only) games. Its tile-level discard/safety/shape reads transfer and are trustworthy;
    anything placement/endgame is out of scope here by design.

Run it with the arm64 venv that has torch + the built libriichi (from the CPU dry-run):
    ~/mortal-dryrun/venv/bin/python mjsoul_mortal.py logs/<game>.json [more.json ...]
    ~/mortal-dryrun/venv/bin/python mjsoul_mortal.py --json logs/<game>.json

Paths to the Mortal tree / checkpoint can be overridden with env vars MORTAL_DIR and
MORTAL_CHECKPOINT (defaults point at ~/mortal-dryrun).
"""
import sys, os, json, math, argparse, hashlib, datetime
from os import path

# Bump when this module's record semantics change (fields, keying, prob definition). Recorded
# in each sidecar's provenance so a stale/incompatible pack can be detected.
MORTAL_MODULE_VERSION = 1
SIDECAR_SCHEMA_VERSION = 1
MODEL_ID = 'mortal-298k'

HERE = path.dirname(path.abspath(__file__))
sys.path.insert(0, HERE)
MORTAL_DIR = os.environ.get('MORTAL_DIR', path.expanduser('~/mortal-dryrun/Mortal/mortal'))
CHECKPOINT = os.environ.get('MORTAL_CHECKPOINT', path.expanduser('~/mortal-dryrun/models/mortal_298k.pth'))
sys.path.insert(0, MORTAL_DIR)

import mjsoul_decode
import mjsoul_to_mjai as conv
from mjsoul_to_mjai import TILE_TO_IDX, deaka

CAVEAT = ('NOTE: mortal-298k is a HANCHAN model scored on TONPUU (East-only) games — '
          'tile/discard/safety reads transfer; placement/endgame is out of scope (policy-only, no GRP).')

REACTION_TYPES = {'dahai', 'reach', 'pon', 'chi', 'daiminkan', 'ankan', 'kakan', 'hora', 'ryukyoku'}
KAN_TYPES = {'daiminkan', 'ankan', 'kakan'}


def _tile_num(mjai_tile):
    t = deaka(mjai_tile)
    return int(t[0]) if t and t[0].isdigit() else None


def chi_index(pai, consumed):
    """chi low/mid/high -> 38/39/40 by the position of the called tile in the run."""
    nums = sorted(_tile_num(t) for t in [pai] + list(consumed))
    pos = nums.index(_tile_num(pai))
    return 38 + pos


def event_to_action_index(ev):
    """Map an mjai reaction event to Mortal's action-space index (0..45), or None."""
    t = ev.get('type')
    if t == 'dahai':
        return TILE_TO_IDX.get(ev['pai'])
    if t == 'reach':
        return 37
    if t == 'chi':
        return chi_index(ev['pai'], ev['consumed'])
    if t == 'pon':
        return 41
    if t in KAN_TYPES:
        return 42
    if t == 'hora':
        return 43
    if t == 'ryukyoku':
        return 44
    if t == 'none':
        return 45
    return None


def action_label(ev):
    """Human label for a reaction event."""
    t = ev.get('type')
    if t == 'dahai':
        return f"discard {ev['pai']}" + (' (tsumogiri)' if ev.get('tsumogiri') else '')
    if t == 'reach':
        return 'riichi'
    if t == 'chi':
        return f"chi {ev['pai']}({'/'.join(ev['consumed'])})"
    if t == 'pon':
        return f"pon {ev['pai']}"
    if t in KAN_TYPES:
        return f"kan {ev.get('pai', ev.get('consumed', ['?'])[0])}"
    if t == 'hora':
        return 'agari'
    if t == 'ryukyoku':
        return 'ryukyoku'
    if t == 'none':
        return 'pass'
    return t


def _softmax(xs):
    m = max(xs)
    exps = [math.exp(x - m) for x in xs]
    s = sum(exps)
    return [e / s for e in exps]


_ENGINE = None


def build_engine():
    global _ENGINE
    if _ENGINE is not None:
        return _ENGINE
    import torch
    from model import Brain, DQN
    from engine import MortalEngine
    device = torch.device(os.environ.get('MORTAL_DEVICE', 'cpu'))
    state = torch.load(CHECKPOINT, weights_only=True, map_location=device)
    cfg = state['config']
    v = cfg['control']['version']
    mortal = Brain(version=v, num_blocks=cfg['resnet']['num_blocks'],
                   conv_channels=cfg['resnet']['conv_channels']).eval()
    dqn = DQN(version=v).eval()
    mortal.load_state_dict(state['mortal'])       # strict
    dqn.load_state_dict(state['current_dqn'])     # strict
    _ENGINE = MortalEngine(mortal, dqn, is_oracle=False, version=v, device=device,
                           enable_amp=False, enable_quick_eval=False,
                           enable_rule_based_agari_guard=True, name='mortal')
    _ENGINE._device_str = str(device)
    return _ENGINE


def infer_actual(events, i, seat):
    """The action `seat` actually took at the decision triggered by events[i].
    A reaction always appears immediately after its trigger; if the next event isn't
    ours, we passed."""
    j = i + 1
    if j < len(events):
        nxt = events[j]
        if nxt.get('actor') == seat and nxt.get('type') in REACTION_TYPES:
            return nxt
    return {'type': 'none'}


def analyze_seat(events, seat, engine):
    """Return per-decision records for `seat` over one converted game."""
    import torch  # noqa: ensure torch present
    from libriichi.mjai import Bot
    bot = Bot(engine, seat)
    records = []
    kyoku_label = None
    junme = 0
    for i, ev in enumerate(events):
        if ev['type'] == 'start_kyoku':
            kyoku_label = mjsoul_decode.round_label(
                (ev['kyoku'] - 1) + 4 * 'ESWN'.index(ev['bakaze']), ev['honba'])
            junme = 0
        r = bot.react(json.dumps(ev), can_act=True)
        if r is None:
            continue
        R = json.loads(r)
        meta = R.get('meta', {}) or {}
        actual = infer_actual(events, i, seat)
        my_idx = event_to_action_index(actual)
        bot_idx = event_to_action_index(R)

        qs = meta.get('q_values') or []
        mb = meta.get('mask_bits') or 0
        legal = [b for b in range(46) if (mb >> b) & 1]
        my_prob = None
        if qs and len(qs) == len(legal):
            probs = _softmax(qs)
            pmap = dict(zip(legal, probs))
            my_prob = pmap.get(my_idx, 0.0)

        # decision kind for grouping
        if actual['type'] in ('dahai', 'reach'):
            junme += 1
            kind = 'discard'
        elif actual['type'] in ('pon', 'chi') or (actual['type'] == 'none' and 45 in legal):
            kind = 'call'
        elif actual['type'] in KAN_TYPES:
            kind = 'kan'
        elif actual['type'] == 'hora':
            kind = 'agari'
        else:
            kind = 'other'

        records.append({
            'kyoku': kyoku_label,
            'junme': junme,
            'kind': kind,
            'my_action': action_label(actual),
            'my_idx': my_idx,
            'mortal_action': action_label(R),
            'mortal_idx': bot_idx,
            'matched': (my_idx == bot_idx),
            'prob_of_my_action': my_prob,
            'mortal_is_greedy': meta.get('is_greedy'),
            'shanten': meta.get('shanten'),
        })
    return records


def dealin_tiles_by_kyoku(doc, seat):
    """Map kyoku_label -> winning tile you dealt into (ron off you), for cheap join."""
    _, rounds = mjsoul_decode.decode(doc)
    out = {}
    for rd in rounds:
        res = rd.get('result')
        if rd['kind'] == 'agari' and isinstance(res, dict):
            for a in res.get('agari', []):
                if a.get('fromWho') == seat and not a.get('tsumo'):
                    lbl = mjsoul_decode.round_label(rd['kyoku'], rd['honba'])
                    out.setdefault(lbl, []).append(conv.to_mjai_tile(a['machi']))
    return out


def analyze_game(doc, engine):
    names, events, skipped = conv.convert_game(doc)
    you = next((i for i, n in enumerate(names) if '(you)' in n), None)
    per_seat = {s: analyze_seat(events, s, engine) for s in range(4)}
    return names, you, per_seat, skipped


# ---------------------------------------------------------------------------
# Sidecar: precomputed Mortal results so a pack is self-contained without weights.
#
# The sidecar-LOAD path below imports NOTHING heavy (no torch, no libriichi) — that is the
# whole point: a recipient with only the raw logs + sidecars + these .py files can produce
# the full report. Heavy imports live only in build_engine()/analyze_seat(), reached solely
# on the inference (pack-build) path.
# ---------------------------------------------------------------------------

def _file_sha256(p):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 16), b''):
            h.update(chunk)
    return h.hexdigest()


_WEIGHTS_SHA = None


def weights_sha256():
    """sha256 of the .pth actually used (inference/pack-build path only)."""
    global _WEIGHTS_SHA
    if _WEIGHTS_SHA is None:
        _WEIGHTS_SHA = _file_sha256(CHECKPOINT)
    return _WEIGHTS_SHA


def sidecar_path_for(log_path):
    base = log_path[:-5] if log_path.endswith('.json') else log_path
    return base + '.mortal.json'


def game_uuid(doc):
    t = doc.get('title') or []
    return t[2] if len(t) > 2 else None


def _decision_id(seat, kyoku, turn, occ):
    """Stable id derived from existing record data. Base is (seat, kyoku_label, turn); `turn`
    (=junme) counts discards, so a discard and the same-go-around call/pass decisions share it,
    hence the `#occ` chronological tiebreak. Deterministic given deterministic inference."""
    return f"{seat}:{kyoku}:{turn}#{occ}"


# only the fields the suite report actually consumes (+ id/seat/turn for keying).
def _records_flat(per_seat):
    flat = []
    for s in range(4):
        seen = {}
        for r in per_seat[s]:
            base = (s, r['kyoku'], r['junme'])
            occ = seen.get(base, 0)
            seen[base] = occ + 1
            flat.append({
                'decision_id': _decision_id(s, r['kyoku'], r['junme'], occ),
                'seat': s,
                'kyoku': r['kyoku'],
                'turn': r['junme'],
                'kind': r['kind'],
                'my_action': r['my_action'],
                'mortal_action': r['mortal_action'],       # Mortal's recommendation
                'matched': r['matched'],
                # policy mass on the action you took (rounded — determinism + size)
                'prob_of_my_action': (None if r['prob_of_my_action'] is None
                                      else round(r['prob_of_my_action'], 6)),
                'shanten': r['shanten'],
            })
    return flat


def make_sidecar(doc, log_path, names, you, per_seat, skipped, device, weights_sha):
    return {
        'schema_version': SIDECAR_SCHEMA_VERSION,
        'uuid': game_uuid(doc),
        'provenance': {
            'model_id': MODEL_ID,
            'weights_sha256': weights_sha,
            'converter_version': conv.CONVERTER_VERSION,
            'mortal_module_version': MORTAL_MODULE_VERSION,
            'generated_utc': datetime.datetime.now(datetime.timezone.utc)
                                     .isoformat(timespec='seconds'),
            'device_used': device,
            'source_log': {'uuid': game_uuid(doc), 'sha256': _file_sha256(log_path)},
        },
        'names': names,
        'you': you,
        'skipped': skipped,
        'records': _records_flat(per_seat),
    }


def write_sidecar(log_path, sidecar):
    p = sidecar_path_for(log_path)
    with open(p, 'w', encoding='utf-8') as f:
        # compact + sorted keys: deterministic bytes for a given (log, weights, code).
        json.dump(sidecar, f, ensure_ascii=False, separators=(',', ':'), sort_keys=True)
        f.write('\n')
    return p


def load_sidecar(log_path):
    p = sidecar_path_for(log_path)
    if not os.path.exists(p):
        return None
    with open(p, encoding='utf-8') as f:
        return json.load(f)


class SidecarError(Exception):
    pass


def sidecar_status(sidecar, doc, log_path):
    """Return (ok: bool, reason: str). Not ok => STALE/invalid; caller decides regenerate vs fail."""
    prov = sidecar.get('provenance', {}) or {}
    src = prov.get('source_log', {}) or {}
    uid = game_uuid(doc)
    if src.get('uuid') != uid:
        return False, f"uuid mismatch: sidecar={src.get('uuid')!r} log={uid!r}"
    cur_sha = _file_sha256(log_path)
    if src.get('sha256') != cur_sha:
        return False, ("raw log changed since sidecar was generated "
                       f"(sha256 {src.get('sha256','?')[:12]}… != {cur_sha[:12]}…)")
    if sidecar.get('schema_version') != SIDECAR_SCHEMA_VERSION:
        return False, (f"schema_version {sidecar.get('schema_version')} != "
                       f"expected {SIDECAR_SCHEMA_VERSION}")
    return True, 'ok'


def sidecar_current(sidecar, doc, log_path, weights_sha):
    """Stronger check used at pack-build time: valid AND generated by the current
    converter/module/weights. If True, no regeneration is needed (idempotency)."""
    ok, _ = sidecar_status(sidecar, doc, log_path)
    if not ok:
        return False
    prov = sidecar.get('provenance', {}) or {}
    return (prov.get('converter_version') == conv.CONVERTER_VERSION
            and prov.get('mortal_module_version') == MORTAL_MODULE_VERSION
            and prov.get('weights_sha256') == weights_sha)


def sidecar_to_per_seat(sidecar):
    """Reconstruct per_seat lists (with the field names the report expects) from a sidecar.
    Pure Python — no heavy deps. Loud on duplicate decision ids rather than silently dropping."""
    per_seat = {0: [], 1: [], 2: [], 3: []}
    seen = set()
    for r in sidecar.get('records', []):
        did = r.get('decision_id')
        if did in seen:
            raise SidecarError(f"duplicate decision_id in sidecar: {did!r} "
                               "(corrupt/ambiguous — refusing to serve)")
        seen.add(did)
        s = r['seat']
        per_seat[s].append({
            'kyoku': r['kyoku'], 'junme': r['turn'], 'kind': r['kind'],
            'my_action': r['my_action'], 'mortal_action': r['mortal_action'],
            'matched': r['matched'], 'prob_of_my_action': r['prob_of_my_action'],
            'shanten': r['shanten'], 'decision_id': did,
        })
    return per_seat


def ensure_sidecar(log_path, refresh=False, engine=None):
    """Pack-build helper (needs the model). Generate a sidecar if missing/stale/outdated;
    otherwise leave it untouched (idempotent). Returns (sidecar_path, action) where action is
    'written' or 'up-to-date'."""
    doc = json.load(open(log_path, encoding='utf-8'))
    wsha = weights_sha256()
    if not refresh:
        sc = load_sidecar(log_path)
        if sc is not None and sidecar_current(sc, doc, log_path, wsha):
            return sidecar_path_for(log_path), 'up-to-date'
    if engine is None:
        engine = build_engine()
    names, events, skipped = conv.convert_game(doc)
    you = next((i for i, n in enumerate(names) if '(you)' in n), None)
    per_seat = {s: analyze_seat(events, s, engine) for s in range(4)}
    device = getattr(engine, '_device_str', os.environ.get('MORTAL_DEVICE', 'cpu'))
    sc = make_sidecar(doc, log_path, names, you, per_seat, skipped, device, wsha)
    return write_sidecar(log_path, sc), 'written'


def get_analysis(log_path, allow_inference=True, write=False, refresh=False, engine=None):
    """Return (doc, names, you, per_seat, skipped, source, provenance).

    Prefers a valid sidecar (ZERO heavy deps). If missing/stale: run inference when
    allow_inference else FAIL LOUD (no-model mode must never serve wrong or guessed data)."""
    doc = json.load(open(log_path, encoding='utf-8'))
    sc = None if refresh else load_sidecar(log_path)
    if sc is not None:
        ok, reason = sidecar_status(sc, doc, log_path)
        if ok:
            per_seat = sidecar_to_per_seat(sc)
            return (doc, sc.get('names'), sc.get('you'), per_seat,
                    sc.get('skipped', []), 'sidecar', sc.get('provenance', {}))
        if not allow_inference:
            raise SidecarError(
                f"STALE sidecar for {os.path.basename(log_path)}: {reason}. "
                f"Refusing to serve wrong data in no-model mode — rebuild the pack with the model.")
        print(f"[mjsoul_mortal] sidecar stale ({reason}); regenerating via inference",
              file=sys.stderr)
    elif not allow_inference:
        raise SidecarError(
            f"No sidecar for {os.path.basename(log_path)} and no model available (no-model mode). "
            f"Expected {os.path.basename(sidecar_path_for(log_path))}.")
    # inference path (heavy)
    if engine is None:
        engine = build_engine()
    names, events, skipped = conv.convert_game(doc)
    you = next((i for i, n in enumerate(names) if '(you)' in n), None)
    per_seat = {s: analyze_seat(events, s, engine) for s in range(4)}
    if write:
        device = getattr(engine, '_device_str', os.environ.get('MORTAL_DEVICE', 'cpu'))
        write_sidecar(log_path, make_sidecar(doc, log_path, names, you, per_seat, skipped,
                                             device, weights_sha256()))
    return doc, names, you, per_seat, skipped, 'inference', None


def seat_summary(records):
    disc = [r for r in records if r['kind'] == 'discard']
    calls = [r for r in records if r['kind'] == 'call']
    def agg(rows):
        n = len(rows)
        if n == 0:
            return {'n': 0, 'match_rate': None, 'avg_mass': None}
        matched = sum(1 for r in rows if r['matched'])
        masses = [r['prob_of_my_action'] for r in rows if r['prob_of_my_action'] is not None]
        return {'n': n, 'match_rate': matched / n,
                'avg_mass': (sum(masses) / len(masses)) if masses else None}
    return {'discard': agg(disc), 'call': agg(calls), 'all': agg(records)}


def print_report(doc, names, you, per_seat, skipped, source='inference', provenance=None):
    out = []
    out.append('=' * 78)
    out.append(f"Mortal policy review — {' vs '.join(names)}")
    disp = doc.get('rule', {}).get('disp', '?')
    dev = (provenance or {}).get('device_used') or os.environ.get('MORTAL_DEVICE', 'cpu')
    out.append(f"ruleset: {disp}   model: mortal-298k (hanchan, 192ch/40blk, policy-only)   device: {dev}")
    out.append(CAVEAT)
    if source == 'sidecar':
        p = provenance or {}
        wsha = (p.get('weights_sha256') or '')[:12]
        out.append(f"SIDECAR-BACKED: Mortal numbers are PRECOMPUTED and authoritative "
                   f"(weights {wsha}…, generated {p.get('generated_utc','?')}, "
                   f"conv v{p.get('converter_version','?')}/mod v{p.get('mortal_module_version','?')}). "
                   "Do NOT run Mortal; recompute everything else from the raw logs as usual.")
    if skipped:
        out.append(f"skipped {len(skipped)} unreconstructable round(s): "
                   + ', '.join(f'#{i}' for i, _ in skipped))
    out.append('=' * 78)

    if you is None:
        out.append("WARNING: no player marked '(you)' — showing all seats, no you-vs-field.")
    # you-vs-field table
    summ = {s: seat_summary(per_seat[s]) for s in range(4)}
    out.append('\nYOU-vs-FIELD (discard decisions):')
    out.append(f"  {'seat':<22} {'#dec':>5} {'agree%':>7} {'avg mass':>9}")
    for s in range(4):
        d = summ[s]['discard']
        tag = '  <- you' if s == you else ''
        mr = f"{d['match_rate']*100:5.1f}" if d['match_rate'] is not None else '   — '
        am = f"{d['avg_mass']*100:6.1f}%" if d['avg_mass'] is not None else '    — '
        out.append(f"  {names[s][:22]:<22} {d['n']:>5} {mr:>7} {am:>9}{tag}")
    if you is not None:
        field = [s for s in range(4) if s != you]
        fmr = [summ[s]['discard']['match_rate'] for s in field if summ[s]['discard']['match_rate'] is not None]
        fam = [summ[s]['discard']['avg_mass'] for s in field if summ[s]['discard']['avg_mass'] is not None]
        yd = summ[you]['discard']
        out.append('  ' + '-' * 44)
        out.append(f"  {'FIELD avg (other 3)':<22} {'':>5} "
                   f"{(sum(fmr)/len(fmr)*100 if fmr else 0):6.1f} {(sum(fam)/len(fam)*100 if fam else 0):8.1f}%")
        out.append(f"  {'YOU':<22} {yd['n']:>5} "
                   f"{(yd['match_rate']*100 if yd['match_rate'] is not None else 0):6.1f} "
                   f"{(yd['avg_mass']*100 if yd['avg_mass'] is not None else 0):8.1f}%")

    # biggest disagreements for you: discards where Mortal put least mass on your choice
    if you is not None:
        dtiles = dealin_tiles_by_kyoku(doc, you)
        disc = [r for r in per_seat[you] if r['kind'] in ('discard', 'call')
                and r['prob_of_my_action'] is not None]
        disc.sort(key=lambda r: r['prob_of_my_action'])
        out.append('\nYOUR BIGGEST DISAGREEMENTS (low policy mass on your action):')
        out.append(f"  {'kyoku':<10} {'jun':>3}  {'your action':<22} {'Mortal':<22} {'mass':>6}  {'shan':>4}")
        for r in disc[:12]:
            di = ''
            if r['kyoku'] in dtiles and r['my_action'].startswith('discard'):
                dtile = r['my_action'].split()[1]
                if dtile in dtiles[r['kyoku']]:
                    di = '  <DEAL-IN'
            mark = '' if r['matched'] else ' *'
            out.append(f"  {r['kyoku']:<10} {r['junme']:>3}  {r['my_action'][:22]:<22} "
                       f"{r['mortal_action'][:22]:<22} {r['prob_of_my_action']*100:5.1f}%  "
                       f"{str(r['shanten']):>4}{mark}{di}")
        # deal-in cluster note
        if dtiles:
            din = [r for r in disc if r['kyoku'] in dtiles and r['my_action'].startswith('discard')
                   and r['my_action'].split()[1] in dtiles[r['kyoku']]]
            if din:
                masses = [r['prob_of_my_action'] for r in din]
                out.append(f"\n  deal-in discards found: {len(din)}; Mortal's avg mass on them "
                           f"{sum(masses)/len(masses)*100:.1f}% (low mass = Mortal disagreed with the push).")
    out.append('')
    return '\n'.join(out)


def main():
    ap = argparse.ArgumentParser(description='Mortal policy-only discard review')
    ap.add_argument('logs', nargs='+')
    ap.add_argument('--json', action='store_true', help='machine-readable output')
    ap.add_argument('--no-model', action='store_true',
                    help='sidecar-only: never import torch/libriichi; fail loud if a sidecar '
                         'is missing or stale (the browser / model-free path)')
    ap.add_argument('--write-sidecar', action='store_true',
                    help='after inference, write <log>.mortal.json next to the log')
    ap.add_argument('--refresh', action='store_true',
                    help='ignore any existing sidecar and re-run inference')
    args = ap.parse_args()

    allow_inference = not args.no_model
    results = []
    for p in args.logs:
        try:
            # build_engine() returns a cached singleton, so inference reuses one engine.
            doc, names, you, per_seat, skipped, source, prov = get_analysis(
                p, allow_inference=allow_inference, write=args.write_sidecar,
                refresh=args.refresh)
        except SidecarError as e:
            print(f"ERROR [{os.path.basename(p)}]: {e}", file=sys.stderr)
            sys.exit(2)
        if args.json:
            results.append({
                'file': p, 'names': names, 'you': you, 'source': source,
                'provenance': prov, 'skipped': skipped,
                'summary': {s: seat_summary(per_seat[s]) for s in range(4)},
                'you_records': per_seat[you] if you is not None else None,
            })
        else:
            print(print_report(doc, names, you, per_seat, skipped, source, prov))
    if args.json:
        print(json.dumps({'caveat': CAVEAT, 'games': results}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
