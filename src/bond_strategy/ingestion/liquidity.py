"""Ликвидность по истории торгов, а не по дневному срезу.

Зачем отдельный модуль. `VALTODAY` в блоке marketdata — оборот за текущую
сессию. При расчёте на прошлую дату там ноль, и фильтр ликвидности отсекал
всё подряд, включая выпуски с оборотом в сотни миллионов рублей в день.
Проверено на прогоне 2026-10-26: из 12 разобранных выпусков 12 получили
EXCLUDED_LOW_LIQUIDITY при нулевом VALTODAY.

Что важно в ответе /history:

- строка есть на КАЖДЫЙ торговый день, включая дни без сделок (VALUE=0),
  поэтому «нет сделок» и «нет данных» различимы: 0 — это факт, None — пропуск;
- один и тот же день приходит по нескольким бордам (TQCB с данными, TQOD
  с None). Складывать борды нельзя — получится двойной счёт. Берётся борд
  с ненулевым значением, при нескольких — максимум;
- страницы по 40 строк, нужен цикл по start.

Средний оборот делится на число торговых дней в окне, а не на число дней с
сделками: иначе бумага, торговавшаяся дважды за месяц, выглядела бы
ликвиднее, чем торгующаяся каждый день теми же объёмами.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from .moex import IssClient, block, num

HISTORY_PAGE = 40


def fetch_history(client: IssClient, secid: str, start: date, end: date) -> list[dict]:
    """Все строки истории за период с полной пагинацией."""
    rows: list[dict] = []
    offset = 0
    while True:
        payload, _ = client.get(
            f"/history/engines/stock/markets/bonds/securities/{secid}.json",
            **{
                "from": start.isoformat(),
                "till": end.isoformat(),
                "iss.only": "history",
                "start": offset,
            },
        )
        page = block(payload, "history")
        rows.extend(page)
        if len(page) < HISTORY_PAGE:
            break
        offset += HISTORY_PAGE
    return rows


def value_by_day(rows: list[dict]) -> dict[date, float]:
    """Оборот по дням без двойного счёта бордов.

    День попадает в результат, даже если сделок не было (0.0): это факт
    отсутствия торговли, и он должен участвовать в среднем.
    """
    per_day: dict[date, dict[str, float | None]] = defaultdict(dict)
    for row in rows:
        traded = row.get("TRADEDATE")
        if not traded:
            continue
        day = date.fromisoformat(str(traded)[:10])
        board = row.get("BOARDID") or ""
        per_day[day][board] = num(row.get("VALUE"))

    result: dict[date, float] = {}
    for day, boards in per_day.items():
        values = [v for v in boards.values() if v is not None]
        if not values:
            # Все борды без данных: день известен, оборот неизвестен.
            continue
        result[day] = max(values)
    return result


def average_daily_turnover(
    rows: list[dict], window_days: int
) -> tuple[float | None, int]:
    """Средний дневной оборот и число учтённых торговых дней.

    Делитель — число торговых дней в окне, а не число дней со сделками.
    """
    by_day = value_by_day(rows)
    if not by_day:
        return None, 0
    days = sorted(by_day)[-window_days:]
    return sum(by_day[d] for d in days) / len(days), len(days)


def liquidity_stats(
    client: IssClient, secid: str, as_of: date, window_days: int = 20
) -> tuple[float | None, int]:
    """Средний дневной оборот за окно, заканчивающееся на as_of.

    Окно берётся с запасом по календарю: 20 торговых дней — это примерно
    28 календарных. Точное число торговых дней определяется по ответу,
    а не по календарю.
    """
    calendar_days = int(window_days * 1.6) + 10
    start = as_of - timedelta(days=calendar_days)
    rows = fetch_history(client, secid, start, as_of)
    return average_daily_turnover(rows, window_days)
