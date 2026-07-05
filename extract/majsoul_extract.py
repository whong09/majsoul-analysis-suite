#!/usr/bin/env python3
"""
One-command Mahjong Soul replay extractor.

Connects to Chrome (CDP :9223), scans the WASM heap for the currently-loaded
replay, decodes it to tenhou.net/6 JSON, and saves it under a human-readable
name built from the game head:

    {YYYY-MM-DD}_{HHMM}_{room}_{placement}_player{seat}.json

Your seat is auto-detected by matching MY_ACCOUNT_ID against the head's account
list — no need to read the dial. Run with the replay open (any point is fine):

    python3 majsoul_extract.py [--room Silver-Room-East]

The room (Bronze/Silver/Gold/Jade/Throne, East/South) is read from the game head's
mode_id; pass --room only to override that.

Requires: pip3 install websockets --break-system-packages
"""
import asyncio, json, struct, re, datetime, base64, sys, os, urllib.request
from pathlib import Path

try:
    import websockets
except ImportError:
    sys.exit("pip3 install websockets --break-system-packages")

# MAKA ("Seer") per-hand analysis: the fetchSeerReport protobuf persists in the WASM
# heap after a replay loads, so we read it straight out of the heap (see scan_seer).
from seer_decode import decode_bare_report, maka_summary

# ---- your identity (from the game head account list) -------------------------
# Set MJS_ACCOUNT_ID to your own Mahjong Soul account id so the extractor can tell which
# seat is you (it's printed in the head account list on each run — find yours there once).
MY_ACCOUNT_ID = int(os.environ.get("MJS_ACCOUNT_ID", "0"))
CDP_PORT = int(os.environ.get("MJS_CDP_PORT", "9223"))
OUT_DIR = Path(os.environ.get("MJS_OUT_DIR", str(Path.home() / "majsoul-logs"))).expanduser()

# ---- protobuf helpers --------------------------------------------------------
def dvar(d, o):
    r = s = 0
    while o < len(d):
        b = d[o]; o += 1; r |= (b & 0x7f) << s; s += 7
        if not (b & 0x80): break
    return r, o

def svar(v):
    return v - 2**64 if v >= 2**63 else v

def proto(d, start=0, end=None):
    if end is None: end = len(d)
    out = []; o = start
    while o < end:
        try: tag, o = dvar(d, o)
        except: break
        fn = tag >> 3; wt = tag & 7
        if wt == 0: v, o = dvar(d, o); out.append((fn, 'v', v))
        elif wt == 2:
            ln, o2 = dvar(d, o)
            if o2 + ln > end: break
            out.append((fn, 'b', d[o2:o2+ln])); o = o2 + ln
        elif wt == 5: o += 4
        elif wt == 1: o += 8
        else: break
    return out

def pv(b):
    r = []; o = 0
    while o < len(b): v, o = dvar(b, o); r.append(v)
    return r

def find_records(data):
    marker = b'.lq.Record'; res = []; pos = 0
    while True:
        idx = data.find(marker, pos)
        if idx == -1: break
        for back in range(2, 6):
            cs = idx - back
            if cs < 0: continue
            if data[cs] == 0x0A:
                length, al = dvar(data, cs + 1)
                if al == idx:
                    turl = data[idx:idx+length].decode('ascii', 'replace')
                    ps = idx + length
                    if ps < len(data) and data[ps] == 0x12:
                        pl, pds = dvar(data, ps + 1)
                        if pds + pl <= len(data):
                            res.append({'t': turl, 'p': data[pds:pds+pl]})
                    break
        pos = idx + 1
    return res

# ---- record decoders (subset needed for tenhou6) -----------------------------
def d_newround(p):
    r = {'chang':0,'ju':0,'ben':0,'scores':[],'liqibang':0,'tiles':[[],[],[],[]],'dora':''}
    for fn,ty,v in proto(p):
        if fn==1 and ty=='v': r['chang']=v
        elif fn==2 and ty=='v': r['ju']=v
        elif fn==3 and ty=='v': r['ben']=v
        elif fn==5 and ty=='b': r['scores']=[svar(x) for x in pv(v)]
        elif fn==6 and ty=='v': r['liqibang']=v
        elif fn in (7,8,9,10) and ty=='b':
            try: r['tiles'][fn-7].append(v.decode('ascii'))
            except: pass
        elif fn==16 and ty=='b':
            try: r['dora']=v.decode('ascii')
            except: pass
    return r

