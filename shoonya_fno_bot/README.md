Shoonya F&O 5m Buyer Bot

Prereqs
- Python 3.10+
- Shoonya API access enabled

Setup
1) Copy `.env.example` to `.env` and fill credentials.
2) Install deps:
   pip install -r requirements.txt

Run
python bot.py

What it does
- Logs into Shoonya
- Resolves nearest-month stock futures for symbols in `WATCHLIST`
- Every new 5‑minute bar, checks:
  - prev bar low equals today’s low so far
  - Williams %R(14) crossed up through −80 between 2 and 1 bars ago
  - RSI(14) two bars ago < 20
- If true, places a MARKET BUY for `LOTS_PER_ORDER` lots with `PRODUCT_TYPE` (I/M)

Notes
- Uses min(low) of today’s intraday 5‑min candles as today’s low (intraday rolling)
- Enforces one signal per 5‑min bar per symbol; can restrict to once-per-day via `ONCE_PER_DAY`
- Timezone assumed Asia/Kolkata