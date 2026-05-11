import asyncio
import json
import os
import threading

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from kiteconnect import KiteConnect, KiteTicker

from config import ACCOUNTS
import db as database

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

# Persistent data directory — override with DATA_DIR env var on Railway
DATA_DIR = os.environ.get("DATA_DIR", ".")
TOKENS_FILE = os.path.join(DATA_DIR, "tokens.json")

# Bootstrap DB on startup (import CSV only if it exists locally)
_csv = "Aurix Capital- Resources/Zerodha/tradebook-TLU065-EQ.csv"
database.bootstrap(_csv if os.path.exists(_csv) else None)

# ── WebSocket clients ────────────────────────────────────────────────────────
ws_clients: set[WebSocket] = set()

# ── Bridge between KiteTicker thread and asyncio ─────────────────────────────
_tick_queue: asyncio.Queue | None = None
_event_loop: asyncio.AbstractEventLoop | None = None

# ── Active KiteTicker ─────────────────────────────────────────────────────────
_ticker: KiteTicker | None = None
_subscribed_tokens: set[int] = set()
_ticker_lock = threading.Lock()


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_tokens() -> dict:
    if os.path.exists(TOKENS_FILE):
        with open(TOKENS_FILE) as f:
            return json.load(f)
    return {}


def save_tokens(tokens: dict):
    with open(TOKENS_FILE, "w") as f:
        json.dump(tokens, f)


# ── Startup ───────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    global _tick_queue, _event_loop
    _event_loop = asyncio.get_event_loop()
    _tick_queue = asyncio.Queue()
    asyncio.create_task(_tick_broadcaster())


async def _tick_broadcaster():
    while True:
        ticks = await _tick_queue.get()
        dead = set()
        for ws in list(ws_clients):
            try:
                await ws.send_json(ticks)
            except Exception:
                dead.add(ws)
        ws_clients -= dead


# ── WebSocket endpoint ────────────────────────────────────────────────────────

@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    ws_clients.add(websocket)
    try:
        while True:
            await websocket.receive_text()
    except (WebSocketDisconnect, Exception):
        ws_clients.discard(websocket)


# ── KiteTicker ────────────────────────────────────────────────────────────────

def _launch_ticker(api_key: str, access_token: str, tokens: list[int]):
    global _ticker, _subscribed_tokens

    def on_ticks(ws, ticks):
        payload = {}
        for t in ticks:
            ohlc = t.get("ohlc", {})
            payload[str(t["instrument_token"])] = {
                "ltp":   t.get("last_price", 0),
                "close": ohlc.get("close", 0),
            }
        if _event_loop and _tick_queue:
            asyncio.run_coroutine_threadsafe(_tick_queue.put(payload), _event_loop)

    def on_connect(ws, response):
        ws.subscribe(tokens)
        ws.set_mode(ws.MODE_FULL, tokens)

    def on_error(ws, code, reason):
        print(f"KiteTicker error {code}: {reason}")

    def on_reconnect(ws, attempts):
        print(f"KiteTicker reconnecting… attempt {attempts}")

    with _ticker_lock:
        if _ticker:
            try:
                _ticker.close()
            except Exception:
                pass
        kt = KiteTicker(api_key, access_token)
        kt.on_ticks     = on_ticks
        kt.on_connect   = on_connect
        kt.on_error     = on_error
        kt.on_reconnect = on_reconnect
        kt.connect(threaded=True)
        _ticker = kt
        _subscribed_tokens = set(tokens)


def _start_ticker_for(accounts_data: list[tuple[str, str]], tokens: list[int]):
    """Start ticker using first logged-in account's credentials."""
    if not tokens or not accounts_data:
        return
    api_key, access_token = accounts_data[0]
    threading.Thread(
        target=_launch_ticker,
        args=(api_key, access_token, tokens),
        daemon=True,
    ).start()


# ── Shared: fetch all instrument tokens (positions + holdings) ─────────────────

