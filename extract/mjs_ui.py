#!/usr/bin/env python3
"""
mjs_ui.py — CDP driver for the Mahjong Soul Unity/WebGL client.

The client is a canvas app with no DOM and no JS control API, so everything goes
through real mouse/key input and screenshots over the Chrome DevTools Protocol,
targeting the tab BY URL (via majsoul_extract.page_ws) so tab-index shuffling never
matters. Coordinates are FRACTIONS of the live canvas rect (re-read each action), so
it is viewport-size agnostic.

Key pieces:
  * screenshot()   -> PIL image of the canvas (Page.captureScreenshot)
  * wait_stable()  -> poll screenshots ~1/s until the frame stops changing, so we ride
                      out load/transition animations instead of guessing sleep lengths
  * classify()     -> which screen are we on? (list / replay / loading / other) by
                      matching a heavily-downscaled grayscale thumbnail to references
  * wait_for(state)-> wait_stable, then keep polling until classify() == state
"""
import sys, os, json, asyncio, base64, io, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import majsoul_extract as MX
import websockets
from PIL import Image

REF_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui_refs")
THUMB = (48, 27)   # downscale size for structural (content-independent) matching

def _thumb(img):
    return img.convert("L").resize(THUMB, Image.BILINEAR)

def _mad(a, b):
    pa, pb = a.load(), b.load()
    s = 0
    for y in range(THUMB[1]):
        for x in range(THUMB[0]):
            s += abs(pa[x, y] - pb[x, y])
    return s / (THUMB[0] * THUMB[1])

