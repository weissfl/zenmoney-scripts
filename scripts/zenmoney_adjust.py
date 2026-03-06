#!/usr/bin/env python3
# FILE: scripts/zenmoney_adjust.py
# VERSION: 0.1.0
#
# START_MODULE_CONTRACT
#   PURPOSE:
#     Create exactly one corrective balance adjustment transaction for a chosen account in ZenMoney via the Diff API.
#     This is used to manually align the account balance when a real operation was missed.
#
#   SCOPE:
#     - Fetches snapshot via ZenMoney Diff API first-sync (serverTimestamp=0).
#     - Resolves a single non-archived account by UUID or title substring.
#     - Resolves a single tag ("Корректировка") by UUID or exact title.
#     - Creates exactly one Transaction where incomeAccount == outcomeAccount == selected account.
#     - Posts the transaction back via Diff API and verifies it is accepted (present in response).
#
#   INPUTS:
#     - Env:
#       - ZENMONEY_API_KEY (required): Bearer token.
#     - Args:
#       - --account <uuid|title-substring> (required)
#       - --amount <number> (required; accepts 100, +100, -100; no sign => outcome)
#       - --tag <uuid|exact-title> (optional; default: "Корректировка")
#       - --comment <text> (optional; default: "Корректировка баланса")
#       - --date <YYYY-MM-DD> (optional; default: today)
#       - --timezone <IANA tz> (optional; default: system)
#       - --timeout-sec <n> (optional; default: 20)
#       - --json (stdout JSON-only; narrative logs go to stderr)
#       - -h/--help
#
#   OUTPUTS:
#     - Default: narrative logs (stdout), final line OK — adjustment added / ERROR — ...
#     - --json: stdout JSON-only; logs go to stderr.
#
#   SIDE_EFFECTS:
#     - Network:
#       - POST https://api.zenmoney.ru/v8/diff/ (snapshot)
#       - POST https://api.zenmoney.ru/v8/diff/ (write transaction)
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
#   - parse_args(argv) -> (args, unknown)
#   - fetch_diff_snapshot(token, timeout_sec) -> dict
#   - resolve_account(diff, selector) -> dict
#   - resolve_tag(diff, selector_or_title_exact) -> dict
#   - build_adjustment_transaction(...) -> dict
#   - post_diff(token, timeout_sec, payload) -> dict
#   - verify_transaction_accepted(resp, tx_id) -> None
#   - emit_json(...) -> None
#   - main(argv) -> int
# END_MODULE_MAP
#
# START_CHANGE_SUMMARY
#   2026-03-06: v0.1.0 — Initial implementation: snapshot fetch, account/tag resolution, adjustment transaction creation
# END_CHANGE_SUMMARY

from __future__ import annotations

import argparse
import sys


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


# START_BLOCK_CLI

CLI_DESCRIPTION = "zenmoney-adjust — add exactly one corrective balance adjustment transaction via ZenMoney Diff API"
CLI_USAGE = (
    "ZENMONEY_API_KEY=... python3 scripts/zenmoney_adjust.py --account <uuid|title> --amount <n> [--tag <uuid|exact>] "
    "[--comment <text>] [--date YYYY-MM-DD] [--timezone <IANA>] [--timeout-sec 20] [--json]"
)

AGENT_HELP = """Purpose: add exactly one balance adjustment transaction to a selected (non-archived) account.
- Requires env ZENMONEY_API_KEY (Bearer token).
- Fetches snapshot via POST https://api.zenmoney.ru/v8/diff/ (serverTimestamp=0).
- Resolves account: UUID exact OR title substring (case-sensitive); rejects archive=true; errors if 0 or >1 matches.
- Resolves tag: UUID exact OR EXACT title match; errors if 0 or >1 matches.
- Amount sign sets direction: + => income; - => outcome; no sign => outcome.
- Creates one Transaction with incomeAccount==outcomeAccount==account.id and instruments from account.instrument.
"""


