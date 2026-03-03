#!/usr/bin/env python3
# FILE: scripts/zenmoney_balance.py
# VERSION: 0.2.0
#
# START_MODULE_CONTRACT
#   PURPOSE:
#     Fetch current account balances from ZenMoney API (read-only) and print a summary.
#
#   SCOPE:
#     - Calls Diff first-sync endpoint (serverTimestamp=0) and reads account/user/instrument entities.
#     - Computes totals per currency and a base-currency total using user.currency + instrument.rate.
#     - No writes.
#
#   INPUTS:
#     - Env:
#       - ZENMONEY_API_KEY (required): Bearer token.
#     - Args:
#       - --account <uuid> (optional): filter to one account id.
#       - --exclude-archived: exclude archived accounts (default: archived INCLUDED).
#       - --in-balance-only: include only accounts with inBalance=true (default: true).
#       - --all-accounts: include all accounts regardless of inBalance.
#       - --timeout-sec <n>: HTTP timeout seconds (default: 20).
#       - --full: expanded output (per-account lines + per-currency totals).
#       - --json: stdout JSON-only; narrative logs go to stderr.
#
#   OUTPUTS:
#     - Default (no flags): narrative logs ending with:
#         OK — balance fetched
#       and includes:
#         Info — total_base[BASE_CUR]=...
#     - With --full: also prints per-account lines and totals per currency.
#     - With --json: stdout is JSON-only; logs go to stderr.
#
#   SIDE_EFFECTS:
#     - Network: POST https://api.zenmoney.ru/v8/diff/
#
#   FAILURE_MODES:
#     - Missing/invalid inputs -> exit 2
#     - Temporary API/network issues -> exit 10
#     - Permanent API/auth/shape issues -> exit 20
#
# END_MODULE_CONTRACT
#
# START_MODULE_MAP
#   - build_parser() -> argparse.ArgumentParser
#   - parse_args(argv) -> Args
#   - fetch_diff_snapshot(token, timeout_sec) -> dict
#   - extract_accounts(diff) -> list[dict]
#   - extract_instruments(diff) -> dict[int,dict]
#   - extract_base_currency(diff) -> (base_instrument_id:int?, base_cur:str?, base_rate:float?)
#   - compute_totals(...) -> dict
#   - render_full_report(totals) -> None
#   - emit_json(...) -> None
#   - main(argv) -> int
# END_MODULE_MAP
#
# START_CHANGE_SUMMARY
#   2026-03-03: v0.2.0 — Regenerated to new Scriptcraft skeleton: add --json mode, move logs to stderr in JSON mode,
#   and make default output contract-compliant (final OK/ERROR line).
# END_CHANGE_SUMMARY

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
from typing import Any, Dict, List, Optional, Tuple


# START_BLOCK_LOGGING

JSON_MODE = False


def _out(msg: str) -> None:
    # In --json mode, stdout is reserved for JSON-only output.
    stream = sys.stderr if JSON_MODE else sys.stdout
    stream.write(msg.rstrip() + "\n")


def log_step(n: int, msg: str) -> None:
    _out(f"Step {n} — {msg}")


def log_info(msg: str) -> None:
    _out(f"Info — {msg}")


def log_warn(msg: str) -> None:
    _out(f"WARN — {msg}")


def log_ok(msg: str) -> None:
    _out(f"OK — {msg}")


def log_error(msg: str, hint: str | None = None) -> None:
    if hint:
        _out(f"HINT — {hint}")
    _out(f"ERROR — {msg}")


# END_BLOCK_LOGGING


# START_BLOCK_EXIT_CODES

EXIT_OK = 0
EXIT_BAD_INPUT = 2
EXIT_RETRYABLE = 10
EXIT_PERMANENT = 20


# END_BLOCK_EXIT_CODES


# START_BLOCK_CONSTANTS

API_DIFF_URL = "https://api.zenmoney.ru/v8/diff/"
DEFAULT_TIMEOUT_SEC = 20


# END_BLOCK_CONSTANTS


# START_BLOCK_MODELS

@dataclass
class Args:
    account_id: Optional[str]
    exclude_archived: bool
    in_balance_only: bool
    timeout_sec: int
    full: bool
    json: bool


# END_BLOCK_MODELS


