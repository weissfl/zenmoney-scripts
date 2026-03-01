#!/usr/bin/env python3
"""zenmoney_balance.py

MODULE_CONTRACT
- PURPOSE:
  Fetch current account balances from ZenMoney API (read-only) and print a human-readable summary.

- INPUTS:
  - Env:
    - ZENMONEY_API_KEY: required; used as Bearer token value in Authorization header.
  - Args:
    - --account <uuid>: optional; filter output to one account id.
    - --exclude-archived: exclude archived accounts (default: archived INCLUDED).
    - --in-balance-only: include only accounts with inBalance=true (default: true).
    - --all-accounts: include all accounts regardless of inBalance.
    - --timeout-sec <n>: HTTP timeout in seconds (default: 20).
    - --full: expanded output (default: only total base amount and currency)

- OUTPUTS:
  - Default (no flags): prints exactly one line:
      "<amount> <BASE_CUR>"

  - With --full: prints narrative logs + per-account lines:
      "<title> [<CUR>] (<id>)  <balance> (archived)"
    plus totals per currency and total_base.

- EXIT CODES:
  - 0 success
  - 2 invalid args / missing env
  - 10 temporary external error (timeouts, 5xx)
  - 20 permanent error (401/403, unexpected response shape)

- SIDE EFFECTS:
  - Network: POST https://api.zenmoney.ru/v8/diff/
  - No writes.

MODULE_MAP
- parse_args() -> Filters
- zenmoney_diff(token, timeout_sec) -> dict
- extract_accounts(diff_obj) -> list[dict]
- extract_instruments(diff_obj) -> dict[int,dict]
- extract_user_base_currency_instrument_id(diff_obj) -> int?
- compute_totals(accounts, instruments, base_instrument_id, filters) -> dict
- print_full_report(totals_obj) -> None

CHANGE_SUMMARY
- 2026-03-01: initial implementation.
- 2026-03-01: added total_base in user's base currency via instrument rates + user.currency.
- 2026-03-01: default include archived accounts; add --exclude-archived.
- 2026-03-01: mark archived accounts with "(archived)" in output.
- 2026-03-01: default output is only "<amount> <BASE_CUR>"; add --full for expanded report.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


# START_BLOCK:constants
API_DIFF_URL = "https://api.zenmoney.ru/v8/diff/"
DEFAULT_TIMEOUT_SEC = 20
# END_BLOCK:constants


# START_BLOCK:models
@dataclass
class Filters:
    account_id: Optional[str]
    include_archived: bool  # when True, archived accounts are included
    in_balance_only: bool
    timeout_sec: int
    full: bool
# END_BLOCK:models


# START_BLOCK:cli
def parse_args(argv: List[str]) -> Filters:
    p = argparse.ArgumentParser(
        prog="zenmoney-balance",
        description="Fetch ZenMoney account balances (read-only) and print a summary.",
    )
    p.add_argument("--account", dest="account_id", default=None, help="Filter to a single account UUID")
    p.add_argument(
        "--exclude-archived",
        action="store_true",
        help="Exclude archived accounts (default: archived INCLUDED)",
    )
    p.add_argument(
        "--in-balance-only",
        dest="in_balance_only",
        action="store_true",
        default=True,
        help="Include only accounts with inBalance=true (default: true)",
    )
    p.add_argument(
        "--all-accounts",
        dest="in_balance_only",
        action="store_false",
        help="Include all accounts regardless of inBalance",
    )
    p.add_argument(
        "--timeout-sec",
        type=int,
        default=DEFAULT_TIMEOUT_SEC,
        help=f"HTTP timeout seconds (default: {DEFAULT_TIMEOUT_SEC})",
    )
    p.add_argument(
        "--full",
        action="store_true",
        help="Full output: list accounts + per-currency totals + total_base (default: only total_base amount and currency)",
    )

    ns = p.parse_args(argv)

    if ns.timeout_sec <= 0 or ns.timeout_sec > 120:
        raise ValueError("timeout-sec must be in range 1..120")

    return Filters(
        account_id=ns.account_id,
        include_archived=(not bool(ns.exclude_archived)),
        in_balance_only=bool(ns.in_balance_only),
        timeout_sec=int(ns.timeout_sec),
        full=bool(ns.full),
    )
# END_BLOCK:cli


# START_BLOCK:errors
class TemporaryApiError(RuntimeError):
    pass


class PermanentApiError(RuntimeError):
    pass
# END_BLOCK:errors


# START_BLOCK:http
def zenmoney_diff(*, token: str, timeout_sec: int) -> Dict[str, Any]:
    payload = {
        "currentClientTimestamp": int(time.time()),
        "serverTimestamp": 0,
    }
    body = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(
        API_DIFF_URL,
        method="POST",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "zenmoney-balance/0.1 (scriptcraft)",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            status = getattr(resp, "status", None) or 200
            raw = resp.read()
    except urllib.error.HTTPError as e:
        status = e.code
        if status in (401, 403):
            raise PermanentApiError(f"HTTP {status} (auth)") from e
        if 400 <= status < 500:
            raise PermanentApiError(f"HTTP {status}") from e
        raise TemporaryApiError(f"HTTP {status}") from e
    except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
        raise TemporaryApiError("network error") from e

    if status >= 500:
        raise TemporaryApiError(f"HTTP {status}")

    try:
        obj = json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise PermanentApiError("failed to parse JSON") from e

    if not isinstance(obj, dict):
        raise PermanentApiError("unexpected response type")

    return obj
# END_BLOCK:http


# START_BLOCK:extract
def extract_accounts(diff_obj: Dict[str, Any]) -> List[Dict[str, Any]]:
    accounts = diff_obj.get("account")
    if accounts is None:
        accounts = diff_obj.get("accounts")

    if not isinstance(accounts, list):
        raise PermanentApiError("response missing 'account' list")

    out: List[Dict[str, Any]] = []
    for a in accounts:
        if isinstance(a, dict):
            out.append(a)
    return out


def extract_instruments(diff_obj: Dict[str, Any]) -> Dict[int, Dict[str, Any]]:
    """Return mapping instrument_id -> {shortTitle, rate}.

    Note: per API docs, `rate` is the cost of 1 unit of currency in RUB.
    We can still compute totals in *any* base currency by converting via rates.
    """

    instruments = diff_obj.get("instrument")
    if instruments is None:
        instruments = diff_obj.get("instruments")

    if instruments is None or not isinstance(instruments, list):
        return {}

    out: Dict[int, Dict[str, Any]] = {}
    for inst in instruments:
        if not isinstance(inst, dict):
            continue

        inst_id = inst.get("id")
        if not isinstance(inst_id, int):
            continue

        short = inst.get("shortTitle") or inst.get("title")
        rate = inst.get("rate")

        out[inst_id] = {
            "shortTitle": short if isinstance(short, str) else None,
            "rate": float(rate) if isinstance(rate, (int, float)) else None,
        }

    return out


def extract_user_base_currency_instrument_id(diff_obj: Dict[str, Any]) -> Optional[int]:
    users = diff_obj.get("user")
    if users is None:
        users = diff_obj.get("users")
    if not isinstance(users, list) or not users:
        return None
    u0 = users[0]
    if not isinstance(u0, dict):
        return None
    cur = u0.get("currency")
    return cur if isinstance(cur, int) else None
# END_BLOCK:extract


# START_BLOCK:format
def should_include_account(a: Dict[str, Any], f: Filters) -> bool:
    if f.account_id is not None and str(a.get("id")) != f.account_id:
        return False

    if not f.include_archived and bool(a.get("archive")):
        return False

    if f.in_balance_only and not bool(a.get("inBalance")):
        return False

    return True


def format_money(x: Any) -> str:
    if x is None:
        return "(null)"
    if isinstance(x, (int, float)):
        return f"{x:g}"
    return str(x)


def format_amount_plain(x: float) -> str:
    """Human-oriented amount without scientific notation.

    - up to 2 decimals
    - trims trailing zeros
    """
    s = f"{x:.2f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def compute_totals(
    accounts: List[Dict[str, Any]],
    instruments: Dict[int, Dict[str, Any]],
    base_instrument_id: Optional[int],
    f: Filters,
) -> Dict[str, Any]:
    kept = [a for a in accounts if should_include_account(a, f)]

    totals: Dict[str, float] = {}

    total_base: float = 0.0
    total_base_ok = True

    base_cur = "?"
    base_rate = None
    if isinstance(base_instrument_id, int):
        base_meta = instruments.get(base_instrument_id) or {}
        base_cur = base_meta.get("shortTitle") or str(base_instrument_id)
        base_rate = base_meta.get("rate")

    rows: List[Dict[str, Any]] = []

    for a in kept:
        title = str(a.get("title", "(no-title)"))
        acc_id = str(a.get("id", "(no-id)"))
        bal = a.get("balance")

        inst_id = a.get("instrument")
        cur = "?"
        rate = None
        if isinstance(inst_id, int):
            meta = instruments.get(inst_id) or {}
            cur = meta.get("shortTitle") or str(inst_id)
            rate = meta.get("rate")

        is_archived = bool(a.get("archive"))

        rows.append(
            {
                "title": title,
                "id": acc_id,
                "balance": bal,
                "cur": cur,
                "archived": is_archived,
                "rate": rate,
            }
        )

        if isinstance(bal, (int, float)):
            totals[cur] = totals.get(cur, 0.0) + float(bal)

        if (
            isinstance(bal, (int, float))
            and isinstance(rate, (int, float))
            and isinstance(base_rate, (int, float))
            and base_rate != 0
        ):
            total_base += float(bal) * float(rate) / float(base_rate)
        else:
            total_base_ok = False

    return {
        "rows": rows,
        "totals": totals,
        "base_cur": base_cur,
        "total_base": total_base,
        "total_base_ok": total_base_ok,
    }


def print_full_report(totals_obj: Dict[str, Any]) -> None:
    rows = totals_obj["rows"]
    totals = totals_obj["totals"]
    base_cur = totals_obj["base_cur"]
    total_base = totals_obj["total_base"]
    total_base_ok = totals_obj["total_base_ok"]

    if not rows:
        print("WARN — no accounts matched filters")

    for r in rows:
        archived_mark = " (archived)" if r.get("archived") else ""
        print(f"{r['title']} [{r['cur']}] ({r['id']})  {format_money(r['balance'])}{archived_mark}")

    for cur in sorted(totals.keys()):
        print(f"Info — total[{cur}]={totals[cur]:g}")

    if total_base_ok and base_cur != "?":
        print(f"Info — total_base[{base_cur}]={total_base:g}")
    else:
        print("WARN — total_base not available (missing rates/base currency)")
# END_BLOCK:format


# START_BLOCK:main
def main(argv: List[str]) -> int:
    try:
        f = parse_args(argv)
    except Exception as e:
        print(f"ERROR — invalid arguments: {e}")
        return 2

    token = os.environ.get("ZENMONEY_API_KEY")
    if not token:
        print("ERROR — missing ZENMONEY_API_KEY")
        print("HINT — export ZENMONEY_API_KEY='...'; then run zenmoney-balance")
        return 2

    if f.full:
        print("Step 1 — calling ZenMoney /v8/diff/")

    try:
        diff_obj = zenmoney_diff(token=token, timeout_sec=f.timeout_sec)
        accounts = extract_accounts(diff_obj)
        instruments = extract_instruments(diff_obj)
        base_instrument_id = extract_user_base_currency_instrument_id(diff_obj)
    except TemporaryApiError as e:
        print(f"ERROR — temporary API error: {e}")
        return 10
    except PermanentApiError as e:
        print(f"ERROR — permanent API error: {e}")
        return 20
    except Exception as e:
        print(f"ERROR — unexpected error: {e}")
        return 20

    totals_obj = compute_totals(accounts, instruments, base_instrument_id, f)

    # Default output: only total_base amount + currency (no extra lines)
    if not f.full:
        if not totals_obj.get("total_base_ok") or totals_obj.get("base_cur") in (None, "?"):
            print("ERROR — total_base not available (missing rates/base currency)")
            return 20
        amt = float(totals_obj["total_base"])
        cur = str(totals_obj["base_cur"])
        print(f"{format_amount_plain(amt)} {cur}")
        return 0

    print("Step 2 — printing balances")
    print_full_report(totals_obj)

    print("OK — balance fetched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
# END_BLOCK:main