def _fetch_all_tokens(saved: dict) -> tuple[list[int], list[tuple]]:
    """Returns (all_tokens, credentials_list) across all accounts."""
    all_tokens: list[int] = []
    credentials: list[tuple] = []

    for account in ACCOUNTS:
        access_token = saved.get(account["api_key"], "")
        if not access_token:
            continue
        try:
            kite = KiteConnect(api_key=account["api_key"])
            kite.set_access_token(access_token)
            credentials.append((account["api_key"], access_token))

            pos = kite.positions().get("net", [])
            for p in pos:
                if p["quantity"] != 0:
                    all_tokens.append(p["instrument_token"])

            hld = kite.holdings()
            for h in hld:
                all_tokens.append(h["instrument_token"])

        except Exception:
            pass

    return list(set(all_tokens)), credentials


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/login/{account_index}")
def login(account_index: int):
    account = ACCOUNTS[account_index]
    kite = KiteConnect(api_key=account["api_key"])
    return HTMLResponse(f'<script>window.location.href="{kite.login_url()}"</script>')


@app.get("/callback")
def callback(request: Request):
    request_token = request.query_params.get("request_token")
    if not request_token:
        return HTMLResponse("<h2>Error: No request_token received.</h2>")

    saved = load_tokens()
    for account in ACCOUNTS:
        try:
            kite = KiteConnect(api_key=account["api_key"])
            session = kite.generate_session(request_token, api_secret=account["api_secret"])
            saved[account["api_key"]] = session["access_token"]
            save_tokens(saved)
            return HTMLResponse(f"""
                <h2 style="font-family:sans-serif;color:green">
                    Login successful for {account['name']}!
                </h2>
                <p style="font-family:sans-serif"><a href="/">Go to Dashboard</a></p>
            """)
        except Exception:
            continue

    return HTMLResponse("<h2>Login failed. Token exchange error.</h2>")


@app.get("/positions")
def positions():
    saved = load_tokens()
    all_positions = []
    credentials = []

    for account in ACCOUNTS:
        access_token = saved.get(account["api_key"], "")
        if not access_token:
            all_positions.append({"account": account["name"], "error": "Not logged in"})
            continue
        try:
            kite = KiteConnect(api_key=account["api_key"])
            kite.set_access_token(access_token)
            credentials.append((account["api_key"], access_token))
            net = [p for p in kite.positions().get("net", []) if p["quantity"] != 0]
            for p in net:
                p["account"] = account["name"]
                all_positions.append(p)
        except Exception as e:
            all_positions.append({"account": account["name"], "error": str(e)})

    # Refresh ticker with ALL tokens (positions + holdings)
    all_tokens, creds = _fetch_all_tokens(saved)
    if all_tokens and creds:
        _start_ticker_for(creds, all_tokens)

    return JSONResponse(all_positions)


@app.get("/holdings")
def holdings():
    saved = load_tokens()
    all_holdings = []

    for account in ACCOUNTS:
        access_token = saved.get(account["api_key"], "")
        if not access_token:
            all_holdings.append({"account": account["name"], "error": "Not logged in"})
            continue
        try:
            kite = KiteConnect(api_key=account["api_key"])
            kite.set_access_token(access_token)
            hld = kite.holdings()
            for h in hld:
                h["account"] = account["name"]
                all_holdings.append(h)
        except Exception as e:
            all_holdings.append({"account": account["name"], "error": str(e)})

    return JSONResponse(all_holdings)


@app.get("/sync-trades")
def sync_trades():
    """Fetch today's executed trades from Kite and save to DB."""
    saved   = load_tokens()
    results = []

    for account in ACCOUNTS:
        access_token = saved.get(account["api_key"], "")
        if not access_token:
            results.append({"account": account["name"], "error": "Not logged in", "inserted": 0})
            continue
        try:
            kite = KiteConnect(api_key=account["api_key"])
            kite.set_access_token(access_token)
            trades  = kite.trades()
            inserted = database.sync_kite_trades(trades, account["name"])
            results.append({"account": account["name"], "fetched": len(trades), "inserted": inserted})
        except Exception as e:
            results.append({"account": account["name"], "error": str(e), "inserted": 0})

    return JSONResponse({"synced": results, "db_stats": database.get_stats()})