def d_discard(p):
    # lq.RecordDiscardTile: field 3 = is_liqi (riichi declaration), field 5 = moqie (tsumogiri)
    r={'seat':0,'tile':'','moqie':False,'riichi':False}
    for fn,ty,v in proto(p):
        if fn==1 and ty=='v': r['seat']=v
        elif fn==2 and ty=='b':
            try: r['tile']=v.decode('ascii')
            except: pass
        elif fn==3 and ty=='v': r['riichi']=bool(v)
        elif fn==5 and ty=='v': r['moqie']=bool(v)
    return r

def d_deal(p):
    r={'seat':0,'tile':''}
    for fn,ty,v in proto(p):
        if fn==1 and ty=='v': r['seat']=v
        elif fn==2 and ty=='b':
            try: r['tile']=v.decode('ascii')
            except: pass
    return r

def d_cpg(p):
    r={'seat':0,'type':0,'tiles':[]}
    for fn,ty,v in proto(p):
        if fn==1 and ty=='v': r['seat']=v
        elif fn==2 and ty=='v': r['type']=v
        elif fn==3 and ty=='b':
            try: r['tiles'].append(v.decode('ascii'))
            except: pass
    return r

def d_angang(p):
    r={'seat':0,'type':0,'tiles':''}
    for fn,ty,v in proto(p):
        if fn==1 and ty=='v': r['seat']=v
        elif fn==2 and ty=='v': r['type']=v
        elif fn==3 and ty=='b':
            try: r['tiles']=v.decode('ascii')
            except: pass
    return r

def d_hule(p):
    r={'hules':[],'delta':[],'new':[],'doras':[],'uradora':[]}
    for fn,ty,v in proto(p):
        if fn==1 and ty=='b':
            h={'seat':0,'hand':[],'melds':[],'hu':'','zimo':False,'pts':0,'doras':[],'uradora':[]}
            for a,b,c in proto(v):
                if a==1 and b=='b':
                    try: h['hand'].append(c.decode('ascii'))
                    except: pass
                elif a==2 and b=='b':
                    try: h['melds'].append(c.decode('ascii'))
                    except: pass
                elif a==3 and b=='b':
                    try: h['hu']=c.decode('ascii')
                    except: pass
                elif a==4 and b=='v': h['seat']=c
                elif a==5 and b=='v': h['zimo']=bool(c)
                elif a==8 and b=='b':   # dora indicators (incl. kan-dora), repeated
                    try: h['doras'].append(c.decode('ascii'))
                    except: pass
                elif a==9 and b=='b':   # uradora indicators (revealed to riichi winners), repeated
                    try: h['uradora'].append(c.decode('ascii'))
                    except: pass
                elif a==15 and b=='v': h['pts']=c
            r['hules'].append(h)
        elif fn==3 and ty=='b': r['delta']=[svar(x) for x in pv(v)]
        elif fn==5 and ty=='b': r['new']=[svar(x) for x in pv(v)]
    # dora/uradora indicators are shared across double-ron winners; take first non-empty
    r['doras']=next((h['doras'] for h in r['hules'] if h['doras']), [])
    r['uradora']=next((h['uradora'] for h in r['hules'] if h['uradora']), [])
    return r

def d_notile(p):
    r={'delta':[],'new':[]}
    for fn,ty,v in proto(p):
        if fn==3 and ty=='b': r['delta']=[svar(x) for x in pv(v)]
        elif fn==5 and ty=='b': r['new']=[svar(x) for x in pv(v)]
    return r

DEC = {'.lq.RecordNewRound':d_newround,'.lq.RecordDiscardTile':d_discard,
       '.lq.RecordDealTile':d_deal,'.lq.RecordChiPengGang':d_cpg,
       '.lq.RecordAnGangAddGang':d_angang,'.lq.RecordHule':d_hule,
       '.lq.RecordNoTile':d_notile}

def infer_from(win, delta):
    if not delta: return win
    mn=0; fs=win
    for i,dd in enumerate(delta):
        if i!=win and dd<mn: mn=dd; fs=i
    return fs

