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


# --- удаление проданной позиции --------------------------------------------
#
# Три способа, и все три должны работать: пользователь пробует их в
# произвольном порядке. Раньше каждый давал ошибку «заполните количество и
# стоимость больше нуля», и портфель не сохранялся вовсе.

def test_sold_checkbox_removes_position():
    """Галочка «Продано» — основной путь, он же виден в интерфейсе."""
    snapshot = validate_portfolio(50_000, [
        {"secid": "RU000A10AA10", "quantity": 10, "market_value": 10_500, "remove": True},
        {"secid": "RU000A0ZYU05", "quantity": 5, "market_value": 5_000, "remove": False},
    ])
    assert [p["secid"] for p in snapshot["positions"]] == ["RU000A0ZYU05"]


def test_zero_quantity_removes_position():
    """Количество 0 — естественное «продал всё»."""
    snapshot = validate_portfolio(50_000, [
        {"secid": "RU000A10AA10", "quantity": 0, "market_value": 10_500},
    ])
    assert snapshot["positions"] == []


def test_blank_row_is_skipped_not_rejected():
    """Пустая строка от кнопки «+» — не ошибка, а передумал добавлять.

    data_editor отдаёт незаполненную строку как None или NaN. Раньше это
    блокировало сохранение всего портфеля из-за одной пустой строки.
    """
    snapshot = validate_portfolio(50_000, [
        {"secid": None, "quantity": None, "market_value": None},
        {"secid": "RU000A0ZYU05", "quantity": 5, "market_value": 5_000},
    ])
    assert [p["secid"] for p in snapshot["positions"]] == ["RU000A0ZYU05"]

    nan_snapshot = validate_portfolio(50_000, [
        {"secid": float("nan"), "quantity": float("nan"), "market_value": float("nan")},
    ])
    assert nan_snapshot["positions"] == []


def test_every_position_can_be_removed():
    """Портфель можно опустеть полностью — это законное состояние."""
    snapshot = validate_portfolio(0, [
        {"secid": "RU000A10AA10", "quantity": 10, "market_value": 10_500, "remove": True},
        {"secid": "RU000A0ZYU05", "quantity": 0, "market_value": 5_000},
    ])
    assert snapshot == {"cash": 0.0, "positions": []}


def test_partial_row_still_rejected():
    """Опечатка всё ещё ловится: SECID есть, а стоимости нет.

    Отличать «удалил» от «недозаполнил» обязательно: молчаливое удаление
    позиции из-за пропущенной ячейки — та же ошибка, только тихая.
    """
    with pytest.raises(ValueError):
        validate_portfolio(50_000, [{"secid": "RU000A10AA10", "quantity": 10}])


def test_negative_quantity_rejected():
    with pytest.raises(ValueError):
        validate_portfolio(50_000, [
            {"secid": "RU000A10AA10", "quantity": -5, "market_value": 10_500},
        ])
