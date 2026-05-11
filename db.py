"""
Database module — Supabase-backed trade history (REST API via supabase-py).
"""

import csv
import os
from collections import defaultdict
from datetime import datetime

from supabase import create_client, Client

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SECRET_KEY", "")

_client: Client | None = None


def _db() -> Client:
    global _client
    if _client is None:
        _client = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _client


# ── Schema ────────────────────────────────────────────────────────────────────

def init_db():
    pass  # Tables are pre-created in Supabase SQL editor


# ── Import from CSV ───────────────────────────────────────────────────────────

def import_csv(csv_path: str, account: str = "Aurix Capital") -> int:
    """Import a Zerodha tradebook CSV. Returns number of rows upserted."""
    if not os.path.exists(csv_path):
        raise FileNotFoundError(csv_path)

    rows = []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            rows.append({
                "account": account,
                "symbol": r["symbol"].strip(),
                "isin": r.get("isin", "").strip() or None,
                "trade_date": r["trade_date"].strip(),
                "exchange": r.get("exchange", "").strip(),
                "segment": r.get("segment", "").strip(),
                "series": r.get("series", "").strip(),
                "trade_type": r["trade_type"].strip().lower(),
                "quantity": float(r["quantity"]),
                "price": float(r["price"]),
                "trade_id": r.get("trade_id", "").strip() or None,
                "order_id": r.get("order_id", "").strip() or None,
                "order_execution_time": r.get("order_execution_time", "").strip() or None,
                "source": "csv",
            })

    if not rows:
        return 0

    result = (
        _db()
        .table("trades")
        .upsert(rows, on_conflict="trade_id", ignore_duplicates=True)
        .execute()
    )
    inserted = len(result.data) if result.data else 0
    rebuild_first_buy()
    return inserted


# ── Sync from Kite live trades ────────────────────────────────────────────────

def sync_kite_trades(trades: list, account: str = "Aurix Capital") -> int:
    """Upsert today's trades fetched from kite.trades(). Returns rows inserted."""
    rows = []
    for t in trades:
        trade_date = (
            t.get("order_timestamp") or
            t.get("exchange_timestamp") or
            datetime.now().strftime("%Y-%m-%d")
        )
        if hasattr(trade_date, "strftime"):
            trade_date = trade_date.strftime("%Y-%m-%d")
        elif "T" in str(trade_date):
            trade_date = str(trade_date)[:10]

        rows.append({
            "account": account,
            "symbol": t.get("tradingsymbol", ""),
            "isin": None,
            "trade_date": trade_date,
            "exchange": t.get("exchange", ""),
            "trade_type": t.get("transaction_type", "").lower(),
            "quantity": float(t.get("quantity", 0) or t.get("filled_quantity", 0)),
            "price": float(t.get("average_price") or t.get("price", 0)),
            "trade_id": str(t.get("trade_id", "")) or None,
            "order_id": str(t.get("order_id", "")) or None,
            "order_execution_time": str(t.get("order_timestamp", "")) or None,
            "source": "kite",
        })

    if not rows:
        return 0

    result = (
        _db()
        .table("trades")
        .upsert(rows, on_conflict="trade_id", ignore_duplicates=True)
        .execute()
    )
    inserted = len(result.data) if result.data else 0
    if inserted:
        rebuild_first_buy()
    return inserted


# ── First-buy cache ───────────────────────────────────────────────────────────

def rebuild_first_buy():
    result = (
        _db()
        .table("trades")
        .select("isin,symbol,trade_date,quantity,price")
        .eq("trade_type", "buy")
        .execute()
    )
    buy_trades = result.data or []

    groups: dict = defaultdict(
        lambda: {"symbol": "", "dates": [], "qty": 0.0, "value": 0.0}
    )
    for t in buy_trades:
        key = t["isin"] or t["symbol"]
        g = groups[key]
        g["symbol"] = t["symbol"]
        g["dates"].append(t["trade_date"])
        g["qty"] += t["quantity"]
        g["value"] += t["quantity"] * t["price"]

    rows = [
        {
            "isin": isin,
            "symbol": g["symbol"],
            "first_date": min(g["dates"]),
            "last_date": max(g["dates"]),
            "total_qty": g["qty"],
            "avg_price": g["value"] / g["qty"] if g["qty"] else 0,
        }
        for isin, g in groups.items()
    ]

    if rows:
        _db().table("first_buy").upsert(rows, on_conflict="isin").execute()


def get_first_buy(isin: str) -> dict | None:
    result = (
        _db().table("first_buy").select("*").eq("isin", isin).limit(1).execute()
    )
    return result.data[0] if result.data else None


def get_first_buy_by_symbol(symbol: str) -> dict | None:
    result = (
        _db().table("first_buy").select("*").eq("symbol", symbol).limit(1).execute()
    )
    return result.data[0] if result.data else None


# ── Query helpers ─────────────────────────────────────────────────────────────

def get_trades(symbol: str = None, isin: str = None,
               from_date: str = None, to_date: str = None,
               trade_type: str = None) -> list[dict]:
    q = _db().table("trades").select("*")
    if symbol:
        q = q.eq("symbol", symbol)
    if isin:
        q = q.eq("isin", isin)
    if from_date:
        q = q.gte("trade_date", from_date)
    if to_date:
        q = q.lte("trade_date", to_date)
    if trade_type:
        q = q.eq("trade_type", trade_type.lower())
    result = q.order("trade_date", desc=True).order("order_execution_time", desc=True).execute()
    return result.data or []


def get_stats() -> dict:
    result = (
        _db()
        .table("trades")
        .select("isin,symbol,trade_date,trade_type,created_at")
        .execute()
    )
    trades = result.data or []
    if not trades:
        return {}

    dates = [t["trade_date"] for t in trades if t.get("trade_date")]
    isins = {t.get("isin") or t.get("symbol") for t in trades}
    buys = sum(1 for t in trades if t.get("trade_type") == "buy")
    sells = sum(1 for t in trades if t.get("trade_type") == "sell")
    created = {t["created_at"][:10] for t in trades if t.get("created_at")}

    return {
        "total_trades": len(trades),
        "unique_stocks": len(isins),
        "earliest_date": min(dates) if dates else None,
        "latest_date": max(dates) if dates else None,
        "total_buys": buys,
        "total_sells": sells,
        "days_synced": len(created),
    }


# ── Bootstrap ─────────────────────────────────────────────────────────────────

def bootstrap(csv_path: str = None):
    """Import CSV if provided and DB is empty."""
    init_db()
    if csv_path:
        result = _db().table("trades").select("id", count="exact").limit(1).execute()
        count = result.count or 0
        if count == 0:
            n = import_csv(csv_path)
            print(f"[db] Imported {n} trades from CSV")
        else:
            print(f"[db] DB already has {count} trades, skipping CSV import")
