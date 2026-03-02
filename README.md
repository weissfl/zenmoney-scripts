# zenmoney-scripts

Небольшие CLI-скрипты для ZenMoney API (Diff endpoint).

**Языки:** Русский | [English](README.en.md)

## Настройка

1) Экспортируй токен (Bearer):

```bash
export ZENMONEY_API_KEY='...'
```

2) Команды установлены как тонкие обёртки в `~/.local/bin` и вызывают Python-скрипты из проекта.

## Команды

### Баланс

Вывод по умолчанию — ровно одна строка:

```bash
zenmoney-balance
# -> 9817.15 USD
```

Расширенный вывод:

```bash
zenmoney-balance --full
```

Примечания:
- Базовая валюта берётся из `user.currency`.
- Конвертация использует `instrument.rate` (в API rate определён как «стоимость единицы валюты в RUB»), но итоговая сумма печатается в базовой валюте.

### Справочник (accounts/tags/instruments)

Печать списков (используй фильтры, чтобы не утонуть в выводе):

```bash
zenmoney-dict --accounts Viet
zenmoney-dict --tags "Проду"
```

### Добавить транзакцию (доход/расход)

```bash
# расход
zenmoney-add --amount 350 --type expense --account "Vietcombank" --category "Продукты" --comment "Food"

# доход
zenmoney-add --amount 50000 --type income --account "Vietcombank" --category "Зарплата" --comment "Salary"
```

Примечания:
- Для скриптов записи (`zenmoney-add`, `zenmoney-transfer`) архивные счета игнорируются (нельзя выбрать).
- Если совпадение по счёту/категории неоднозначно, скрипт завершится с ошибкой и покажет кандидатов.

### Перевод между счетами

```bash
zenmoney-transfer --from "Axis Bank" --to "IDFC" --amount 100 --comment "Transfer"
```

Примечания:
- Архивные счета игнорируются.
- Если instruments (валюты) различаются, скрипт печатает WARN и всё равно делает перевод 1:1.

## Структура проекта

- Спека (Scriptcraft): `docs/script-specs.xml`
- Скрипты: `scripts/*.py`
- Обёртки-команды: `~/.local/bin/zenmoney-*`

## API

- Доки: https://github.com/zenmoney/ZenPlugins/wiki/ZenMoney-API
