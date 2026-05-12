# Aurix Capital — Live Trading Dashboard

## Project Overview
A live trading dashboard for Zerodha brokerage accounts. Built with FastAPI + Supabase (PostgreSQL) backend and a single-page HTML frontend. Connects to Zerodha Kite Connect API for live prices, positions, holdings, and historical chart data. Deployed on Railway.

## Running the App
```bash
py -m uvicorn main:app --host 0.0.0.0 --port 8080
```
- Always use port **8080** (port 8000 is used by another application)
- Use `py` command, not `python` (Windows Python Launcher)
- Open at: http://localhost:8080
- Env vars must be set before running (see `.env` file)

## Environment Variables
Set these before running locally (stored in `.env`, gitignored):
```
SUPABASE_URL=https://ebuxgwvudfotflbmnkvw.supabase.co
SUPABASE_SECRET_KEY=<your-supabase-secret-key>
KITE_API_KEY=unyt735beny8a0do
KITE_API_SECRET=0bj7eu2yt9b2bdvktkcacrgciioskb98
DATA_DIR=.
```
On Railway these are set as service environment variables.

## Credentials
- **Account name**: Aurix Capital
- **API Key**: `unyt735beny8a0do`
- **API Secret**: `0bj7eu2yt9b2bdvktkcacrgciioskb98`
- Credentials stored in `config.py`
- Access token stored in **Supabase `app_tokens` table** (primary) and `tokens.json` (local fallback) — persists across Railway redeploys

## Login Flow
1. Click **Generate Token** in the dashboard header
2. Click **Login** → redirects to Zerodha login
3. After Zerodha login, copy the callback URL and hit `/callback?request_token=...` on port 8080
   - e.g. `http://localhost:8080/callback?request_token=XXXX`
4. Token is saved to `tokens.json` automatically

## File Structure
```
main.py           — FastAPI app, all API endpoints, KiteTicker WebSocket bridge
config.py         — ACCOUNTS list with API credentials
db.py             — Supabase module (trades table, first_buy cache, CSV import)
tokens.json       — Saved access tokens (keyed by api_key)
static/index.html — Full single-page dashboard UI
trial.py          — Standalone recent-holdings app (largely superseded)
.env              — Local secrets (gitignored)
railway.toml      — Railway deployment config
Aurix Capital- Resources/Zerodha/tradebook-TLU065-EQ.csv — Trade history CSV
```

## Architecture

### Backend (main.py)
- FastAPI with WebSocket support (`websockets` library required)
- KiteTicker runs in a daemon thread; ticks bridged to asyncio via `asyncio.run_coroutine_threadsafe`
- DB bootstrapped from CSV on startup (only if DB is empty)
- Key endpoints:
  - `GET /positions` — live positions across all accounts
  - `GET /holdings` — all holdings
  - `GET /recent-holdings?days=N` — holdings first bought within last N days
  - `GET /chart/{instrument_token}?days=180&interval=day` — OHLC candlestick data (requires Historical Data subscription)
  - `GET /funds` — available cash and margin
  - `GET /sync-trades` — fetch today's trades from Kite and save to DB
  - `GET /trade-history` — query DB trades
  - `GET /callback?request_token=...` — exchange request token for access token
  - `GET /login/{account_index}` — redirect to Zerodha login URL
  - `GET /status` — login status per account
  - `WS /ws` — WebSocket for live price ticks

### Database (db.py)
- **Supabase PostgreSQL** via `supabase-py` (HTTP REST API — no direct TCP connection, IPv4-safe)
- Tables are **pre-created in Supabase SQL editor** — `init_db()` is a no-op
- `trades` table: all trades (from CSV + live Kite sync)
- `first_buy` table: materialized cache of first/last buy date, avg price per ISIN
- `bootstrap()` imports CSV only if DB is empty
- `rebuild_first_buy()` recomputes aggregates in Python and upserts to Supabase after any new trades
- Uses `SUPABASE_URL` + `SUPABASE_SECRET_KEY` env vars (not a connection string)

