#!/usr/bin/env python3
"""
Bulk replay extractor — drives the Mahjong Soul *Log* list to extract every replay.

The client is a Unity WebGL canvas (no DOM, no JS API), so control is real mouse input
over CDP; screens are told apart with mjs_ui (screenshot classification) and every
transition is awaited with wait_for()/wait_stable() instead of guessed sleeps. All
clicks are FRACTIONS of the live canvas rect, so it is viewport-size agnostic.

Per row (top to bottom): click View -> wait_for('replay') -> run majsoul_extract.py
(--skip already-saved uuids, so it grabs the freshly-opened game) -> click exit ->
wait_for('list') -> scroll down exactly one row. Dedup by uuid; stop when scrolling
no longer yields new games.

    python3 bulk_extract.py [--max N] [--from-scratch] [--analyze N] [--no-analyze] [--no-mortal]
    python3 bulk_extract.py --current            # just the replay already open (single game)

Start on the Log screen (Records -> Overview). Reload the page first for a clean heap
(then selection is unambiguous). Run in the background; prints one line per game.

MAKA and Mortal are BOTH ON by default for every extracted game:
  * MAKA: the single-game extractor reads the game's "Seer" (MAKA) report straight from
    the WASM heap — the fetchSeerReport protobuf lands there on every open — and folds
    per-round + per-decision ratings into each JSON under a "maka" key. Free for already-
    analyzed games. For games that AREN'T analyzed yet, we spend daily MAKA quota to
    analyze them (maka_analyze: opens the panel, clicks "Start Analysis" only once the
    panel is CONFIRMED open so a misfire can't waste an attempt, then polls the heap until
    the report lands). This is UNLIMITED by default until the quota runs out: 3 consecutive
    analyze failures/timeouts are taken as "quota exhausted" and auto-analyze switches off
    for the rest of the run. --analyze N caps the spend to N; --no-analyze reads MAKA only
    and never spends quota.
  * Mortal: after each new game is saved, its Mortal policy sidecar (<log>.mortal.json) is
    written LOCALLY via the arm64 venv (torch+libriichi) — no quota, always runs. If the
    venv (MORTAL_VENV, default ~/mortal-dryrun/venv/bin/python) is missing it degrades to a
    skip instead of failing the extract. --no-mortal turns it off.

--current runs the whole pipeline (extract -> MAKA-analyze if needed -> Mortal sidecar) on
the replay already open on screen, without walking the list — the single-game default.
"""
import sys, os, json, asyncio, subprocess, re, glob
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import majsoul_extract as MX
import mjs_ui

HERE = os.path.dirname(os.path.abspath(__file__))
EXTRACTOR = os.path.join(HERE, "majsoul_extract.py")
# Mortal sidecar runs LOCALLY (no quota) via the arm64 venv that has torch+libriichi.
MORTAL_SCRIPT = os.path.join(HERE, "mjsoul_mortal.py")
MORTAL_VENV = os.environ.get("MORTAL_VENV", os.path.expanduser("~/mortal-dryrun/venv/bin/python"))

VIEW_FX      = 0.862     # x of the "View" buttons (their y is DETECTED per screen)
EXIT_FX, EXIT_FY = 0.93, 0.067
ROW_PITCH    = 0.213     # one row = this fraction of canvas height
DRAG_FX      = 0.60
MAKA_FX, MAKA_FY   = 0.965, 0.837   # the MAKA diamond (bottom-right of a replay)
START_FX, START_FY = 0.50, 0.685    # "Start Analysis" button in the MAKA panel
PANELX_FX, PANELX_FY = 0.85, 0.29   # the panel's X (close) button

def run_extractor(skip):
    """Run the single-game extractor on the currently-open replay. It writes the
    tenhou6 JSON *including* MAKA (read from the heap) and prints a 'maka :' line."""
    cmdline = [sys.executable, EXTRACTOR]
    if skip: cmdline += ["--skip", ",".join(skip)]
    p = subprocess.run(cmdline, capture_output=True, text=True)
    out = p.stdout + p.stderr
    u = re.search(r'uuid\s*:\s*(\S+)', out)
    f = re.search(r'Saved -> (.+\.json)', out)
    mk = re.search(r'maka\s*:\s*(.+)', out)
    return (u.group(1) if u else None,
            f.group(1).strip() if f else None,
            mk.group(1).strip() if mk else "maka: n/a")

