"""
sheets_export.py — Export portfolio snapshot to Google Sheets.

Creates (or updates) a Google Sheet with the current portfolio state:
  Sheet 1: "Summary"   — account balances, portfolio Greeks, timestamp
  Sheet 2: "Positions" — every open position with quotes + Greeks
  Sheet 3: "Strategies" — strategy-level roll-up

Uses the Google Sheets REST API directly (no gspread / google-api-python-client
dependency) so the bundled app stays lean.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Tuple

import requests


_SHEETS_BASE = "https://sheets.googleapis.com/v4/spreadsheets"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}",
            "Content-Type": "application/json"}


def _safe(v, fmt=None):
    """Coerce a value to something Sheets-friendly."""
    if v is None:
        return ""
    if isinstance(v, float):
        if v != v:  # NaN
            return ""
        if fmt == "$":
            return round(v, 2)
        if fmt == "greek":
            return round(v, 4)
        return round(v, 2)
    return v


# ── Build row data ──────────────────────────────────────────────────────────

def _summary_rows(accounts: list[dict], timestamp: str) -> list[list]:
    """Build the Summary sheet rows."""
    rows = [
        ["Portfolio Snapshot", "", "", timestamp],
        [],
        ["Account", "Net Liquidating Value", "Cash Balance", "Maintenance Req",
         "Positions Count"],
    ]
    for acct in accounts:
        bal = acct.get("balances") or {}
        n_pos = len(acct.get("positions", []))
        rows.append([
            acct.get("nickname") or acct.get("number", "?"),
            _safe(bal.get("net-liquidating-value"), "$"),
            _safe(bal.get("cash-balance"), "$"),
            _safe(bal.get("maintenance-requirement"), "$"),
            n_pos,
        ])

    # Portfolio-wide Greeks
    rows += [[], ["Portfolio Greeks"]]
    rows.append(["", "Net Delta", "Net Theta", "Net Gamma", "Net Vega"])
    totals = {"delta": 0.0, "theta": 0.0, "gamma": 0.0, "vega": 0.0}
    for acct in accounts:
        for p in acct.get("positions", []):
            mult = getattr(p, "multiplier", 100)
            sign = getattr(p, "sign", 1)
            qty  = getattr(p, "quantity", 0)
            for g in ("delta", "theta", "gamma", "vega"):
                val = getattr(p, g, None)
                if val is not None:
                    totals[g] += val * mult * qty * sign
    rows.append([
        "Total",
        _safe(totals["delta"], "greek"),
        _safe(totals["theta"], "greek"),
        _safe(totals["gamma"], "greek"),
        _safe(totals["vega"], "greek"),
    ])
    return rows


def _positions_rows(accounts: list[dict]) -> list[list]:
    """Build the Positions sheet rows."""
    header = [
        "Account", "Symbol", "Root", "Type", "Strike", "Expiration", "DTE",
        "Qty", "Direction", "Avg Open", "Mark Price", "Market Value",
        "P&L ($)", "P&L (%)", "Delta", "Theta", "Gamma", "Vega",
    ]
    rows = [header]
    for acct in accounts:
        acct_name = acct.get("nickname") or acct.get("number", "?")
        for p in acct.get("positions", []):
            dte = None
            if p.expires_at:
                dte = (p.expires_at - datetime.now(timezone.utc)).days
            rows.append([
                acct_name,
                p.symbol,
                p.root,
                p.type_label if hasattr(p, "type_label") else (
                    "Call" if getattr(p, "call_put", "") == "C"
                    else "Put" if getattr(p, "call_put", "") == "P"
                    else getattr(p, "instrument_type", "")),
                _safe(getattr(p, "strike", None)),
                p.expires_at.strftime("%Y-%m-%d") if p.expires_at else "",
                dte if dte is not None else "",
                p.quantity,
                "Long" if p.is_long else "Short",
                _safe(p.avg_open_price, "$"),
                _safe(p.mark_price, "$"),
                _safe(getattr(p, "market_value", None), "$"),
                _safe(getattr(p, "pnl", None), "$"),
                _safe(getattr(p, "pnl_pct", None)),
                _safe(getattr(p, "delta", None), "greek"),
                _safe(getattr(p, "theta", None), "greek"),
                _safe(getattr(p, "gamma", None), "greek"),
                _safe(getattr(p, "vega", None), "greek"),
            ])
    return rows


def _strategies_rows(accounts: list[dict],
                     strategies_all: dict,
                     all_strategies: list) -> list[list]:
    """Build the Strategies sheet rows from the rendered strategy objects."""
    header = [
        "Account", "Strategy", "Root", "DTE", "Credit/Debit",
        "Market Value", "P&L ($)", "P&L (%)",
        "Net Delta", "Net Theta", "Net Gamma", "Net Vega", "Legs",
    ]
    rows = [header]
    for strat in all_strategies:
        # Figure out account from the first leg's account
        acct_name = getattr(strat, "_acct_name", "") or strat.root
        legs_desc = []
        for leg in strat.legs:
            d = "+" if leg.is_long else "-"
            s = getattr(leg, "strike", "")
            cp = getattr(leg, "call_put", "")
            legs_desc.append(f"{d}{int(leg.quantity)} {s}{cp}")
        rows.append([
            acct_name,
            strat.name,
            strat.root,
            strat.dte if strat.dte is not None else "",
            _safe(strat.credit_debit, "$"),
            _safe(strat.market_value, "$"),
            _safe(strat.pnl, "$"),
            _safe(strat.pnl_pct),
            _safe(strat.net_delta, "greek"),
            _safe(strat.net_theta, "greek"),
            _safe(strat.net_gamma, "greek"),
            _safe(strat.net_vega, "greek"),
            " / ".join(legs_desc),
        ])
    return rows


# ── Sheets API calls ────────────────────────────────────────────────────────

def _create_spreadsheet(token: str, title: str) -> Optional[dict]:
    """Create a new spreadsheet with three sheets. Returns the API response."""
    body = {
        "properties": {"title": title},
        "sheets": [
            {"properties": {"title": "Summary",    "index": 0}},
            {"properties": {"title": "Positions",  "index": 1}},
            {"properties": {"title": "Strategies", "index": 2}},
        ],
    }
    try:
        r = requests.post(_SHEETS_BASE, headers=_hdr(token), json=body, timeout=15)
        if r.ok:
            return r.json()
        print(f"[sheets] create failed: {r.status_code} {r.text[:300]}", flush=True)
        return None
    except Exception as e:
        print(f"[sheets] create error: {e}", flush=True)
        return None


def _spreadsheet_exists(token: str, spreadsheet_id: str) -> bool:
    """Check if a spreadsheet still exists and is accessible."""
    try:
        r = requests.get(
            f"{_SHEETS_BASE}/{spreadsheet_id}",
            headers=_hdr(token),
            params={"fields": "spreadsheetId"},
            timeout=10,
        )
        return r.ok
    except Exception:
        return False


def _clear_and_write(token: str, spreadsheet_id: str,
                     sheet_name: str, rows: list[list]) -> bool:
    """Clear a sheet and write new rows."""
    range_str = f"{sheet_name}!A1"
    # Clear existing data
    try:
        requests.post(
            f"{_SHEETS_BASE}/{spreadsheet_id}/values/{sheet_name}:clear",
            headers=_hdr(token),
            json={},
            timeout=10,
        )
    except Exception:
        pass  # OK if it fails (sheet might be empty)

    # Write new data
    try:
        r = requests.put(
            f"{_SHEETS_BASE}/{spreadsheet_id}/values/{range_str}",
            headers=_hdr(token),
            params={"valueInputOption": "RAW"},
            json={"range": range_str, "values": rows},
            timeout=15,
        )
        if not r.ok:
            print(f"[sheets] write {sheet_name}: {r.status_code} {r.text[:200]}",
                  flush=True)
            return False
        return True
    except Exception as e:
        print(f"[sheets] write {sheet_name} error: {e}", flush=True)
        return False


def _format_spreadsheet(token: str, spreadsheet_id: str,
                        sheet_ids: dict[str, int]) -> None:
    """Apply formatting: bold headers, auto-resize columns, freeze header row."""
    reqs = []
    for name, sid in sheet_ids.items():
        # Bold first row
        reqs.append({
            "repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
                "cell": {"userEnteredFormat": {
                    "textFormat": {"bold": True},
                    "backgroundColor": {"red": 0.15, "green": 0.15, "blue": 0.18},
                    "textFormat": {"bold": True,
                                   "foregroundColor": {"red": 0.9, "green": 0.9, "blue": 0.9}},
                }},
                "fields": "userEnteredFormat(textFormat,backgroundColor)",
            }
        })
        # Freeze first row
        reqs.append({
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sid,
                    "gridProperties": {"frozenRowCount": 1},
                },
                "fields": "gridProperties.frozenRowCount",
            }
        })
        # Auto-resize columns
        reqs.append({
            "autoResizeDimensions": {
                "dimensions": {
                    "sheetId": sid,
                    "dimension": "COLUMNS",
                    "startIndex": 0,
                    "endIndex": 20,
                },
            }
        })

    if reqs:
        try:
            requests.post(
                f"{_SHEETS_BASE}/{spreadsheet_id}:batchUpdate",
                headers=_hdr(token),
                json={"requests": reqs},
                timeout=15,
            )
        except Exception as e:
            print(f"[sheets] format error: {e}", flush=True)


# ── Public API ──────────────────────────────────────────────────────────────

def export_portfolio(
    google_access_token: str,
    accounts: list[dict],
    all_strategies: list,
    strategies_all: dict | None = None,
    existing_spreadsheet_id: str | None = None,
) -> Tuple[Optional[str], Optional[str], str]:
    """
    Export the current portfolio to Google Sheets.

    Returns (spreadsheet_id, url, message).
    - On success: (id, "https://docs.google.com/...", "Exported N positions")
    - On failure: (None, None, "error description")
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    title = f"Portfolio Snapshot — {timestamp}"

    # Try to reuse existing spreadsheet
    spreadsheet_id = existing_spreadsheet_id
    if spreadsheet_id and not _spreadsheet_exists(google_access_token, spreadsheet_id):
        print("[sheets] saved spreadsheet not found — creating new one", flush=True)
        spreadsheet_id = None

    if spreadsheet_id:
        # Update the title
        try:
            requests.post(
                f"{_SHEETS_BASE}/{spreadsheet_id}:batchUpdate",
                headers=_hdr(google_access_token),
                json={"requests": [{
                    "updateSpreadsheetProperties": {
                        "properties": {"title": title},
                        "fields": "title",
                    }
                }]},
                timeout=10,
            )
        except Exception:
            pass
    else:
        result = _create_spreadsheet(google_access_token, title)
        if not result:
            return None, None, "Failed to create Google Sheet — check your Google sign-in."
        spreadsheet_id = result["spreadsheetId"]

    url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}"

    # Get sheet IDs for formatting
    sheet_ids = {}
    try:
        r = requests.get(
            f"{_SHEETS_BASE}/{spreadsheet_id}",
            headers=_hdr(google_access_token),
            params={"fields": "sheets.properties"},
            timeout=10,
        )
        if r.ok:
            for s in r.json().get("sheets", []):
                props = s.get("properties", {})
                sheet_ids[props["title"]] = props["sheetId"]
    except Exception:
        pass

    # Build and write data
    summary_rows = _summary_rows(accounts, timestamp)
    positions_rows = _positions_rows(accounts)
    strategies_rows = _strategies_rows(accounts, strategies_all or {}, all_strategies)

    ok1 = _clear_and_write(google_access_token, spreadsheet_id, "Summary", summary_rows)
    ok2 = _clear_and_write(google_access_token, spreadsheet_id, "Positions", positions_rows)
    ok3 = _clear_and_write(google_access_token, spreadsheet_id, "Strategies", strategies_rows)

    if not (ok1 and ok2 and ok3):
        # Partial write — might need re-auth
        if not ok1 and not ok2 and not ok3:
            return None, None, (
                "Failed to write data — your Google token may lack Sheets permission.\n"
                "Try signing out and back in from Cloud Sync."
            )

    # Apply formatting
    _format_spreadsheet(google_access_token, spreadsheet_id, sheet_ids)

    n_pos = sum(len(a.get("positions", [])) for a in accounts)
    n_strat = len(all_strategies)
    msg = f"Exported {n_pos} positions, {n_strat} strategies"
    return spreadsheet_id, url, msg
