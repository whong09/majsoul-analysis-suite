# Mahjong Soul Replay Extraction

Extract a Mahjong Soul (majsoul) game replay from the browser's WASM heap and convert it to tenhou.net/6 JSON format.

## Prerequisites

- Mahjong Soul running in a browser (Unity WebGL client)
- Browser automation MCP connected (Playwright or Chrome MCP)
- The target replay must be **currently loaded/viewed** in the browser (open the replay viewer for the game you want)

## Important: Heap Access Path

The WASM heap is accessed differently depending on browser context:

| Context | Heap path |
|---------|-----------|
| Playwright MCP (`browser_evaluate`) | `unityInstance.Module.HEAPU8` |
| Chrome MCP (`chrome_evaluate`) | `Module.HEAPU8` (if global) or `unityInstance.Module.HEAPU8` |

If `Module` is not defined, check `unityInstance.Module`. The `unityInstance` object has keys: `Module`, `SetFullscreen`, `SendMessage`, `Quit`, `GetMemoryInfo`.

## Workflow

### Step 1: Atomic scan + extract (CRITICAL: do this in ONE evaluate call)

The WASM heap is mutated by the running game. If you scan in one call and extract in another, the offsets may be stale. **Always scan and extract in a single atomic JavaScript evaluation.**

```javascript
() => {
  const heap = unityInstance.Module.HEAPU8;
  const pattern = [0x0a, 0x12, 0x2e, 0x6c, 0x71, 0x2e, 0x52, 0x65, 0x63, 0x6f, 0x72, 0x64, 0x4e, 0x65, 0x77, 0x52, 0x6f, 0x75, 0x6e, 0x64, 0x12];

  // Find all RecordNewRound markers
  const matches = [];
  for (let i = 0; i < heap.length - pattern.length; i++) {
    let found = true;
    for (let j = 0; j < pattern.length; j++) {
      if (heap[i+j] !== pattern[j]) { found = false; break; }
    }
    if (found) matches.push(i);
  }

  // Cluster them (gap > 100KB = new cluster)
  const clusters = [];
  let current = [matches[0]];
  for (let i = 1; i < matches.length; i++) {
    if (matches[i] - matches[i-1] > 100000) {
      clusters.push(current);
      current = [matches[i]];
    } else {
      current.push(matches[i]);
    }
  }
  clusters.push(current);

  // Pick cluster with 4-8 rounds (East-only game), use last (most recent)
  const target = clusters.filter(c => c.length >= 4 && c.length <= 8);
  if (target.length === 0) return JSON.stringify({error: 'no suitable cluster', clusters: clusters.map(c => c.length)});
  const chosen = target[target.length - 1];

  // Extract from first RecordNewRound to 40KB past the last.
  // The tail must be generous: the final round's discards + win record can
  // exceed 10KB, and too short a slice silently truncates the last Hule so
  // the final round looks unfinished.
  const start = chosen[0];
  const end = Math.min(chosen[chosen.length - 1] + 40000, heap.length);
  const slice = heap.slice(start, end);

  // Verify first bytes still match pattern
  const verify = Array.from(slice.slice(0, 21));
  if (!verify.every((b, i) => b === pattern[i])) {
    return JSON.stringify({error: 'heap mutated during extraction'});
  }

  // Base64 encode
  let binary = '';
  for (let i = 0; i < slice.length; i++) binary += String.fromCharCode(slice[i]);
  window.__majsoulB64 = btoa(binary);

  return JSON.stringify({
    success: true,
    rounds: chosen.length,
    byteLength: slice.length,
    b64Length: window.__majsoulB64.length
  });
}
```

This scan takes ~30 seconds on a ~500MB heap.

### Step 2: Exfiltrate via local HTTP POST

Start a local HTTP server to receive the base64 data (avoids passing 50KB+ through conversation context):

```python
# Save as /tmp/receive_b64.py and run: python3 /tmp/receive_b64.py
from http.server import HTTPServer, BaseHTTPRequestHandler
import os

OUTPUT = os.path.expanduser('~/Downloads/majsoul_game_full.b64')

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers['Content-Length'])
        data = self.rfile.read(length)
        with open(OUTPUT, 'wb') as f:
            f.write(data)
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(b'OK')
        print(f"Received {length} bytes -> {OUTPUT}")

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

HTTPServer(('127.0.0.1', 9876), Handler).serve_forever()
```