class MJS:
    def __init__(self, ws):
        self.ws = ws; self._id = 0
        self._refs = self._load_refs()

    @classmethod
    async def connect(cls):
        ws = await websockets.connect(MX.page_ws(), max_size=None, open_timeout=15)
        self = cls(ws)
        await self.cmd("Page.enable")
        return self

    async def close(self):
        await self.ws.close()

    async def cmd(self, method, params=None):
        self._id += 1; i = self._id
        await self.ws.send(json.dumps({"id": i, "method": method, "params": params or {}}))
        while True:
            m = json.loads(await self.ws.recv())
            if m.get("id") == i:
                return m

    async def eval(self, expr):
        m = await self.cmd("Runtime.evaluate", {"expression": expr, "returnByValue": True})
        return m["result"]["result"].get("value")

    async def rect(self):
        return json.loads(await self.eval(
            "JSON.stringify((r=>({x:r.x,y:r.y,w:r.width,h:r.height}))"
            "((document.querySelector('#unity-canvas')||document.querySelector('canvas')).getBoundingClientRect()))"))

    async def _mouse(self, typ, x, y, buttons):
        await self.cmd("Input.dispatchMouseEvent",
                       {"type": typ, "x": x, "y": y, "button": "left",
                        "clickCount": 1, "buttons": buttons})

    async def click(self, fx, fy):
        r = await self.rect(); x = r["x"] + fx*r["w"]; y = r["y"] + fy*r["h"]
        await self._mouse("mouseMoved", x, y, 0);   await asyncio.sleep(0.05)
        await self._mouse("mousePressed", x, y, 1);  await asyncio.sleep(0.05)
        await self._mouse("mouseReleased", x, y, 0); await asyncio.sleep(0.05)

    async def drag(self, fx, fy0, fy1, steps=14, hold=0.30):
        r = await self.rect(); X = r["x"] + fx*r["w"]
        y0 = r["y"] + fy0*r["h"]; y1 = r["y"] + fy1*r["h"]
        await self._mouse("mousePressed", X, y0, 1)
        for k in range(1, steps+1):
            await self._mouse("mouseMoved", X, y0 + (y1-y0)*k/steps, 1)
            await asyncio.sleep(0.03)
        if hold: await asyncio.sleep(hold)      # kill fling momentum -> scroll == drag distance
        await self._mouse("mouseReleased", X, y1, 0); await asyncio.sleep(0.3)

    async def screenshot(self):
        m = await self.cmd("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
        return Image.open(io.BytesIO(base64.b64decode(m["result"]["data"]))).convert("RGB")

    async def wait_stable(self, timeout=25, interval=1.0, thresh=3.0, settle=2):
        """Poll screenshots until the frame stops changing (settle consecutive quiet
        frames) — rides out loading/transition animations. Returns the last image."""
        prev = None; quiet = 0; t0 = time.time()
        while time.time() - t0 < timeout:
            img = _thumb(await self.screenshot())
            if prev is not None:
                quiet = quiet + 1 if _mad(prev, img) < thresh else 0
                if quiet >= settle:
                    return img
            prev = img
            await asyncio.sleep(interval)
        return prev

    # --- screen classification ------------------------------------------------
    def _load_refs(self):
        refs = {}
        if os.path.isdir(REF_DIR):
            for f in os.listdir(REF_DIR):
                if f.endswith(".png"):
                    refs[f[:-4]] = _thumb(Image.open(os.path.join(REF_DIR, f)).convert("RGB"))
        return refs

    def classify_thumb(self, thumb):
        if not self._refs:
            return None, {}
        scores = {name: _mad(thumb, ref) for name, ref in self._refs.items()}
        best = min(scores, key=scores.get)
        # strip trailing digits so list1/list2 -> "list"
        return best.rstrip("0123456789"), scores

    async def classify(self):
        return self.classify_thumb(_thumb(await self.screenshot()))[0]

    async def wait_for(self, state, timeout=25):
        """Wait until settled AND classify() == state. Returns True on success."""
        t0 = time.time()
        while time.time() - t0 < timeout:
            thumb = await self.wait_stable(timeout=max(4, timeout - (time.time()-t0)))
            cls = self.classify_thumb(thumb)[0]
            if cls == state:
                return True
            await asyncio.sleep(0.6)
        return False

    # --- View-button detection (drift-proof row targeting) --------------------
    @staticmethod
    def find_view_buttons(img):
        """Locate the warm-tan 'View' buttons down the right column of the list. Returns
        a list of center y-fractions, top to bottom. Robust to scroll drift because it
        reads the actual pixels rather than assuming fixed row fractions."""
        W, H = img.size
        px = img.load()
        x0, x1 = int(0.85*W), int(0.885*W)
        def cream(c):
            r, g, b = c[:3]
            return r > 195 and g > 155 and r > b + 25 and b < 205
        runs = []; start = None
        for y in range(int(0.12*H), int(0.99*H)):
            band = sum(cream(px[x, y]) for x in range(x0, x1)) >= 2
            if band and start is None: start = y
            elif not band and start is not None:
                if y - start >= 4: runs.append((start, y))   # real button, not speckle
                start = None
        if start is not None and (int(0.99*H) - start) >= 4:
            runs.append((start, int(0.99*H)))
        return [((a+b)/2)/H for a, b in runs]

    async def view_buttons(self):
        return self.find_view_buttons(await self.screenshot())

    async def save_ref(self, name):
        os.makedirs(REF_DIR, exist_ok=True)
        img = await self.screenshot()
        img.save(os.path.join(REF_DIR, name + ".png"))
        self._refs = self._load_refs()
        return name


# CLI: `python3 mjs_ui.py ref <name>`  saves current screen as a reference.
#      `python3 mjs_ui.py classify`     prints the current screen + all scores.
#      `python3 mjs_ui.py wait <state>` waits for a screen state.
async def _main():
    m = await MJS.connect()
    try:
        cmd = sys.argv[1] if len(sys.argv) > 1 else "classify"
        if cmd == "ref":
            print("saved ref:", await m.save_ref(sys.argv[2]))
        elif cmd == "classify":
            thumb = _thumb(await m.screenshot())
            best, scores = m.classify_thumb(thumb)
            print("screen:", best)
            for n, s in sorted(scores.items(), key=lambda x: x[1]):
                print(f"   {n:12} {s:6.1f}")
        elif cmd == "wait":
            print("reached" if await m.wait_for(sys.argv[2]) else "TIMEOUT", sys.argv[2])
    finally:
        await m.close()

if __name__ == "__main__":
    asyncio.run(_main())
