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

        # 2. Cross-check by running each SDK call individually
        print("\n[Direct SDK calls]")
        try:
            from tastytrade import Account
            sess = pnl.make_oauth_session(token)
            account = Account.model_construct(account_number=num)

            print("  get_balances() ...")
            bal = account.get_balances(sess)
            print(f"    net_liquidating_value = {bal.net_liquidating_value}")

            print("  get_history(start_date=Jan 1) ...")
            from datetime import date
            txns = account.get_history(sess, start_date=date(date.today().year, 1, 1))
            print(f"    {len(txns)} transactions")
            if txns:
                t = txns[0]
                print(f"    first: type={t.transaction_type!r} sub={t.transaction_sub_type!r} "
                      f"value={t.value} commission={t.commission}")

            print("  get_net_liquidating_value_history(start_time=Jan 1) ...")
            from datetime import datetime, timezone
            start = datetime(date.today().year, 1, 1, tzinfo=timezone.utc)
            hist = account.get_net_liquidating_value_history(sess, start_time=start)
            print(f"    {len(hist)} snapshots")
            if hist:
                first = hist[0]
                print(f"    first snapshot fields: {list(first.model_fields.keys())}")
                print(f"    first snapshot data:   {first.model_dump()}")

        except Exception:
            print("  EXCEPTION:")
            traceback.print_exc()

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