def run_mortal(log_path):
    """Write the Mortal sidecar for a freshly-extracted log. Local + free (no quota), so
    it runs on EVERY new game by default. Needs the arm64 venv (torch+libriichi); if that
    interpreter is missing it degrades to a skip rather than failing the extract. Returns a
    short status string for the per-game log line."""
    if not log_path or not os.path.exists(MORTAL_VENV):
        return "mortal: skipped (no venv)"
    p = subprocess.run([MORTAL_VENV, MORTAL_SCRIPT, "--write-sidecar", log_path],
                       capture_output=True, text=True)
    if p.returncode != 0:
        tail = ((p.stderr or p.stdout).strip().splitlines() or [""])[-1]
        return f"mortal: FAILED ({tail[:80]})"
    ag = re.search(r'([\d.]+)%\s+agree', p.stdout)
    return f"mortal: sidecar ({ag.group(1)}% agree)" if ag else "mortal: sidecar written"

async def process_open_game(m, saved, analyze_budget, maka_fails, do_mortal):
    """Full per-game pipeline for the replay currently on screen: extract -> (MAKA-analyze
    if un-analyzed and quota remains) -> (Mortal sidecar). Shared by the bulk walker and the
    single-game --current mode. Returns (uuid, path, maka, analyze_budget, maka_fails)."""
    uuid, path, maka = run_extractor(saved)
    # un-analyzed game + budget left: spend a MAKA attempt (quota-safe), then re-extract.
    if uuid and analyze_budget > 0 and "none" in maka and uuid not in saved:
        print(f"  analyzing {uuid} ({'∞' if analyze_budget >= 10**8 else analyze_budget} left)…", flush=True)
        res = await maka_analyze(m, uuid)
        if res == 'analyzed':
            analyze_budget -= 1; maka_fails = 0
            # The report is in the heap (maka_analyze confirmed it), but a fresh extractor
            # subprocess can scan a beat before the full body has settled and miss it —
            # which would waste the quota we just spent. Re-extract with a short bounded
            # retry until MAKA actually lands (no extra quota; run_extractor is idempotent).
            for _ in range(4):
                uuid, path, maka = run_extractor(saved)      # re-extract with MAKA
                if "none" not in maka:
                    break
                await asyncio.sleep(2)
            maka += "  [auto-analyzed]" if "none" not in maka \
                    else "  [auto-analyzed but MAKA didn't settle — quota spent, not captured]"
        else:
            maka_fails += 1
            maka += f"  [{res}]"
            if maka_fails >= 3:                               # persistent trouble / no quota
                analyze_budget = 0
                maka += " [3 consecutive fails — MAKA quota likely gone, auto-analyze off]"
    # Mortal sidecar on every genuinely-new game (local, no quota).
    if do_mortal and uuid and uuid not in saved:
        maka += "  |  " + run_mortal(path)
    return uuid, path, maka, analyze_budget, maka_fails

def _list_thumb(img):
    W, H = img.size
    reg = img.crop((int(0.20*W), int(0.15*H), int(0.95*W), int(0.98*H)))
    return reg.convert("L").resize((48, 27), mjs_ui.Image.BILINEAR)

def _changed(a, b, thresh=2.0):
    # diff ONLY the list panel: a one-row scroll changes a big fraction of it (~3 MAD),
    # while the animated background (which dominates a full-frame diff) is excluded.
    return mjs_ui._mad(_list_thumb(a), _list_thumb(b)) > thresh