def _print_help() -> None:
    sys.stdout.write(CLI_DESCRIPTION + "\n\n")
    sys.stdout.write(f"Usage: {CLI_USAGE}\n\n")
    sys.stdout.write("Options:\n")
    sys.stdout.write("  --account <selector>       Account UUID or title substring (required)\n")
    sys.stdout.write("  --amount <n>               Adjustment amount (e.g. 100, +100, -100) (required)\n")
    sys.stdout.write("  --tag <selector>           Tag UUID or EXACT title (default: Корректировка)\n")
    sys.stdout.write("  --comment <text>           Comment (default: Корректировка баланса)\n")
    sys.stdout.write("  --date YYYY-MM-DD          Date (default: today)\n")
    sys.stdout.write("  --timezone <IANA>          Timezone (default: system)\n")
    sys.stdout.write("  --timeout-sec <n>          HTTP timeout seconds (default: 20)\n")
    sys.stdout.write("  --json                     JSON-only output on stdout (logs go to stderr)\n")
    sys.stdout.write("  -h/--help                  Show this help\n\n")
    sys.stdout.write("Exit codes:\n")
    sys.stdout.write("  0=success; 2=invalid input; 10=retryable; 20=permanent error\n\n")
    sys.stdout.write("BEGIN_AGENT_HELP\n")
    sys.stdout.write(AGENT_HELP)
    sys.stdout.write("END_AGENT_HELP\n")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--account", required=False)
    p.add_argument("--amount", required=False)
    p.add_argument("--tag", required=False, default="Корректировка")
    p.add_argument("--comment", required=False, default="Корректировка баланса")
    p.add_argument("--date", required=False)
    p.add_argument("--timezone", required=False)
    p.add_argument("--timeout-sec", required=False, default="20")
    p.add_argument("--json", action="store_true")
    p.add_argument("-h", "--help", action="store_true")
    return p


# END_BLOCK_CLI


# START_BLOCK_HTTP
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

API_DIFF_URL = "https://api.zenmoney.ru/v8/diff/"


class TemporaryApiError(RuntimeError):
    pass


class PermanentApiError(RuntimeError):
    pass


class InputError(RuntimeError):
    pass


def zenmoney_diff(*, token: str, payload: dict, timeout_sec: int) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        API_DIFF_URL,
        method="POST",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "zenmoney-adjust/0.1 (scriptcraft)",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            status = getattr(resp, "status", None) or 200
            raw = resp.read()
    except urllib.error.HTTPError as e:
        status = e.code
        if status in (401, 403):
            raise PermanentApiError(f"HTTP {status} (auth)")
        if 400 <= status < 500:
            raise PermanentApiError(f"HTTP {status}")
        raise TemporaryApiError(f"HTTP {status}")
    except (urllib.error.URLError, socket.timeout, TimeoutError):
        raise TemporaryApiError("network error")

    if status >= 500:
        raise TemporaryApiError(f"HTTP {status}")

    try:
        obj = json.loads(raw.decode("utf-8"))
    except Exception:
        raise PermanentApiError("failed to parse JSON")

    if not isinstance(obj, dict):
        raise PermanentApiError("unexpected response type")

    return obj


def fetch_snapshot(*, token: str, timeout_sec: int) -> dict:
    payload = {
        "currentClientTimestamp": int(time.time()),
        "serverTimestamp": 0,
    }
    return zenmoney_diff(token=token, payload=payload, timeout_sec=timeout_sec)


def today_yyyy_mm_dd(tz: str | None) -> str:
    if tz and ZoneInfo is not None:
        try:
            now = datetime.now(ZoneInfo(tz))
        except Exception:
            now = datetime.now().astimezone()
    else:
        now = datetime.now().astimezone()
    return now.strftime("%Y-%m-%d")


def extract_list(obj: dict, key: str, alt: str) -> list[dict]:
    v = obj.get(key)
    if v is None:
        v = obj.get(alt)
    if not isinstance(v, list):
        return []
    out = []
    for it in v:
        if isinstance(it, dict):
            out.append(it)
    return out


def looks_like_uuid(s: str) -> bool:
    try:
        uuid.UUID(s)
        return True
    except Exception:
        return False


def resolve_account(diff: dict, selector: str) -> dict:
    accounts = extract_list(diff, "account", "accounts")
    if not selector:
        raise InputError("account selector is empty")

    matches = []

    if looks_like_uuid(selector):
        for acc in accounts:
            if str(acc.get("id")) == selector:
                if acc.get("archive"):
                    continue
                matches.append(acc)
    else:
        for acc in accounts:
            title = acc.get("title", "")
            if selector in title:
                if acc.get("archive"):
                    continue
                matches.append(acc)

    if not matches:
        raise InputError(f"account not found: {selector}")

    if len(matches) > 1:
        cand = ", ".join([m.get("title", "?") for m in matches[:5]])
        raise InputError(f"account ambiguous: {selector}. Candidates: {cand}")

    return matches[0]