def to_tenhou6(recs, names):
    t={"title":["Mahjong Soul"],"name":names,"rule":{"disp":"East","aka":1},"log":[]}
    cur=None; draws=[[],[],[],[]]; disc=[[],[],[],[]]
    def flush(res=None, doras=None, uradora=None):
        nonlocal cur,draws,disc
        if cur is None: return
        s100=[s//100 for s in cur['scores']]
        ri=[cur['chang']*4+cur['ju'],cur['ben'],cur['liqibang']]+s100
        # dora indicators: prefer the full list from the win record (includes kan-dora);
        # fall back to the round-start opening indicator otherwise.
        dlist = doras if doras else ([cur['dora']] if cur['dora'] else [])
        e=[ri, dlist, uradora or []]
        for s in range(4): e+= [cur['tiles'][s],draws[s],disc[s]]
        if res: e.append(res)
        t['log'].append(e); cur=None; draws=[[],[],[],[]]; disc=[[],[],[],[]]
    for m in recs:
        dec=DEC.get(m['t']);
        if not dec: continue
        r=dec(m['p'])
        if m['t']=='.lq.RecordNewRound': flush(); cur=r; draws=[[],[],[],[]]; disc=[[],[],[],[]]
        elif m['t']=='.lq.RecordDealTile': draws[r['seat']].append(r['tile'])
        elif m['t']=='.lq.RecordDiscardTile':
            # r = tsumogiri (moqie); t = riichi declared on a tedashi; tsumogiri wins
            # for a tsumogiri-riichi (written plain 'r', per the analysis spec)
            pre='r' if r['moqie'] else ('t' if r['riichi'] else '')
            disc[r['seat']].append(pre+r['tile'])
        elif m['t']=='.lq.RecordChiPengGang':
            ti=r['tiles']; mt=r['type']
            cs=(f"c{ti[0]},{ti[1]},{ti[2]}" if mt==0 else f"p{ti[0]},{ti[1]},{ti[2]}" if mt==1 else f"m{','.join(ti)}") if len(ti)>=3 else '?'
            draws[r['seat']].append(cs)
        elif m['t']=='.lq.RecordAnGangAddGang':
            disc[r['seat']].append((f"a{r['tiles']}" if r['type']==2 else f"k{r['tiles']}"))
        elif m['t']=='.lq.RecordHule':
            al=[]
            for h in r['hules']:
                fw=h['seat'] if h['zimo'] else infer_from(h['seat'],r['delta'])
                al.append({'who':h['seat'],'fromWho':fw,'hand':h['hand'],'melds':h['melds'],'machi':h['hu'],'tsumo':h['zimo'],'points':h['pts']})
            flush({'agari':al,'owari':r['delta'],'sc':r['new']}, r['doras'], r['uradora'])
        elif m['t']=='.lq.RecordNoTile':
            flush({'owari':r['delta'],'sc':r['new']})
    flush()
    return t

# ---- game head parsing (uuid, start_time, accounts) --------------------------
def parse_accounts(block):
    accts=[]; i=0
    while i < len(block):
        if block[i]==0x5a:  # field 11 (accounts) LEN
            ln,o=dvar(block,i+1)
            if 0<ln<220 and o+ln<=len(block):
                body=block[o:o+ln]; aid=seat=nick=None; j=0
                while j<len(body):
                    tag,j=dvar(body,j); fn=tag>>3; wt=tag&7
                    if wt==0:
                        v,j=dvar(body,j)
                        if fn==1: aid=v
                        elif fn==2: seat=v
                    elif wt==2:
                        l2,j2=dvar(body,j)
                        if j2+l2<=len(body):
                            val=body[j2:j2+l2]; j=j2+l2
                            if fn==3:
                                try: nick=val.decode('utf-8')
                                except: nick=None
                        else: break
                    elif wt==5: j+=4
                    elif wt==1: j+=8
                    else: break
                if aid is not None and nick is not None and seat is not None:
                    accts.append((seat,aid,nick))
                i=o+ln; continue
        i+=1
    # dedupe keep first per seat
    seen={};
    for s,a,n in accts:
        if s not in seen: seen[s]=(a,n)
    return {s:seen[s] for s in seen}

# mode_id -> room label. mode_id lives in the head's GameConfig.meta and encodes
# both the ranked room and the length. 4-player rooms step by 3 (Bronze 2/3,
# Silver 5/6, Gold 8/9, Jade 11/12, Throne 15/16); 3-player rooms are 21-26.
# (Anchors — Gold 8/9, Jade 11/12, Throne 15/16, 3p 21-26 — cross-checked against
#  amae-koromo's GameMode enum; Bronze/Silver follow the same +3 grid.)
ROOM_MODES = {
    2:  "Bronze-Room-East",   3:  "Bronze-Room-South",
    5:  "Silver-Room-East",   6:  "Silver-Room-South",
    8:  "Gold-Room-East",     9:  "Gold-Room-South",
    11: "Jade-Room-East",     12: "Jade-Room-South",
    15: "Throne-Room-East",   16: "Throne-Room-South",
    21: "Gold-Room-East-3p",  22: "Gold-Room-South-3p",
    23: "Jade-Room-East-3p",  24: "Jade-Room-South-3p",
    25: "Throne-Room-East-3p",26: "Throne-Room-South-3p",
}

def parse_config(block, hstart):
    """From the RecordGame head, read (mode_id, length) out of GameConfig (field 5):
    config.mode (f2).f1 = round length (1=East, 2=South); config.meta (f3).f2 =
    mode_id (the ranked room+length id). Either may be None (e.g. friendly rooms)."""
    mode_id=length=None
    for fn,ty,v in proto(block, hstart, min(len(block), hstart+2000)):
        if fn==5 and ty=='b':
            for cfn,cty,cv in proto(v):
                if cfn==2 and cty=='b':          # GameMode
                    for mfn,mty,mv in proto(cv):
                        if mfn==1 and mty=='v': length=mv
                elif cfn==3 and cty=='b':        # GameMetaData
                    for gfn,gty,gv in proto(cv):
                        if gfn==2 and gty=='v': mode_id=gv
            break
    return mode_id, length

def room_label(mode_id, length):
    """Map a parsed (mode_id, length) to a filename-safe room label. Unknown mode
    ids keep the raw number (so the table is easy to extend) and still get the
    East/South suffix from `length` when available."""
    if mode_id in ROOM_MODES: return ROOM_MODES[mode_id]
    ew = {1:"East", 2:"South"}.get(length)
    if mode_id: return f"Room{mode_id}" + (f"-{ew}" if ew else "")
    if ew:      return f"Room-{ew}"
    return None

def parse_head(block):
    m=re.search(rb'\d{6}-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', block)
    if not m: return None
    uuid=m.group().decode(); hstart=m.start()-2
    # RecordGame head: uuid=field1, start_time=field2, end_time=field3. The client's
    # Log list shows the END time, so we capture both and label files by end_time (so a
    # file cross-references to its lobby row); start_time is kept for reference.
    start_time=end_time=None
    for fn,ty,v in proto(block, hstart, min(len(block), hstart+400)):
        if ty=='v' and 1_700_000_000 < v < 1_900_000_000:
            if fn==2 and start_time is None: start_time=v
            elif fn==3 and end_time is None: end_time=v
        if start_time is not None and end_time is not None: break
    mode_id,length=parse_config(block, hstart)
    return {'uuid':uuid,'start_time':start_time,'end_time':end_time,
            'accounts':parse_accounts(block),
            'mode_id':mode_id,'room':room_label(mode_id, length)}

# ---- CDP -------------------------------------------------------------------
def page_ws():
    js=json.load(urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json"))
    for t in js:
        if t.get('type')=='page' and 'mahjongsoul' in t.get('url',''):
            return t['webSocketDebuggerUrl']
    sys.exit("No MahjongSoul tab found on CDP :%d" % CDP_PORT)

SCAN_JS = r"""
(() => {
  const heap = unityInstance.Module.HEAPU8, N = heap.length;
  const pat=[0x0a,0x12,0x2e,0x6c,0x71,0x2e,0x52,0x65,0x63,0x6f,0x72,0x64,0x4e,0x65,0x77,0x52,0x6f,0x75,0x6e,0x64,0x12];
  const m=[];
  for(let i=0;i<N-pat.length;i++){let f=true;for(let j=0;j<pat.length;j++){if(heap[i+j]!==pat[j]){f=false;break;}}if(f)m.push(i);}
  if(!m.length) return JSON.stringify({error:'no rounds'});
  const cl=[]; let cur=[m[0]];
  for(let i=1;i<m.length;i++){if(m[i]-m[i-1]>100000){cl.push(cur);cur=[m[i]];}else cur.push(m[i]);}
  cl.push(cur);
  const tgt=cl.filter(c=>c.length>=1&&c.length<=16);  // 1 = early tobi, up to a full south game
  if(!tgt.length) return JSON.stringify({error:'no round records in heap', sizes: cl.map(c=>c.length)});
  // stash every 4-8 cluster: records(+40k) and the head region just before it.
  // The heap holds stale games too; picking the actively-viewed one is done in Python.
  const out=[];
  for(const ch of tgt){
    const start=ch[0], end=Math.min(ch[ch.length-1]+40000, N);
    const sl=heap.slice(start,end); let b=''; for(let i=0;i<sl.length;i++) b+=String.fromCharCode(sl[i]);
    const hlo=Math.max(0,start-30000); const hs=heap.slice(hlo,start);
    let hb=''; for(let i=0;i<hs.length;i++) hb+=String.fromCharCode(hs[i]);
    out.push({start:start, rec:btoa(b), head:btoa(hb)});
  }
  window.__clusters = out;
  return JSON.stringify({count: out.length});
})()
"""

async def evaluate(ws, expr, idn, await_promise=False):
    await ws.send(json.dumps({"id":idn,"method":"Runtime.evaluate","params":{"expression":expr,"returnByValue":True,"awaitPromise":await_promise,"timeout":180000}}))
    while True:
        m=json.loads(await ws.recv())
        if m.get("id")==idn: return m

async def get_var(ws, name, idn):
    m=await evaluate(ws, f"window.{name}", idn)
    return m['result']['result']['value']

# ---- MAKA / Seer report from the heap ----------------------------------------
def _seer_scan_js(uuid):
    """JS to find the game's Seer report in HEAPU8. The report is a length-delimited
    field (0x12 <len> Report), and Report starts with its uuid (0x0a <len> uuid) then
    the decisions (0x12 ...). We match that anchor for THIS uuid, then back up to the
    enclosing 0x12 wrapper to read the exact report length so we slice it precisely."""
    uu = ",".join(str(b) for b in uuid.encode())
    return (r"""
(() => {
  const h=unityInstance.Module.HEAPU8, N=h.length;
  const uu=[%s], L=uu.length;
  for(let i=6;i<N-L-4;i++){
    if(h[i]===0x0a && h[i+1]===L && h[i+2+L]===0x12){
      let ok=true; for(let k=0;k<L;k++){ if(h[i+2+k]!==uu[k]){ ok=false; break; } }
      if(!ok) continue;
      let rl=-1;                                   // exact Report length from the wrapper
      for(let back=2; back<=6; back++){
        if(h[i-back]===0x12){
          let p=i-back+1, len=0, sh=0, good=true;
          while(p<=i){ const bb=h[p++]; len|=(bb&0x7f)<<sh; sh+=7; if(!(bb&0x80)) break; if(sh>35){good=false;break;} }
          if(good && p===i && len>0){ rl=len; break; }
        }
      }
      const end = rl>0 ? Math.min(N, i+rl) : Math.min(N, i+150000);
      let s=''; for(let j=i;j<end;j++) s+=String.fromCharCode(h[j]);
      return JSON.stringify({off:i, exact: rl>0, win:btoa(s)});
    }
  }
  return JSON.stringify({off:-1});
})()
""" % uu)

async def scan_seer(ws, uuid, idn, retries=3, delay=1.5):
    """Read & decode this game's MAKA/Seer report from the heap. Returns a report dict
    (uuid, decisions, rounds) or None if the game hasn't been MAKA-analyzed.

    Retries briefly: right after a replay opens, the fetchSeerReport response for an
    already-analyzed game may still be in flight, so a single read can miss it and
    falsely report 'none'. We re-scan a few times before concluding it's un-analyzed."""
    for attempt in range(retries):
        try:
            m = await evaluate(ws, _seer_scan_js(uuid), idn + attempt)
            r = json.loads(m['result']['result']['value'])
        except Exception:
            r = None
        if r and r.get('off', -1) >= 0:
            return decode_bare_report(base64.b64decode(r['win']))
        if attempt < retries - 1:
            await asyncio.sleep(delay)
    return None

# ---- fragmented-heap fallback ------------------------------------------------
# When the client keeps replay records as scattered individual allocations rather
# than one contiguous RecordGame stream (seen after some load paths / client
# builds), the primary cluster scan finds RecordNewRound markers with no adjacent
# actions and reports "no ending". We can't reliably re-thread turn order from
# scattered allocations, but the round-ending records (Hule/NoTile) each carry
# old/delta/new score vectors, so we CAN chain them to recover final scores and
# placement. This scan grabs every Hule/NoTile payload plus a few head windows.
SCAN_ALL_JS = r"""
(() => {
  const heap = unityInstance.Module.HEAPU8, N = heap.length;
  const match=(i,s)=>{for(let k=0;k<s.length;k++){if(heap[i+k]!==s.charCodeAt(k))return false;}return true;};
  const b64=(a)=>{let b='';for(let i=0;i<a.length;i++)b+=String.fromCharCode(a[i]);return btoa(b);};
  const endings=[]; const heads=[];
  for(let i=1;i<N-40;i++){
    if(heap[i-1]!==0x0a) continue;             // field1 (type_url) LEN tag
    const len=heap[i];
    if(len<10||len>40) continue;
    if(!match(i+1,'.lq.Record')) continue;
    let name=''; for(let k=i+1;k<i+1+len;k++) name+=String.fromCharCode(heap[k]);
    if(name.indexOf('.',10)!==-1) continue;    // skip descriptor field-name strings
    if(heap[i+1+len]!==0x12) continue;         // field2 (payload) tag must follow
    let p=i+2+len, pl=0, sh=0;                 // read payload length varint
    while(p<N){const bb=heap[p++]; pl|=(bb&0x7f)<<sh; sh+=7; if(!(bb&0x80))break; if(sh>35){pl=0;break;}}
    if(name==='.lq.RecordHule'||name==='.lq.RecordNoTile'){
      if(pl>0&&pl<200000&&p+pl<=N&&endings.length<250) endings.push({n:name, p:b64(heap.slice(p,p+pl))});
    } else if((name==='.lq.RecordGame'||name==='.lq.RecordCollectedData'||name==='.lq.RecordListEntry')&&heads.length<16){
      heads.push(b64(heap.slice(Math.max(0,i-256), Math.min(i+8192,N))));
    }
    if(endings.length>=250&&heads.length>=16) break;
  }
  window.__frag={endings,heads};
  return JSON.stringify({endings:endings.length, heads:heads.length});
})()
"""

async def scan_fragmented(url):
    async with websockets.connect(url, max_size=None, open_timeout=15) as ws:
        await evaluate(ws, SCAN_ALL_JS, 10)
        return await get_var(ws, "__frag", 11)

def recover_final_scores(frag):
    """From scattered round-ending records, dedupe, chain old<-new, and return
    (final_scores, [candidate finals]). Chain: round k+1's old == round k's new,
    so a game's terminal round is the one whose `new` is nobody else's `old`."""
    endings=[]; seen=set()
    for e in frag['endings']:
        pay=base64.b64decode(e['p'])
        d = d_hule(pay) if e['n']=='.lq.RecordHule' else d_notile(pay)
        if len(d['new'])!=4 or len(d['delta'])!=4: continue
        key=(tuple(d['new']), tuple(d['delta']))
        if key in seen: continue
        seen.add(key)
        d['old']=tuple(n-dd for n,dd in zip(d['new'],d['delta']))
        endings.append(d)
    if not endings: return None, []
    old_set={e['old'] for e in endings}
    by_new={tuple(e['new']):e for e in endings}
    terminals=[e for e in endings if tuple(e['new']) not in old_set] or endings
    def chain_len(e):
        n=1; cur=e; guard=set()
        while cur['old'] in by_new and cur['old'] not in guard:
            guard.add(cur['old']); cur=by_new[cur['old']]; n+=1
        return n
    # longest chain wins (most rounds); prefer a busted game (tobi) then top score
    terminals.sort(key=lambda e:(chain_len(e), any(s<0 for s in e['new']), max(e['new'])), reverse=True)
    return terminals[0]['new'], [t['new'] for t in terminals]

def recover_head(frag):
    for hb in frag['heads']:
        h=parse_head(base64.b64decode(hb))
        if h and any(a==MY_ACCOUNT_ID for (a,n) in h['accounts'].values()): return h
    for hb in frag['heads']:
        h=parse_head(base64.b64decode(hb))
        if h and h['accounts']: return h
    return None

async def main():
    room_override = sys.argv[sys.argv.index("--room")+1] if "--room" in sys.argv else None
    url = page_ws()
    async with websockets.connect(url, max_size=None, open_timeout=15) as ws:
        print("Scanning heap...")
        m = await evaluate(ws, SCAN_JS, 1)
        info = json.loads(m['result']['result']['value'])
        if info.get('error'): sys.exit("Scan failed: %s" % info)
        clusters = await get_var(ws, "__clusters", 2)
        print(f"  found {len(clusters)} candidate game cluster(s)")

    # Group clusters by game (East-1 signature) so stale cached games are separated;
    # the actively-viewed game is re-decoded on every navigation, so it has the most
    # copies. Tie-break by freshest (highest heap address).
    def signature(rec):
        for x in rec:
            if x['t']=='.lq.RecordNewRound':
                r=DEC[x['t']](x['p'])
                return (r['dora'], tuple(sorted(r['tiles'][0])))
        return None
    groups={}
    for c in clusters:
        recs=find_records(base64.b64decode(c['rec']))
        sig=signature(recs)
        if sig is None: continue
        groups.setdefault(sig, []).append((c, recs))
    if not groups: sys.exit("No decodable game in heap")
    def has_final(recs):
        return any(x['t'] in ('.lq.RecordHule','.lq.RecordNoTile') and DEC[x['t']](x['p'])['new'] for x in recs)
    def group_uuid(mem):
        for c,_ in reversed(mem):
            h=parse_head(base64.b64decode(c['head']))
            if h and h.get('uuid'): return h['uuid']
        return None
    # --fresh (bulk-nav): the just-opened replay is the finished game we haven't saved
    # yet, so exclude already-seen uuids (via --skip) and take the freshest remaining.
    # This is robust to a previously-opened game still holding more heap copies.
    fresh = "--fresh" in sys.argv
    skip = set(sys.argv[sys.argv.index("--skip")+1].split(",")) if "--skip" in sys.argv else set()
    if fresh:
        cands=[s for s in groups if any(has_final(r) for _,r in groups[s])
               and group_uuid(groups[s]) not in skip]
        if not cands: sys.exit("no new finished game in heap (all seen, or none reached its end yet)")
        best_sig=max(cands, key=lambda s: max(c['start'] for c,_ in groups[s]))
    elif skip:
        # bulk-nav: exclude already-seen games and pick only among FINISHED, not-yet-
        # saved games, so we never fall through to the fragmented recovery on a game
        # that is really just a stale copy. Clean exit if nothing new is loaded.
        fin = [s for s in groups if group_uuid(groups[s]) not in skip
               and any(has_final(r) for _,r in groups[s])]
        if not fin: sys.exit("BULK: no new finished game in heap (all seen / none loaded)")
        best_sig=max(fin, key=lambda s:(len(groups[s]), max(c['start'] for c,_ in groups[s])))
    else:
        # winner: a FINISHED game beats an unfinished/noise fragment; then most copies, then freshest
        best_sig=max(groups, key=lambda s:(any(has_final(r) for _,r in groups[s]),
                                           len(groups[s]), max(c['start'] for c,_ in groups[s])))
    members=sorted(groups[best_sig], key=lambda cr: cr[0]['start'])
    # prefer the freshest copy that actually reached the game end
    chosen, recs = next(((c,r) for c,r in reversed(members) if has_final(r)), members[-1])
    # The head sits before only SOME copies; use whichever parses with our account.
    head=None
    for c,_ in reversed(members):
        h=parse_head(base64.b64decode(c['head']))
        if h and any(a==MY_ACCOUNT_ID for (a,n) in h['accounts'].values()):
            head=h; break
    if head is None:
        for c,_ in reversed(members):
            h=parse_head(base64.b64decode(c['head']))
            if h: head=h; break
    print(f"  picked game with {len(members)} copies (of {len(groups)} distinct games in heap)")

    finals=None
    for x in recs:
        if x['t'] in ('.lq.RecordHule','.lq.RecordNoTile'):
            r=DEC[x['t']](x['p'])
            if r['new']: finals=r['new']
    if finals is None:
        # No contiguous finished game. Either the replay hasn't been played to the
        # end, or the records are fragmented across the heap (individual allocations
        # instead of one stream). Try to recover final scores from scattered
        # round-ending records so the user at least gets the result + placement.
        print("  no contiguous finished game in heap; trying fragmented-heap recovery...")
        frag = await scan_fragmented(url)
        ffinals, cands = recover_final_scores(frag)
        if ffinals is None:
            sys.exit("This game hasn't reached its end in the replay yet — the final scores\n"
                     "aren't in the heap. Play/scrub the replay to the last round, then re-run.")
        fhead = recover_head(frag) or head
        fnames=["Player0","Player1","Player2","Player3"]; fseat=None
        if fhead:
            for seat,(aid,nick) in fhead['accounts'].items():
                if 0<=seat<4: fnames[seat]=nick
                if aid==MY_ACCOUNT_ID: fseat=seat
        print("\nWARN: the replay records are FRAGMENTED across the heap (not a contiguous\n"
              "stream), so turn-by-turn play can't be reliably reconstructed and no\n"
              "analyzable log was written. Recovered the final result only:\n")
        if fseat is not None:
            fplace = sorted(range(4), key=lambda s:(-ffinals[s], s)).index(fseat)+1
            fsuf = {1:'1st',2:'2nd',3:'3rd',4:'4th'}.get(fplace,'?')
            print(f"  you = seat{fseat} ({fnames[fseat]}) = {ffinals[fseat]} pts = {fsuf} place")
        else:
            print("  (couldn't detect your seat — placement unavailable)")
        print(f"  final scores: {ffinals}")
        if len(cands) > 1: print(f"  (other terminal candidates seen: {cands[1:]})")
        if fhead and fhead.get('uuid'): print(f"  uuid: {fhead['uuid']}")
        print("\nTo get a full, analyzable log: reload the replay from your replay list and\n"
              "let it play/skip through to the end once, then re-run — that rebuilds the\n"
              "contiguous record buffer this extractor needs.")
        sys.exit(0)

    # seat + names
    names=["Player0","Player1","Player2","Player3"]; self_seat=None; uuid=None; start=None
    if head:
        uuid=head['uuid']
        start=head.get('end_time') or head.get('start_time')   # label by END (lobby time)
        for seat,(aid,nick) in head['accounts'].items():
            if 0<=seat<4: names[seat]=nick
            if aid==MY_ACCOUNT_ID: self_seat=seat
    if self_seat is None:
        if head and head.get('accounts'):
            print("  head accounts (seat -> nickname -> account_id):")
            for seat,(aid,nick) in sorted(head['accounts'].items()):
                print(f"    seat{seat}: {nick}  ({aid}){'  <-- you' if aid==MY_ACCOUNT_ID else ''}")
        if MY_ACCOUNT_ID == 0:
            print("WARN: MJS_ACCOUNT_ID not set — can't tell which seat is you.\n"
                  "      Find your account_id in the list above and set MJS_ACCOUNT_ID.")
        else:
            print("WARN: your account_id wasn't in this game's head; defaulting to seat 0")
        self_seat=0

    room = room_override or (head.get('room') if head else None) or "Unknown-Room"

    # MAKA/Seer per-hand analysis (if this game was analyzed): read it from the heap.
    maka = None
    if uuid and "--no-maka" not in sys.argv:
        try:
            async with websockets.connect(url, max_size=None, open_timeout=15) as sws:
                rep = await scan_seer(sws, uuid, 30)
            if rep and rep.get('rounds'):
                maka = {"summary": maka_summary(rep), "rounds": rep["rounds"],
                        "decisions": rep["decisions"]}
        except Exception:
            maka = None

    place = sorted(range(4), key=lambda s:(-finals[s], s)).index(self_seat)+1 if finals else 0
    suf = {1:'1st-place',2:'2nd-place',3:'3rd-place',4:'4th-place'}.get(place,'NA')

    if start:
        dt=datetime.datetime.fromtimestamp(start)
        date_s=dt.strftime('%Y-%m-%d'); time_s=dt.strftime('%H%M')
    elif uuid:
        date_s='20'+uuid[:2]+'-'+uuid[2:4]+'-'+uuid[4:6]; time_s='0000'
    else:
        dt=datetime.datetime.now(); date_s=dt.strftime('%Y-%m-%d'); time_s=dt.strftime('%H%M')

    fname=f"{date_s}_{time_s}_{room}_{suf}_player{self_seat}.json"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    t=to_tenhou6(recs, names)
    t['title']=["Mahjong Soul", room.replace('-',' ')] + ([uuid] if uuid else [])
    if self_seat is not None: t['name'][self_seat]=names[self_seat]+" (you)"
    if head and head.get('start_time'): t['start_time']=head['start_time']  # game begin
    if head and head.get('end_time'):   t['end_time']=head['end_time']      # = lobby time
    if maka: t['maka']=maka
    (OUT_DIR/fname).write_text(json.dumps(t,indent=2,ensure_ascii=False))
    # rename-safe: drop any other file for the SAME game saved under a previous name
    # scheme (e.g. labelled by start_time before we switched to end_time).
    if uuid:
        for old in OUT_DIR.glob("*.json"):
            if old.name == fname: continue
            try:
                if uuid in json.loads(old.read_text()).get("title", []):
                    old.unlink(); print(f"  (removed stale duplicate {old.name})")
            except Exception: pass

    print(f"\nyou = seat{self_seat} ({names[self_seat]}) = {finals[self_seat] if finals else '?'} pts = {suf}")
    print(f"room : {room}" + (f"  (mode_id {head['mode_id']})" if head and head.get('mode_id') else ""))
    print(f"final: {finals}")
    if uuid: print(f"uuid : {uuid}")
    if maka:
        ms=maka['summary']
        you=ms['seat_rating'].get(self_seat)
        print(f"maka : {ms['rounds']} rounds, {ms['decisions']} decisions; seat_rating {ms['seat_rating']}"
              + (f"  (you: {you})" if you is not None else ""))
    else:
        print("maka : none (game not MAKA-analyzed)")
    print(f"\nSaved -> {OUT_DIR/fname}  ({len(t['log'])} rounds)")

if __name__=='__main__':
    asyncio.run(main())
