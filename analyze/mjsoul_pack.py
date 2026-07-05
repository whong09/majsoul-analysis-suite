#!/usr/bin/env python3
"""
mjsoul_pack.py — build a small, self-contained "shareable pack" of mahjong games whose Mortal
numbers are PRECOMPUTED into sidecars, so the pack runs the full suite on a machine with no model.

At build time (this machine, which HAS the local Mortal weights) it:
  1. ensures an up-to-date `<log>.mortal.json` sidecar exists for each log (generating via
     mjsoul_mortal inference only when missing/stale — idempotent otherwise),
  2. assembles a pack containing ONLY the raw logs, their sidecars, the pure-Python suite
     modules, and mahjong_analysis_instructions.md,
  3. writes MANIFEST.json + README.md, and
  4. asserts nothing large or private leaks in (no weights/.pth, nothing over the size cap).

The recipient (e.g. a browser Claude with no model) runs the suite normally; mjsoul_mortal in
`--no-model` mode reads Mortal's numbers from the sidecars and imports no heavy deps. The model,
weights, and Mortal repo never travel.

Usage:
    ~/mortal-dryrun/venv/bin/python mjsoul_pack.py --out pack_out logs/a.json logs/b.json
    ~/mortal-dryrun/venv/bin/python mjsoul_pack.py --out pack_out --zip --glob 'logs/2026-07-04_*.json'
"""
import sys, os, json, glob as globmod, shutil, hashlib, argparse, datetime, zipfile
from os import path

HERE = path.dirname(path.abspath(__file__))
sys.path.insert(0, HERE)
import mjsoul_mortal as mm

PACK_VERSION = 1
MAX_FILE_BYTES = 5 * 1024 * 1024          # 5 MB hard cap per file (safety)
# suite modules the pack ships. mjsoul_to_mjai is a REQUIRED import dependency of mjsoul_mortal
# (tile mapping + record reconstruction) — the task's module list omitted it, but the no-model
# load path won't import without it.
MODULES = [
    'mjsoul_decode.py', 'mjsoul_turns.py', 'mjsoul_analyze.py', 'mjsoul_luck.py',
    'mjsoul_value.py', 'mjsoul_mortal.py', 'mjsoul_to_mjai.py',
]
EXTRA_FILES = ['mahjong_analysis_instructions.md']
SIDECAR_NOTE = ("This pack is SIDECAR-BACKED: Mortal's per-decision numbers are PRECOMPUTED and "
                "authoritative. Do NOT try to run Mortal (no weights ship here). Everything else "
                "(decode/turns/analyze/luck/value) recomputes from the raw logs as usual.")


def _sha256(p):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 16), b''):
            h.update(chunk)
    return h.hexdigest()


def resolve_logs(args):
    logs = list(args.logs)
    for g in (args.glob or []):
        logs.extend(sorted(globmod.glob(g)))
    # de-dup, keep order, only real .json logs (exclude sidecars)
    seen, out = set(), []
    for p in logs:
        ap = path.abspath(p)
        if ap in seen:
            continue
        seen.add(ap)
        if p.endswith('.mortal.json'):
            continue
        if not p.endswith('.json'):
            raise SystemExit(f"not a .json log: {p}")
        if not path.exists(p):
            raise SystemExit(f"log not found: {p}")
        out.append(p)
    if not out:
        raise SystemExit("no logs to pack")
    return out


