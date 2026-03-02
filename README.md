# zenmoney-scripts

Small CLI scripts for ZenMoney API (Diff endpoint).

## Setup

1) Export token (Bearer):

```bash
export ZENMONEY_API_KEY='...'
```

2) Commands are installed as thin wrappers in `~/.local/bin` and call the project scripts.

## Commands

### Balance

Default output: one line only:

```bash
zenmoney-balance
# -> 9817.15 USD
```

Expanded output:

```bash
zenmoney-balance --full
```

Notes:
- Base currency comes from `user.currency`.
- Conversion uses `instrument.rate` (defined by API as value in RUB), but the final total is printed in the base currency.

### Dictionary (reference lists)

Print accounts/tags/instruments (use filters to keep output small):

```bash
zenmoney-dict --accounts Viet
zenmoney-dict --tags "Проду"
```

### Add transaction (income/expense)

```bash
# expense
zenmoney-add --amount 350 --type expense --account "Vietcombank" --category "Продукты" --comment "Food"

# income
zenmoney-add --amount 50000 --type income --account "Vietcombank" --category "Зарплата" --comment "Salary"
```

Notes:
- Archived accounts are ignored (not selectable) for writing scripts.
- If account/category match is ambiguous, the script errors with a candidate list.

### Transfer between accounts

```bash
zenmoney-transfer --from "Axis Bank" --to "IDFC" --amount 100 --comment "Transfer"
```

Notes:
- Archived accounts are ignored.
- If instruments (currencies) differ, the script prints WARN and still performs a 1:1 transfer.

## Project layout

- Spec (Scriptcraft): `docs/script-specs.xml`
- Scripts: `scripts/*.py`
- Wrappers: `~/.local/bin/zenmoney-*`

## API

- Docs: https://github.com/zenmoney/ZenPlugins/wiki/ZenMoney-API