async def scroll_to_top(m):
    """Fling up toward the newest entries until the list stops moving (reached the top).
    A fixed number of flings isn't enough when we start deep in a long list, so we keep
    going until a fling no longer changes the list panel."""
    prev = None
    for _ in range(15):
        await m.drag(DRAG_FX, 0.28, 0.85, hold=0)   # strong fling (content down)
        await asyncio.sleep(0.5)
        cur = await m.screenshot()
        if prev is not None and not _changed(prev, cur):
            break                                   # list didn't move -> at the top
        prev = cur
    await m.wait_stable()

async def scroll_one_row(m):
    """Scroll down exactly one row, VERIFYING the list actually moved (the drag right
    after an exit is sometimes absorbed). Returns False if it can't move — i.e. we've
    hit the bottom of the list, which is the clean stop signal."""
    before = await m.screenshot()
    for attempt in range(4):
        await m.drag(DRAG_FX, 0.60, 0.60 - ROW_PITCH * (1.0 + 0.15*attempt), hold=0.30)
        await m.wait_stable()
        after = await m.screenshot()
        if _changed(before, after):
            return True
    return False

async def maka_analyze(m, uuid, timeout=100):
    """Trigger MAKA analysis on the currently-open un-analyzed game and wait for the
    report to land in the heap. QUOTA-SAFE: only clicks 'Start Analysis' once the MAKA
    panel is confirmed open (via classification), so a misfired diamond-click can never
    spend an attempt. Returns 'analyzed' | 'panel-failed' | 'timeout'. Leaves the client
    back on the replay.

    `uuid` must be the game actually ON SCREEN. Polling is uuid-agnostic (waits for ANY
    new seer report to appear in the heap), so a stale extractor pick can't cause a false
    210s timeout — whatever game was analyzed lands its report and we detect it."""
    await m.wait_stable(timeout=8)                  # settle after the extractor's heap scan
    opened = False
    for _ in range(3):
        await m.click(MAKA_FX, MAKA_FY)             # open the MAKA panel
        if await m.wait_for('makapanel', timeout=8):
            opened = True; break
        await m.wait_stable(timeout=4)              # let a mis-timed frame settle, retry
    if not opened:
        return 'panel-failed'
    await m.wait_stable(timeout=5)                  # let the panel fully render
    await m.click(START_FX, START_FY)               # spend one attempt
    await m.wait_stable(timeout=8)
    t0 = asyncio.get_event_loop().time()
    result = 'timeout'
    while asyncio.get_event_loop().time() - t0 < timeout:
        now = await _seer_uuids(m)
        if uuid in now:                             # THIS game's report has landed (not just any)
            result = 'analyzed'; break
        await asyncio.sleep(4)
    # dismiss the panel so the normal exit-door flow works
    if await m.classify() == 'makapanel':
        await m.click(PANELX_FX, PANELX_FY)
        await m.wait_for('replay', timeout=8)
    return result

async def _seer_uuids(m):
    """Set of game uuids that currently have a Seer/MAKA report in the WASM heap."""
    js = r"""(() => {
      const h=unityInstance.Module.HEAPU8, N=h.length;
      const isu=c=>((c>=48&&c<=57)||(c>=97&&c<=102)||c===45);
      const out=[];
      for(let i=6;i<N-50;i++){
        if(h[i]===0x0a && h[i+1]===0x2b && h[i+2+0x2b]===0x12){
          let ok=true; for(let k=0;k<0x2b;k++){ if(!isu(h[i+2+k])){ok=false;break;} }
          if(ok){ let s=''; for(let k=0;k<0x2b;k++) s+=String.fromCharCode(h[i+2+k]); out.push(s); }
        }
      }
      return JSON.stringify([...new Set(out)]);
    })()"""
    try:
        import json as _j
        return set(_j.loads(await m.eval(js)))
    except Exception:
        return set()

async def open_top_button(m):
    """Detect the top visible View button and open it; confirm we entered a replay.
    Using the DETECTED y (not a fixed fraction) makes this immune to scroll drift.
    Returns True if a replay opened."""
    buttons = m.find_view_buttons(await m.screenshot())
    if not buttons:
        return False
    await m.click(VIEW_FX, buttons[0])
    if await m.wait_for('replay', timeout=18):
        return True
    await m.wait_for('list', timeout=6)
    return False

