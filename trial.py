"""
Trial: Recent Holdings Dashboard (port 8001)
Uses Zerodha tradebook CSV to get accurate first-buy dates.
Shows only holdings first purchased within the last N days.
"""

import json
import os
from datetime import datetime, timedelta

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from kiteconnect import KiteConnect

from config import ACCOUNTS
import db as database

app = FastAPI()

TOKENS_FILE = "tokens.json"
CUTOFF_DAYS = 7


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_tokens() -> dict:
    if os.path.exists(TOKENS_FILE):
        with open(TOKENS_FILE) as f:
            return json.load(f)
    return {}


def filter_recent(holdings: list, cutoff_days: int) -> list:
    cutoff = (datetime.today() - timedelta(days=cutoff_days)).strftime("%Y-%m-%d")
    result = []
    for h in holdings:
        isin = h.get("isin", "")
        row  = database.get_first_buy(isin) or database.get_first_buy_by_symbol(h.get("tradingsymbol", ""))
        if row and row["first_date"] >= cutoff:
            h["first_buy"]   = row["first_date"]
            h["last_buy"]    = row["last_date"]
            h["days_held"]   = (datetime.today() - datetime.strptime(row["first_date"], "%Y-%m-%d")).days
            h["avg_buy_db"]  = round(row["avg_price"], 2)
            result.append(h)
    return result


# ── Route: data ───────────────────────────────────────────────────────────────

@app.get("/recent-holdings")
def recent_holdings(days: int = CUTOFF_DAYS):
    saved   = load_tokens()
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
            all_holdings = kite.holdings()
            recent       = filter_recent(all_holdings, days)
            for h in recent:
                h["account"] = account["name"]
                result.append(h)
        except Exception as e:
            errors.append({"account": account["name"], "error": str(e)})

    # Sort by first_buy descending (newest first)
    result.sort(key=lambda h: h.get("first_buy", ""), reverse=True)

    return JSONResponse({
        "holdings":    result,
        "errors":      errors,
        "cutoff_days": days,
        "total":       len(result),
    })


# ── Route: UI ─────────────────────────────────────────────────────────────────

@app.get("/")
def index():
    return HTMLResponse(PAGE_HTML)


