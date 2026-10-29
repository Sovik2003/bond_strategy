from datetime import date

import pytest

from bond_strategy.analytics.duration import duration_coverage, portfolio_duration
from bond_strategy.models import AmortizationPayment, BondIssue
from bond_strategy.rebalance.config import load_rebalance_config

CFG_PATH = "config/rebalance.json"


@pytest.fixture(scope="module")
def cfg():
    return load_rebalance_config(CFG_PATH)


def test_floor_is_bbb_and_bbb_minus_is_excluded(cfg):
    assert cfg.rating_floor == "BBB"
    assert cfg.low_rating_levels == frozenset({"BBB+", "BBB"})
    assert "BBB-" not in cfg.low_rating_levels


def test_bands_boundaries(cfg):
    assert cfg.band_for(0.5).code == "0-1"
    assert cfg.band_for(1.0).code == "0-1"
    assert cfg.band_for(1.0001).code == "1-2"
    assert cfg.band_for(5.5).code == "5+"
    assert cfg.band_for(None) is None


def test_over_5y_band_has_no_target(cfg):
    band = cfg.band_by_code("5+") if hasattr(cfg, "band_by_code") else cfg.band_for(7)
    assert band.target is None
    assert band.upper == 0.10


def test_config_id_is_stable(cfg):
    assert cfg.config_id == load_rebalance_config(CFG_PATH).config_id
    assert len(cfg.config_id) == 12


def test_duration_order_validation(tmp_path):
    bad = tmp_path / "bad.json"
    import json

    data = json.loads(open(CFG_PATH, encoding="utf-8").read())
    data["duration"]["target_min"] = 4.0
    bad.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        load_rebalance_config(bad)


def test_group_defaults_to_issuer(cfg):
    assert cfg.group_of("ISS_X") == "ISS_X"


def test_portfolio_duration_reports_coverage_separately():
    weights = {"A": (2.0, 0.5), "B": (None, 0.3), "C": (4.0, 0.2)}
    assert portfolio_duration(weights) == pytest.approx((2.0 * 0.5 + 4.0 * 0.2) / 0.7)
    assert duration_coverage(weights) == pytest.approx(0.7)


def test_portfolio_duration_none_when_nothing_covered():
    assert portfolio_duration({"A": (None, 1.0)}) is None
    assert duration_coverage({"A": (None, 1.0)}) == 0.0
