"""Кредитные рейтинги из MOEX ISS (пространство /iss/cci/).

Зачем свой модуль. Рейтингов нет ни в description, ни в marketdata — там только
EMITTER_ID. Настоящий источник:

    /iss/cci/rating/companies/ecbd_{EMITTER_ID}.json

отдаёт рейтинги всех четырёх аккредитованных ЦБ агентств: АКРА, Эксперт РА,
НКР, НРА.

Три ловушки, на которых легко получить неверный рейтинг:

1. В выдаче лежит «Оценка собственной кредитоспособности» — это НЕ кредитный
   рейтинг, а внутренняя метрика агентства. У ДОМ.РФ рядом стоят
   AAA(RU) основной шкалы и aa(ru) оценки собственной кредитоспособности.
   Взять вторую значит занизить рейтинг на два уровня. Отбрасывается по
   названию шкалы, а не по id: id — внутренний номер и может измениться.

2. Отозванный рейтинг приходит как обычная строка со значением «Отозван».
   Компания с отозванным рейтингом рейтинга не имеет, и это надо отличать
   от «рейтинг есть, но низкий».

3. У одного эмитента до четырёх рейтингов от разных агентств, и они бывают
   разными. Правило: берётся МИНИМАЛЬНЫЙ из действующих — доверяем худшей
   оценке. Для решения о покупке осторожность дешевле ошибки.

Шкалы пишутся по-разному, поэтому нормализуются к общему виду AAA / AA+ / ... :
    АКРА        AAA(RU)     -> AAA
    Эксперт РА  ruAA+       -> AA+
    НКР         AAA.ru      -> AAA
    НРА         A+|ru|      -> A+
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from .moex import IssClient

# От лучшего к худшему. Порядок задаёт смысл «минимального» рейтинга.
RATING_SCALE = [
    "AAA", "AA+", "AA", "AA-",
    "A+", "A", "A-",
    "BBB+", "BBB", "BBB-",
    "BB+", "BB", "BB-",
    "B+", "B", "B-",
    "CCC+", "CCC", "CCC-",
    "CC", "C", "D",
]
_RANK = {level: i for i, level in enumerate(RATING_SCALE)}

WITHDRAWN_MARKERS = {"отозван", "отозван без присвоения", "withdrawn"}

# Шкала «оценка собственной кредитоспособности» — не кредитный рейтинг.
NOT_A_CREDIT_RATING = "оценка собственной кредитоспособности"

_LEVEL_RE = re.compile(
    r"""
    ^\s*
    (?:ru)?                       # префикс Эксперт РА: ruAA+
    (AAA|AA|A|BBB|BB|B|CCC|CC|C|D)
    ([+-])?                       # необязательный знак
    \s*
    (?:\(ru\)|\.ru|\|ru\|)?       # суффиксы АКРА / НКР / НРА
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)


@dataclass(frozen=True)
class Rating:
    agency: str
    level: str          # нормализованный: AAA, AA+, ...
    raw: str            # как пришло из ISS
    rating_date: date | None
    book_name: str


def normalize_level(raw: str | None) -> str | None:
    """Приводит обозначение рейтинга к общему виду. None — если не разобрать."""
    if not raw:
        return None
    match = _LEVEL_RE.match(str(raw))
    if not match:
        return None
    letters, sign = match.group(1).upper(), match.group(2)
    level = letters + (sign if sign else "")
    # D и C знаком не снабжаются; AA+ / A- и т.п. остаются как есть.
    return level if level in _RANK else letters if letters in _RANK else None


def is_withdrawn(raw: str | None) -> bool:
    return str(raw or "").strip().lower() in WITHDRAWN_MARKERS


def is_credit_rating(book_name: str | None) -> bool:
    """Отсекает оценки собственной кредитоспособности и прочие не-рейтинги."""
    if not book_name:
        return True  # шкала не названа — не выдумываем причину отказа
    return NOT_A_CREDIT_RATING not in book_name.strip().lower()


def _as_blocks(payload) -> dict[str, list]:
    """ISS с iss.json=extended отдаёт список: [charsetinfo, {блок: [...]}, ...]."""
    out: dict[str, list] = {}
    items = payload if isinstance(payload, list) else [payload]
    for item in items:
        if not isinstance(item, dict):
            continue
        for key, value in item.items():
            if isinstance(value, list) and not key.endswith(".cursor"):
                out[key] = value
    return out


def parse_ratings(payload) -> list[Rating]:
    """Разбирает ответ /iss/cci/. Отозванные и не-рейтинги не попадают в результат."""
    result: list[Rating] = []
    for row in _as_blocks(payload).get("cci_rating_companies", []):
        raw = row.get("rating_level_name_short_ru")
        if is_withdrawn(raw):
            continue
        book_name = row.get("rating_book_name_short_ru") or ""
        if not is_credit_rating(book_name):
            continue
        level = normalize_level(raw)
        if level is None:
            continue
        result.append(
            Rating(
                agency=row.get("agency_name_short_ru") or "—",
                level=level,
                raw=str(raw),
                rating_date=_to_date(row.get("rating_date")),
                book_name=book_name,
            )
        )
    return result


def _to_date(value) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def pick_rating(ratings: list[Rating]) -> Rating | None:
    """Минимальный из действующих рейтингов.

    Худшая оценка определяет решение: если АКРА говорит AA, а Эксперт РА A+,
    для покупки берётся A+ — доверяем худшей оценке.

    Сортировка записана явно, а не через min/max по индексу: RATING_SCALE
    упорядочен от лучшего к худшему, поэтому «худший» — это НАИБОЛЬШИЙ индекс.
    На этом легко ошибиться зеркально и получить самый лестный рейтинг вместо
    самого осторожного.
    """
    if not ratings:
        return None

    def sort_key(r: Rating) -> tuple[int, int, str]:
        rank = _RANK.get(r.level, len(RATING_SCALE))
        # Худший рейтинг — в начало: больший индекс важнее.
        # При равном уровне — более свежий: большая дата важнее.
        return (-rank, -(r.rating_date.toordinal() if r.rating_date else 0), r.agency)

    return sorted(ratings, key=sort_key)[0]


def fetch_company_ratings(client: IssClient, emitter_id: str | int) -> list[Rating]:
    """Рейтинги эмитента по EMITTER_ID из ISS."""
    if emitter_id in (None, ""):
        return []
    payload, _ = client.get(
        f"/cci/rating/companies/ecbd_{emitter_id}.json",
        **{"iss.json": "extended", "iss.meta": "off"},
    )
    return parse_ratings(payload)


def fetch_security_rating(client: IssClient, emitter_id: str | int, isin: str) -> Rating | None:
    """Рейтинг конкретного выпуска, если агентство его присвоило."""
    if emitter_id in (None, "") or not isin:
        return None
    payload, _ = client.get(
        f"/cci/rating/companies/ecbd_{emitter_id}/securities/isin_{isin}.json",
        **{"iss.json": "extended", "iss.meta": "off"},
    )
    for row in _as_blocks(payload).get("cci_rating_securities", []):
        raw = row.get("rating_level_name_short_ru")
        if is_withdrawn(raw):
            continue
        book_name = row.get("rating_book_name_short_ru") or ""
        if not is_credit_rating(book_name):
            continue
        level = normalize_level(raw)
        if level is None:
            continue
        return Rating(
            agency=row.get("agency_name_short_ru") or "—",
            level=level,
            raw=str(raw),
            rating_date=_to_date(row.get("rating_date")),
            book_name=book_name,
        )
    return None