PAGE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <title>Recent Holdings — Aurix Capital Trial</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: 'Segoe UI', sans-serif; background: #0f0f0f; color: #e0e0e0; }

    header {
      background: #1a1a2e; padding: 16px 24px;
      display: flex; justify-content: space-between; align-items: center;
      border-bottom: 1px solid #2a2a4a;
    }
    .header-left h1 { font-size: 1.1rem; color: #7c83fd; }
    .header-left p  { font-size: 0.72rem; color: #555; margin-top: 2px; letter-spacing: 1px; text-transform: uppercase; }
    .header-right   { font-size: 0.75rem; color: #666; }

    .toolbar {
      padding: 12px 24px; background: #111;
      display: flex; gap: 12px; align-items: center; flex-wrap: wrap;
      border-bottom: 1px solid #1e1e1e;
    }
    .btn { background: #7c83fd; color: #fff; border: none; padding: 7px 18px; border-radius: 6px; cursor: pointer; font-size: 0.82rem; font-weight: 700; }
    .btn:hover { background: #5c63dd; }
    .label-tag { font-size: 0.78rem; color: #aaa; }
    select {
      background: #1e1e2e; color: #e0e0e0; border: 1px solid #333;
      padding: 6px 12px; border-radius: 6px; font-size: 0.82rem; cursor: pointer;
    }
    #status { font-size: 0.78rem; color: #666; margin-left: auto; }

    .summary { padding: 14px 24px; background: #161616; display: flex; gap: 16px; flex-wrap: wrap; }
    .card { background: #1e1e2e; border-radius: 8px; padding: 12px 20px; min-width: 150px; }
    .card .label { font-size: 0.68rem; color: #666; text-transform: uppercase; letter-spacing: 1px; }
    .card .value { font-size: 1.2rem; font-weight: 700; margin-top: 4px; }

    .container { padding: 20px 24px; }
    table { width: 100%; border-collapse: collapse; font-size: 0.84rem; background: #1a1a1a; border-radius: 10px; overflow: hidden; }
    thead tr { background: #1e1e3a; }
    th { padding: 11px 14px; text-align: left; color: #888; font-size: 0.72rem; text-transform: uppercase; letter-spacing: .5px; }
    tbody tr { border-bottom: 1px solid #1e1e1e; transition: background .15s; }
    tbody tr:hover { background: #202020; }
    td { padding: 11px 14px; vertical-align: middle; }

    .positive { color: #4caf50; font-weight: 600; }
    .negative { color: #f44336; font-weight: 600; }
    .neutral  { color: #888; }

    .day-chip {
      display: inline-block; padding: 2px 8px; border-radius: 20px;
      font-size: 0.72rem; font-weight: 700; white-space: nowrap;
    }
    .day-chip.fresh  { background: #1a3a1a; color: #4caf50; border: 1px solid #4caf50; }
    .day-chip.recent { background: #1a2a3a; color: #7c83fd; border: 1px solid #7c83fd; }
    .day-chip.older  { background: #2a2a1a; color: #ff9800; border: 1px solid #ff9800; }

    .error-row td { color: #f44336; font-style: italic; }
    .empty { text-align: center; padding: 40px; color: #444; }

    .source-note { font-size: 0.72rem; color: #444; padding: 8px 24px; background: #0d0d0d; border-top: 1px solid #1a1a1a; }
  </style>
</head>
<body>

<header>
  <div class="header-left">
    <h1>RECENT HOLDINGS</h1>
    <p>Aurix Capital &mdash; Trial View</p>
  </div>
  <div class="header-right" id="last-updated"></div>
</header>

<div class="toolbar">
  <button class="btn" onclick="load()">&#8635; Refresh</button>
  <span class="label-tag">Show holdings from last</span>
  <select id="days-select" onchange="load()">
    <option value="3">3 days</option>
    <option value="7" selected>7 days</option>
    <option value="14">14 days</option>
    <option value="30">30 days</option>
    <option value="90">90 days</option>
  </select>
  <span id="status"></span>
</div>

<div class="summary">
  <div class="card">
    <div class="label">Holdings Shown</div>
    <div class="value" id="count">—</div>
  </div>
  <div class="card">
    <div class="label">Total Invested</div>
    <div class="value" id="total-invested">—</div>
  </div>
  <div class="card">
    <div class="label">Current Value</div>
    <div class="value" id="total-value">—</div>
  </div>
  <div class="card">
    <div class="label">Total P&amp;L</div>
    <div class="value" id="total-pnl">—</div>
  </div>
  <div class="card">
    <div class="label">Day Change</div>
    <div class="value" id="total-daychange">—</div>
  </div>
</div>

<div class="container">
  <table>
    <thead>
      <tr>
        <th>Symbol</th>
        <th>Qty</th>
        <th>Avg Price (Kite)</th>
        <th>Avg Price (CSV)</th>
        <th>LTP</th>
        <th>P&amp;L</th>
        <th>Day Change</th>
        <th>Value</th>
        <th>First Bought</th>
        <th>Held</th>
      </tr>
    </thead>
    <tbody id="tbody">
      <tr><td colspan="10" class="empty">Loading…</td></tr>
    </tbody>
  </table>
</div>

<div class="source-note">
  Dates sourced from tradebook CSV &mdash; tradebook-TLU065-EQ.csv &nbsp;|&nbsp;
  Prices from Zerodha Kite API
</div>

<script>
  const R = '&#8377;';

  function fmt(n, dec=2) {
    if (n == null || isNaN(n)) return '—';
    return parseFloat(n).toLocaleString('en-IN', { minimumFractionDigits: dec, maximumFractionDigits: dec });
  }

  function dayChip(days) {
    if (days === 0)      return `<span class="day-chip fresh">Today</span>`;
    if (days === 1)      return `<span class="day-chip fresh">Yesterday</span>`;
    if (days <= 3)       return `<span class="day-chip fresh">${days}d ago</span>`;
    if (days <= 7)       return `<span class="day-chip recent">${days}d ago</span>`;
    return                      `<span class="day-chip older">${days}d ago</span>`;
  }

  async function load() {
    const days = document.getElementById('days-select').value;
    document.getElementById('status').textContent = 'Fetching…';

    try {
      const res  = await fetch(`/recent-holdings?days=${days}`);
      const data = await res.json();
      render(data);
      document.getElementById('last-updated').textContent =
        'Updated: ' + new Date().toLocaleTimeString();
      document.getElementById('status').textContent =
        `${data.total} holding(s) in last ${days} days`;
    } catch (e) {
      document.getElementById('status').textContent = 'Error: ' + e.message;
    }
  }

  function render({ holdings, errors }) {
    document.getElementById('count').textContent = holdings.length;

    let invested = 0, value = 0, pnl = 0, dayChng = 0;
    holdings.forEach(h => {
      const qty = h.quantity || 0;
      invested += (h.average_price || 0) * qty;
      value    += (h.last_price   || 0) * qty;
      pnl      += parseFloat(h.pnl || 0);
      dayChng  += (h.day_change || 0) * qty;
    });

    document.getElementById('total-invested').innerHTML  = R + fmt(invested);

    const valEl = document.getElementById('total-value');
    valEl.innerHTML  = R + fmt(value);

    const pnlEl = document.getElementById('total-pnl');
    pnlEl.innerHTML  = R + fmt(pnl);
    pnlEl.className  = 'value ' + (pnl >= 0 ? 'positive' : 'negative');

    const dcEl = document.getElementById('total-daychange');
    dcEl.innerHTML  = R + fmt(dayChng);
    dcEl.className  = 'value ' + (dayChng >= 0 ? 'positive' : 'negative');

    if (!holdings.length && !errors.length) {
      document.getElementById('tbody').innerHTML =
        `<tr><td colspan="10" class="empty">No holdings found in selected period</td></tr>`;
      return;
    }

    document.getElementById('tbody').innerHTML =
      holdings.map(h => {
        const qty     = h.quantity || 0;
        const pnl     = parseFloat(h.pnl || 0);
        const dayC    = (h.day_change || 0) * qty;
        const val     = (h.last_price || 0) * qty;
        const days    = h.days_held ?? '—';
        const priceDiff = h.avg_buy_db && h.average_price
          ? ((h.average_price - h.avg_buy_db) / h.avg_buy_db * 100).toFixed(2)
          : null;

        return `
          <tr>
            <td>
              <strong>${h.tradingsymbol}</strong><br>
              <span style="color:#444;font-size:.7rem">${h.exchange}</span>
            </td>
            <td>${qty.toLocaleString()}</td>
            <td>${R}${fmt(h.average_price)}</td>
            <td>
              ${R}${fmt(h.avg_buy_db)}
              ${priceDiff !== null
                ? `<br><span style="font-size:.7rem;color:${Math.abs(priceDiff)<0.5?'#555':priceDiff>0?'#f44336':'#4caf50'}">${priceDiff > 0 ? '+' : ''}${priceDiff}%</span>`
                : ''}
            </td>
            <td>${R}${fmt(h.last_price)}</td>
            <td class="${pnl >= 0 ? 'positive' : 'negative'}">${R}${fmt(pnl)}</td>
            <td class="${dayC >= 0 ? 'positive' : 'negative'}">${R}${fmt(dayC)}</td>
            <td class="neutral">${R}${fmt(val)}</td>
            <td style="color:#888;font-size:.82rem">${h.first_buy || '—'}</td>
            <td>${dayChip(days)}</td>
          </tr>`;
      }).join('') +
      errors.map(e =>
        `<tr class="error-row"><td>${e.account}</td><td colspan="9">Error: ${e.error}</td></tr>`
      ).join('');
  }

  load();
  setInterval(load, 30000);
</script>
</body>
</html>
"""