def resolve_tag(diff: dict, selector_or_title_exact: str) -> dict:
    tags = extract_list(diff, "tag", "tags")
    if not selector_or_title_exact:
        raise InputError("tag selector is empty")

    matches = []

    if looks_like_uuid(selector_or_title_exact):
        for tag in tags:
            if str(tag.get("id")) == selector_or_title_exact:
                matches.append(tag)
    else:
        for tag in tags:
            title = tag.get("title", "")
            if title == selector_or_title_exact:
                matches.append(tag)

    if not matches:
        raise InputError(f"tag not found: {selector_or_title_exact}")

    if len(matches) > 1:
        cand = ", ".join([m.get("title", "?") for m in matches[:5]])
        raise InputError(f"tag ambiguous: {selector_or_title_exact}. Candidates: {cand}")

    return matches[0]


def build_adjustment_transaction(
    *,
    tx_id: str,
    user_id: int,
    date_str: str,
    now_ts: int,
    amount: float,
    direction: str,
    account_id: str,
    instrument_id: int,
    tag_id: str,
    comment: str,
) -> dict:
    if direction == "income":
        income = amount
        outcome = 0
    else:
        income = 0
        outcome = amount

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


def verify_transaction_accepted(resp: dict, tx_id: str) -> None:
    tx_res = resp.get("transaction")
    if not isinstance(tx_res, list):
        raise PermanentApiError("API response missing transaction list")

    found = False
    for tx in tx_res:
        if isinstance(tx, dict) and str(tx.get("id")) == tx_id:
            found = True
            break

    if not found:
        raise PermanentApiError("transaction not found in API response")


# END_BLOCK_HTTP


# START_BLOCK_MAIN


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
        "meta": {"version": "0.1.0"},
    }
    if not ok:
        payload["error"] = error or "error"
        if hint:
            payload["hint"] = hint
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")


