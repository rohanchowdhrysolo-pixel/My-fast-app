# AI FX Hybrid Chart Analysis Engine — v3 Verified Final

This release is the verified rebuild of the agreed six-timeframe REAL-FX research engine. It is designed to abstain (`NO TRADE`) when the evidence or validation is insufficient rather than force `UP`/`DOWN`.

## What was checked and fixed before release

- REAL FX only. OTC remains disabled until a genuine broker-specific OTC feed exists; synthetic OTC data is never substituted.
- REAL pairs include EUR/JPY and GBP/JPY with explicit JPY pip sizing.
- Native provider intervals are 1M, 5M and 15M. 3M, 10M and 50M are constructed only from complete source buckets.
- The 50M validation problem is fixed: a normal analysis uses 5,000 recent 5M bars, while a 50M analysis/backtest additionally retrieves bounded older 5M windows so the 50M walk-forward test can reach the required history. The code never claims that one 5,000-row request is enough for 50M validation.
- Twelve Data's current documentation limits a single time-series response to 5,000 data points and supports `start_date`/`end_date` for historical windows. The deep-history code uses multiple bounded requests accordingly.
- FX weekend/long market-closed gaps are not automatically labeled as missing data; suspicious mid-session gaps are still flagged.
- Symbol-specific pip size is used for spread/slippage research calculations.
- Walk-forward validation reports sample size, historical win rate, expectancy R, profit factor, maximum drawdown R and number of windows.
- Validation gates require at least 40 validation signals, historical win rate >= 52%, profit factor >= 1.0, and maximum drawdown <= the configurable `MAX_VALIDATION_DD_R` (default 12R). These are research gates, not guarantees.
- The requested timeframe must itself confirm the final direction; other timeframes cannot override a requested-timeframe `NO TRADE`.
- Higher-timeframe agreement and a minimum of four directional timeframes are required before a directional result can pass fusion.
- Vision is optional and confirmatory only. It cannot create a trade by itself and cannot override REAL market data.
- Vision requests use a resized/compressed screenshot, a bounded timeout, parsed JSON when available, and parsed `trend` only for conflict checks.
- Uploaded screenshots are normalized to a bounded JPEG size before storage, reducing disk growth from repeated phone uploads.
- `/health` performs a real SQLite `SELECT 1` check and dynamic health/API responses are not cacheable.
- The service worker does not cache navigation pages, `/health`, or `/api/*`, preventing a cached Render loading page from masking a healthy deployment.
- DATA SYNC calls `/api/sync` only; it does not accidentally execute a full analysis.
- Gunicorn binds to Render's `$PORT`, uses four threads and a 120-second request timeout for external-data/Vision requests.
- Python is pinned to 3.13 for reproducible builds.
- Code was syntax-checked, JavaScript syntax-checked, ZIP-integrity checked, and exercised with pure-function/synthetic tests covering aggregation, gap detection, deep-history paging, JPY pip sizing, validation minimums and Vision conflict handling.

## Timeframes

`1M`, `3M`, `5M`, `10M`, `15M`, `50M`

The screenshot does not determine the market-data timeframe. The selected timeframe controls REAL candles. Vision separately checks whether the screenshot visibly matches that timeframe; if it cannot tell, it uses `unknown` rather than guessing.

## Environment variables

Required:

- `TWELVE_DATA_API_KEY` = your Twelve Data key

Vision:

- `VISION_API_KEY` = your OpenAI API key
- `VISION_API_URL` = `https://api.openai.com/v1/responses`
- `VISION_MODEL` = `gpt-5.6-luna`

Optional:

- `SLIPPAGE_PIPS` = `0.5`
- `MAX_SPREAD_PIPS` = `4.0`
- `MAX_VALIDATION_DD_R` = `12.0`

Never place API keys in `index.html` or GitHub.

## Render

Build command:

`pip install -r requirements.txt`

Start command:

`gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120 --graceful-timeout 30 app:app`

Health check path:

`/health`

If using the existing Render service, keep the existing service name/URL; `render.yaml` documents the configuration and is not a requirement to create a second service.

## Verification sequence after deployment

1. Wait for `Deploy successful`.
2. Open `/health` and confirm JSON contains `"ok": true` and `"database": true`.
3. Open `/api/status` and confirm the FX API is configured, the database is available, PWA is enabled, and all six timeframes are listed.
4. Use `DATA SYNC` for a REAL FX pair.
5. Select the requested timeframe and run `ANALYZE`.
6. Upload a screenshot only when it corresponds to the selected chart. Vision may return `unknown` if the timeframe is not visible enough to verify.
7. Keep `NO TRADE` when validation, MTF confirmation, data quality, regime, spread, drawdown or Vision checks fail. Do not lower gates just to force a signal.

## External-service limits that cannot be guaranteed by code

The application code cannot guarantee that a third-party API key has the required Twelve Data plan/market access, that an external API is online, or that Render will never restart/spin down a free service. Twelve Data currently documents a 5,000-row single-request limit for time-series data and plan/credit limits that vary by account. Render Free services can spin down after inactivity. These are infrastructure/account conditions, not hidden code bugs.

## Persistence

Render Free filesystem storage is ephemeral. SQLite and stored screenshots are therefore suitable for this research deployment but are not durable production storage. A persistent production deployment should use a managed database and durable object storage.

## Safety / research scope

This is a research and paper-trading system. It does not execute real-money orders. Evidence score is not a probability, and the project does not claim 100% accuracy, guaranteed profit, or a fixed win probability.