Then POST from the browser:

```javascript
() => {
  return fetch('http://127.0.0.1:9876', { method: 'POST', body: window.__majsoulB64 })
    .then(r => r.text());
}
```

### Step 3: Decode to binary

```bash
base64 -d -i ~/Downloads/majsoul_game_full.b64 -o ~/Downloads/majsoul_game_full.bin
```

### Step 4: Run the decoder

Save the Python decoder below as `decode_to_tenhou.py`, place `majsoul_game_full.bin` in the same directory, then run:

```bash
python3 decode_to_tenhou.py
```

Output: `majsoul_tenhou6.json` (tenhou.net/6 viewer format)

---

## Cluster Selection Notes

- **4-Player East**: expect 4-8 rounds per game
- **4-Player South**: expect 8-16 rounds
- Multiple clusters = multiple cached games in heap. Pick the one matching expected round count.
- Verify by checking starting scores (all 25000 for a fresh game).
- If no 4-8 round cluster exists, try the 12-round cluster (could be South game or extended East with renchan).

## Protobuf Wire Format Reference

The game record is a sequence of protobuf `Any` messages:
- Field 1 (tag 0x0A, LEN): type_url string (e.g. `.lq.RecordNewRound`)
- Field 2 (tag 0x12, LEN): serialized payload

### Record Types

| Type | Purpose |
|------|---------|
| `.lq.RecordNewRound` | Round start: hands, scores, dora |
| `.lq.RecordDealTile` | Player draws a tile |
| `.lq.RecordDiscardTile` | Player discards (with riichi/tsumogiri flags) |
| `.lq.RecordChiPengGang` | Chi/Pon/open Kan call |
| `.lq.RecordAnGangAddGang` | Closed Kan / added Kan |
| `.lq.RecordHule` | Win (tsumo or ron) |
| `.lq.RecordNoTile` | Exhaustive draw |
| `.lq.RecordLiuJu` | Abortive draw |

### Key Field Mappings

**RecordNewRound:**
- 1: chang (round wind, 0=East)
- 2: ju (dealer seat)
- 3: ben (honba)
- 5: scores (packed varint)
- 6: liqibang (riichi sticks)
- 7/8/9/10: hand tiles for seats 0-3 (repeated bytes, 2-char strings like "3m")
- 16: dora indicator tile

**RecordDiscardTile:**
- 1: seat
- 2: tile
- 3: is_liqi (riichi declaration — set once, on the riichi tile)
- 5: moqie (tsumogiri — set on drawn-and-thrown tiles)

**RecordDealTile:**
- 1: seat
- 2: tile

**RecordChiPengGang:**
- 1: seat
- 2: type (0=chi, 1=pon, 2=minkan)
- 3: tiles (repeated)
- 4: froms (packed varint)

**RecordHule:**
- 1: HuleInfo sub-message (repeated for double/triple ron)
  - 1: hand tiles (repeated bytes)
  - 2: melds (repeated string descriptions)
  - 3: hu_tile
  - 4: seat
  - 5: zimo (bool)
  - 6: qinjia (bool -- dealer won this hand, NOT from_seat)
  - 8: dora indicators (repeated tile string — the FULL list at win time, so it includes kan-dora; use this to populate tenhou6 slot [1] rather than only the opening `RecordNewRound` field-16 indicator)
  - 9: uradora indicators (repeated tile string — present only on riichi wins; populates tenhou6 slot [2])
  - 15: total points
