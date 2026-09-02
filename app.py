import os, io, sqlite3, base64, json, math
from datetime import datetime, timezone
from pathlib import Path
import requests
from flask import Flask, jsonify, request, send_from_directory
from PIL import Image

BASE=Path(__file__).resolve().parent
DB=BASE/"data.db"
UPLOADS=BASE/"uploads"
UPLOADS.mkdir(exist_ok=True)
app=Flask(__name__, static_folder=".", static_url_path="")
app.config["MAX_CONTENT_LENGTH"]=8*1024*1024

# Twelve Data is used as the default REAL-FX adapter.
# Put TWELVE_DATA_API_KEY in the server environment. Never put it in index.html.
TWELVE_DATA_API_KEY=os.getenv("TWELVE_DATA_API_KEY","").strip()
VISION_API_KEY=os.getenv("VISION_API_KEY","").strip()
VISION_API_URL=os.getenv("VISION_API_URL","").strip()
VISION_MODEL=os.getenv("VISION_MODEL","").strip()

REAL_MARKETS=["EUR/USD","GBP/USD","USD/JPY","AUD/USD","NZD/USD","EUR/GBP","USD/CAD","USD/CHF"]
OTC_MARKETS=["EUR/USD","GBP/USD","USD/JPY","NZD/USD"]

