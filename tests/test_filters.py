from datetime import date

import pytest

from bond_strategy.config import load_config
from bond_strategy.models import DataStatus, Quality
from bond_strategy.signals.filters import hard_filter
from tests.facts import (
    TEST_DATE,
    all_issues,
    conflicting_rating,
    defaulted,
    floating,
    illiquid,
    no_price,
    no_rating,
    normal,
    stale_price,
    with_offer,
)


@pytest.fixture(scope="module")
def cfg():
    return load_config("config/strategy.json")


def test_every_issue_gets_decidable_answer(cfg):
    for issue in all_issues():
        result = hard_filter(issue, TEST_DATE, cfg)
        assert isinstance(result.passed, bool)
        if not result.passed:
            assert result.reasons, f"{issue.secid}: отказ без причины"


def test_normal_passes(cfg):
    result = hard_filter(normal(), TEST_DATE, cfg)
    assert result.passed, result.reasons


def test_offer_excluded(cfg):
    assert "EXCLUDED_OFFER" in hard_filter(with_offer(), TEST_DATE, cfg).codes


def test_past_offer_does_not_exclude(cfg):
    issue = normal()
    issue.offer_date = date(2026, 1, 1)
    assert hard_filter(issue, TEST_DATE, cfg).passed


def test_floating_excluded(cfg):
    assert "EXCLUDED_COUPON_TYPE" in hard_filter(floating(), TEST_DATE, cfg).codes


def test_default_excluded(cfg):
    assert "EXCLUDED_DEFAULT" in hard_filter(defaulted(), TEST_DATE, cfg).codes


def test_no_price_blocks(cfg):
    codes = hard_filter(no_price(), TEST_DATE, cfg).codes
    assert any(c.startswith("DATA_BLOCKED") for c in codes)


def test_stale_price_blocks(cfg):
    assert "DATA_BLOCKED_STALE" in hard_filter(stale_price(), TEST_DATE, cfg).codes


def test_conflicting_rating_blocks(cfg):
    assert "DATA_BLOCKED_CONFLICT" in hard_filter(conflicting_rating(), TEST_DATE, cfg).codes


def test_illiquid_excluded(cfg):
    codes = hard_filter(illiquid(), TEST_DATE, cfg).codes
    assert "EXCLUDED_LOW_LIQUIDITY" in codes
    assert "EXCLUDED_WIDE_SPREAD" in codes


def test_no_rating_excluded(cfg):
    assert "EXCLUDED_NO_RATING" in hard_filter(no_rating(), TEST_DATE, cfg).codes


def test_short_maturity_excluded(cfg):
    issue = normal()
    issue.maturity_date = date(2026, 12, 1)
    assert "EXCLUDED_TOO_SHORT" in hard_filter(issue, TEST_DATE, cfg).codes


def test_list_level_excluded(cfg):
    issue = normal()
    issue.list_level = 3
    assert "EXCLUDED_LIST_LEVEL" in hard_filter(issue, TEST_DATE, cfg).codes


def test_qualified_only_excluded(cfg):
    issue = normal()
    issue.qualified_only = True
    assert "EXCLUDED_QUALIFIED_ONLY" in hard_filter(issue, TEST_DATE, cfg).codes


def test_medium_quality_uses_looser_spread(cfg):
    issue = normal()
    issue.quality = Quality.MEDIUM
    issue.avg_spread = 0.008  # выше core-порога 0.5%, но ниже medium 1.0%
    issue.rating = "BBB-"
    assert hard_filter(issue, TEST_DATE, cfg).passed


def test_status_severity_order():
    issue = normal()
    issue.status = dict(issue.status, price=DataStatus.STALE)
    issue.status["rating"] = DataStatus.CONFLICT
    assert issue.critical_status() is DataStatus.CONFLICT
    assert set(issue.blocked_fields()) == {"price", "rating"}
