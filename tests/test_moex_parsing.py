"""Тесты разбора данных MOEX ISS без обращения к сети."""
from datetime import date

import pytest

from bond_strategy.moex.details import (
    CouponRow,
    IssueDetails,
    Schedule,
    coupon_is_fixed,
)
from bond_strategy.moex.mapping import estimate_duration_years
from bond_strategy.moex.ofz import OfzCurve, OfzPoint


def _curve(*points):
    return OfzCurve(points=[OfzPoint(s, d, y) for s, d, y in points])


# ---------- ОФЗ-кривая ----------

def test_curve_interpolates_between_points():
    curve = _curve(("S", 365, 10.0), ("L", 1825, 20.0))
    mid = curve.yield_for_duration(1095)
    assert mid is not None
    assert 0.10 < mid < 0.20


def test_curve_returns_fraction_not_percent():
    curve = _curve(("S", 730, 13.0), ("L", 1460, 15.0))
    assert curve.yield_for_duration(730) == pytest.approx(0.13)


def test_curve_clamps_beyond_last_point():
    """Экстраполяция дала бы ложную точность — берётся крайняя точка."""
    curve = _curve(("S", 365, 10.0), ("M", 1095, 14.0))
    assert curve.yield_for_duration(365 * 30) == pytest.approx(0.14)
    assert curve.yield_for_duration(10) == pytest.approx(0.10)


def test_curve_empty_below_two_points():
    assert OfzCurve(points=[]).is_empty()
    assert OfzCurve(points=[OfzPoint("A", 100, 10.0)]).is_empty()
    assert OfzCurve(points=[]).yield_for_duration(500) is None


# ---------- Фиксированный купон ----------

def _details(**kw) -> IssueDetails:
    base = dict(
        secid="TEST",
        facevalue=1000.0,
        initial_facevalue=1000.0,
        coupon_frequency=2,
        bond_type="Облигация с фиксированным (известным) купоном",
        list_level=1,
    )
    base.update(kw)
    return IssueDetails(**base)


def _schedule(coupons, amort=None, offers=None) -> Schedule:
    return Schedule(
        coupons=list(coupons),
        amortizations=list(amort or []),
        offers=list(offers or []),
    )


def _c(day: str, value) -> CouponRow:
    return CouponRow(
        coupon_date=date.fromisoformat(day), value=value, value_rub=value, facevalue=1000.0
    )


def test_fixed_when_all_future_values_known():
    s = _schedule([_c("2027-01-01", 40.0), _c("2027-07-01", 40.0)])
    ok, reason = coupon_is_fixed(s, _details(), date(2026, 10, 26))
    assert ok, reason


def test_not_fixed_when_value_missing():
    s = _schedule([_c("2027-01-01", None), _c("2027-07-01", None)])
    ok, reason = coupon_is_fixed(s, _details(), date(2026, 10, 26))
    assert not ok
    assert reason


def test_not_fixed_when_partially_unknown():
    s = _schedule([_c("2027-01-01", 40.0), _c("2027-07-01", None)])
    ok, _ = coupon_is_fixed(s, _details(), date(2026, 10, 26))
    assert not ok


def test_not_fixed_with_benchmark():
    """Ссылка на ключевую ставку или RUONIA — плавающий купон по определению."""
    s = _schedule([_c("2027-01-01", 40.0)])
    ok, reason = coupon_is_fixed(
        s, _details(coupon_benchmark="RREFKEYR"), date(2026, 10, 26)
    )
    assert not ok
    assert "BENCHMARK" in reason


def test_not_fixed_without_future_coupons():
    s = _schedule([_c("2025-01-01", 40.0)])
    ok, reason = coupon_is_fixed(s, _details(), date(2026, 10, 26))
    assert not ok
    assert reason


# ---------- Дюрация ----------

def test_duration_positive_and_less_than_maturity():
    d = _details(matdate=date(2029, 10, 26))
    years = estimate_duration_years(d, date(2026, 10, 26), ytm=0.15)
    assert years is not None
    assert 0 < years < 3.1


def test_duration_none_without_maturity():
    assert estimate_duration_years(_details(matdate=None), date(2026, 10, 26)) is None


def test_duration_none_for_past_maturity():
    d = _details(matdate=date(2025, 1, 1))
    assert estimate_duration_years(d, date(2026, 10, 26)) is None