def build(args):
    logs = resolve_logs(args)
    out_dir = args.out
    if path.exists(out_dir):
        if not args.force:
            raise SystemExit(f"output {out_dir} exists; pass --force to overwrite")
        shutil.rmtree(out_dir)
    os.makedirs(path.join(out_dir, 'logs'))

    # 1. ensure sidecars (generates via inference only when missing/stale)
    game_prov = []
    for lp in logs:
        scp, action = mm.ensure_sidecar(lp, refresh=args.refresh)
        doc = json.load(open(lp, encoding='utf-8'))
        sc = json.load(open(scp, encoding='utf-8'))
        prov = sc.get('provenance', {})
        print(f"  sidecar {action:11s} {path.basename(scp)}  ({len(sc.get('records',[]))} records)",
              file=sys.stderr)
        # 2. copy raw log + sidecar into pack/logs
        for src in (lp, scp):
            dst = path.join(out_dir, 'logs', path.basename(src))
            shutil.copy2(src, dst)
        game_prov.append({
            'uuid': sc.get('uuid'),
            'log': 'logs/' + path.basename(lp),
            'sidecar': 'logs/' + path.basename(scp),
            'you': sc.get('you'),
            'names': sc.get('names'),
            'records': len(sc.get('records', [])),
            'log_sha256': prov.get('source_log', {}).get('sha256'),
            'provenance': prov,
        })

    # 3. copy suite modules + instructions
    for m in MODULES + EXTRA_FILES:
        src = path.join(HERE, m)
        if not path.exists(src):
            raise SystemExit(f"required suite file missing: {m}")
        shutil.copy2(src, path.join(out_dir, path.basename(m)))

    # cross-check all sidecars agree on the weights (one model built this pack)
    wsets = {g['provenance'].get('weights_sha256') for g in game_prov}
    if len(wsets) != 1:
        raise SystemExit(f"STOP: sidecars disagree on weights_sha256 {wsets} — mixed models, "
                         "refusing to ship an inconsistent pack.")
    weights_sha = next(iter(wsets))

    # 4. SAFETY sweep: nothing large or private
    contents = []
    for root, _, files in os.walk(out_dir):
        for fn in files:
            fp = path.join(root, fn)
            rel = path.relpath(fp, out_dir)
            sz = path.getsize(fp)
            low = fn.lower()
            if low.endswith('.pth') or low.endswith('.pt') or low.endswith('.ckpt'):
                raise SystemExit(f"STOP: model weights file about to ship: {rel}")
            if 'mortal_298k' in low or low == 'weights' or 'checkpoint' in low:
                raise SystemExit(f"STOP: suspected weights/model artifact: {rel}")
            if sz > MAX_FILE_BYTES:
                raise SystemExit(f"STOP: {rel} is {sz} bytes (> {MAX_FILE_BYTES} cap) — refusing to bloat the pack")
            contents.append({'path': rel, 'bytes': sz, 'sha256': _sha256(fp)})
    contents.sort(key=lambda c: c['path'])

    # 5. MANIFEST + README
    manifest = {
        'pack_version': PACK_VERSION,
        'generated_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
        'sidecar_backed': True,
        'note': SIDECAR_NOTE,
        'model': {'model_id': mm.MODEL_ID, 'weights_sha256': weights_sha},
        'versions': {'converter_version': mm.conv.CONVERTER_VERSION,
                     'mortal_module_version': mm.MORTAL_MODULE_VERSION,
                     'sidecar_schema_version': mm.SIDECAR_SCHEMA_VERSION},
        'games': game_prov,
        'modules': [path.basename(m) for m in MODULES],
        'contents': contents,
    }
    with open(path.join(out_dir, 'MANIFEST.json'), 'w', encoding='utf-8') as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write('\n')

    total = sum(c['bytes'] for c in contents) + path.getsize(path.join(out_dir, 'MANIFEST.json'))
    readme = _readme(game_prov, weights_sha, total)
    with open(path.join(out_dir, 'README.md'), 'w', encoding='utf-8') as f:
        f.write(readme)
    total += path.getsize(path.join(out_dir, 'README.md'))

    zpath = None
    if args.zip:
        zpath = out_dir.rstrip('/') + '.zip'
        with zipfile.ZipFile(zpath, 'w', zipfile.ZIP_DEFLATED) as z:
            for root, _, files in os.walk(out_dir):
                for fn in sorted(files):
                    fp = path.join(root, fn)
                    z.write(fp, path.relpath(fp, path.dirname(out_dir)))

    return manifest, total, zpath


def _readme(game_prov, weights_sha, total_bytes):
    lines = []
    lines.append('# Mahjong analysis pack (sidecar-backed)\n')
    lines.append(SIDECAR_NOTE + '\n')
    lines.append('## How to use (no model needed)\n')
    lines.append('```\n'
                 'python mjsoul_mortal.py --no-model logs/<game>.json     # reads Mortal from the sidecar\n'
                 'python mjsoul_analyze.py logs/<game>.json               # recomputes from the raw log\n'
                 'python mjsoul_turns.py   logs/<game>.json               # recomputes from the raw log\n'
                 '```\n')
    lines.append('`--no-model` imports no torch / libriichi and never touches weights. If a raw log '
                 'was edited after the pack was built, the sidecar hash check fails LOUDLY rather than '
                 'serving stale numbers.\n')
    lines.append(f'\n## Provenance\n\n- model: `{mm.MODEL_ID}`  weights sha256 `{weights_sha[:16]}…`\n'
                 f'- converter v{mm.conv.CONVERTER_VERSION} / mortal-module v{mm.MORTAL_MODULE_VERSION} '
                 f'/ sidecar-schema v{mm.SIDECAR_SCHEMA_VERSION}\n'
                 f'- pack total size: ~{total_bytes/1024:.0f} KB\n\n')
    lines.append('| game (uuid) | you | records |\n|---|---|---|\n')
    for g in game_prov:
        you_name = g['names'][g['you']] if (g['names'] and g['you'] is not None) else '?'
        lines.append(f"| {g['uuid']} | {you_name} | {g['records']} |\n")
    lines.append('\nMortal numbers are policy-only (no GRP/placement model) from a HANCHAN model '
                 'scored on TONPUU games — tile/discard/safety reads transfer; placement/endgame is '
                 'out of scope. See mjsoul_mortal.md semantics in the source suite.\n')
    return ''.join(lines)


def main():
    ap = argparse.ArgumentParser(description='Build a sidecar-backed shareable pack')
    ap.add_argument('logs', nargs='*', help='raw log .json paths')
    ap.add_argument('--glob', action='append', help='glob(s) of logs to include')
    ap.add_argument('--out', required=True, help='output pack directory')
    ap.add_argument('--zip', action='store_true', help='also write <out>.zip')
    ap.add_argument('--refresh', action='store_true', help='force-regenerate all sidecars')
    ap.add_argument('--force', action='store_true', help='overwrite an existing --out dir')
    args = ap.parse_args()

    manifest, total, zpath = build(args)
    print(f"\nPack built: {args.out}")
    print(f"  games: {len(manifest['games'])}   files: {len(manifest['contents'])}   "
          f"total: {total/1024:.1f} KB")
    print(f"  weights_sha256: {manifest['model']['weights_sha256'][:16]}…  (NOT shipped)")
    if zpath:
        print(f"  zip: {zpath} ({path.getsize(zpath)/1024:.1f} KB)")


if __name__ == '__main__':
    main()
