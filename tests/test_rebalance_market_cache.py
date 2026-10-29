"""Тесты дискового кэша рынка (этап 9).

Зачем кэш проверяется отдельно: он отвечает за то, чтобы обрыв связи не
обнулял работу, и при этом не отдавал устаревшие данные. Ошибка здесь тише
всех остальных — отчёт просто посчитается по вчерашнему рынку.
"""
from datetime import date

import pytest

from bond_strategy.models import AmortizationPayment, BondIssue, DataStatus, Quality
from bond_strategy.rebalance.market_cache import (
    MarketCache,
    issue_from_dict,
    issue_to_dict,
)


def sample(**overrides):
    params = dict(
        isin="RU000TEST0001",
        secid="TEST1",
        issuer_id="484",
        issuer_name="Эмитент",
        industry="Промышленность",
        nominal=1000.0,
        initial_nominal=1000.0,
        coupon_type="FIXED",
        coupon_rate=0.15,
        coupon_frequency=2,
        maturity_date=date(2029, 10, 26),
        offer_date=date(2027, 3, 1),
        rating="A+",
        rating_agency="АКРА",
        rating_date=date(2026, 6, 1),
        clean_price=101.5,
        accrued_interest=6.1,
        price_date=date(2026, 10, 23),
        min_lot=10,
        quality=Quality.MEDIUM,
        security_name="Бумага",
        amortization=[AmortizationPayment(date(2027, 6, 1), 0.25)],
        coupon_schedule=[(date(2027, 4, 1), 75.0)],
        status={"price": DataStatus.FRESH, "rating": DataStatus.MISSING},
    )
    params.update(overrides)
    return BondIssue(**params)


def test_round_trip_preserves_all_fields():
    original = sample()
    restored = issue_from_dict(issue_to_dict(original))
    assert restored.isin == original.isin
    assert restored.secid == original.secid
    assert restored.maturity_date == original.maturity_date
    assert restored.offer_date == original.offer_date
    assert restored.price_date == original.price_date
    assert restored.rating_date == original.rating_date
    assert restored.min_lot == original.min_lot
    assert restored.clean_price == original.clean_price
    assert restored.accrued_interest == original.accrued_interest


def test_round_trip_preserves_amortization_and_schedule():
    restored = issue_from_dict(issue_to_dict(sample()))
    assert len(restored.amortization) == 1
    assert restored.amortization[0].payment_date == date(2027, 6, 1)
    assert restored.amortization[0].share_of_nominal == 0.25
    assert restored.coupon_schedule == [(date(2027, 4, 1), 75.0)]


def test_round_trip_preserves_enums_and_status():
    restored = issue_from_dict(issue_to_dict(sample()))
    assert restored.quality is Quality.MEDIUM
    assert restored.status["price"] is DataStatus.FRESH
    assert restored.status["rating"] is DataStatus.MISSING


def test_round_trip_handles_none_dates():
    restored = issue_from_dict(issue_to_dict(sample(maturity_date=None, rating_date=None)))
    assert restored.maturity_date is None
    assert restored.rating_date is None


def test_cache_persists_between_instances(tmp_path):
    cache = MarketCache(tmp_path, "2026-10-26-abc123")
    cache.put(sample())
    cache.save()

    reopened = MarketCache(tmp_path, "2026-10-26-abc123")
    assert reopened.has("TEST1")
    restored = reopened.get("TEST1")
    assert restored is not None and restored.isin == "RU000TEST0001"


def test_different_fingerprint_does_not_see_old_data(tmp_path):
    """Кэш от другой даты не должен подмешиваться в текущий расчёт."""
    cache = MarketCache(tmp_path, "2026-10-26-abc123")
    cache.put(sample())
    cache.save()

    other_day = MarketCache(tmp_path, "2026-10-27-abc123")
    assert not other_day.has("TEST1")
    assert other_day.get("TEST1") is None


def test_different_config_does_not_see_old_data(tmp_path):
    cache = MarketCache(tmp_path, "2026-10-26-abc123")
    cache.put(sample())
    cache.save()

    other_config = MarketCache(tmp_path, "2026-10-26-def456")
    assert not other_config.has("TEST1")


def test_missing_file_is_empty_not_an_error(tmp_path):
    cache = MarketCache(tmp_path / "nested", "fp")
    assert len(cache) == 0
    assert cache.all_issues() == []


def test_corrupt_file_is_treated_as_empty(tmp_path):
    """Битый файл не должен останавливать прогон."""
    root = tmp_path
    root.mkdir(exist_ok=True)
    (root / "market-fp.json").write_text("{не json", encoding="utf-8")
    cache = MarketCache(root, "fp")
    assert len(cache) == 0
    cache.put(sample())
    cache.save()
    assert MarketCache(root, "fp").has("TEST1")


def test_put_many_and_all_issues(tmp_path):
    cache = MarketCache(tmp_path, "fp")
    cache.put_many([sample(), sample(isin="RU000TEST0002", secid="TEST2")])
    secids = {issue.secid for issue in cache.all_issues()}
    assert secids == {"TEST1", "TEST2"}


def test_save_is_atomic_and_leaves_no_temp_file(tmp_path):
    cache = MarketCache(tmp_path, "fp")
    cache.put(sample())
    cache.save()
    assert list(tmp_path.glob("*.tmp")) == []
    assert (tmp_path / "market-fp.json").exists()


def test_fingerprint_includes_date_and_config():
    first = MarketCache.make_fingerprint(date(2026, 10, 26), "abc")
    second = MarketCache.make_fingerprint(date(2026, 10, 27), "abc")
    third = MarketCache.make_fingerprint(date(2026, 10, 26), "def")
    assert first != second and first != third
