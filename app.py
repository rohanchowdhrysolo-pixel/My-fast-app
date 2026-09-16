import os, io, sqlite3, base64, json, math, statistics
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import Counter
import requests
from flask import Flask, jsonify, request, send_from_directory
from PIL import Image

BASE = Path(__file__).resolve().parent
DB = BASE / "data.db"
UPLOADS = BASE / "uploads"
UPLOADS.mkdir(exist_ok=True)
app = Flask(__name__, static_folder=".", static_url_path="")
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024

TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY", "").strip()
VISION_API_KEY = os.getenv("VISION_API_KEY", "").strip()
VISION_API_URL = os.getenv("VISION_API_URL", "https://api.openai.com/v1/responses").strip()
VISION_MODEL = os.getenv("VISION_MODEL", "gpt-5.6-luna").strip()

REAL_MARKETS = ["EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD", "NZD/USD", "EUR/GBP", "USD/CAD", "USD/CHF"]
OTC_MARKETS = ["EUR/USD", "GBP/USD", "USD/JPY", "NZD/USD"]
TIMEFRAMES = ["1M", "3M", "5M", "10M", "15M", "50M"]
PIP_SIZE = {"USD/JPY": 0.01}
DEFAULT_PIP = 0.0001
EST_SPREAD_PIPS = {"EUR/USD": 1.2, "GBP/USD": 1.8, "USD/JPY": 1.5, "AUD/USD": 1.6, "NZD/USD": 2.0, "EUR/GBP": 1.8, "USD/CAD": 1.8, "USD/CHF": 1.8}


def utc_now():
    return datetime.now(timezone.utc)