### Supabase Table Schema (run once in SQL Editor if recreating)
```sql
CREATE TABLE IF NOT EXISTS app_tokens (
    api_key      TEXT PRIMARY KEY,
    access_token TEXT NOT NULL,
    updated_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS trades (
    id                   SERIAL PRIMARY KEY,
    account              TEXT    NOT NULL DEFAULT 'Aurix Capital',
    symbol               TEXT    NOT NULL,
    isin                 TEXT,
    trade_date           DATE    NOT NULL,
    exchange             TEXT,
    segment              TEXT,
    series               TEXT,
    trade_type           TEXT    NOT NULL,
    quantity             REAL    NOT NULL,
    price                REAL    NOT NULL,
    trade_id             TEXT    UNIQUE,
    order_id             TEXT,
    order_execution_time TEXT,
    source               TEXT    DEFAULT 'kite',
    created_at           TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_trades_isin     ON trades (isin);
CREATE INDEX IF NOT EXISTS idx_trades_symbol   ON trades (symbol);
CREATE INDEX IF NOT EXISTS idx_trades_date     ON trades (trade_date);
CREATE INDEX IF NOT EXISTS idx_trades_order_id ON trades (order_id);

CREATE TABLE IF NOT EXISTS first_buy (
    isin        TEXT PRIMARY KEY,
    symbol      TEXT,
    first_date  TEXT NOT NULL,
    last_date   TEXT NOT NULL,
    total_qty   REAL NOT NULL,
    avg_price   REAL NOT NULL
);
```

### Frontend (static/index.html)
- Three tabs: **Positions**, **Holdings**, **Recent Holdings**
- Each tab has: metrics bar (summary totals) + **card grid** (one card per stock) + **detail table**
- Cards show: stock name, badge, size %, value, qty, % P&L, abs P&L, 180-day candlestick chart, entry price line, live CMP
- Charts use TradingView lightweight-charts v4.1.0 (CDN)
- Live prices via WebSocket (`/ws`), flash green/red on tick
- Charts stagger 300ms per stock to avoid API rate limits
- `charts{}` object keyed as `{prefix}-{token}`: `ps-` (positions), `hl-` (holdings), `rh-` (recent)
- Cell ID prefixes: `crd-` (position cards), `hl-` (holdings), `rh-` (recent holdings)
- Currency: `&#8377;` HTML entity (not `₹` literal — encoding issues)
- Indian number formatting via `fmtK()`: 1.4L, 26.9K, 1.84Cr

#### Holdings tab specifics
- **T+1 quantity**: Kite returns `quantity` (settled) and `t1_quantity` (unsettled, bought today/yesterday). All calculations use `totalQty = quantity + t1_quantity`. Cards show an orange `T1 +N` badge; table shows `150 (+50 T1)` format. T+1 P&L is computed as `(last_price − average_price) × t1_quantity` since Kite's `pnl` field only covers settled shares
- **% Invested** metric: `holdings_value / (holdings_value + available_cash)` from `/funds`. Shows progress bar. Falls back gracefully if not logged in
- **Available Cash** metric: `equity.available.cash` from Kite margins API

#### Positions tab specifics
- Metrics: Day's P&L, Total P&L, Open Positions, Total Value
- Filter buttons: All / Long / Short

#### Theme
- Background: deep blue-dark (`#080b14`) instead of gray-black
- Cards/panels: `#0d1220`, borders: `#1c2238`
- Header: gradient (`#0e1528` → `#0a1020`) with subtle glow border
- Title: purple gradient text (`#818cf8` → `#a78bfa`)
- Accent: `#7c83fd` (indigo-purple)

## Key Technical Notes
- KiteTicker only supports one API key — uses first logged-in account's credentials; instrument tokens are exchange-wide
- Historical data API requires a paid Zerodha add-on (~₹2000/month) — confirmed working with current API key
- `container.getBoundingClientRect().width` used for chart width (not `clientWidth` — returns 0 in CSS grid before layout)
- `FileResponse` used to serve index.html (not manual file read) to avoid encoding issues
- WebSocket ping sent every 20s to keep connection alive
- Charts re-render on tab switch via `resizeAllCharts()`
- Supabase direct connection (`db.*.supabase.co`) resolves to IPv6 — Railway is IPv4-only, so supabase-py (HTTP API) is used instead of psycopg2

## Dependencies
```
fastapi
uvicorn[standard]
websockets
kiteconnect
supabase
```
Install: `py -m pip install fastapi "uvicorn[standard]" websockets kiteconnect supabase`
