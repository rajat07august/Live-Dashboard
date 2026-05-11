"""
Database module — SQLite-backed trade history.
Single source of truth for all order/trade dates.
"""

import csv
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime

DATA_DIR = os.environ.get("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "trades.db")


# ── Connection ────────────────────────────────────────────────────────────────

@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Schema ────────────────────────────────────────────────────────────────────

def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                account              TEXT    NOT NULL DEFAULT 'Aurix Capital',
                symbol               TEXT    NOT NULL,
                isin                 TEXT,
                trade_date           DATE    NOT NULL,
                exchange             TEXT,
                segment              TEXT,
                series               TEXT,
                trade_type           TEXT    NOT NULL,   -- buy / sell
                quantity             REAL    NOT NULL,
                price                REAL    NOT NULL,
                trade_id             TEXT    UNIQUE,
                order_id             TEXT,
                order_execution_time TEXT,
                source               TEXT    DEFAULT 'kite',  -- kite | csv
                created_at           TEXT    DEFAULT (datetime('now','localtime'))
            );

            CREATE INDEX IF NOT EXISTS idx_trades_isin       ON trades (isin);
            CREATE INDEX IF NOT EXISTS idx_trades_symbol     ON trades (symbol);
            CREATE INDEX IF NOT EXISTS idx_trades_date       ON trades (trade_date);
            CREATE INDEX IF NOT EXISTS idx_trades_order_id   ON trades (order_id);

            -- Materialised first-buy view (rebuilt on demand)
            CREATE TABLE IF NOT EXISTS first_buy (
                isin        TEXT PRIMARY KEY,
                symbol      TEXT,
                first_date  DATE NOT NULL,
                last_date   DATE NOT NULL,
                total_qty   REAL NOT NULL,
                avg_price   REAL NOT NULL
            );
        """)


# ── Import from CSV ───────────────────────────────────────────────────────────

def import_csv(csv_path: str, account: str = "Aurix Capital") -> int:
    """Import a Zerodha tradebook CSV. Returns number of new rows inserted."""
    if not os.path.exists(csv_path):
        raise FileNotFoundError(csv_path)

    rows = []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            rows.append((
                account,
                r["symbol"].strip(),
                r.get("isin", "").strip() or None,
                r["trade_date"].strip(),
                r.get("exchange", "").strip(),
                r.get("segment", "").strip(),
                r.get("series", "").strip(),
                r["trade_type"].strip().lower(),
                float(r["quantity"]),
                float(r["price"]),
                r.get("trade_id", "").strip() or None,
                r.get("order_id", "").strip() or None,
                r.get("order_execution_time", "").strip() or None,
                "csv",
            ))

    inserted = 0
    with get_conn() as conn:
        for row in rows:
            try:
                conn.execute("""
                    INSERT OR IGNORE INTO trades
                        (account, symbol, isin, trade_date, exchange, segment, series,
                         trade_type, quantity, price, trade_id, order_id,
                         order_execution_time, source)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, row)
                inserted += conn.execute("SELECT changes()").fetchone()[0]
            except Exception:
                pass

    rebuild_first_buy()
    return inserted


# ── Sync from Kite live trades ────────────────────────────────────────────────

def sync_kite_trades(trades: list, account: str = "Aurix Capital") -> int:
    """
    Upsert today's trades fetched from kite.trades().
    Returns number of new rows inserted.
    """
    inserted = 0
    with get_conn() as conn:
        for t in trades:
            trade_date = (
                t.get("order_timestamp") or
                t.get("exchange_timestamp") or
                datetime.now().strftime("%Y-%m-%d")
            )
            # Normalise to date-only string
            if hasattr(trade_date, "strftime"):
                trade_date = trade_date.strftime("%Y-%m-%d")
            elif "T" in str(trade_date):
                trade_date = str(trade_date)[:10]

            try:
                conn.execute("""
                    INSERT OR IGNORE INTO trades
                        (account, symbol, isin, trade_date, exchange,
                         trade_type, quantity, price, trade_id, order_id,
                         order_execution_time, source)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    account,
                    t.get("tradingsymbol", ""),
                    None,
                    trade_date,
                    t.get("exchange", ""),
                    t.get("transaction_type", "").lower(),
                    float(t.get("quantity", 0) or t.get("filled_quantity", 0)),
                    float(t.get("average_price") or t.get("price", 0)),
                    str(t.get("trade_id", "")) or None,
                    str(t.get("order_id", "")) or None,
                    str(t.get("order_timestamp", "")) or None,
                    "kite",
                ))
                inserted += conn.execute("SELECT changes()").fetchone()[0]
            except Exception:
                pass

    if inserted:
        rebuild_first_buy()
    return inserted


# ── First-buy cache ───────────────────────────────────────────────────────────

def rebuild_first_buy():
    with get_conn() as conn:
        conn.execute("DELETE FROM first_buy")
        conn.execute("""
            INSERT INTO first_buy (isin, symbol, first_date, last_date, total_qty, avg_price)
            SELECT
                COALESCE(isin, symbol)          AS key,
                symbol,
                MIN(trade_date)                 AS first_date,
                MAX(trade_date)                 AS last_date,
                SUM(quantity)                   AS total_qty,
                SUM(quantity * price) / SUM(quantity) AS avg_price
            FROM trades
            WHERE trade_type = 'buy'
            GROUP BY COALESCE(isin, symbol)
        """)


def get_first_buy(isin: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM first_buy WHERE isin = ?", (isin,)
        ).fetchone()
        return dict(row) if row else None


def get_first_buy_by_symbol(symbol: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM first_buy WHERE symbol = ?", (symbol,)
        ).fetchone()
        return dict(row) if row else None


# ── Query helpers ─────────────────────────────────────────────────────────────

def get_trades(symbol: str = None, isin: str = None,
               from_date: str = None, to_date: str = None,
               trade_type: str = None) -> list[dict]:
    clauses, params = [], []
    if symbol:
        clauses.append("symbol = ?"); params.append(symbol)
    if isin:
        clauses.append("isin = ?"); params.append(isin)
    if from_date:
        clauses.append("trade_date >= ?"); params.append(from_date)
    if to_date:
        clauses.append("trade_date <= ?"); params.append(to_date)
    if trade_type:
        clauses.append("trade_type = ?"); params.append(trade_type.lower())

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM trades {where} ORDER BY trade_date DESC, order_execution_time DESC",
            params
        ).fetchall()
        return [dict(r) for r in rows]


def get_stats() -> dict:
    with get_conn() as conn:
        row = conn.execute("""
            SELECT
                COUNT(*)                                        AS total_trades,
                COUNT(DISTINCT COALESCE(isin, symbol))          AS unique_stocks,
                MIN(trade_date)                                 AS earliest_date,
                MAX(trade_date)                                 AS latest_date,
                SUM(CASE WHEN trade_type='buy'  THEN 1 ELSE 0 END) AS total_buys,
                SUM(CASE WHEN trade_type='sell' THEN 1 ELSE 0 END) AS total_sells,
                COUNT(DISTINCT DATE(created_at))                AS days_synced
            FROM trades
        """).fetchone()
        return dict(row)


# ── Bootstrap ─────────────────────────────────────────────────────────────────

def bootstrap(csv_path: str = None):
    """Create DB, import CSV if provided and DB is empty."""
    init_db()
    if csv_path:
        with get_conn() as conn:
            count = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        if count == 0:
            n = import_csv(csv_path)
            print(f"[db] Imported {n} trades from CSV")
        else:
            print(f"[db] DB already has data, skipping CSV import")
