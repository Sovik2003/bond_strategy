"""Государственная кривая по ОФЗ.

Выпуск сравнивается не с одной «эталонной» бумагой, а с интерполяцией
кривой на его дюрацию: сравнение с ОФЗ другой срочности искажает премию.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable

from ..normalization.moex_bonds import _to_float


@dataclass(frozen=True)
class CurvePoint:
    secid: str
    maturity: date
    years: float
    yield_pct: float


def build_curve(
    ofz_rows: Iterable[dict[str, Any]], settlement: date
) -> list[CurvePoint]:
    """Точки кривой: только выпуски с доходностью и дюрацией."""
    points: list[CurvePoint] = []
    for row in ofz_rows:
        maturity = row.get("MATDATE")
        yld = _to_float(row.get("MD_YIELD"))
        duration = _to_float(row.get("MD_DURATION"))
        # YIELD=0 у ОФЗ означает отсутствие сделок, а не нулевую доходность
        if not maturity or yld is None or yld <= 0:
            continue
        try:
            mat_date = date.fromisoformat(str(maturity)[:10])
        except ValueError:
            continue
        if mat_date <= settlement:
            continue
        # дюрация ISS приходит в днях
        years = (duration / 365.0) if duration else (mat_date - settlement).days / 365.0
        if years <= 0:
            continue
        points.append(CurvePoint(row["SECID"], mat_date, round(years, 4), yld))
    return sorted(points, key=lambda p: p.years)


def interpolate_yield(curve: list[CurvePoint], years: float) -> float | None:
    """Линейная интерполяция доходности ОФЗ на заданную дюрацию.

    За пределами кривой используется ближайшая точка: экстраполяция
    дала бы ложную точность.
    """
    if not curve:
        return None
    if years <= curve[0].years:
        return curve[0].yield_pct
    if years >= curve[-1].years:
        return curve[-1].yield_pct

    for left, right in zip(curve, curve[1:]):
        if left.years <= years <= right.years:
            span = right.years - left.years
            if span <= 0:
                return left.yield_pct
            weight = (years - left.years) / span
            return left.yield_pct + weight * (right.yield_pct - left.yield_pct)
    return curve[-1].yield_pct


def benchmark_for(
    curve: list[CurvePoint],
    issue_maturity: date | None,
    issue_duration_years: float | None,
    settlement: date,
) -> float | None:
    """Доходность ОФЗ сопоставимой срочности в долях (не процентах)."""
    if not curve:
        return None
    if issue_duration_years is not None and issue_duration_years > 0:
        years = issue_duration_years
    elif issue_maturity is not None:
        years = (issue_maturity - settlement).days / 365.0
    else:
        return None

    pct = interpolate_yield(curve, years)
    return None if pct is None else pct / 100.0