# ---------- Амортизация ----------

def test_amortization_excludes_maturity_row():
    s = Schedule(
        amortizations=[
            _amort("2027-06-01", 750, "amortization"),
            _amort("2028-06-01", 500, "amortization"),
            _amort("2029-06-01", 0, "maturity"),
        ]
    )
    only_amort = s.amortization_only()
    assert len(only_amort) == 2
    assert s.maturity_row() is not None
    assert s.maturity_row().data_source == "maturity"


def _amort(day: str, face: float, source: str):
    from bond_strategy.moex.details import AmortRow

    return AmortRow(
        amort_date=date.fromisoformat(day),
        value=None,
        value_prc=None,
        facevalue=face,
        initial_facevalue=1000.0,
        data_source=source,
    )


def test_future_offers_filtered_by_date():
    from bond_strategy.moex.details import OfferRow

    s = Schedule(
        offers=[
            OfferRow(offer_date=date(2025, 1, 1), offer_type="put", price=100.0),
            OfferRow(offer_date=date(2028, 1, 1), offer_type="put", price=100.0),
        ]
    )
    assert len(s.future_offers(date(2026, 10, 26))) == 1


# ---------- Пагинация bondization ----------

class _FakeClient:
    """Минимальный двойник: отдаёт bondization по 20 строк, как ISS."""

    PAGE = 20

    def __init__(self, n_coupons: int):
        self.n_coupons = n_coupons
        self.requests: list[int] = []

    def get(self, path: str, stamp: str | None = None, **params):
        start = int(params.get("start", 0))
        self.requests.append(start)
        rows = [
            ["TEST", "name", 1000, f"2027-01-{(i % 28) + 1:02d}", None, None, 1000, 1000,
             "SUR", 40.0, 4.0, 40.0, "TEST", "TQCB"]
            for i in range(self.n_coupons)
        ][start:start + self.PAGE]
        cols = ["isin", "name", "issuevalue", "coupondate", "recorddate", "startdate",
                "initialfacevalue", "facevalue", "faceunit", "value", "valueprc",
                "value_rub", "secid", "primary_boardid"]
        return {
            "coupons": {"columns": cols, "data": rows},
            "coupons.cursor": {"columns": ["INDEX", "TOTAL", "PAGESIZE"],
                               "data": [[start, self.n_coupons, self.PAGE]]},
            "amortizations": {"columns": [], "data": []},
            "amortizations.cursor": {"columns": ["INDEX", "TOTAL", "PAGESIZE"], "data": [[0, 0, 0]]},
            "offers": {"columns": [], "data": []},
        }


def test_bondization_pagination_collects_all_coupons():
    """График не должен обрываться на первой странице.

    Регрессия: ISS отдаёт по 20 строк. Если добора нет, у выпуска с 35
    купонами теряется 15 выплат, и доходность считается заниженной.
    """
    from bond_strategy.moex.details import fetch_schedule

    client = _FakeClient(n_coupons=35)
    schedule = fetch_schedule(client, "TEST", stamp="2026-10-26")

    assert len(schedule.coupons) == 35
    # было как минимум два запроса: начальный и добор
    assert len(client.requests) >= 2
    assert client.requests[0] == 0


def test_actual_coupons_used_in_schedule():
    """Фактические купоны из ISS имеют приоритет над генерацией из ставки."""
    from bond_strategy.analytics.cashflows import build_schedule, uses_actual_coupons
    from bond_strategy.models import BondIssue, CouponPayment

    issue = BondIssue(
        isin="X", ticker="X", issuer_id="i", issuer_name="n", industry="i",
        nominal=1000.0, coupon_rate=0.10, coupon_frequency=2,
        maturity_date=date(2028, 1, 1),
        coupons=[
            CouponPayment(date(2027, 1, 1), 99.0),
            CouponPayment(date(2027, 7, 1), 11.0),
        ],
    )
    assert uses_actual_coupons(issue)
    flows = build_schedule(issue, date(2026, 10, 26))
    coupons = [f.amount_per_bond for f in flows if f.kind == "coupon"]
    # ставка 10% дала бы 50 на каждый период — берём фактические
    assert coupons == [99.0, 11.0]
