# AI FX Chart Bot v2 — READY Foundation

## Included
- Mobile-first PWA with Home Screen install support.
- REAL FX markets are listed before OTC.
- Exact market selection before screenshot upload.
- REAL FX provider adapter using Twelve Data.
- 1min / 5min / 15min.
- SQLite candle cache/database.
- Chronological 70/30 out-of-sample baseline backtest.
- Wilder RSI(14), EMA 9/21.
- Screenshot upload + metadata storage.
- Optional server-side vision hook.
- Conservative NO TRADE gate.
- OTC intentionally disabled until a genuine OTC feed is connected.

## Fast start
1. Create a Twelve Data account and obtain an API key.
2. Set `TWELVE_DATA_API_KEY` as a server environment secret. Do NOT put it in HTML.
3. Install:
   `pip install -r requirements.txt`
4. Run:
   `python app.py`
5. Open `http://127.0.0.1:5000`.
6. Select EUR/USD, GBP/USD, NZD/USD (Kiwi), etc.
7. Choose 1m/5m/15m.
8. Click Sync candles.
9. Run backtest.
10. Upload a matching screenshot and click Analyze.

## Important
- The Twelve Data feed supports 1min/5min/15min intraday intervals and historical time-series access, subject to plan/credits and data availability.
- The REST feed is not HFT execution infrastructure. For very low latency, a provider WebSocket is a separate stage.
- No future accuracy is guaranteed.
- The baseline backtest is deliberately conservative and should be replaced by walk-forward testing, transaction-cost/spread modeling, and multiple out-of-sample periods before real-money use.
- OTC is NO TRADE until a genuine broker/provider OTC feed is integrated.
- The optional vision hook requires a compatible server-side vision endpoint; it is not falsely advertised as active when not configured.

## Next production stages
A. Provider verification + spread/session handling
B. Historical data pagination and database retention
C. Walk-forward / multi-window backtesting
D. Multi-timeframe confirmation
E. Support/resistance + candlestick + price-action features
F. Screenshot vision model with structured JSON output
G. Hybrid confirmation engine
H. Paper trading + audit logs
I. Monitoring and deployment