@app.get("/trade-history")
def trade_history(symbol: str = None, from_date: str = None,
                  to_date: str = None, trade_type: str = None):
    trades = database.get_trades(
        symbol=symbol, from_date=from_date,
        to_date=to_date, trade_type=trade_type
    )
    return JSONResponse({"trades": trades, "total": len(trades)})


@app.get("/db-stats")
def db_stats():
    return JSONResponse(database.get_stats())


@app.get("/chart/{instrument_token}")
def chart_data(instrument_token: int, days: int = 180, interval: str = "day"):
    from datetime import datetime, timedelta
    saved = load_tokens()
    for account in ACCOUNTS:
        access_token = saved.get(account["api_key"], "")
        if not access_token:
            continue
        try:
            kite = KiteConnect(api_key=account["api_key"])
            kite.set_access_token(access_token)
            from_dt = datetime.today() - timedelta(days=days)
            to_dt   = datetime.today()
            data    = kite.historical_data(instrument_token, from_dt, to_dt, interval)
            return JSONResponse([{
                "time":   d["date"].strftime("%Y-%m-%d") if hasattr(d["date"], "strftime") else str(d["date"])[:10],
                "open":   d["open"],
                "high":   d["high"],
                "low":    d["low"],
                "close":  d["close"],
                "volume": d.get("volume", 0),
            } for d in data])
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"error": "Not logged in"}, status_code=401)


@app.get("/funds")
def funds():
    saved = load_tokens()
    for account in ACCOUNTS:
        access_token = saved.get(account["api_key"], "")
        if not access_token:
            continue
        try:
            kite = KiteConnect(api_key=account["api_key"])
            kite.set_access_token(access_token)
            m  = kite.margins()
            eq = m.get("equity", {})
            return JSONResponse({
                "available_cash": eq.get("available", {}).get("cash", 0),
                "net":            eq.get("net", 0),
                "used":           eq.get("utilised", {}).get("debits", 0),
            })
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"available_cash": 0, "net": 0, "used": 0})


@app.get("/recent-holdings")
def recent_holdings(days: int = 7):
    from datetime import datetime, timedelta
    saved   = load_tokens()
    cutoff  = (datetime.today() - timedelta(days=days)).strftime("%Y-%m-%d")
    result  = []
    errors  = []

    for account in ACCOUNTS:
        access_token = saved.get(account["api_key"], "")
        if not access_token:
            errors.append({"account": account["name"], "error": "Not logged in"})
            continue
        try:
            kite = KiteConnect(api_key=account["api_key"])
            kite.set_access_token(access_token)
            for h in kite.holdings():
                isin = h.get("isin", "")
                row  = database.get_first_buy(isin) or database.get_first_buy_by_symbol(h.get("tradingsymbol", ""))
                if row and row["first_date"] >= cutoff:
                    h["account"]    = account["name"]
                    h["first_buy"]  = row["first_date"]
                    h["last_buy"]   = row["last_date"]
                    h["days_held"]  = (datetime.today() - datetime.strptime(row["first_date"], "%Y-%m-%d")).days
                    h["avg_buy_db"] = round(row["avg_price"], 2)
                    result.append(h)
        except Exception as e:
            errors.append({"account": account["name"], "error": str(e)})

    result.sort(key=lambda h: h.get("first_buy", ""), reverse=True)
    return JSONResponse({"holdings": result, "errors": errors, "total": len(result)})


@app.get("/status")
def status():
    saved = load_tokens()
    return JSONResponse([
        {
            "index": i,
            "name": a["name"],
            "logged_in": bool(saved.get(a["api_key"], "")),
        }
        for i, a in enumerate(ACCOUNTS)
    ])


@app.get("/")
def index():
    return FileResponse("static/index.html", media_type="text/html")