def db():
    c=sqlite3.connect(DB)
    c.row_factory=sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS candles(
      symbol TEXT, interval TEXT, ts TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL,
      source TEXT, PRIMARY KEY(symbol,interval,ts))""")
    c.execute("""CREATE TABLE IF NOT EXISTS screenshots(
      id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, interval TEXT, market_type TEXT,
      filename TEXT, path TEXT, width INTEGER, height INTEGER, uploaded_at TEXT,
      vision_status TEXT)""")
    c.commit(); return c

def td(symbol, interval, limit=500):
    if not TWELVE_DATA_API_KEY:
        raise RuntimeError("TWELVE_DATA_API_KEY is not configured on the server.")
    if interval not in {"1min","5min","15min"}: raise ValueError("Unsupported timeframe")
    url="https://api.twelvedata.com/time_series"
    p={"symbol":symbol,"interval":interval,"outputsize":min(int(limit),5000),"timezone":"UTC","apikey":TWELVE_DATA_API_KEY}
    r=requests.get(url,params=p,timeout=20); r.raise_for_status(); d=r.json()
    if d.get("status")=="error": raise RuntimeError(d.get("message","Twelve Data error"))
    vals=d.get("values") or []
    out=[]
    for x in reversed(vals):
        out.append({"ts":x["datetime"],"open":float(x["open"]),"high":float(x["high"]),"low":float(x["low"]),"close":float(x["close"]),"volume":float(x.get("volume") or 0)})
    return out

def save_candles(symbol,interval,rows):
    c=db()
    for x in rows:
        c.execute("INSERT OR REPLACE INTO candles VALUES(?,?,?,?,?,?,?,?,?)",(symbol,interval,x["ts"],x["open"],x["high"],x["low"],x["close"],x["volume"],"twelvedata"))
    c.commit(); c.close(); return len(rows)

def load_candles(symbol,interval,limit=500):
    c=db(); rows=c.execute("SELECT ts,open,high,low,close,volume FROM candles WHERE symbol=? AND interval=? ORDER BY ts DESC LIMIT ?",(symbol,interval,limit)).fetchall(); c.close()
    return [dict(r) for r in reversed(rows)]

def ema(v,n):
    if len(v)<n:return None
    e=sum(v[:n])/n;k=2/(n+1)
    for x in v[n:]:e=x*k+e*(1-k)
    return e

def rsi(v,n=14):
    if len(v)<=n:return None
    gains=[];losses=[]
    for i in range(1,len(v)): 
        d=v[i]-v[i-1];gains.append(max(d,0));losses.append(max(-d,0))
    ag=sum(gains[:n])/n; al=sum(losses[:n])/n
    for i in range(n,len(gains)):
        ag=(ag*(n-1)+gains[i])/n; al=(al*(n-1)+losses[i])/n
    if al==0:return 100.0
    return 100-(100/(1+ag/al))

def signal_at(c):
    close=[x["close"] for x in c]; e9=ema(close,9); e21=ema(close,21); rr=rsi(close,14)
    if e9 is None or e21 is None or rr is None:return "NO TRADE",e9,e21,rr
    if e9>e21 and rr>=52:return "UP",e9,e21,rr
    if e9<e21 and rr<=48:return "DOWN",e9,e21,rr
    return "NO TRADE",e9,e21,rr

def baseline_backtest(c):
    # Strictly chronological. Development uses earlier candles; test uses later unseen candles.
    if len(c)<180:return {"win_rate":None,"signals":0,"train":0,"test":0}
    split=int(len(c)*0.70); test=c[split:]
    wins=sigs=0
    for i in range(split,len(c)-1):
        hist=c[:i+1]
        sig,_,_,_=signal_at(hist)
        if sig=="NO TRADE":continue
        sigs+=1; nxt=c[i+1]["close"]-c[i]["close"]
        if (sig=="UP" and nxt>0) or (sig=="DOWN" and nxt<0):wins+=1
    return {"win_rate":round(100*wins/sigs,2) if sigs else None,"signals":sigs,"train":split,"test":len(test)}

def vision_optional(image_path, market_name, interval):
    # Generic vision hook. It is OFF unless a server-side compatible vision endpoint is configured.
    if not (VISION_API_KEY and VISION_API_URL and VISION_MODEL):
        return {"status":"not_configured","summary":None}
    # Send a compact request to a configured vision endpoint.
    mime="image/jpeg"
    b64=base64.b64encode(Path(image_path).read_bytes()).decode()
    payload={"model":VISION_MODEL,"input":[{"role":"user","content":[
        {"type":"input_text","text":f"Analyze this trading chart image for {market_name} on {interval}. Return only JSON with trend, support, resistance, ema_visible, rsi_visible, candle_structure, warnings. Do not give a guaranteed prediction."},
        {"type":"input_image","image_url":f"data:{mime};base64,{b64}"}
    ]}]}
    r=requests.post(VISION_API_URL,headers={"Authorization":f"Bearer {VISION_API_KEY}","Content-Type":"application/json"},json=payload,timeout=45)
    r.raise_for_status()
    return {"status":"ok","summary":r.json()}

@app.get("/")
def index():return send_from_directory(".", "index.html")

@app.get("/api/status")
def status():
    return jsonify({
      "FX_API": bool(TWELVE_DATA_API_KEY),
      "Database": DB.exists(),
      "Screenshot upload": True,
      "Vision AI": bool(VISION_API_KEY and VISION_API_URL and VISION_MODEL),
      "REAL market priority": True,
      "OTC genuine feed": False,
      "PWA": True
    })

@app.get("/api/sync")
def sync():
    symbol=request.args.get("symbol","EUR/USD"); interval=request.args.get("interval","5min")
    if symbol not in REAL_MARKETS:return jsonify({"error":"OTC is intentionally disabled until a genuine OTC feed is connected."}),400
    rows=td(symbol,interval,500); saved=save_candles(symbol,interval,rows)
    return jsonify({"saved":saved,"source":"Twelve Data REAL FX feed","symbol":symbol,"interval":interval})

@app.get("/api/backtest")
def backtest():
    symbol=request.args.get("symbol","EUR/USD"); interval=request.args.get("interval","5min")
    if symbol not in REAL_MARKETS:return jsonify({"error":"OTC backtest unavailable without genuine OTC data."}),400
    rows=load_candles(symbol,interval,500)
    if len(rows)<180:
        try: rows=td(symbol,interval,500); save_candles(symbol,interval,rows)
        except Exception as e:return jsonify({"error":str(e)}),502
    bt=baseline_backtest(rows); sig,e9,e21,rr=signal_at(rows)
    return jsonify({"signal":sig,"indicators":{"ema9":round(e9,6) if e9 else None,"ema21":round(e21,6) if e21 else None,"rsi14":round(rr,2) if rr else None},"backtest":bt,"reason":"Chronological 70/30 baseline test. This is a research metric, not a future win-rate guarantee.","source":"REAL FX historical data + local SQLite cache."})

@app.post("/api/analyze")
def analyze():
    symbol=request.form.get("symbol","EUR/USD"); interval=request.form.get("interval","5min"); mtype=request.form.get("market_type","REAL"); name=request.form.get("market_name",symbol)
    if mtype!="REAL" or symbol not in REAL_MARKETS:return jsonify({"signal":"NO TRADE","reason":"Genuine OTC data is not connected. No synthetic OTC candles are used.","source":"OTC disabled by design.","backtest":{"win_rate":None,"signals":0}}),200
    try:
        rows=load_candles(symbol,interval,500)
        if len(rows)<180:rows=td(symbol,interval,500);save_candles(symbol,interval,rows)
        bt=baseline_backtest(rows);sig,e9,e21,rr=signal_at(rows)
        vision={"status":"not_uploaded","summary":None}
        f=request.files.get("screenshot")
        if f and f.filename:
            im=Image.open(f.stream); im.verify()
            f.stream.seek(0); safe=f.filename.replace("/","_").replace("\\","_")
            out=UPLOADS/(datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")+"_"+safe)
            f.save(out)
            with Image.open(out) as im2:w,h=im2.size
            c=db();c.execute("INSERT INTO screenshots(symbol,interval,market_type,filename,path,width,height,uploaded_at,vision_status) VALUES(?,?,?,?,?,?,?,?,?)",(symbol,interval,mtype,safe,str(out),w,h,datetime.now(timezone.utc).isoformat(),"pending"));c.commit();c.close()
            vision=vision_optional(out,name,interval)
        # Conservative gate: require enough unseen-test signals and >=60% baseline before allowing directional output.
        final=sig if bt["signals"]>=50 and bt["win_rate"] is not None and bt["win_rate"]>=60 else "NO TRADE"
        reason=f"EMA9/21 + Wilder RSI14 baseline. Out-of-sample test={bt['win_rate']}% over {bt['signals']} signals. Screenshot vision={vision['status']}. No guarantee."
        return jsonify({"signal":final,"indicators":{"ema9":round(e9,6),"ema21":round(e21,6),"rsi14":round(rr,2)},"backtest":bt,"vision":vision,"reason":reason,"source":"REAL FX provider data + SQLite + chronological backtest + optional screenshot vision."})
    except Exception as e:return jsonify({"error":str(e)}),502

if __name__=="__main__":
    db()
    app.run(host="0.0.0.0",port=int(os.getenv("PORT","5000")),debug=False)