# START_BLOCK_CLI

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Fetch ZenMoney balances (read-only) via Diff API")
    p.add_argument("--account", default=None, help="Filter to a single account UUID")
    p.add_argument("--exclude-archived", action="store_true", help="Exclude archived accounts")
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
    p.add_argument("--full", action="store_true", help="Expanded report")
    p.add_argument("--json", action="store_true", help="JSON-only output on stdout (logs go to stderr)")
    return p


def parse_args(argv: List[str]) -> Args:
    ns = build_parser().parse_args(argv)

    if ns.timeout_sec <= 0 or ns.timeout_sec > 120:
        raise ValueError("timeout-sec must be in range 1..120")

    return Args(
        account_id=str(ns.account).strip() if ns.account else None,
        exclude_archived=bool(ns.exclude_archived),
        in_balance_only=bool(ns.in_balance_only),
        timeout_sec=int(ns.timeout_sec),
        full=bool(ns.full),
        json=bool(ns.json),
    )


# END_BLOCK_CLI


# START_BLOCK_JSON

def emit_json(
    ok: bool,
    code: int,
    *,
    data: dict | None = None,
    error: str | None = None,
    hint: str | None = None,
) -> None:
    payload: dict = {
        "ok": ok,
        "code": code,
        "data": data or {},
        "meta": {"version": "0.2.0"},
    }
    if not ok:
        payload["error"] = error or "error"
        if hint:
            payload["hint"] = hint
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")


# END_BLOCK_JSON


# START_BLOCK_HTTP

class TemporaryApiError(RuntimeError):
    pass


class PermanentApiError(RuntimeError):
    pass


def fetch_diff_snapshot(*, token: str, timeout_sec: int) -> Dict[str, Any]:
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
            "User-Agent": "zenmoney-balance/0.2.0 (scriptcraft)",
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


# END_BLOCK_HTTP


# START_BLOCK_EXTRACT

def _extract_list(obj: Dict[str, Any], key: str, alt: str) -> List[Dict[str, Any]]:
    v = obj.get(key)
    if v is None:
        v = obj.get(alt)
    if not isinstance(v, list):
        return []
    out: List[Dict[str, Any]] = []
    for it in v:
        if isinstance(it, dict):
            out.append(it)
    return out


def extract_accounts(diff_obj: Dict[str, Any]) -> List[Dict[str, Any]]:
    accounts = _extract_list(diff_obj, "account", "accounts")
    if not accounts:
        raise PermanentApiError("response missing 'account' list")
    return accounts


def extract_instruments(diff_obj: Dict[str, Any]) -> Dict[int, Dict[str, Any]]:
    instruments = _extract_list(diff_obj, "instrument", "instruments")
    out: Dict[int, Dict[str, Any]] = {}
    for inst in instruments:
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


def extract_base_currency(diff_obj: Dict[str, Any], instruments: Dict[int, Dict[str, Any]]) -> Tuple[Optional[int], Optional[str], Optional[float]]:
    users = _extract_list(diff_obj, "user", "users")
    if not users:
        return None, None, None
    cur = users[0].get("currency")
    if not isinstance(cur, int):
        return None, None, None
    meta = instruments.get(cur) or {}
    base_cur = meta.get("shortTitle")
    base_rate = meta.get("rate")
    return cur, (base_cur if isinstance(base_cur, str) else str(cur)), (base_rate if isinstance(base_rate, (int, float)) else None)


# END_BLOCK_EXTRACT


# START_BLOCK_COMPUTE

def should_include_account(a: Dict[str, Any], args: Args) -> bool:
    if args.account_id is not None and str(a.get("id")) != args.account_id:
        return False

    if args.exclude_archived and bool(a.get("archive")):
        return False

    if args.in_balance_only and not bool(a.get("inBalance")):
        return False

    return True


