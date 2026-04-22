"""
Standalone diagnostic for the SDK-based YTD calculation.

Run from terminal:
    cd ~/Desktop/dashboard
    python3 diagnose_pnl.py

Prints, for every account:
    1. Whether the SDK call succeeded
    2. The full traceback if it failed
    3. Every computed value (NetLiq today, NetLiq Jan 1, deposits, fees, P/L)

Paste the entire output back so we can see what TastyTrade actually returns.
"""
import sys
import traceback

import api
import pnl


def main() -> int:
    creds = api.load_credentials()
    if not creds:
        print("ERROR: no saved credentials. Log in via the app first.")
        return 1

    print("Refreshing access token...")
    token, err = api.get_access_token(creds["refresh_token"], creds["secret_token"])
    if not token:
        print(f"ERROR getting access token: {err}")
        return 1
    print("OK access token acquired\n")

    accounts = api.list_accounts(token)
    if not accounts:
        print("No accounts returned.")
        return 1

    for acct in accounts:
        num  = acct.get("account-number", "?")
        nick = acct.get("nickname") or num
        bar  = "─" * 60
        print(f"\n{bar}\n  {nick}  ({num})\n{bar}")

        # 1. Run the SDK path with raise_on_error so we see the real exception
        print("\n[SDK path]")
        try:
            result = pnl.compute_ytd_pnl(token, num, raise_on_error=True)
            if result is None:
                print("  Returned None (no exception raised — odd)")
            else:
                for k, v in result.items():
                    print(f"  {k:24s} {v}")
        except Exception:
            print("  EXCEPTION in pnl.compute_ytd_pnl():")
            traceback.print_exc()

        # 2. Show Money Movement breakdown from the raw transactions
        print("\n[Money Movement breakdown]")
        try:
            txns = pnl._get_history_ytd(token, num)
            from collections import defaultdict
            mm_by_sub = defaultdict(lambda: {"count": 0, "sum": 0.0})
            for t in txns:
                if (t.get("transaction-type") or "").lower() == "money movement":
                    sub = t.get("transaction-sub-type") or "(none)"
                    mm_by_sub[sub]["count"] += 1
                    mm_by_sub[sub]["sum"]   += pnl._signed_value(t)
            for sub, info in sorted(mm_by_sub.items()):
                print(f"  {sub:35s} count={info['count']:>3d}  sum={info['sum']:+.2f}")
        except Exception:
            print("  EXCEPTION:")
            traceback.print_exc()

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
