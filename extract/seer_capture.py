#!/usr/bin/env python3
"""
seer_capture.py — capture the Mahjong Soul MAKA ("Seer") analysis off the websocket.

MAKA (the in-client AI review, grades A+/B/C/E) is internally "Seer". On every replay
open the client calls `.lq.Lobby.fetchSeerReport` and the response carries the full
per-decision analysis. This module snoops that frame via CDP's Network domain (no page
JS API exists). Reading an already-analyzed game's report costs ZERO quota.

NOTE: the same protobuf also persists in the WASM heap, so the extractor reads MAKA
straight from the heap (see majsoul_extract.scan_seer) and this websocket path is only
needed when you must observe the fetch live (e.g. right after triggering a new analysis).
Decoding is shared via seer_decode.

Usage:
  from seer_capture import SeerSnoop
  async with SeerSnoop() as snoop:      # starts CDP Network capture
      ...open a replay via mjs_ui...     # fetchSeerReport fires during the load
      report = await snoop.wait(uuid)    # -> dict, or None if the game wasn't analyzed
"""
import asyncio, json, base64
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import majsoul_extract as MX
import websockets
from seer_decode import decode_report, maka_summary, tile_name   # noqa: F401 (re-exported)


class SeerSnoop:
    """Captures raw fetchSeerReport response frames off the MJS websocket via CDP.
    Its own CDP connection (separate from the mjs_ui driver) enables Network events."""
    def __init__(self):
        self.ws = None; self._id = 0; self._task = None
        self._seer_idx = None       # 2-byte request index we're waiting to see answered
        self._reports = []          # decoded reports, in arrival order

    async def __aenter__(self):
        self.ws = await websockets.connect(MX.page_ws(), max_size=None, open_timeout=15)
        await self._cmd("Network.enable")
        self._task = asyncio.create_task(self._pump())
        return self

    async def __aexit__(self, *a):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self.ws.close()

    async def _cmd(self, method, params=None):
        self._id += 1; i = self._id
        await self.ws.send(json.dumps({"id": i, "method": method, "params": params or {}}))
        while True:
            m = json.loads(await self.ws.recv())
            if m.get("id") == i:
                return m

    async def _pump(self):
        while True:
            try:
                m = json.loads(await self.ws.recv())
            except (websockets.ConnectionClosed, json.JSONDecodeError):
                return
            try:
                self._handle(m)
            except (KeyError, IndexError, ValueError, TypeError):
                continue          # ignore malformed / non-binary frames, keep listening

    def _handle(self, m):
        meth = m.get("method")
        if meth == "Network.webSocketFrameSent":
            # CDP uses the 'response' key for sent frames too (WebSocketFrame)
            raw = base64.b64decode(m["params"]["response"]["payloadData"])
            if b"fetchSeerReport" in raw:
                self._seer_idx = raw[1:3]     # match the response by index
        elif meth == "Network.webSocketFrameReceived":
            raw = base64.b64decode(m["params"]["response"]["payloadData"])
            if self._seer_idx and raw[1:3] == self._seer_idx and len(raw) > 60:
                rep = decode_report(raw)
                if rep:
                    self._reports.append(rep)
                self._seer_idx = None

    def take(self, uuid=None):
        """Pop a captured report. With uuid, return the matching one (or None); without,
        the most recent."""
        if uuid is not None:
            for i, r in enumerate(self._reports):
                if r["uuid"] == uuid:
                    return self._reports.pop(i)
            return None
        return self._reports.pop() if self._reports else None

    async def wait(self, uuid, timeout=6.0):
        """Wait up to timeout for the report for uuid to arrive, then pop it."""
        for _ in range(int(timeout / 0.3)):
            r = self.take(uuid)
            if r:
                return r
            await asyncio.sleep(0.3)
        return None

    def all(self):
        r = self._reports; self._reports = []
        return r


# CLI: decode a saved raw frame -> summary.  `python3 seer_capture.py seer.bin`
if __name__ == "__main__":
    raw = open(sys.argv[1], "rb").read()
    rep = decode_report(raw)
    if not rep:
        print("not a seer report / game not analyzed"); sys.exit(1)
    print(f"uuid {rep['uuid']}  decisions {len(rep['decisions'])}  rounds {len(rep['rounds'])}")
    for d in rep["decisions"][:5]:
        cand = "  ".join(f"{c['tile']}:{c['score']}" for c in d["candidates"])
        print(f"  seat{d['seat']} best{str(d['best']):>4}  [{cand}]")
    print("maka_summary:", maka_summary(rep))
