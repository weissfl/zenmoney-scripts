#!/usr/bin/env python3
"""zenmoney_transfer.py

MODULE_CONTRACT
- PURPOSE:
  Create an in-app transfer between two non-archived accounts by posting a single Transaction with
  both outcome and income equal to the transfer amount.

- INPUTS:
  - Env:
    - ZENMONEY_API_KEY: required; used as Bearer token value in Authorization header.
  - Args:
    - --from <uuid|title-substring> (required)
    - --to <uuid|title-substring> (required)
    - --amount <number> (required; accepts 1000, 1000.50, 1 000,50)
    - --comment <text> (optional; default: "Transfer")
    - --date <YYYY-MM-DD> (optional; default: today)
    - --timezone <IANA tz> (optional; default: system)
    - --timeout-sec <n> (optional; default: 20)

- OUTPUTS:
  - stdout narrative logs.
  - On instrument mismatch: prints WARN and still proceeds (1:1 mixed-currency transfer).
  - Final line: OK — transfer added  OR  ERROR — transfer not added

- EXIT CODES:
  - 0 success
  - 2 invalid args / not found / ambiguous
  - 10 temporary external error (timeouts, 5xx)
  - 20 permanent error (401/403, unexpected response shape)

- SIDE EFFECTS:
  - Writes a new transfer transaction into ZenMoney.

MODULE_MAP
- parse_args() -> Args
- normalize_amount(str) -> float
- zenmoney_diff(token, payload, timeout_sec) -> dict
- fetch_snapshot(token, timeout_sec) -> dict
- resolve_one_account(items, query) -> dict
- build_transfer_transaction(...) -> dict
- main(argv) -> int

CHANGE_SUMMARY
- 2026-03-01: initial implementation (S-004).
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
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

try:
    from zoneinfo import ZoneInfo  # py3.9+
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore


# START_BLOCK:constants
API_DIFF_URL = "https://api.zenmoney.ru/v8/diff/"
DEFAULT_TIMEOUT_SEC = 20
# END_BLOCK:constants


# START_BLOCK:models
@dataclass
class Args:
    from_query: str
    to_query: str
    amount: float
    comment: str
    date_str: str
    timezone: Optional[str]
    timeout_sec: int
# END_BLOCK:models


# START_BLOCK:errors
class TemporaryApiError(RuntimeError):
    pass


class PermanentApiError(RuntimeError):
    pass


class InputError(RuntimeError):
    pass
# END_BLOCK:errors


# START_BLOCK:cli
def normalize_amount(s: str) -> float:
    # Accept: "1000", "1000.50", "1 000,50".
    raw = str(s)
    raw = raw.replace("\u00A0", " ")
    raw = raw.replace(" ", "")
    raw = raw.replace(",", ".")
    try:
        val = float(raw)
    except Exception as e:
        raise InputError("amount must be a number") from e
    if val <= 0:
        raise InputError("amount must be > 0")
    return val


def today_yyyy_mm_dd(tz: Optional[str]) -> str:
    if tz and ZoneInfo is not None:
        try:
            now = datetime.now(ZoneInfo(tz))
        except Exception:
            now = datetime.now().astimezone()
    else:
        now = datetime.now().astimezone()
    return now.strftime("%Y-%m-%d")


def parse_args(argv: List[str]) -> Args:
    p = argparse.ArgumentParser(
        prog="zenmoney-transfer",
        description="Create a transfer transaction between two ZenMoney accounts.",
    )
    p.add_argument("--from", dest="from_query", required=True, help="From account UUID or title substring")
    p.add_argument("--to", dest="to_query", required=True, help="To account UUID or title substring")
    p.add_argument("--amount", required=True, help="Amount (e.g. 1000, 1 000,50)")
    p.add_argument("--comment", default="Transfer", help="Comment")
    p.add_argument("--date", default="", help="YYYY-MM-DD (default: today)")
    p.add_argument("--timezone", default="", help="IANA timezone, e.g. Asia/Ho_Chi_Minh")
    p.add_argument(
        "--timeout-sec",
        type=int,
        default=DEFAULT_TIMEOUT_SEC,
        help=f"HTTP timeout seconds (default: {DEFAULT_TIMEOUT_SEC})",
    )

    ns = p.parse_args(argv)

    if ns.timeout_sec <= 0 or ns.timeout_sec > 120:
        raise InputError("timeout-sec must be in range 1..120")

    tz = ns.timezone.strip() or None

    date_str = ns.date.strip()
    if not date_str:
        date_str = today_yyyy_mm_dd(tz)
    else:
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except Exception as e:
            raise InputError("date must be YYYY-MM-DD") from e

    amount = normalize_amount(ns.amount)

    return Args(
        from_query=str(ns.from_query).strip(),
        to_query=str(ns.to_query).strip(),
        amount=amount,
        comment=str(ns.comment),
        date_str=date_str,
        timezone=tz,
        timeout_sec=int(ns.timeout_sec),
    )
# END_BLOCK:cli


# START_BLOCK:http
def zenmoney_diff(*, token: str, payload: Dict[str, Any], timeout_sec: int) -> Dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(
        API_DIFF_URL,
        method="POST",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "zenmoney-transfer/0.1 (scriptcraft)",
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


def fetch_snapshot(*, token: str, timeout_sec: int) -> Dict[str, Any]:
    payload = {
        "currentClientTimestamp": int(time.time()),
        "serverTimestamp": 0,
    }
    return zenmoney_diff(token=token, payload=payload, timeout_sec=timeout_sec)
# END_BLOCK:http


# START_BLOCK:resolve
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


def _looks_like_uuid(s: str) -> bool:
    try:
        uuid.UUID(s)
        return True
    except Exception:
        return False


def resolve_one_account(accounts: List[Dict[str, Any]], query: str) -> Dict[str, Any]:
    if not query:
        raise InputError("account query is empty")

    matches: List[Dict[str, Any]] = []

    if _looks_like_uuid(query):
        for a in accounts:
            if str(a.get("id")) == query:
                if bool(a.get("archive")):
                    continue
                matches.append(a)
    else:
        for a in accounts:
            title = str(a.get("title", ""))
            if query in title:
                if bool(a.get("archive")):
                    continue
                matches.append(a)

    if not matches:
        raise InputError(f"account not found: {query}")

    if len(matches) > 1:
        cand = ", ".join([str(m.get("title", "?")) for m in matches[:5]])
        raise InputError(f"account ambiguous: {query}. Candidates: {cand}")

    return matches[0]
# END_BLOCK:resolve


# START_BLOCK:tx
def build_transfer_transaction(
    *,
    tx_id: str,
    user_id: int,
    date_str: str,
    now_ts: int,
    amount: float,
    from_account_id: str,
    to_account_id: str,
    instrument_id: int,
    comment: str,
) -> Dict[str, Any]:
    # Use full-ish transaction object (similar to zenmoney_add.py) to avoid HTTP 400 schema issues.
    return {
        "id": tx_id,
        "user": user_id,
        "date": date_str,
        "income": amount,
        "outcome": amount,
        "changed": now_ts,
        "created": now_ts,
        "deleted": False,
        "viewed": False,
        "hold": None,
        "source": None,
        "incomeInstrument": instrument_id,
        "outcomeInstrument": instrument_id,
        "incomeAccount": to_account_id,
        "outcomeAccount": from_account_id,
        "tag": [],
        "comment": comment or "Transfer",
        "payee": None,
        "originalPayee": None,
        "opIncome": None,
        "opOutcome": None,
        "opIncomeInstrument": None,
        "opOutcomeInstrument": None,
        "latitude": None,
        "longitude": None,
        "merchant": None,
        "incomeBankID": None,
        "outcomeBankID": None,
        "reminderMarker": None,
    }
# END_BLOCK:tx


# START_BLOCK:main
def main(argv: List[str]) -> int:
    try:
        a = parse_args(argv)
    except InputError as e:
        print(f"ERROR — invalid arguments: {e}")
        print("ERROR — transfer not added")
        return 2
    except Exception as e:
        print(f"ERROR — invalid arguments: {e}")
        print("ERROR — transfer not added")
        return 2

    token = os.environ.get("ZENMONEY_API_KEY")
    if not token:
        print("ERROR — missing ZENMONEY_API_KEY")
        print("HINT — export ZENMONEY_API_KEY='...'")
        print("ERROR — transfer not added")
        return 2

    print("Step 1 — fetching snapshot via /v8/diff/")

    try:
        snap = fetch_snapshot(token=token, timeout_sec=a.timeout_sec)
    except TemporaryApiError as e:
        print(f"ERROR — temporary API error: {e}")
        print("ERROR — transfer not added")
        return 10
    except PermanentApiError as e:
        print(f"ERROR — permanent API error: {e}")
        print("ERROR — transfer not added")
        return 20

    accounts = _extract_list(snap, "account", "accounts")
    users = _extract_list(snap, "user", "users")

    if not users or not isinstance(users[0].get("id"), int):
        print("ERROR — could not determine user id from diff response")
        print("ERROR — transfer not added")
        return 20

    user_id = int(users[0]["id"])

    try:
        from_acc = resolve_one_account(accounts, a.from_query)
        to_acc = resolve_one_account(accounts, a.to_query)
    except InputError as e:
        print(f"ERROR — {e}")
        print("ERROR — transfer not added")
        return 2

    from_id = str(from_acc.get("id"))
    to_id = str(to_acc.get("id"))

    from_inst = from_acc.get("instrument")
    to_inst = to_acc.get("instrument")

    if not isinstance(from_inst, int) or not isinstance(to_inst, int):
        print("ERROR — account instrument missing")
        print("ERROR — transfer not added")
        return 20

    if from_inst != to_inst:
        print("WARN — instruments differ (mixed currency transfer 1:1)")
        print(f"Info — from={from_acc.get('title','?')} instrument={from_inst}")
        print(f"Info — to={to_acc.get('title','?')} instrument={to_inst}")

    now_ts = int(time.time())
    tx_id = str(uuid.uuid4())

    tx = build_transfer_transaction(
        tx_id=tx_id,
        user_id=user_id,
        date_str=a.date_str,
        now_ts=now_ts,
        amount=a.amount,
        from_account_id=from_id,
        to_account_id=to_id,
        instrument_id=from_inst,
        comment=a.comment,
    )

    print("Step 2 — sending transfer via /v8/diff/")

    payload = {
        "currentClientTimestamp": now_ts,
        "serverTimestamp": 0,
        "transaction": [tx],
    }

    try:
        res = zenmoney_diff(token=token, payload=payload, timeout_sec=a.timeout_sec)
    except TemporaryApiError as e:
        print(f"ERROR — temporary API error: {e}")
        print("ERROR — transfer not added")
        return 10
    except PermanentApiError as e:
        print(f"ERROR — permanent API error: {e}")
        print("ERROR — transfer not added")
        return 20

    if not isinstance(res.get("transaction"), list):
        print("ERROR — API response missing transaction list")
        print("ERROR — transfer not added")
        return 20

    print(
        "Info — created id=%s amount=%g date=%s from=%s to=%s"
        % (tx_id, a.amount, a.date_str, from_acc.get("title", "?"), to_acc.get("title", "?"))
    )
    print("OK — transfer added")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
# END_BLOCK:main