def known_uuids():
    """UUIDs already on disk, so we skip re-extracting them."""
    saved = set()
    for fp in glob.glob(os.path.join(str(MX.OUT_DIR), "*.json")):
        try:
            t = json.load(open(fp))
            u = next((x for x in t.get("title", []) if re.match(r'\d{6}-[0-9a-f]{8}', str(x))), None)
            if u: saved.add(u)
        except Exception: pass
    return saved

async def main():
    argv = sys.argv[1:]
    # MAKA and Mortal are ON by default for every extract. MAKA reads free for already-
    # analyzed games; for un-analyzed ones it spends daily quota, UNLIMITED by default until
    # the quota runs out (3 consecutive analyze failures => quota gone, auto-analyze off).
    # --analyze N caps the spend; --no-analyze reads MAKA only (never spends). --no-mortal
    # skips the local Mortal sidecar. Mortal has no quota, so it always runs when enabled.
    if "--no-analyze" in argv:
        analyze_budget = 0
    elif "--analyze" in argv:
        analyze_budget = int(argv[argv.index("--analyze")+1])
    else:
        analyze_budget = 10**9                       # "unlimited" — real cap is the daily quota
    do_mortal = "--no-mortal" not in argv
    maka_fails = 0
    saved = set() if "--from-scratch" in argv else known_uuids()

    m = await mjs_ui.MJS.connect()

    # --current: just the replay already open on screen (single game). No list walking, and
    # no on-disk dedup — the user pointed at THIS game, so process it even if already saved
    # (re-extract, MAKA-analyze if un-analyzed, rewrite the Mortal sidecar idempotently).
    if "--current" in argv:
        if not await m.wait_for('replay', timeout=10):
            print("bulk: --current needs a replay open on screen."); await m.close(); return
        uuid, path, maka, *_ = await process_open_game(m, set(), analyze_budget, maka_fails, do_mortal)
        if uuid:
            print(f"[1] {uuid}  ->  {os.path.basename(path) if path else '(no file?)'}  |  {maka}", flush=True)
        else:
            print(f"bulk: no game found in the heap.  |  {maka}", flush=True)
        await m.close(); return

    max_games = int(argv[argv.index("--max")+1]) if "--max" in argv else 200
    print(f"bulk: {len(saved)} game(s) already on disk will be skipped; cap={max_games}  "
          f"(maka-analyze={'off' if not analyze_budget else ('∞' if analyze_budget >= 10**8 else analyze_budget)}, "
          f"mortal={'on' if do_mortal else 'off'})", flush=True)
    if not await m.wait_for('list', timeout=10):
        print("bulk: not on the Log list — open Records -> Overview first."); await m.close(); return
    print("bulk: flinging to top…", flush=True)
    await scroll_to_top(m)

    got = 0; dup_streak = 0; cycle = 0
    while got < max_games:
        if await open_top_button(m):
            uuid, path, maka, analyze_budget, maka_fails = await process_open_game(
                m, saved, analyze_budget, maka_fails, do_mortal)
            await m.click(EXIT_FX, EXIT_FY)
            await m.wait_for('list', timeout=25)
            if uuid and uuid not in saved:
                saved.add(uuid); got += 1; dup_streak = 0
                print(f"[{got}] {uuid}  ->  {os.path.basename(path) if path else '(no file?)'}  |  {maka}", flush=True)
            else:
                dup_streak += 1
                print(f"  (cycle {cycle}: {uuid or 'no new game'} — already have it)", flush=True)
        else:
            dup_streak += 1
            print(f"  (cycle {cycle}: no View button / didn't open)", flush=True)
        if not await scroll_one_row(m):
            print("bulk: reached bottom of the list.", flush=True); break
        if dup_streak >= 6:   # many rows in a row already-saved: an incremental re-run
            print("bulk: 6 consecutive already-saved rows — stopping (nothing new here).", flush=True); break
        cycle += 1

    await m.close()
    print(f"bulk: done. extracted {got} new game(s) this run; {len(saved)} total known.", flush=True)

if __name__ == "__main__":
    asyncio.run(main())
