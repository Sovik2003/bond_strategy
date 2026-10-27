"""Кривая ОФЗ и подбор сопоставимой доходности.

В ISS нет готовой «доходности ОФЗ сопоставимой срочности». Она строится
из рыночных данных ОФЗ (борд TQOB): берётся эффективная доходность каждого
выпуска и дюрация, затем для нужной дюрации выполняется линейная
интерполяция по соседним точкам.

Если точек недостаточно, возвращается None — премия не подменяется нулём.
"""
from __future__ import annotations

from dataclasses import dataclass

from .client import IssClient, block, num

OFZ_BOARD = "TQOB"
OFZ_PATH = "/engines/stock/markets/bonds/boards/TQOB/securities.json"


@dataclass(frozen=True)
class OfzPoint:
    secid: str
    duration_days: float
    yield_pct: float


@dataclass
class OfzCurve:
    points: list[OfzPoint]

    def is_empty(self) -> bool:
        return len(self.points) < 2

    def yield_for_duration(self, duration_days: float) -> float | None:
        """Линейная интерполяция доходности по дюрации, в долях (не %)."""
        if not self.points:
            return None
        ordered = sorted(self.points, key=lambda p: p.duration_days)
        if duration_days <= ordered[0].duration_days:
            return ordered[0].yield_pct / 100.0
        if duration_days >= ordered[-1].duration_days:
            return ordered[-1].yield_pct / 100.0

        for left, right in zip(ordered, ordered[1:]):
            if left.duration_days <= duration_days <= right.duration_days:
                span = right.duration_days - left.duration_days
                if span <= 0:
                    return left.yield_pct / 100.0
                w = (duration_days - left.duration_days) / span
                blended = left.yield_pct + w * (right.yield_pct - left.yield_pct)
                return blended / 100.0
        return None


def fetch_ofz_curve(client: IssClient, stamp: str | None = None) -> OfzCurve:
    """Кривая только по классическим ОФЗ-ПД.

    Борд TQOB содержит не только ОФЗ-ПД: там же валютные выпуски, линкеры
    на инфляцию и ОФЗ с плавающим купоном. Их доходность не сопоставима
    с корпоративной, поэтому без фильтра кривая получается немонотонной
    (например, 7.9% на 5 лет против 14.5% на 1.5 года).
    """
    payload = client.get(
        OFZ_PATH, stamp=stamp, **{"iss.only": "marketdata,securities"}
    )
    md = {r.get("SECID"): r for r in block(payload, "marketdata")}
    sec = {r.get("SECID"): r for r in block(payload, "securities")}

    points: list[OfzPoint] = []
    for secid, row in md.items():
        meta = sec.get(secid, {})
        duration = num(row.get("DURATION"))
        yld = num(row.get("YIELD"))
        if duration is None or yld is None or duration <= 0 or yld <= 0:
            continue
        if meta.get("FACEUNIT") != "SUR":
            continue
        # только фиксированный купон
        if "фиксированным" not in (meta.get("BONDTYPE") or ""):
            continue
        # ОФЗ-ПД имеют префикс SU262 (SU290 — ПК, SU460/520 — линкеры/амортизация)
        if not str(secid).startswith("SU262"):
            continue
        points.append(OfzPoint(secid=secid, duration_days=duration, yield_pct=yld))

    return OfzCurve(points=points)


def ofz_benchmark(curve: OfzCurve, duration_days: float | None) -> float | None:
    if duration_days is None or duration_days <= 0 or curve.is_empty():
        return None
    return curve.yield_for_duration(duration_days)
