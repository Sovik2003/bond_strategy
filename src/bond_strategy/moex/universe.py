"""Загрузка универсума облигаций MOEX и предварительная классификация.

Источник: /engines/stock/markets/bonds/securities.json — все борды сразу.
Это дешёвый батч, поэтому по нему строится предфильтр, а дорогие запросы
(description, bondization, история) выполняются только для выживших.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..moex.client import IssClient, block, dt, num

UNIVERSE_PATH = "/engines/stock/markets/bonds/securities.json"

# Ключевые слова COUPON_DETAILS, означающие плавающий купон.
FLOATING_MARKERS = (
    "ключевая ставка",
    "ruonia",
    "ипц",
    "плавающ",
)

# FACEUNIT, которые допускаются: только рублёвые выпуски.
ALLOWED_FACEUNIT = {"SUR"}


@dataclass
class RawBondRow:
    """Строка универсума без интерпретации — как пришла из ISS."""

    secid: str
    isin: str | None
    shortname: str
    boardid: str
    faceunit: str | None
    facevalue: float | None
    matdate: date | None
    offerdate: date | None
    couponpercent: float | None
    couponvalue: float | None
    accruedint: float | None
    prevprice: float | None
    lotsize: int | None
    listlevel: int | None
    bondtype: str | None
    bonds_subtype: str | None
    coupon_details: str | None
    sectype: str | None
    prevdate: date | None
    issuesize: float | None


def fetch_universe(client: IssClient, stamp: str | None = None) -> list[RawBondRow]:
    payload = client.get(UNIVERSE_PATH, stamp=stamp, **{"iss.only": "securities"})
    rows: list[RawBondRow] = []
    for r in block(payload, "securities"):
        secid = r.get("SECID")
        if not secid:
            continue
        rows.append(
            RawBondRow(
                secid=secid,
                isin=r.get("ISIN"),
                shortname=r.get("SHORTNAME") or "",
                boardid=r.get("BOARDID") or "",
                faceunit=r.get("FACEUNIT"),
                facevalue=num(r.get("FACEVALUE")),
                matdate=dt(r.get("MATDATE")),
                offerdate=dt(r.get("OFFERDATE")),
                couponpercent=num(r.get("COUPONPERCENT")),
                couponvalue=num(r.get("COUPONVALUE")),
                accruedint=num(r.get("ACCRUEDINT")),
                prevprice=num(r.get("PREVPRICE")),
                lotsize=int(num(r.get("LOTSIZE")) or 1),
                listlevel=int(num(r.get("LISTLEVEL")) or 0) or None,
                bondtype=r.get("BONDTYPE"),
                bonds_subtype=r.get("BONDSUBTYPE"),
                coupon_details=r.get("COUPON_DETAILS"),
                sectype=r.get("SECTYPE"),
                prevdate=dt(r.get("PREVDATE")),
                issuesize=num(r.get("ISSUESIZE")),
            )
        )
    return rows


def coupon_kind_hint(row: RawBondRow) -> str:
    """Грубая классификация купона по батч-полям.

    Это именно подсказка: для амортизируемых выпусков COUPON_DETAILS пуст,
    поэтому окончательное решение принимает проверка графика в bondization.
    """
    details = (row.coupon_details or "").lower()
    bondtype = (row.bondtype or "").lower()

    if any(marker in details for marker in FLOATING_MARKERS):
        return "FLOATING"
    if "плавающ" in bondtype:
        return "FLOATING"
    if "фиксированным (известным)" in details:
        return "FIXED"
    if "фиксированным (неизвестным)" in details:
        return "UNKNOWN"
    if "амортизируемая" in bondtype:
        return "UNKNOWN_AMORTIZING"
    return "UNKNOWN"


def prefilter(
    rows: list[RawBondRow],
    as_of: date,
    min_days_to_maturity: int,
    boards: tuple[str, ...] = ("TQCB",),
) -> tuple[list[RawBondRow], list[tuple[str, str]]]:
    """Дешёвый отсев до дорогих запросов.

    Возвращает (прошедшие, [(secid, причина_отказа)]).
    """
    passed: list[RawBondRow] = []
    rejected: list[tuple[str, str]] = []

    for row in rows:
        if row.boardid not in boards:
            rejected.append((row.secid, f"COULD_NOT_TRADE_BOARD:{row.boardid}"))
            continue
        if row.faceunit not in ALLOWED_FACEUNIT:
            rejected.append((row.secid, f"CURRENCY:{row.faceunit}"))
            continue
        if row.matdate is None:
            rejected.append((row.secid, "NO_MATURITY"))
            continue
        if (row.matdate - as_of).days < min_days_to_maturity:
            rejected.append((row.secid, "TOO_SHORT"))
            continue
        if row.offerdate and row.offerdate > as_of:
            rejected.append((row.secid, "HAS_OFFER"))
            continue

        kind = coupon_kind_hint(row)
        if kind == "FLOATING":
            rejected.append((row.secid, "FLOATING_COUPON"))
            continue

        # Структурные и конвертируемые — вне стратегии.
        bondtype = (row.bondtype or "").lower()
        if "структурн" in bondtype or "конвертируем" in bondtype or "дисконтн" in bondtype:
            rejected.append((row.secid, f"UNSUPPORTED_TYPE:{row.bondtype}"))
            continue
        if "линкер" in bondtype or "индексируем" in bondtype:
            rejected.append((row.secid, "INDEX_LINKED"))
            continue

        passed.append(row)

    return passed, rejected


def summarize_prefilter(
    passed: list[RawBondRow], rejected: list[tuple[str, str]]
) -> dict[str, int]:
    by_reason: dict[str, int] = {}
    for _, reason in rejected:
        key = reason.split(":")[0]
        by_reason[key] = by_reason.get(key, 0) + 1
    by_reason["PASSED"] = len(passed)
    return dict(sorted(by_reason.items(), key=lambda kv: -kv[1]))