- 2: old_scores (packed varint)
- 3: delta_scores (packed varint, **signed 64-bit** two's complement, NOT zigzag)
- 5: new_scores (packed varint)

**Inferring deal-in player:** `from_seat` is NOT stored in HuleInfo. Infer from delta_scores -- the player with the most negative delta (excluding the winner) dealt in. For tsumo, `from_who = winner` (all other players pay, but dealer pays most -- don't confuse this with deal-in).

### Tile Encoding

Standard 2-character format: `[0-9][mpsz]`
- m = man (characters), p = pin (circles), s = sou (bamboo), z = honors (1-4=winds, 5-7=dragons)
- 0m/0p/0s = red five (aka dora)

## Troubleshooting

- **`Module is not defined`**: Use `unityInstance.Module.HEAPU8` instead.
- **Heap scan finds nothing**: The replay must be actively loaded. Navigate to the replay viewer and start playback.
- **Multiple RecordNewRound clusters**: The heap may contain multiple cached games. Use the cluster with the expected number of rounds and verify starting scores.
- **Heap mutated between calls**: Always scan AND extract in a single atomic evaluate call.
- **Final round looks unfinished (no Hule/NoTile after the last RecordNewRound)**: The tail slice past the last round-start is too short and truncated the final round's win record. Increase the `+ 40000` end offset. This is NOT a client/loading issue — the data is already in the heap.
- **"No contiguous finished game" / fragmented heap**: Sometimes the client keeps the replay records as *scattered individual allocations* spread across the whole heap instead of one contiguous `RecordNewRound → discards → Hule` stream. The primary scan then finds lone `RecordNewRound` markers with no adjacent actions and can't reconstruct the game. `majsoul_extract.py` falls back to a whole-heap scan that gathers the scattered round-ending records (`Hule`/`NoTile`) and chains them by score (`new` of round *k* == `old` of round *k+1*) to recover final scores + placement — but turn-by-turn order can't be reliably rebuilt, so **no analyzable log is written** in this mode. Fix: **reload the replay from your replay list and let it play/skip through to the end once**, then re-run — that rebuilds the contiguous buffer.
- **Black screen after reload**: Clear IndexedDB (`UnityCache`, `/idbfs`) then reload.
- **base64 decode fails**: macOS `base64` requires `-d -i <file> -o <file>` syntax (not piped).

## Notes

- The WASM heap is ~500-600MB. Scanning takes 20-40 seconds.
- Game records are typically 30-60KB for a full East-only game.
- Player names/account IDs are NOT in the round records -- they're in the outer `GameDetailRecords` wrapper which may be elsewhere in heap.
- Seat 0 = East at game start. Dealer rotates per round via the `ju` field.

---

## Decoder Script (decode_to_tenhou.py)

```python
#!/usr/bin/env python3
"""Decode Mahjong Soul game record from binary and convert to tenhou.net/6 format."""

import struct
import json
from pathlib import Path

BIN_FILE = Path(__file__).parent / "majsoul_game_full.bin"


def decode_varint(data, offset):
    result = 0
    shift = 0
    while offset < len(data):
        b = data[offset]
        offset += 1
        result |= (b & 0x7F) << shift
        shift += 7
        if not (b & 0x80):
            break
    return result, offset


def decode_signed_varint(val):
    """Interpret a varint as signed 64-bit two's complement."""
    if val >= 2**63:
        return val - 2**64
    return val


def decode_protobuf(data, start=0, end=None):
    if end is None:
        end = len(data)
    fields = []
    offset = start
    while offset < end:
        tag, offset = decode_varint(data, offset)
        field_num = tag >> 3
        wire_type = tag & 7
        if wire_type == 0:
            value, offset = decode_varint(data, offset)
            fields.append((field_num, 'varint', value))
        elif wire_type == 2:
            length, offset = decode_varint(data, offset)
            value = data[offset:offset + length]
            offset += length
            fields.append((field_num, 'bytes', value))
        elif wire_type == 5:
            value = struct.unpack_from('<I', data, offset)[0]
            offset += 4
            fields.append((field_num, 'fixed32', value))
        elif wire_type == 1:
            value = struct.unpack_from('<Q', data, offset)[0]
            offset += 8
            fields.append((field_num, 'fixed64', value))
        else:
            break
    return fields


def find_any_messages(data):
    marker = b'.lq.Record'
    results = []
    pos = 0
    while True:
        idx = data.find(marker, pos)
        if idx == -1:
            break
        for back in range(2, 6):
            check_start = idx - back
            if check_start < 0:
                continue
            if data[check_start] == 0x0A:
                length, after_len = decode_varint(data, check_start + 1)
                if after_len == idx:
                    type_url = data[idx:idx + length].decode('ascii', errors='replace')
                    payload_start = idx + length
                    if payload_start < len(data) and data[payload_start] == 0x12:
                        payload_len, payload_data_start = decode_varint(data, payload_start + 1)
                        if payload_data_start + payload_len <= len(data):
                            payload = data[payload_data_start:payload_data_start + payload_len]
                            results.append({'type_url': type_url, 'payload': payload})
                break
        pos = idx + 1
    return results


def decode_packed_varints(value):
    """Decode packed repeated varint field."""
    result = []
    off = 0
    while off < len(value):
        v, off = decode_varint(value, off)
        result.append(v)
    return result


def decode_record_new_round(payload):
    fields = decode_protobuf(payload)
    result = {'chang': 0, 'ju': 0, 'ben': 0, 'scores': [], 'liqibang': 0,
              'tiles': [[], [], [], []], 'dora': ''}
    for fnum, wtype, value in fields:
        if fnum == 1 and wtype == 'varint':
            result['chang'] = value
        elif fnum == 2 and wtype == 'varint':
            result['ju'] = value
        elif fnum == 3 and wtype == 'varint':
            result['ben'] = value
        elif fnum == 5 and wtype == 'bytes':
            result['scores'] = decode_packed_varints(value)
        elif fnum == 6 and wtype == 'varint':
            result['liqibang'] = value
        elif fnum in (7, 8, 9, 10) and wtype == 'bytes':
            seat = fnum - 7
            try:
                result['tiles'][seat].append(value.decode('ascii'))
            except:
                pass
        elif fnum == 16 and wtype == 'bytes':
            try:
                result['dora'] = value.decode('ascii')
            except:
                pass
    return result


def decode_record_discard_tile(payload):
    fields = decode_protobuf(payload)
    result = {'seat': 0, 'tile': '', 'moqie': False, 'is_riichi': False}
    for fnum, wtype, value in fields:
        if fnum == 1 and wtype == 'varint':
            result['seat'] = value
        elif fnum == 2 and wtype == 'bytes':
            try:
                result['tile'] = value.decode('ascii')
            except:
                pass
        elif fnum == 3 and wtype == 'varint':
            result['is_riichi'] = bool(value)   # field 3 = is_liqi
        elif fnum == 5 and wtype == 'varint':
            result['moqie'] = bool(value)        # field 5 = moqie (tsumogiri)
    return result


def decode_record_deal_tile(payload):
    fields = decode_protobuf(payload)
    result = {'seat': 0, 'tile': ''}
    for fnum, wtype, value in fields:
        if fnum == 1 and wtype == 'varint':
            result['seat'] = value
        elif fnum == 2 and wtype == 'bytes':
            try:
                result['tile'] = value.decode('ascii')
            except:
                pass
    return result


def decode_record_chi_peng_gang(payload):
    fields = decode_protobuf(payload)
    result = {'seat': 0, 'type': 0, 'tiles': [], 'froms': []}
    for fnum, wtype, value in fields:
        if fnum == 1 and wtype == 'varint':
            result['seat'] = value
        elif fnum == 2 and wtype == 'varint':
            result['type'] = value
        elif fnum == 3 and wtype == 'bytes':
            try:
                result['tiles'].append(value.decode('ascii'))
            except:
                pass
        elif fnum == 4 and wtype == 'bytes':
            result['froms'] = decode_packed_varints(value)
    return result


def decode_record_angang_addgang(payload):
    fields = decode_protobuf(payload)
    result = {'seat': 0, 'type': 0, 'tiles': ''}
    for fnum, wtype, value in fields:
        if fnum == 1 and wtype == 'varint':
            result['seat'] = value
        elif fnum == 2 and wtype == 'varint':
            result['type'] = value
        elif fnum == 3 and wtype == 'bytes':
            try:
                result['tiles'] = value.decode('ascii')
            except:
                pass
    return result


def decode_record_hule(payload):
    fields = decode_protobuf(payload)
    result = {'hules': [], 'old_scores': [], 'delta_scores': [], 'new_scores': []}
    for fnum, wtype, value in fields:
        if fnum == 1 and wtype == 'bytes':
            hule_fields = decode_protobuf(value)
            hule = {'seat': 0, 'hand': [], 'melds': [], 'hu_tile': '', 'zimo': False, 'points': 0}
            for hf, hw, hv in hule_fields:
                if hf == 1 and hw == 'bytes':
                    try:
                        hule['hand'].append(hv.decode('ascii'))
                    except:
                        pass
                elif hf == 2 and hw == 'bytes':
                    try:
                        hule['melds'].append(hv.decode('ascii'))
                    except:
                        pass
                elif hf == 3 and hw == 'bytes':
                    try:
                        hule['hu_tile'] = hv.decode('ascii')
                    except:
                        pass
                elif hf == 4 and hw == 'varint':
                    hule['seat'] = hv
                elif hf == 5 and hw == 'varint':
                    hule['zimo'] = bool(hv)
                elif hf == 15 and hw == 'varint':
                    hule['points'] = hv
            result['hules'].append(hule)
        elif fnum == 2 and wtype == 'bytes':
            result['old_scores'] = decode_packed_varints(value)
        elif fnum == 3 and wtype == 'bytes':
            raw = decode_packed_varints(value)
            result['delta_scores'] = [decode_signed_varint(v) for v in raw]
        elif fnum == 5 and wtype == 'bytes':
            result['new_scores'] = decode_packed_varints(value)
    return result


def decode_record_no_tile(payload):
    fields = decode_protobuf(payload)
    result = {'old_scores': [], 'delta_scores': [], 'new_scores': []}
    for fnum, wtype, value in fields:
        if fnum == 2 and wtype == 'bytes':
            result['old_scores'] = decode_packed_varints(value)
        elif fnum == 3 and wtype == 'bytes':
            raw = decode_packed_varints(value)
            result['delta_scores'] = [decode_signed_varint(v) for v in raw]
        elif fnum == 5 and wtype == 'bytes':
            result['new_scores'] = decode_packed_varints(value)
    return result


DECODERS = {
    '.lq.RecordNewRound': decode_record_new_round,
    '.lq.RecordDiscardTile': decode_record_discard_tile,
    '.lq.RecordDealTile': decode_record_deal_tile,
    '.lq.RecordChiPengGang': decode_record_chi_peng_gang,
    '.lq.RecordAnGangAddGang': decode_record_angang_addgang,
    '.lq.RecordHule': decode_record_hule,
    '.lq.RecordNoTile': decode_record_no_tile,
}


def _infer_from_seat(winner_seat, delta_scores):
    """Infer who dealt the winning tile from delta_scores (most negative player != winner)."""
    if not delta_scores:
        return winner_seat
    min_val = 0
    from_seat = winner_seat
    for i, d in enumerate(delta_scores):
        if i != winner_seat and d < min_val:
            min_val = d
            from_seat = i
    return from_seat


def convert_to_tenhou6(records):
    tenhou = {
        "title": ["Mahjong Soul", "4-Player East"],
        "name": ["Player0", "Player1", "Player2", "Player3"],
        "rule": {"disp": "East", "aka": 1},
        "log": []
    }

    current_round = None
    round_draws = [[], [], [], []]
    round_discards = [[], [], [], []]

    def flush_round(result_obj=None):
        nonlocal current_round, round_draws, round_discards
        if current_round is None:
            return
        nr = current_round
        scores_100 = [s // 100 for s in nr['scores']]
        round_info = [nr['chang'] * 4 + nr['ju'], nr['ben'], nr['liqibang']] + scores_100
        dora = [nr['dora']] if nr['dora'] else []
        uradora = []
        entry = [round_info, dora, uradora]
        for seat in range(4):
            entry.append(nr['tiles'][seat])
            entry.append(round_draws[seat])
            entry.append(round_discards[seat])
        if result_obj:
            entry.append(result_obj)
        tenhou['log'].append(entry)
        current_round = None
        round_draws = [[], [], [], []]
        round_discards = [[], [], [], []]

    for msg in records:
        rtype = msg['type_url']
        decoder = DECODERS.get(rtype)
        if not decoder:
            continue
        rec = decoder(msg['payload'])

        if rtype == '.lq.RecordNewRound':
            flush_round()
            current_round = rec
            round_draws = [[], [], [], []]
            round_discards = [[], [], [], []]
        elif rtype == '.lq.RecordDealTile':
            round_draws[rec['seat']].append(rec['tile'])
        elif rtype == '.lq.RecordDiscardTile':
            tile = rec['tile']
            # r = tsumogiri (moqie); t = tedashi riichi. Tsumogiri wins so a
            # tsumogiri-riichi is written as plain 'r' (invisible to a t-scan).
            prefix = ''
            if rec['moqie']:
                prefix = 'r'
            elif rec['is_riichi']:
                prefix = 't'
            round_discards[rec['seat']].append(prefix + tile)
        elif rtype == '.lq.RecordChiPengGang':
            seat = rec['seat']
            mtype = rec['type']
            tiles = rec['tiles']
            if mtype == 0:
                call_str = f"c{tiles[0]},{tiles[1]},{tiles[2]}"
            elif mtype == 1:
                call_str = f"p{tiles[0]},{tiles[1]},{tiles[2]}"
            elif mtype == 2:
                call_str = f"m{','.join(tiles)}"
            else:
                call_str = f"?{','.join(tiles)}"
            round_draws[seat].append(call_str)
        elif rtype == '.lq.RecordAnGangAddGang':
            seat = rec['seat']
            mtype = rec['type']
            tile = rec['tiles']
            if mtype == 2:
                round_discards[seat].append(f"a{tile}")
            else:
                round_discards[seat].append(f"k{tile}")
        elif rtype == '.lq.RecordHule':
            agari_list = []
            for hule in rec['hules']:
                if hule['zimo']:
                    from_who = hule['seat']
                else:
                    from_who = _infer_from_seat(hule['seat'], rec['delta_scores'])
                agari_list.append({
                    'who': hule['seat'],
                    'fromWho': from_who,
                    'hand': hule['hand'],
                    'melds': hule['melds'],
                    'machi': hule['hu_tile'],
                    'tsumo': hule['zimo'],
                    'points': hule['points']
                })
            result_obj = {
                'agari': agari_list,
                'owari': rec['delta_scores'],
                'sc': rec['new_scores']
            }
            flush_round(result_obj)
        elif rtype == '.lq.RecordNoTile':
            result_obj = {
                'ryuukyoku': True,
                'owari': rec['delta_scores'],
                'sc': rec['new_scores']
            }
            flush_round(result_obj)

    flush_round()
    return tenhou


def print_game_summary(messages):
    print("=" * 60)
    print("GAME SUMMARY")
    print("=" * 60)
    for msg in messages:
        rtype = msg['type_url']
        decoder = DECODERS.get(rtype)
        if not decoder:
            continue
        rec = decoder(msg['payload'])
        if rtype == '.lq.RecordNewRound':
            wind = ['East', 'South', 'West', 'North'][rec['chang']]
            print(f"\n{'='*40}")
            print(f"  {wind} {rec['ju']+1} | Honba {rec['ben']} | Riichi sticks: {rec['liqibang']}")
            print(f"  Scores: {rec['scores']}")
            print(f"  Dora indicator: {rec['dora']}")
            for i in range(4):
                dealer = ' (dealer)' if i == rec['ju'] else ''
                print(f"  P{i}{dealer}: {' '.join(rec['tiles'][i])}")
            print()
        elif rtype == '.lq.RecordHule':
            for h in rec['hules']:
                if h['zimo']:
                    ztype = 'Tsumo'
                else:
                    from_seat = _infer_from_seat(h['seat'], rec['delta_scores'])
                    ztype = f"Ron (from P{from_seat})"
                print(f"  >>> P{h['seat']} WIN - {ztype} on {h['hu_tile']} | {h['points']} pts")
                print(f"      Hand: {' '.join(h['hand'])}")
                if h['melds']:
                    print(f"      Melds: {h['melds']}")
            print(f"  Delta: {rec['delta_scores']}")
            print(f"  New scores: {rec['new_scores']}")


def main():
    print(f"Loading: {BIN_FILE}")
    data = BIN_FILE.read_bytes()
    print(f"Loaded {len(data):,} bytes")
    messages = find_any_messages(data)
    print(f"Decoded {len(messages)} record messages\n")
    print_game_summary(messages)
    tenhou = convert_to_tenhou6(messages)
    out_path = Path(__file__).parent / "majsoul_tenhou6.json"
    out_path.write_text(json.dumps(tenhou, indent=2, ensure_ascii=False))
    print(f"\nSaved tenhou6 format to: {out_path}")
    print(f"Total rounds: {len(tenhou['log'])}")


if __name__ == '__main__':
    main()
```
