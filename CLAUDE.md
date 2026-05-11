# Aurix Capital — Live Trading Dashboard

## Project Overview
A live trading dashboard for Zerodha brokerage accounts. Built with FastAPI + SQLite backend and a single-page HTML frontend. Connects to Zerodha Kite Connect API for live prices, positions, holdings, and historical chart data.

## Running the App
```bash
py -m uvicorn main:app --host 0.0.0.0 --port 8080
```
- Always use port **8080** (port 8000 is used by another application)
- Use `py` command, not `python` (Windows Python Launcher)
- Open at: http://localhost:8080

## Credentials
- **Account name**: Aurix Capital
- **API Key**: `unyt735beny8a0do`
- **API Secret**: `0bj7eu2yt9b2bdvktkcacrgciioskb98`
- Credentials stored in `config.py`
- Access token stored in `tokens.json` (auto-generated after login, changes daily)

## Login Flow
1. Click **Generate Token** in the dashboard header
2. Click **Login** → redirects to Zerodha login
3. After Zerodha login, copy the callback URL and hit `/callback?request_token=...` on port 8080
   - e.g. `http://localhost:8080/callback?request_token=XXXX`
4. Token is saved to `tokens.json` automatically

## File Structure
```
main.py          — FastAPI app, all API endpoints, KiteTicker WebSocket bridge
config.py        — ACCOUNTS list with API credentials
db.py            — SQLite module (trades table, first_buy cache, CSV import)
tokens.json      — Saved access tokens (keyed by api_key)
trades.db        — SQLite database
static/index.html — Full single-page dashboard UI
trial.py         — Standalone recent-holdings app (largely superseded)
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
- SQLite with WAL mode
- `trades` table: all trades (from CSV + live Kite sync)
- `first_buy` table: materialized cache of first/last buy date, avg price per ISIN
- `bootstrap()` imports CSV only if DB is empty
- `rebuild_first_buy()` called after any new trades are inserted

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

## Key Technical Notes
- KiteTicker only supports one API key — uses first logged-in account's credentials; instrument tokens are exchange-wide
- Historical data API requires a paid Zerodha add-on (~₹2000/month) — confirmed working with current API key
- `container.getBoundingClientRect().width` used for chart width (not `clientWidth` — returns 0 in CSS grid before layout)
- `FileResponse` used to serve index.html (not manual file read) to avoid encoding issues
- WebSocket ping sent every 20s to keep connection alive
- Charts re-render on tab switch via `resizeAllCharts()`

## Dependencies
```
fastapi
uvicorn[standard]
websockets
kiteconnect
```
Install: `pip install fastapi "uvicorn[standard]" websockets kiteconnect`