def main(argv: list[str]) -> int:
    global JSON_MODE

    args, unknown = build_parser().parse_known_args(argv)
    if args.help:
        _print_help()
        return EXIT_OK
    if unknown:
        log_error(f"unknown arg: {unknown[0]}", hint="Run with --help")
        return EXIT_BAD_INPUT

    JSON_MODE = bool(args.json)

    log_step(1, "validating inputs")
    log_info(f"account_selector={args.account}")
    log_info(f"tag_selector={args.tag}")
    log_info(f"timeout_sec={args.timeout_sec}")
    if JSON_MODE:
        log_info("json_mode=true")

    if not args.account:
        msg = "missing --account"
        if JSON_MODE:
            emit_json(False, EXIT_BAD_INPUT, error=msg, hint="Run with --help")
        else:
            log_error(msg, hint="Run with --help")
        return EXIT_BAD_INPUT

    if not args.amount:
        msg = "missing --amount"
        if JSON_MODE:
            emit_json(False, EXIT_BAD_INPUT, error=msg, hint="Run with --help")
        else:
            log_error(msg, hint="Run with --help")
        return EXIT_BAD_INPUT

    token = os.environ.get("ZENMONEY_API_KEY")
    if not token:
        msg = "missing ZENMONEY_API_KEY"
        if JSON_MODE:
            emit_json(False, EXIT_BAD_INPUT, error=msg, hint="export ZENMONEY_API_KEY='...'")
        else:
            log_error(msg, hint="export ZENMONEY_API_KEY='...'")
        return EXIT_BAD_INPUT


    try:
        amount_str = str(args.amount).replace(",", ".")
        amount = float(amount_str)
    except Exception:
        msg = f"invalid amount: {args.amount}"
        if JSON_MODE:
            emit_json(False, EXIT_BAD_INPUT, error=msg)
        else:
            log_error(msg)
        return EXIT_BAD_INPUT

    try:
        timeout_sec = int(args.timeout_sec)
        if timeout_sec <= 0 or timeout_sec > 120:
            raise ValueError()
    except Exception:
        msg = "timeout-sec must be in range 1..120"
        if JSON_MODE:
            emit_json(False, EXIT_BAD_INPUT, error=msg)
        else:
            log_error(msg)
        return EXIT_BAD_INPUT

    tz = args.timezone.strip() if args.timezone else None
    date_str = args.date.strip() if args.date else today_yyyy_mm_dd(tz)
    if args.date:
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except Exception:
            msg = "date must be YYYY-MM-DD"
            if JSON_MODE:
                emit_json(False, EXIT_BAD_INPUT, error=msg)
            else:
                log_error(msg)
            return EXIT_BAD_INPUT

    log_step(2, "fetching snapshot via /v8/diff/")
    try:
        snap = fetch_snapshot(token=token, timeout_sec=timeout_sec)
    except TemporaryApiError as e:
        msg = f"temporary API error: {e}"
        if JSON_MODE:
            emit_json(False, EXIT_RETRYABLE, error=msg)
        else:
            log_error(msg)
            log_error("adjustment not added")
        return EXIT_RETRYABLE
    except PermanentApiError as e:
        msg = f"permanent API error: {e}"
        if JSON_MODE:
            emit_json(False, EXIT_PERMANENT, error=msg)
        else:
            log_error(msg)
            log_error("adjustment not added")
        return EXIT_PERMANENT

    log_step(3, "resolving account")
    try:
        account = resolve_account(snap, args.account)
    except InputError as e:
        msg = str(e)
        if JSON_MODE:
            emit_json(False, EXIT_BAD_INPUT, error=msg)
        else:
            log_error(msg)
            log_error("adjustment not added")
        return EXIT_BAD_INPUT

    account_id = str(account.get("id"))
    account_title = account.get("title", "?")
    instrument = account.get("instrument")
    if not isinstance(instrument, int):
        msg = "account instrument missing"
        if JSON_MODE:
            emit_json(False, EXIT_PERMANENT, error=msg)
        else:
            log_error(msg)
            log_error("adjustment not added")
        return EXIT_PERMANENT

    log_info(f"resolved account: {account_title} ({account_id})")

    log_step(4, "resolving tag")
    try:
        tag = resolve_tag(snap, args.tag)
    except InputError as e:
        msg = str(e)
        if JSON_MODE:
            emit_json(False, EXIT_BAD_INPUT, error=msg)
        else:
            log_error(msg)
            log_error("adjustment not added")
        return EXIT_BAD_INPUT

    tag_id = str(tag.get("id"))
    tag_title = tag.get("title", "?")
    log_info(f"resolved tag: {tag_title} ({tag_id})")

    log_step(5, "determining direction")
    amount_sign = amount_str[0] if amount_str and amount_str[0] in "+-" else ""
    if amount_sign == "+":
        direction = "income"
        amount_abs = amount
    elif amount_sign == "-":
        direction = "outcome"
        amount_abs = abs(amount)
    else:
        direction = "outcome"
        amount_abs = amount

    log_info(f"direction={direction} amount={amount_abs:g}")

    users = extract_list(snap, "user", "users")
    if not users or not isinstance(users[0].get("id"), int):
        msg = "could not determine user id from diff response"
        if JSON_MODE:
            emit_json(False, EXIT_PERMANENT, error=msg)
        else:
            log_error(msg)
            log_error("adjustment not added")
        return EXIT_PERMANENT

    user_id = int(users[0]["id"])

    log_step(6, "building adjustment transaction")
    now_ts = int(time.time())
    tx_id = str(uuid.uuid4())

    tx = build_adjustment_transaction(
        tx_id=tx_id,
        user_id=user_id,
        date_str=date_str,
        now_ts=now_ts,
        amount=amount_abs,
        direction=direction,
        account_id=account_id,
        instrument_id=instrument,
        tag_id=tag_id,
        comment=args.comment,
    )

    log_step(7, "sending transaction via /v8/diff/")
    payload = {
        "currentClientTimestamp": now_ts,
        "serverTimestamp": 0,
        "transaction": [tx],
    }

    try:
        res = zenmoney_diff(token=token, payload=payload, timeout_sec=timeout_sec)
    except TemporaryApiError as e:
        msg = f"temporary API error: {e}"
        if JSON_MODE:
            emit_json(False, EXIT_RETRYABLE, error=msg)
        else:
            log_error(msg)
            log_error("adjustment not added")
        return EXIT_RETRYABLE
    except PermanentApiError as e:
        msg = f"permanent API error: {e}"
        if JSON_MODE:
            emit_json(False, EXIT_PERMANENT, error=msg)
        else:
            log_error(msg)
            log_error("adjustment not added")
        return EXIT_PERMANENT

    log_step(8, "verifying transaction accepted")
    try:
        verify_transaction_accepted(res, tx_id)
    except PermanentApiError as e:
        msg = str(e)
        if JSON_MODE:
            emit_json(False, EXIT_PERMANENT, error=msg)
        else:
            log_error(msg)
            log_error("adjustment not added")
        return EXIT_PERMANENT

    if JSON_MODE:
        emit_json(
            True,
            EXIT_OK,
            data={
                "transaction_id": tx_id,
                "account_id": account_id,
                "amount": amount_abs,
                "direction": direction,
                "tag_id": tag_id,
            },
        )
    else:
        log_info(
            f"created id={tx_id} account={account_title} amount={amount_abs:g} direction={direction} tag={tag_title} date={date_str}"
        )
        log_ok("adjustment added")

    return EXIT_OK


# END_BLOCK_MAIN


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        if JSON_MODE:
            emit_json(False, EXIT_RETRYABLE, error="interrupted", hint="Try again")
        else:
            log_error("interrupted", hint="Try again")
        raise SystemExit(EXIT_RETRYABLE)
