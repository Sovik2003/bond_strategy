from datetime import date
from types import SimpleNamespace

import pytest

from portfolio_inputs import validate_portfolio, save_portfolio, load_portfolio
from bond_strategy.config import load_config
from bond_strategy.pipeline import run_pipeline
from bond_strategy.portfolio.engine import position_size
from bond_strategy.models import SignalType
from bond_strategy.universe import UniverseStats
from tests.facts import normal


def test_manual_portfolio_persistence_and_validation(tmp_path):
    path = tmp_path / 'portfolio.json'
    snapshot = validate_portfolio(30000, [dict(secid=' test1 ', quantity=10, market_value=20000)])
    save_portfolio(path, snapshot)
    assert load_portfolio(path) == {'cash': 30000.0, 'positions': [
        {'secid': 'TEST1', 'quantity': 10, 'market_value': 20000.0}]}
    for rows in ([dict(secid='T', quantity=1.5, market_value=100)],
                 [dict(secid='T', quantity=1, market_value=float('nan'))],
                 [dict(secid='T', quantity=1, market_value=100)] * 2):
        with pytest.raises(ValueError):
            validate_portfolio(10, rows)
    with pytest.raises(ValueError):
        validate_portfolio(float('inf'), [])
    assert load_portfolio(path) == snapshot


def test_pipeline_accounts_for_entered_position_outside_screen(monkeypatch):
    import bond_strategy.pipeline as p
    cfg = load_config('config/strategy.json')
    candidate = normal()
    held = normal()
    held.secid, held.isin = 'HELD', 'HELD_ISIN'
    rows = [{'SECID': 'TEST1'}, {'SECID': 'HELD'}]
    monkeypatch.setattr(p, 'ofz_curve', lambda c: [(1.0, 0.1)])
    monkeypatch.setattr(p, 'fetch_board', lambda c, b: rows)
    monkeypatch.setattr(p, 'screen', lambda rows, d, c: ([rows[0]], UniverseStats()))
    def build(client, selected, as_of, cfg, path, workers=6, stats=None):
        return ([candidate if r['SECID'] == 'TEST1' else held for r in selected],
                stats or UniverseStats(), [])
    monkeypatch.setattr(p, 'build_universe', build)
    observed = {}
    def evaluate(issue, portfolio, prices, as_of, cfg, rate):
        observed['total'] = portfolio.total_value(prices)
        observed['issuer'] = portfolio.issuer_share(issue.issuer_id, prices)
        observed['size'] = position_size(portfolio, issue, prices, cfg, rate)
        return SimpleNamespace(score=100, signal_type=SignalType.WATCH,
                               suggested_amount=None, suggested_quantity=None,
                               reasons=[], failed_conditions=[])
    monkeypatch.setattr(p, 'evaluate_issue', evaluate)
    result = run_pipeline(date(2026, 10, 26), cfg, object(), workers=1, enrich_limit=1,
                          cash=40000, holdings=[{'secid': 'HELD', 'quantity': 10, 'market_value': 10000}])
    assert observed['total'] == 50000
    assert observed['issuer'] == pytest.approx(0.2)
    assert observed['size'] < 4  # issuer limit leaves only 2,500 rubles
    assert result.summary['portfolio']['total_value'] == 50000
    assert result.summary['portfolio']['cash'] == 40000


def test_missing_holding_is_never_silently_ignored(monkeypatch):
    import bond_strategy.pipeline as p
    monkeypatch.setattr(p, 'ofz_curve', lambda c: [])
    monkeypatch.setattr(p, 'fetch_board', lambda c, b: [])
    monkeypatch.setattr(p, 'screen', lambda r, d, c: ([], UniverseStats()))
    monkeypatch.setattr(p, 'build_universe', lambda *a, **kw: ([], UniverseStats(), []))
    with pytest.raises(ValueError, match='не найдена'):
        run_pipeline(date(2026, 10, 26), load_config('config/strategy.json'), object(),
                     cash=40000, holdings=[{'secid': 'UNKNOWN', 'quantity': 1, 'market_value': 10000}])


def test_security_name_reaches_output_and_holdings(monkeypatch):
    import bond_strategy.pipeline as p
    cfg = load_config('config/strategy.json')
    issue = normal(); issue.secid, issue.isin = 'HELD', 'HELD_ISIN'
    issue.security_name = 'ТестБум 1'
    rows = [{'SECID': 'HELD', 'SHORTNAME': 'ТестБум 1'}]
    monkeypatch.setattr(p, 'ofz_curve', lambda c: [(1.0, 0.1)])
    monkeypatch.setattr(p, 'fetch_board', lambda c, b: rows)
    monkeypatch.setattr(p, 'screen', lambda r, d, c: ([], UniverseStats()))
    monkeypatch.setattr(p, 'build_universe', lambda *a, **kw: ([issue], UniverseStats(), []))
    monkeypatch.setattr(p, 'evaluate_issue', lambda *a, **kw: SimpleNamespace(
        score=0, signal_type=SignalType.WATCH, suggested_amount=None, suggested_quantity=None,
        reasons=[], failed_conditions=[]))
    r = run_pipeline(date(2026, 10, 26), cfg, object(), workers=1, cash=40000,
                     holdings=[{'secid': 'HELD', 'quantity': 10, 'market_value': 10000}])
    assert r.summary['portfolio']['holdings'][0]['security_name'] == 'ТестБум 1'