def db():
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("""CREATE TABLE IF NOT EXISTS candles(
        symbol TEXT, timeframe TEXT, ts TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL, source TEXT,
        PRIMARY KEY(symbol,timeframe,ts))""")
    c.execute("""CREATE TABLE IF NOT EXISTS screenshots(
        id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, timeframe TEXT, market_type TEXT, filename TEXT,
        path TEXT, width INTEGER, height INTEGER, uploaded_at TEXT, vision_status TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS analyses(
        id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, symbol TEXT, market_type TEXT,
        requested_tf TEXT, final_signal TEXT, evidence_score REAL, calibrated_win_rate REAL, validation_signals INTEGER,
        regime TEXT, data_quality TEXT, spread_pips REAL, slippage_pips REAL, reasons_json TEXT, context_json TEXT,
        screenshot_vision TEXT, outcome TEXT)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_candles ON candles(symbol,timeframe,ts)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_analyses ON analyses(symbol,created_at)")
    c.commit()
    return c


def safe_float(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except Exception:
        return None


def td_request(path, params, timeout=25):
    if not TWELVE_DATA_API_KEY:
        raise RuntimeError("TWELVE_DATA_API_KEY is not configured on the server.")
    p = dict(params)
    p["apikey"] = TWELVE_DATA_API_KEY
    url = "https://api.twelvedata.com/" + path.lstrip("/")
    r = requests.get(url, params=p, timeout=timeout)
    r.raise_for_status()
    d = r.json()
    if isinstance(d, dict) and d.get("status") == "error":
        raise RuntimeError(d.get("message", "Twelve Data error"))
    return d


def fetch_td(symbol, interval, limit=1000):
    d = td_request("time_series", {"symbol": symbol, "interval": interval, "outputsize": min(int(limit), 5000), "timezone": "UTC"})
    vals = d.get("values") or []
    out = []
    for x in reversed(vals):
        o, h, l, c = map(safe_float, (x.get("open"), x.get("high"), x.get("low"), x.get("close")))
        if None in (o, h, l, c):
            continue
        out.append({"ts": x.get("datetime"), "open": o, "high": h, "low": l, "close": c, "volume": safe_float(x.get("volume")) or 0.0})
    return out


def save_candles(symbol, timeframe, rows, source="twelvedata"):
    c = db()
    n = 0
    for x in rows:
        if not x.get("ts"): continue
        c.execute("INSERT OR REPLACE INTO candles VALUES(?,?,?,?,?,?,?,?,?)", (symbol, timeframe, x["ts"], x["open"], x["high"], x["low"], x["close"], x.get("volume", 0.0), source))
        n += 1
    c.commit(); c.close(); return n


def load_candles(symbol, timeframe, limit=2000):
    c = db()
    rows = c.execute("SELECT ts,open,high,low,close,volume FROM candles WHERE symbol=? AND timeframe=? ORDER BY ts DESC LIMIT ?", (symbol, timeframe, limit)).fetchall()
    c.close()
    return [dict(r) for r in reversed(rows)]


def parse_dt(s):
    if isinstance(s, datetime): return s
    s = str(s).replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(s)
    except Exception:
        d = datetime.strptime(str(s), "%Y-%m-%d %H:%M:%S")
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)


def aggregate(rows, minutes, source_minutes):
    """Build derived candles only from complete source buckets."""
    if not rows: return []
    expected=max(1, int(minutes/source_minutes))
    step=timedelta(minutes=minutes)
    bucket={}
    for r in rows:
        t=parse_dt(r["ts"]); epoch=int(t.timestamp())
        start_epoch=epoch-(epoch % int(step.total_seconds()))
        k=datetime.fromtimestamp(start_epoch,timezone.utc)
        bucket.setdefault(k,[]).append(r)
    out=[]
    for k in sorted(bucket):
        b=sorted(bucket[k],key=lambda x:parse_dt(x["ts"]))
        if len(b)!=expected: continue
        if any(abs((parse_dt(z["ts"])-parse_dt(a["ts"])).total_seconds()/60-source_minutes)>0.01 for a,z in zip(b,b[1:])): continue
        out.append({"ts":k.isoformat(),"open":b[0]["open"],"high":max(x["high"] for x in b),"low":min(x["low"] for x in b),"close":b[-1]["close"],"volume":sum(x.get("volume",0.0) for x in b)})
    return out


def data_quality(rows, minutes, max_stale_minutes=None):
    reasons=[]
    if len(rows) < 80: reasons.append("INSUFFICIENT_HISTORY")
    clean=[]; seen=set(); bad_ohlc=0
    for r in rows:
        try:
            t=parse_dt(r["ts"]); vals=[r["open"],r["high"],r["low"],r["close"]]
            if any(not math.isfinite(float(v)) for v in vals) or r["high"] < max(r["open"],r["close"]) or r["low"] > min(r["open"],r["close"]) or r["low"] > r["high"]:
                bad_ohlc += 1; continue
            key=t.isoformat()
            if key in seen: continue
            seen.add(key); clean.append(r)
        except Exception:
            bad_ohlc += 1
    if bad_ohlc: reasons.append("BAD_OHLC")
    gaps=0
    clean=sorted(clean,key=lambda x:parse_dt(x["ts"]))
    for a,b in zip(clean,clean[1:]):
        delta=(parse_dt(b["ts"])-parse_dt(a["ts"])).total_seconds()/60
        if delta > minutes*2.5: gaps += 1
    if gaps: reasons.append("DATA_GAPS")
    stale=False
    if clean and max_stale_minutes is not None:
        stale=(utc_now()-parse_dt(clean[-1]["ts"])).total_seconds()/60 > max_stale_minutes
        if stale: reasons.append("STALE_DATA")
    score=max(0,100-20*len(reasons)-min(20,bad_ohlc*2)-min(20,gaps*5))
    return {"status":"PASS" if not reasons else "WARN","score":score,"reasons":reasons,"rows":len(clean),"gaps":gaps,"bad_ohlc":bad_ohlc,"stale":stale}


def ema_series(a,n):
    if not a: return []
    out=[]; k=2/(n+1); e=float(a[0])
    for i,x in enumerate(a):
        e=float(x) if i==0 else float(x)*k+e*(1-k); out.append(e)
    return out


def rsi_series(a,n=14):
    if not a: return []
    out=[50.0]*len(a)
    if len(a)<=n:return out
    gains=[max(a[i]-a[i-1],0) for i in range(1,len(a))]
    losses=[max(a[i-1]-a[i],0) for i in range(1,len(a))]
    ag=sum(gains[:n])/n; al=sum(losses[:n])/n
    out[n]=100 if al==0 else 100-100/(1+ag/al)
    for i in range(n,len(gains)):
        ag=(ag*(n-1)+gains[i])/n; al=(al*(n-1)+losses[i])/n
        out[i+1]=100 if al==0 else 100-100/(1+ag/al)
    return out


def atr_series(rows,n=14):
    if not rows:return []
    tr=[]
    for i,r in enumerate(rows):
        pc=rows[i-1]["close"] if i else r["close"]
        tr.append(max(r["high"]-r["low"],abs(r["high"]-pc),abs(r["low"]-pc)))
    out=[]; v=tr[0]
    for i,x in enumerate(tr):
        v=x if i==0 else (v*(n-1)+x)/n; out.append(v)
    return out


def macd_series(a):
    e12=ema_series(a,12); e26=ema_series(a,26); line=[x-y for x,y in zip(e12,e26)]; sig=ema_series(line,9)
    return line,sig,[x-y for x,y in zip(line,sig)]


def adx_series(rows,n=14):
    if len(rows)<2:return [0.0]*len(rows)
    tr=[]; plus=[]; minus=[]
    for i,r in enumerate(rows):
        if i==0: tr.append(r["high"]-r["low"]); plus.append(0); minus.append(0); continue
        pc=rows[i-1]["close"]; up=r["high"]-rows[i-1]["high"]; dn=rows[i-1]["low"]-r["low"]
        tr.append(max(r["high"]-r["low"],abs(r["high"]-pc),abs(r["low"]-pc)))
        plus.append(up if up>dn and up>0 else 0); minus.append(dn if dn>up and dn>0 else 0)
    atr=ema_series(tr,n); p=ema_series(plus,n); m=ema_series(minus,n); dx=[]
    for a,b,c in zip(atr,p,m):
        pi=100*b/max(a,1e-12); mi=100*c/max(a,1e-12); dx.append(100*abs(pi-mi)/max(pi+mi,1e-12))
    return ema_series(dx,n)


def bollinger(a,n=20,mult=2):
    if not a:return None,None,None
    s=a[-n:] if len(a)>=n else a; mid=sum(s)/len(s); sd=statistics.pstdev(s) if len(s)>1 else 0
    return mid+mult*sd,mid,mid-mult*sd


def candle_pattern(rows):
    if len(rows)<2:return "UNKNOWN"
    c,p=rows[-1],rows[-2]; body=abs(c["close"]-c["open"]); rng=max(c["high"]-c["low"],1e-12); up=c["high"]-max(c["open"],c["close"]); lo=min(c["open"],c["close"])-c["low"]
    if body/rng<0.10:return "DOJI"
    if lo>2*body and up<body:return "HAMMER"
    if up>2*body and lo<body:return "SHOOTING_STAR"
    if c["close"]>c["open"] and c["close"]>=p["open"] and c["open"]<=p["close"]:return "BULLISH_ENGULFING"
    if c["close"]<c["open"] and c["close"]<=p["open"] and c["open"]>=p["close"]:return "BEARISH_ENGULFING"
    return "BULLISH" if c["close"]>c["open"] else "BEARISH"


def feature_snapshot(rows):
    c=[x["close"] for x in rows]; e9=ema_series(c,9); e21=ema_series(c,21); e50=ema_series(c,50); rs=rsi_series(c); at=atr_series(rows); ml,ms,mh=macd_series(c); adx=adx_series(rows)
    n=len(rows)-1; sup=min(x["low"] for x in rows[-40:]); res=max(x["high"] for x in rows[-40:]); bb_u,bb_m,bb_l=bollinger(c)
    atr_pct=at[n]/max(abs(c[n]),1e-12)*100
    slope=(e21[n]-e21[max(0,n-10)])/max(10, n)*100/max(abs(c[n]),1e-12)
    return {"price":c[n],"ema9":e9[n],"ema21":e21[n],"ema50":e50[n],"rsi14":rs[n],"atr14":at[n],"atr_pct":atr_pct,"macd":ml[n],"macd_signal":ms[n],"macd_hist":mh[n],"adx14":adx[n],"support":sup,"resistance":res,"bb_upper":bb_u,"bb_mid":bb_m,"bb_lower":bb_l,"candle":candle_pattern(rows),"ema21_slope":slope}


def regime_from(f):
    if f["adx14"]>=28 and f["ema9"]>f["ema21"]>f["ema50"] and f["ema21_slope"]>0.00002:return "STRONG_TREND_UP"
    if f["adx14"]>=28 and f["ema9"]<f["ema21"]<f["ema50"] and f["ema21_slope"]<-0.00002:return "STRONG_TREND_DOWN"
    if f["atr_pct"]>0.35 and f["adx14"]<20:return "HIGH_VOL_RANGE"
    if f["adx14"]<18:return "RANGE"
    return "TRANSITION"


def layer_signal(rows, tf):
    if len(rows)<80:return {"timeframe":tf,"signal":"NO TRADE","score":0,"reasons":["INSUFFICIENT_HISTORY"],"features":{},"regime":"UNSTABLE"}
    f=feature_snapshot(rows); reg=regime_from(f); up=down=0; ur=[]; dr=[]
    def add(side,pts,reason):
        nonlocal up,down
        if side=="UP":up+=pts;ur.append(reason)
        elif side=="DOWN":down+=pts;dr.append(reason)
    add("UP" if f["ema9"]>f["ema21"] else "DOWN",15,"EMA9/EMA21 alignment")
    add("UP" if f["ema21"]>f["ema50"] else "DOWN",10,"EMA21/EMA50 structure")
    if f["rsi14"]>=55:add("UP",10,"RSI bullish momentum")
    elif f["rsi14"]<=45:add("DOWN",10,"RSI bearish momentum")
    if f["macd_hist"]>0:add("UP",10,"MACD histogram positive")
    elif f["macd_hist"]<0:add("DOWN",10,"MACD histogram negative")
    if reg=="STRONG_TREND_UP":add("UP",15,"strong bullish regime")
    elif reg=="STRONG_TREND_DOWN":add("DOWN",15,"strong bearish regime")
    p=rows[-1]["close"]; loc=(p-f["support"])/max(f["resistance"]-f["support"],1e-12)
    if loc<0.25 and f["ema9"]>=f["ema21"]:add("UP",8,"near support with bullish structure")
    if loc>0.75 and f["ema9"]<=f["ema21"]:add("DOWN",8,"near resistance with bearish structure")
    cp=f["candle"]
    if cp in {"BULLISH_ENGULFING","HAMMER"}:add("UP",8,"bullish price-action pattern")
    elif cp in {"BEARISH_ENGULFING","SHOOTING_STAR"}:add("DOWN",8,"bearish price-action pattern")
    # Breakout confirmation
    prior=rows[-11:-1];
    if prior:
        if p>max(x["high"] for x in prior):add("UP",7,"recent resistance breakout")
        if p<min(x["low"] for x in prior):add("DOWN",7,"recent support breakdown")
    diff=abs(up-down); raw=max(up,down); sig="UP" if up>down else "DOWN" if down>up else "NO TRADE"
    if reg in {"RANGE","HIGH_VOL_RANGE","TRANSITION"}: sig="NO TRADE"; ur.insert(0,"REGIME_FILTER"); dr.insert(0,"REGIME_FILTER")
    if diff<18: sig="NO TRADE"
    reasons=(ur if up>=down else dr)[:8]
    return {"timeframe":tf,"signal":sig,"score":round(min(raw,100),1),"direction_gap":round(diff,1),"reasons":reasons,"up_score":round(up,1),"down_score":round(down,1),"features":{k:(round(v,8) if isinstance(v,float) else v) for k,v in f.items()},"regime":reg}


def build_timeframes(base1, base5, base15):
    return {"1M":base1,"3M":aggregate(base1,3,1),"5M":base5,"10M":aggregate(base5,10,5),"15M":base15,"50M":aggregate(base5,50,5)}


TF_MINUTES={"1M":1,"3M":3,"5M":5,"10M":10,"15M":15,"50M":50}


def direction_vote(layers):
    weights={"50M":2.0,"15M":1.8,"10M":1.4,"5M":1.2,"3M":1.0,"1M":0.7}
    up=sum(weights[k] for k,v in layers.items() if v["signal"]=="UP"); down=sum(weights[k] for k,v in layers.items() if v["signal"]=="DOWN")
    active=[v for v in layers.values() if v["signal"] in {"UP","DOWN"}]
    if not active:return "NO TRADE",0
    total=up+down; score=100*max(up,down)/max(total,1e-9); sig="UP" if up>down else "DOWN" if down>up else "NO TRADE"
    return sig,round(score,1)


def get_quote(symbol):
    try:
        d=td_request("quote", {"symbol":symbol})
        bid=safe_float(d.get("bid")); ask=safe_float(d.get("ask"));
        if bid is not None and ask is not None and ask>=bid:
            pip=PIP_SIZE.get(symbol,DEFAULT_PIP); return {"bid":bid,"ask":ask,"spread_pips":(ask-bid)/pip,"source":"Twelve Data quote"}
    except Exception:
        pass
    return {"bid":None,"ask":None,"spread_pips":EST_SPREAD_PIPS.get(symbol,2.0),"source":"estimated configuration; live bid/ask unavailable"}


def vision_optional(path,symbol,tf):
    if not (VISION_API_KEY and VISION_API_URL and VISION_MODEL):return {"status":"not_configured"}
    try:
        raw=Path(path).read_bytes(); b64=base64.b64encode(raw).decode(); mime="image/jpeg"
        try:
            with Image.open(path) as im:mime=Image.MIME.get(im.format,"image/jpeg")
        except Exception: pass
        payload={"model":VISION_MODEL,"input":[{"role":"user","content":[{"type":"input_text","text":f"Analyze this FX chart for {symbol}, requested timeframe {tf}. Return strict JSON with detected_timeframe, timeframe_match (true/false/unknown), trend (UP/DOWN/NEUTRAL), support, resistance, candle_structure, visible_indicators, warnings. Do not claim certainty or guaranteed profit. If the screenshot does not clearly show the chart timeframe, use unknown and do not guess."},{"type":"input_image","image_url":f"data:{mime};base64,{b64}"}]}]}
        r=requests.post(VISION_API_URL,headers={"Authorization":f"Bearer {VISION_API_KEY}","Content-Type":"application/json"},json=payload,timeout=45); r.raise_for_status()
        data=r.json(); text=None
        for item in data.get("output",[]) if isinstance(data,dict) else []:
            for content in item.get("content",[]) if isinstance(item,dict) else []:
                if isinstance(content,dict) and content.get("type") in {"output_text","text"} and content.get("text"):
                    text=content["text"]; break
            if text: break
        parsed=None
        if text:
            try:
                cleaned=text.strip()
                if cleaned.startswith("```"):
                    cleaned=cleaned.strip("`").replace("json", "", 1).strip()
                parsed=json.loads(cleaned)
            except Exception:
                parsed=None
        return {"status":"ok","result":data,"text":text,"parsed":parsed}
    except Exception as e:return {"status":"error","error":str(e)}


def validation_backtest(rows, spread_pips, slippage_pips, horizon=1):
    if len(rows)<220:return {"status":"INSUFFICIENT","signals":0,"win_rate":None,"expectancy_r":None,"profit_factor":None,"max_drawdown_r":None,"walk_forward_windows":0}
    n=len(rows); min_train=max(120,int(n*0.35)); step=max(10,int(n*0.10)); wins=losses=0; rvals=[]; windows=0
    pip=PIP_SIZE.get("UNKNOWN",DEFAULT_PIP); cost=(spread_pips+slippage_pips)*pip
    for start in range(min_train,n-horizon,step):
        end=min(start+step,n-horizon); windows+=1
        for i in range(start,end):
            layer=layer_signal(rows[:i+1],"BT")
            if layer["signal"] not in {"UP","DOWN"} or layer["score"]<62:continue
            entry=rows[i]["close"] + (cost/2 if layer["signal"]=="UP" else -cost/2)
            exitp=rows[i+horizon]["close"]
            move=(exitp-entry) if layer["signal"]=="UP" else (entry-exitp)
            r=move/max(cost, pip*0.5)
            # A small adverse move can be a loss; equal is neutral.
            if r>0:wins+=1; rvals.append(min(r,3.0))
            elif r<0:losses+=1; rvals.append(max(r,-3.0))
    total=wins+losses; wr=100*wins/total if total else None
    gains=sum(x for x in rvals if x>0); loss=sum(-x for x in rvals if x<0); pf=gains/loss if loss else (None if not gains else 999.0)
    eq=peak=dd=0.0
    for r in rvals:
        eq+=r; peak=max(peak,eq); dd=max(dd,peak-eq)
    return {"status":"OK" if total>=40 else "WEAK_SAMPLE","signals":total,"wins":wins,"losses":losses,"win_rate":round(wr,2) if wr is not None else None,"expectancy_r":round(sum(rvals)/len(rvals),4) if rvals else None,"profit_factor":round(pf,3) if pf is not None else None,"max_drawdown_r":round(dd,3),"walk_forward_windows":windows,"spread_pips":spread_pips,"slippage_pips":slippage_pips}


def fuse(symbol, layers, validation, quote, vision):
    sig, vote_score=direction_vote(layers); reasons=[]; gates=[]
    if sig=="NO TRADE":reasons.append("MTF_CONFLICT_OR_NO_DIRECTION")
    # Require higher-timeframe agreement and at least 4 directional layers.
    dirs=[v["signal"] for v in layers.values()]
    same=sum(1 for x in dirs if x==sig)
    if sig in {"UP","DOWN"} and same<4:reasons.append("INSUFFICIENT_MTF_CONFIRMATION")
    high=[layers[k]["signal"] for k in ("50M","15M","10M")]
    if sig in {"UP","DOWN"} and high.count(sig)<2:reasons.append("HIGHER_TF_CONFLICT")
    if validation.get("signals",0)<40:reasons.append("VALIDATION_SAMPLE_TOO_SMALL")
    if validation.get("win_rate") is not None and validation["win_rate"]<52:reasons.append("VALIDATION_EDGE_WEAK")
    if validation.get("profit_factor") is not None and validation["profit_factor"]<1.0:reasons.append("VALIDATION_PROFIT_FACTOR_FAIL")
    if quote.get("spread_pips",99)>float(os.getenv("MAX_SPREAD_PIPS","4.0")):reasons.append("SPREAD_TOO_HIGH")
    vision_status=vision.get("status") if isinstance(vision,dict) else "not_configured"
    if vision_status=="error":reasons.append("VISION_ERROR")
    if vision_status=="ok":
        parsed=vision.get("parsed") or {}
        if isinstance(parsed,dict) and parsed.get("timeframe_match") is False:
            reasons.append("VISION_TIMEFRAME_MISMATCH")
    # Vision is confirmatory only; never overrides market data.
    if vision_status=="ok":
        txt=json.dumps(vision.get("result",{})).upper()
        if sig=="UP" and "DOWN" in txt and "UP" not in txt:reasons.append("VISION_CONFLICT")
        if sig=="DOWN" and "UP" in txt and "DOWN" not in txt:reasons.append("VISION_CONFLICT")
    quality=[v for v in layers.values() if v["regime"]=="UNSTABLE"]
    if quality:reasons.append("TIMEFRAME_DATA_QUALITY")
    evidence=round(min(100, vote_score*0.45 + min(100, max((validation.get("win_rate") or 0),50))*0.25 + min(100,validation.get("signals",0)/2)*0.15 + (10 if validation.get("profit_factor") and validation["profit_factor"]>=1.1 else 0) + (5 if same>=5 else 0)),1)
    if reasons or sig=="NO TRADE" or evidence<68: final="NO TRADE"; gates.append("ABSTAIN")
    else: final=sig; gates.append("PASS")
    if final=="NO TRADE" and not reasons: reasons.append("LOW_EVIDENCE")
    return {"signal":final,"raw_direction":sig,"evidence_score":evidence,"vote_score":vote_score,"reasons":reasons,"gates":gates,"directional_timeframes":same,"vision_status":vision_status}


def ensure_market(symbol, mtype):
    if mtype!="REAL": return False,"OTC is disabled until a genuine broker-specific OTC feed is connected. Synthetic OTC data is never used."
    if symbol not in REAL_MARKETS:return False,"Unsupported REAL FX symbol."
    return True,""


def fetch_bundle(symbol):
    # Native supported intervals reduce aggregation drift. 3/10/50 are derived from 1m/5m.
    one=fetch_td(symbol,"1min",1000); five=fetch_td(symbol,"5min",1000); fifteen=fetch_td(symbol,"15min",500)
    save_candles(symbol,"1M",one); save_candles(symbol,"5M",five); save_candles(symbol,"15M",fifteen)
    return build_timeframes(one,five,fifteen)


def analysis_payload(symbol, layers, quality, validation, quote, fusion, vision):
    return {"symbol":symbol,"timeframes":TIMEFRAMES,"layers":layers,"data_quality":quality,"validation":validation,"market_costs":quote,"fusion":fusion,"vision":vision,"generated_at":utc_now().isoformat(),"note":"Evidence score is not a probability. Directional signals are intentionally blocked when validation, data quality, regime or MTF confirmation is insufficient."}


@app.get("/")
def index(): return send_from_directory(".","index.html")

@app.get("/api/status")
def status():
    db(); return jsonify({"FX_API":bool(TWELVE_DATA_API_KEY),"Database":DB.exists(),"Screenshot_upload":True,"Vision_AI":bool(VISION_API_KEY and VISION_API_URL and VISION_MODEL),"Vision_API_URL_configured":bool(VISION_API_URL),"Vision_model_configured":bool(VISION_MODEL),"REAL_market_priority":True,"OTC_genuine_feed":False,"PWA":True,"timeframes":TIMEFRAMES,"engine":"Multi-Layer Hybrid Decision Engine v3"})

@app.get("/api/sync")
def sync():
    symbol=request.args.get("symbol","EUR/USD").upper(); ok,msg=ensure_market(symbol,"REAL")
    if not ok:return jsonify({"error":msg}),400
    try:
        bundle=fetch_bundle(symbol); return jsonify({"saved":{k:len(v) for k,v in bundle.items()},"source":"Twelve Data REAL FX + local cache","symbol":symbol,"timeframes":TIMEFRAMES})
    except Exception as e:return jsonify({"error":str(e)}),502

@app.get("/api/backtest")
def backtest():
    symbol=request.args.get("symbol","EUR/USD").upper(); ok,msg=ensure_market(symbol,"REAL")
    if not ok:return jsonify({"error":msg}),400
    try:
        bundle=fetch_bundle(symbol); quote=get_quote(symbol); out={}
        for tf,rows in bundle.items():
            q=data_quality(rows,TF_MINUTES[tf],10 if tf=="1M" else max(30,TF_MINUTES[tf]*2))
            out[tf]={"quality":q,"backtest":validation_backtest(rows,quote["spread_pips"],float(os.getenv("SLIPPAGE_PIPS","0.5")))}
        return jsonify({"symbol":symbol,"results":out,"source":"REAL FX historical data; walk-forward research test with configurable spread/slippage."})
    except Exception as e:return jsonify({"error":str(e)}),502

@app.post("/api/analyze")
def analyze():
    symbol=request.form.get("symbol","EUR/USD").upper(); mtype=request.form.get("market_type","REAL"); requested=request.form.get("timeframe","15M").upper()
    if requested not in TIMEFRAMES: requested="15M"
    ok,msg=ensure_market(symbol,mtype)
    if not ok:return jsonify({"signal":"NO TRADE","reason":msg,"source":"Safety gate"}),200
    try:
        bundle=fetch_bundle(symbol); quote=get_quote(symbol); layers={}; quality={}
        for tf,rows in bundle.items():
            q=data_quality(rows,TF_MINUTES[tf],10 if tf=="1M" else max(30,TF_MINUTES[tf]*2)); quality[tf]=q
            if q["status"]=="PASS" and len(rows)>=80: layers[tf]=layer_signal(rows,tf)
            else: layers[tf]={"timeframe":tf,"signal":"NO TRADE","score":0,"direction_gap":0,"reasons":q["reasons"] or ["DATA_QUALITY_FAIL"],"up_score":0,"down_score":0,"features":{},"regime":"UNSTABLE"}
        validation=validation_backtest(bundle[requested],quote["spread_pips"],float(os.getenv("SLIPPAGE_PIPS","0.5")))
        vision={"status":"not_uploaded"}; f=request.files.get("screenshot")
        if f and f.filename:
            im=Image.open(f.stream); im.verify(); f.stream.seek(0); safe=Path(f.filename).name.replace(" ","_")
            out=UPLOADS/(utc_now().strftime("%Y%m%dT%H%M%S%fZ_")+safe); f.save(out)
            with Image.open(out) as im2:w,h=im2.size
            vision=vision_optional(out,symbol,requested)
            c=db(); c.execute("INSERT INTO screenshots(symbol,timeframe,market_type,filename,path,width,height,uploaded_at,vision_status) VALUES(?,?,?,?,?,?,?,?,?)",(symbol,requested,mtype,safe,str(out),w,h,utc_now().isoformat(),vision.get("status"))); c.commit(); c.close()
        fusion=fuse(symbol,layers,validation,quote,vision)
        # If requested TF itself is a strong NO TRADE, do not override it with other timeframes.
        if layers[requested]["signal"]=="NO TRADE" and fusion["signal"] in {"UP","DOWN"}:
            fusion["reasons"].append("REQUESTED_TF_NOT_CONFIRMED"); fusion["signal"]="NO TRADE"
        payload=analysis_payload(symbol,layers,quality,validation,quote,fusion,vision)
        c=db(); c.execute("INSERT INTO analyses(created_at,symbol,market_type,requested_tf,final_signal,evidence_score,calibrated_win_rate,validation_signals,regime,data_quality,spread_pips,slippage_pips,reasons_json,context_json,screenshot_vision,outcome) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(utc_now().isoformat(),symbol,mtype,requested,fusion["signal"],fusion["evidence_score"],validation.get("win_rate"),validation.get("signals",0),layers[requested].get("regime"),json.dumps(quality),quote.get("spread_pips"),float(os.getenv("SLIPPAGE_PIPS","0.5")),json.dumps(fusion["reasons"]),json.dumps(payload),json.dumps(vision),None)); c.commit(); c.close()
        return jsonify(payload)
    except Exception as e:
        return jsonify({"signal":"NO TRADE","error":str(e),"reason":"ANALYSIS_ERROR — no directional signal issued."}),502

@app.get("/api/journal")
def journal():
    c=db(); rows=c.execute("SELECT id,created_at,symbol,requested_tf,final_signal,evidence_score,calibrated_win_rate,validation_signals,regime,reasons_json,outcome FROM analyses ORDER BY id DESC LIMIT 50").fetchall(); c.close()
    return jsonify([dict(r) for r in rows])

@app.get("/health")
def health(): return jsonify({"ok":True,"time":utc_now().isoformat()})

if __name__=="__main__":
    db(); app.run(host="0.0.0.0",port=int(os.getenv("PORT","5000")),debug=False)
