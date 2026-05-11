"""
Database module — Supabase/PostgreSQL-backed trade history.
"""

import csv
import os
from contextlib import contextmanager
from datetime import datetime

import psycopg2
import psycopg2.extras

DATABASE_URL = os.environ.get("DATABASE_URL", "")


# ── Connection ────────────────────────────────────────────────────────────────

@contextmanager
def get_conn():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _cursor(conn):
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


# ── Schema ────────────────────────────────────────────────────────────────────

def init_db():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
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
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_trades_isin     ON trades (isin)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_trades_symbol   ON trades (symbol)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_trades_date     ON trades (trade_date)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_trades_order_id ON trades (order_id)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS first_buy (
                isin        TEXT PRIMARY KEY,
                symbol      TEXT,
                first_date  TEXT NOT NULL,
                last_date   TEXT NOT NULL,
                total_qty   REAL NOT NULL,
                avg_price   REAL NOT NULL
            )
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
        cur = conn.cursor()
        for row in rows:
            try:
                cur.execute("""
                    INSERT INTO trades
                        (account, symbol, isin, trade_date, exchange, segment, series,
                         trade_type, quantity, price, trade_id, order_id,
                         order_execution_time, source)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (trade_id) DO NOTHING
                """, row)
                inserted += cur.rowcount
            except Exception:
                pass

    rebuild_first_buy()
    return inserted


# ── Sync from Kite live trades ────────────────────────────────────────────────

def sync_kite_trades(trades: list, account: str = "Aurix Capital") -> int:
    """Upsert today's trades fetched from kite.trades(). Returns new rows inserted."""
    inserted = 0
    with get_conn() as conn:
        cur = conn.cursor()
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

            try:
                cur.execute("""
                    INSERT INTO trades
                        (account, symbol, isin, trade_date, exchange,
                         trade_type, quantity, price, trade_id, order_id,
                         order_execution_time, source)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (trade_id) DO NOTHING
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
                inserted += cur.rowcount
            except Exception:
                pass

    if inserted:
        rebuild_first_buy()
    return inserted


# ── First-buy cache ───────────────────────────────────────────────────────────

def rebuild_first_buy():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM first_buy")
        cur.execute("""
            INSERT INTO first_buy (isin, symbol, first_date, last_date, total_qty, avg_price)
            SELECT
                COALESCE(isin, symbol)                        AS isin,
                symbol,
                MIN(trade_date)::text                         AS first_date,
                MAX(trade_date)::text                         AS last_date,
                SUM(quantity)                                 AS total_qty,
                SUM(quantity * price) / SUM(quantity)         AS avg_price
            FROM trades
            WHERE trade_type = 'buy'
            GROUP BY COALESCE(isin, symbol), symbol
        """)


def get_first_buy(isin: str) -> dict | None:
    with get_conn() as conn:
        cur = _cursor(conn)
        cur.execute("SELECT * FROM first_buy WHERE isin = %s", (isin,))
        row = cur.fetchone()
        return dict(row) if row else None


def get_first_buy_by_symbol(symbol: str) -> dict | None:
    with get_conn() as conn:
        cur = _cursor(conn)
        cur.execute("SELECT * FROM first_buy WHERE symbol = %s", (symbol,))
        row = cur.fetchone()
        return dict(row) if row else None


# ── Query helpers ─────────────────────────────────────────────────────────────

def get_trades(symbol: str = None, isin: str = None,
               from_date: str = None, to_date: str = None,
               trade_type: str = None) -> list[dict]:
    clauses, params = [], []
    if symbol:
        clauses.append("symbol = %s"); params.append(symbol)
    if isin:
        clauses.append("isin = %s"); params.append(isin)
    if from_date:
        clauses.append("trade_date >= %s"); params.append(from_date)
    if to_date:
        clauses.append("trade_date <= %s"); params.append(to_date)
    if trade_type:
        clauses.append("trade_type = %s"); params.append(trade_type.lower())

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with get_conn() as conn:
        cur = _cursor(conn)
        cur.execute(
            f"SELECT * FROM trades {where} ORDER BY trade_date DESC, order_execution_time DESC",
            params
        )
        return [dict(r) for r in cur.fetchall()]


def get_stats() -> dict:
    with get_conn() as conn:
        cur = _cursor(conn)
        cur.execute("""
            SELECT
                COUNT(*)                                            AS total_trades,
                COUNT(DISTINCT COALESCE(isin, symbol))              AS unique_stocks,
                MIN(trade_date)::text                               AS earliest_date,
                MAX(trade_date)::text                               AS latest_date,
                SUM(CASE WHEN trade_type='buy'  THEN 1 ELSE 0 END) AS total_buys,
                SUM(CASE WHEN trade_type='sell' THEN 1 ELSE 0 END) AS total_sells,
                COUNT(DISTINCT created_at::date)                    AS days_synced
            FROM trades
        """)
        row = cur.fetchone()
        return dict(row) if row else {}


# ── Bootstrap ─────────────────────────────────────────────────────────────────

def bootstrap(csv_path: str = None):
    """Create tables, import CSV if provided and DB is empty."""
    init_db()
    if csv_path:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM trades")
            count = cur.fetchone()[0]
        if count == 0:
            n = import_csv(csv_path)
            print(f"[db] Imported {n} trades from CSV")
        else:
            print(f"[db] DB already has data, skipping CSV import")
