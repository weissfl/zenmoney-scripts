#!/usr/bin/env python3
"""zenmoney_add.py

MODULE_CONTRACT
- PURPOSE:
  Add a single income/expense transaction into ZenMoney via the Diff endpoint (POST /v8/diff/).

- INPUTS:
  - Env:
    - ZENMONEY_API_KEY: required; used as Bearer token value in Authorization header.
  - Args:
    - --amount <number> (required)
    - --type income|expense (required)
    - --account <uuid|title-substring> (required)
    - --category <uuid|title-substring> (required)
    - --comment <text> (optional)
    - --date <YYYY-MM-DD> (optional; default: today)
    - --timezone <IANA tz> (optional; default: system)
    - --timeout-sec <n> (optional; default: 20)

- OUTPUTS:
  - stdout narrative logs.
  - On success: prints a short confirmation and ends with `OK — transaction added`.
  - On failure: prints `ERROR — ...` and ends with `ERROR — ...`.

- EXIT CODES:
  - 0 success
  - 2 invalid args / not found / ambiguous
  - 10 temporary external error (timeouts, 5xx)
  - 20 permanent error (401/403, unexpected response shape)

- SIDE EFFECTS:
  - Writes a new transaction into ZenMoney (irreversible via this script).

MODULE_MAP
- parse_args() -> Args
- zenmoney_diff(token, payload, timeout_sec) -> dict
- fetch_snapshot(token, timeout_sec) -> dict
- resolve_one(items, *, kind, query, allow_archived=False) -> dict
- build_transaction(...) -> dict
- main(argv) -> int

CHANGE_SUMMARY
- 2026-03-01: initial implementation (S-002).
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
from typing import Any, Dict, List, Optional, Tuple

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
    amount: float
    tx_type: str
    account_query: str
    category_query: str
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
def parse_args(argv: List[str]) -> Args:
    p = argparse.ArgumentParser(
        prog="zenmoney-add",
        description="Add a single transaction to ZenMoney (income/expense).",
    )
    p.add_argument("--amount", required=True, help="Transaction amount (number)")
    p.add_argument("--type", required=True, choices=["income", "expense"], dest="tx_type")
    p.add_argument("--account", required=True, help="Account UUID or title substring")
    p.add_argument("--category", required=True, help="Category/Tag UUID or title substring")
    p.add_argument("--comment", default="", help="Optional comment")
    p.add_argument("--date", default="", help="YYYY-MM-DD (default: today)")
    p.add_argument("--timezone", default="", help="IANA timezone, e.g. Asia/Ho_Chi_Minh")
    p.add_argument(
        "--timeout-sec",
        type=int,
        default=DEFAULT_TIMEOUT_SEC,
        help=f"HTTP timeout seconds (default: {DEFAULT_TIMEOUT_SEC})",
    )

    ns = p.parse_args(argv)

    try:
        amount = float(str(ns.amount).replace(",", "."))
    except Exception as e:
        raise InputError("amount must be a number") from e

    if amount <= 0:
        raise InputError("amount must be > 0")

    if ns.timeout_sec <= 0 or ns.timeout_sec > 120:
        raise InputError("timeout-sec must be in range 1..120")

    tz = ns.timezone.strip() or None

    date_str = ns.date.strip()
    if not date_str:
        date_str = today_yyyy_mm_dd(tz)
    else:
        # Basic format validation
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except Exception as e:
            raise InputError("date must be YYYY-MM-DD") from e

    return Args(
        amount=amount,
        tx_type=str(ns.tx_type),
        account_query=str(ns.account).strip(),
        category_query=str(ns.category).strip(),
        comment=str(ns.comment),
        date_str=date_str,
        timezone=tz,
        timeout_sec=int(ns.timeout_sec),
    )


def today_yyyy_mm_dd(tz: Optional[str]) -> str:
    if tz and ZoneInfo is not None:
        try:
            now = datetime.now(ZoneInfo(tz))
        except Exception:
            now = datetime.now().astimezone()
    else:
        now = datetime.now().astimezone()
    return now.strftime("%Y-%m-%d")
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
            "User-Agent": "zenmoney-add/0.1 (scriptcraft)",
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
def looks_like_uuid(s: str) -> bool:
    try:
        uuid.UUID(s)
        return True
    except Exception:
        return False


def resolve_one(
    items: List[Dict[str, Any]],
    *,
    kind: str,
    query: str,
    allow_archived: bool,
) -> Dict[str, Any]:
    if not query:
        raise InputError(f"{kind} query is empty")

    matches: List[Dict[str, Any]] = []

    if looks_like_uuid(query):
        for it in items:
            if str(it.get("id")) == query:
                if (not allow_archived) and bool(it.get("archive")):
                    continue
                matches.append(it)
    else:
        for it in items:
            title = str(it.get("title", ""))
            if query in title:
                if (not allow_archived) and bool(it.get("archive")):
                    continue
                matches.append(it)

    if not matches:
        raise InputError(f"{kind} not found: {query}")

    if len(matches) > 1:
        # Provide first few candidates
        cand = ", ".join([str(m.get("title", "?")) for m in matches[:5]])
        raise InputError(f"{kind} ambiguous: {query}. Candidates: {cand}")

    return matches[0]


def extract_list(obj: Dict[str, Any], key: str, alt: str) -> List[Dict[str, Any]]:
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
# END_BLOCK:resolve


# START_BLOCK:tx
def build_transaction(
    *,
    tx_id: str,
    user_id: int,
    date_str: str,
    now_ts: int,
    amount: float,
    tx_type: str,
    account_id: str,
    instrument_id: int,
    tag_id: str,
    comment: str,
) -> Dict[str, Any]:
    if tx_type == "income":
        income = amount
        outcome = 0
    else:
        income = 0
        outcome = amount

    # ZenMoney API tends to accept a "full" Transaction object with many nullable fields.
    # We send a superset similar to the wiki examples to avoid HTTP 400 schema errors.
    return {
        "id": tx_id,
        "user": user_id,
        "date": date_str,
        "income": income,
        "outcome": outcome,
        "changed": now_ts,
        "created": now_ts,
        "deleted": False,
        "viewed": False,
        "hold": None,
        "source": None,
        "incomeInstrument": instrument_id,
        "outcomeInstrument": instrument_id,
        "incomeAccount": account_id,
        "outcomeAccount": account_id,
        "tag": [tag_id],
        "comment": comment or "",
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
        return 2
    except Exception as e:
        print(f"ERROR — invalid arguments: {e}")
        return 2

    token = os.environ.get("ZENMONEY_API_KEY")
    if not token:
        print("ERROR — missing ZENMONEY_API_KEY")
        print("HINT — export ZENMONEY_API_KEY='...'")
        return 2

    print("Step 1 — fetching snapshot via /v8/diff/")

    try:
        snap = fetch_snapshot(token=token, timeout_sec=a.timeout_sec)
    except TemporaryApiError as e:
        print(f"ERROR — temporary API error: {e}")
        print("ERROR — transaction not added")
        return 10
    except PermanentApiError as e:
        print(f"ERROR — permanent API error: {e}")
        print("ERROR — transaction not added")
        return 20

    accounts = extract_list(snap, "account", "accounts")
    tags = extract_list(snap, "tag", "tags")
    users = extract_list(snap, "user", "users")

    if not users or not isinstance(users[0].get("id"), int):
        print("ERROR — could not determine user id from diff response")
        print("ERROR — transaction not added")
        return 20

    user_id = int(users[0]["id"])

    try:
        account = resolve_one(accounts, kind="account", query=a.account_query, allow_archived=False)
        tag = resolve_one(tags, kind="category", query=a.category_query, allow_archived=True)
    except InputError as e:
        print(f"ERROR — {e}")
        print("ERROR — transaction not added")
        return 2

    account_id = str(account.get("id"))
    instrument = account.get("instrument")
    if not isinstance(instrument, int):
        print("ERROR — account instrument missing")
        print("ERROR — transaction not added")
        return 20

    tag_id = str(tag.get("id"))

    now_ts = int(time.time())
    tx_id = str(uuid.uuid4())

    tx = build_transaction(
        tx_id=tx_id,
        user_id=user_id,
        date_str=a.date_str,
        now_ts=now_ts,
        amount=a.amount,
        tx_type=a.tx_type,
        account_id=account_id,
        instrument_id=instrument,
        tag_id=tag_id,
        comment=a.comment,
    )

    print("Step 2 — sending transaction via /v8/diff/")

    payload = {
        "currentClientTimestamp": now_ts,
        "serverTimestamp": 0,
        "transaction": [tx],
    }

    try:
        res = zenmoney_diff(token=token, payload=payload, timeout_sec=a.timeout_sec)
    except TemporaryApiError as e:
        print(f"ERROR — temporary API error: {e}")
        print("ERROR — transaction not added")
        return 10
    except PermanentApiError as e:
        print(f"ERROR — permanent API error: {e}")
        print("ERROR — transaction not added")
        return 20

    # Basic success check
    tx_res = res.get("transaction")
    if not isinstance(tx_res, list):
        print("ERROR — API response missing transaction list")
        print("ERROR — transaction not added")
        return 20

    print(
        f"Info — created id={tx_id} type={a.tx_type} amount={a.amount:g} date={a.date_str} account={account.get('title','?')} category={tag.get('title','?')}"
    )
    print("OK — transaction added")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
# END_BLOCK:main
