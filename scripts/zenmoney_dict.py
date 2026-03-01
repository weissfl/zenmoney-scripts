#!/usr/bin/env python3
"""zenmoney_dict.py

MODULE_CONTRACT
- PURPOSE:
  Read-only dictionary/reference printer for ZenMoney entities.
  Fetches a first-sync Diff snapshot (serverTimestamp=0) and prints selected entity lists.

- INPUTS:
  - Env:
    - ZENMONEY_API_KEY: required; used as Bearer token value in Authorization header.
  - Args:
    - --accounts <substring>    (optional filter by account.title)
    - --tags <substring>        (optional filter by tag.title)
    - --instruments <substring> (optional filter by instrument.shortTitle/title)
    - --no-accounts | --no-tags | --no-instruments | --no-user
    - --timeout-sec <n> (default: 20)

- OUTPUTS:
  - stdout narrative logs (brief) + printed lists.
  - Final line: OK — dict printed  OR  ERROR — ...

- EXIT CODES:
  - 0 success
  - 2 invalid args / missing env
  - 10 temporary external error (timeouts, 5xx)
  - 20 permanent error (401/403, unexpected response shape)

- SIDE EFFECTS:
  - Network: POST https://api.zenmoney.ru/v8/diff/
  - No writes.

MODULE_MAP
- parse_args() -> Args
- zenmoney_diff(token, payload, timeout_sec) -> dict
- fetch_snapshot(token, timeout_sec) -> dict
- print_accounts(snapshot, flt)
- print_tags(snapshot, flt)
- print_instruments(snapshot, flt)
- print_user(snapshot)

CHANGE_SUMMARY
- 2026-03-01: initial implementation (S-003).
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
class Args:
    accounts_filter: str
    tags_filter: str
    instruments_filter: str
    show_accounts: bool
    show_tags: bool
    show_instruments: bool
    show_user: bool
    timeout_sec: int
# END_BLOCK:models


# START_BLOCK:errors
class TemporaryApiError(RuntimeError):
    pass


class PermanentApiError(RuntimeError):
    pass
# END_BLOCK:errors


# START_BLOCK:cli
def parse_args(argv: List[str]) -> Args:
    p = argparse.ArgumentParser(
        prog="zenmoney-dict",
        description="Print ZenMoney reference/dictionary lists (accounts, tags, instruments, user currency).",
    )

    p.add_argument("--accounts", default="", help="Substring filter for accounts by title")
    p.add_argument("--tags", default="", help="Substring filter for tags/categories by title")
    p.add_argument("--instruments", default="", help="Substring filter for instruments by shortTitle/title")

    p.add_argument("--no-accounts", action="store_true", help="Do not print accounts")
    p.add_argument("--no-tags", action="store_true", help="Do not print tags")
    p.add_argument("--no-instruments", action="store_true", help="Do not print instruments")
    p.add_argument("--no-user", action="store_true", help="Do not print user info")

    p.add_argument(
        "--timeout-sec",
        type=int,
        default=DEFAULT_TIMEOUT_SEC,
        help=f"HTTP timeout seconds (default: {DEFAULT_TIMEOUT_SEC})",
    )

    ns = p.parse_args(argv)

    if ns.timeout_sec <= 0 or ns.timeout_sec > 120:
        raise ValueError("timeout-sec must be in range 1..120")

    return Args(
        accounts_filter=str(ns.accounts),
        tags_filter=str(ns.tags),
        instruments_filter=str(ns.instruments),
        show_accounts=not bool(ns.no_accounts),
        show_tags=not bool(ns.no_tags),
        show_instruments=not bool(ns.no_instruments),
        show_user=not bool(ns.no_user),
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
            "User-Agent": "zenmoney-dict/0.1 (scriptcraft)",
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


# START_BLOCK:extract
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


def _match_substring(title: str, flt: str) -> bool:
    if not flt:
        return True
    return flt in title
# END_BLOCK:extract


# START_BLOCK:print
def print_user(snap: Dict[str, Any]) -> None:
    users = _extract_list(snap, "user", "users")
    if not users:
        print("WARN — no user[] in snapshot")
        return
    u0 = users[0]
    uid = u0.get("id")
    cur = u0.get("currency")
    print("User:")
    print(f"  id={uid} | currency_instrument_id={cur}")


def print_accounts(snap: Dict[str, Any], flt: str) -> None:
    accs = _extract_list(snap, "account", "accounts")
    print("Accounts:")
    if not accs:
        print("  (none)")
        return
    for a in accs:
        title = str(a.get("title", ""))
        if not _match_substring(title, flt):
            continue
        print(
            "  "
            + " | ".join(
                [
                    title,
                    f"id={a.get('id')}",
                    f"archive={bool(a.get('archive'))}",
                    f"inBalance={bool(a.get('inBalance'))}",
                    f"instrument={a.get('instrument')}",
                ]
            )
        )


def print_tags(snap: Dict[str, Any], flt: str) -> None:
    tags = _extract_list(snap, "tag", "tags")
    print("Tags:")
    if not tags:
        print("  (none)")
        return
    for t in tags:
        title = str(t.get("title", ""))
        if not _match_substring(title, flt):
            continue
        print(
            "  "
            + " | ".join(
                [
                    title,
                    f"id={t.get('id')}",
                    f"parent={t.get('parent')}",
                ]
            )
        )


def print_instruments(snap: Dict[str, Any], flt: str) -> None:
    insts = _extract_list(snap, "instrument", "instruments")
    print("Instruments:")
    if not insts:
        print("  (none)")
        return
    for i in insts:
        short = str(i.get("shortTitle", ""))
        title = str(i.get("title", ""))
        if flt and (flt not in short) and (flt not in title):
            continue
        print(
            "  "
            + " | ".join(
                [
                    short or title,
                    f"id={i.get('id')}",
                    f"rate={i.get('rate')}",
                ]
            )
        )
# END_BLOCK:print


# START_BLOCK:main
def main(argv: List[str]) -> int:
    try:
        a = parse_args(argv)
    except Exception as e:
        print(f"ERROR — invalid arguments: {e}")
        print("ERROR — dict not printed")
        return 2

    token = os.environ.get("ZENMONEY_API_KEY")
    if not token:
        print("ERROR — missing ZENMONEY_API_KEY")
        print("HINT — export ZENMONEY_API_KEY='...'")
        print("ERROR — dict not printed")
        return 2

    print("Step 1 — fetching snapshot via /v8/diff/")

    try:
        snap = fetch_snapshot(token=token, timeout_sec=a.timeout_sec)
    except TemporaryApiError as e:
        print(f"ERROR — temporary API error: {e}")
        print("ERROR — dict not printed")
        return 10
    except PermanentApiError as e:
        print(f"ERROR — permanent API error: {e}")
        print("ERROR — dict not printed")
        return 20

    if a.show_user:
        print_user(snap)
    if a.show_accounts:
        print_accounts(snap, a.accounts_filter)
    if a.show_tags:
        print_tags(snap, a.tags_filter)
    if a.show_instruments:
        print_instruments(snap, a.instruments_filter)

    print("OK — dict printed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
# END_BLOCK:main
