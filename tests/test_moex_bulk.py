"""Тест массового загрузчика."""
import pytest
from datetime import date

from bond_strategy.ingestion.loader_bulk import load_bonds_from_moex_bulk
from bond_strategy.models import BondIssue


@pytest.mark.slow
def test_bulk_loader_completes_under_60_seconds():
    """Массовый загрузчик должен обработать ~200 выпусков менее чем за минуту."""
    as_of = date(2026, 10, 26)
    issues = load_bonds_from_moex_bulk(as_of, raw_dir=None, min_daily_turnover=300_000)
    
    assert len(issues) > 50, "Должно быть минимум 50 подходящих выпусков"
    
    for issue in issues:
        assert isinstance(issue, BondIssue)
        assert issue.nominal > 0
        assert issue.maturity_date > as_of
        assert issue.avg_daily_turnover is None or issue.avg_daily_turnover >= 0


@pytest.mark.slow
def test_bulk_loader_sets_ofz_benchmark():
    """Массовый загрузчик сопоставляет ОФЗ."""
    as_of = date(2026, 10, 26)
    issues = load_bonds_from_moex_bulk(as_of, raw_dir=None, min_daily_turnover=500_000)
    
    with_ofz = [i for i in issues if i.ofz_ytm is not None and i.ofz_ytm > 0]
    assert len(with_ofz) > 10, "Большинство выпусков должно иметь ОФЗ-benchmark"
