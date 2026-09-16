# AI FX Hybrid Chart Analysis Engine — FINAL v3

## What this build fixes
- REAL FX only; OTC remains disabled until a genuine broker-specific OTC feed exists.
- Native 1M / 5M / 15M Twelve Data candles.
- Derived 3M / 10M / 50M candles are built only from complete, correctly spaced source candles.
- Timeframe-aware data-quality checks: 3M/10M/15M/50M are no longer falsely marked as `DATA_GAPS` just because their candle spacing is larger than 1 minute.
- Screenshot timeframe is treated separately from market-data timeframe; optional Vision checks the visible timeframe and can block on mismatch.
- Multi-timeframe quant + price action + regime + validation + spread/slippage + abstention gates.
- NO TRADE is intentional when evidence is weak/conflicting or validation is poor.
- PWA manifest, service worker, and mobile icons included.

## Environment variables on Render
Required:
- `TWELVE_DATA_API_KEY` = your Twelve Data key

Vision:
- `VISION_API_KEY` = your OpenAI API key
- `VISION_API_URL` = `https://api.openai.com/v1/responses`
- `VISION_MODEL` = `gpt-5.6-luna`

Optional:
- `SLIPPAGE_PIPS` = `0.5`
- `MAX_SPREAD_PIPS` = `4.0`

Never put API keys in `index.html` or GitHub.

## Render
Build command:
`pip install -r requirements.txt`

Start command:
`gunicorn app:app`

## Verification
1. `/health` must return `{"ok":true,...}`.
2. `/api/status` must show `FX_API: true`, `Vision_AI: true`, `Database: true`, and the six timeframes.
3. Run DATA SYNC for a REAL pair.
4. Analyze a screenshot with the requested timeframe selected.
5. Check that 3M/10M/15M/50M are not falsely rejected as `DATA_GAPS` merely because of their normal candle spacing.
6. A weak/negative validation result should remain `NO TRADE`; do not lower safety gates merely to force UP/DOWN.

This is a research/paper-trading system. Evidence score is not a probability and no profit/accuracy guarantee is made.