def compute_totals(
    accounts: List[Dict[str, Any]],
    instruments: Dict[int, Dict[str, Any]],
    base_rate: Optional[float],
    args: Args,
) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    totals: Dict[str, float] = {}
    total_base = 0.0
    total_base_ok = True

    for a in accounts:
        if not should_include_account(a, args):
            continue

        title = str(a.get("title", "(no-title)"))
        acc_id = str(a.get("id", "(no-id)"))
        bal = a.get("balance")
        inst_id = a.get("instrument")
        archived = bool(a.get("archive"))

        cur = "?"
        rate = None
        if isinstance(inst_id, int):
            meta = instruments.get(inst_id) or {}
            cur = meta.get("shortTitle") or str(inst_id)
            rate = meta.get("rate")

        rows.append({"title": title, "id": acc_id, "balance": bal, "cur": cur, "archived": archived, "instrument": inst_id})

        if isinstance(bal, (int, float)):
            totals[cur] = totals.get(cur, 0.0) + float(bal)
        else:
            # If any account has non-numeric balance, we still keep working; it just blocks base total.
            pass

        if isinstance(bal, (int, float)) and isinstance(rate, (int, float)) and isinstance(base_rate, (int, float)) and base_rate != 0:
            total_base += float(bal) * float(rate) / float(base_rate)
        else:
            total_base_ok = False

    return {
        "rows": rows,
        "totals": totals,
        "total_base": total_base,
        "total_base_ok": total_base_ok,
    }


# END_BLOCK_COMPUTE


# START_BLOCK_RENDER

def fmt_amount(x: float) -> str:
    # Keep stable human-readable formatting
    s = f"{x:.2f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def render_full_report(rows: List[Dict[str, Any]], totals: Dict[str, float]) -> None:
    for r in rows:
        archived_mark = " (archived)" if r.get("archived") else ""
        _out(f"{r['title']} [{r['cur']}] ({r['id']})  {r.get('balance')}{archived_mark}")

    for cur in sorted(totals.keys()):
        log_info(f"total[{cur}]={totals[cur]:g}")


# END_BLOCK_RENDER


# START_BLOCK_MAIN

def main(argv: List[str]) -> int:
    global JSON_MODE

    try:
        args = parse_args(argv)
    except Exception as e:
        # No JSON yet; keep it simple.
        log_error(f"invalid arguments: {e}")
        return EXIT_BAD_INPUT

    JSON_MODE = bool(args.json)

    token = os.environ.get("ZENMONEY_API_KEY")
    if not token:
        if JSON_MODE:
            emit_json(False, EXIT_BAD_INPUT, error="missing ZENMONEY_API_KEY", hint="export ZENMONEY_API_KEY='...' and retry")
        else:
            log_error("missing ZENMONEY_API_KEY", hint="export ZENMONEY_API_KEY='...' and retry")
        return EXIT_BAD_INPUT

    log_step(1, "fetching snapshot via /v8/diff/")

    try:
        diff_obj = fetch_diff_snapshot(token=token, timeout_sec=args.timeout_sec)
        accounts = extract_accounts(diff_obj)
        instruments = extract_instruments(diff_obj)
        _base_id, base_cur, base_rate = extract_base_currency(diff_obj, instruments)
    except TemporaryApiError as e:
        if JSON_MODE:
            emit_json(False, EXIT_RETRYABLE, error=str(e))
        else:
            log_error(f"temporary API error: {e}")
        return EXIT_RETRYABLE
    except PermanentApiError as e:
        if JSON_MODE:
            emit_json(False, EXIT_PERMANENT, error=str(e))
        else:
            log_error(f"permanent API error: {e}")
        return EXIT_PERMANENT
    except Exception as e:
        if JSON_MODE:
            emit_json(False, EXIT_PERMANENT, error=f"unexpected error: {e}")
        else:
            log_error(f"unexpected error: {e}")
        return EXIT_PERMANENT

    totals_obj = compute_totals(accounts, instruments, base_rate, args)
    rows = totals_obj["rows"]
    totals = totals_obj["totals"]
    total_base = float(totals_obj["total_base"])
    total_base_ok = bool(totals_obj["total_base_ok"]) and bool(base_cur)

    if JSON_MODE:
        emit_json(
            True,
            EXIT_OK,
            data={
                "base_cur": base_cur,
                "total_base": total_base,
                "total_base_ok": total_base_ok,
                "rows": rows if args.full else [],
                "totals": totals if args.full else {},
            },
        )
        return EXIT_OK

    if args.full:
        log_step(2, "printing balances")
        render_full_report(rows, totals)

    if total_base_ok:
        log_info(f"total_base[{base_cur}]={fmt_amount(total_base)}")
    else:
        log_warn("total_base not available (missing rates/base currency)")

    log_ok("balance fetched")
    return EXIT_OK


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        if JSON_MODE:
            emit_json(False, EXIT_RETRYABLE, error="interrupted", hint="Try again")
        else:
            log_error("interrupted", hint="Try again")
        raise SystemExit(EXIT_RETRYABLE)

# END_BLOCK_MAIN
