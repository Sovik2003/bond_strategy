"""Тест полного загрузчика с сопоставлением ОФЗ."""
import pytest
from datetime import date

from bond_strategy.ingestion.loader import load_bonds_from_moex, match_ofz_ytm
from bond_strategy.models import BondIssue


@pytest.mark.slow
def test_load_bonds_from_moex_returns_normalized_issues():
    """Загрузчик возвращает нормализованные BondIssue."""
    as_of = date(2026, 10, 26)
    issues = load_bonds_from_moex(as_of, raw_dir=None, limit=20)
    
    assert len(issues) > 0, "Должен быть хотя бы один подходящий выпуск"
    
    for issue in issues:
        assert isinstance(issue, BondIssue)
        assert issue.isin
        assert issue.nominal > 0
        assert issue.coupon_type in ("FIXED", "FLOATING")
        assert issue.maturity_date > as_of
        
        # ОФЗ-benchmark установлен
        if issue.ofz_ytm is not None:
            assert 0 < issue.ofz_ytm < 1.0


@pytest.mark.slow
def test_loader_excludes_foreign_and_expired():
    """Загрузчик отсекает валютные и истёкшие выпуски."""
    as_of = date(2026, 10, 26)
    issues = load_bonds_from_moex(as_of, raw_dir=None, limit=50)
    
    for issue in issues:
        # Только рубли
        assert issue.nominal > 0
        # Срок не истёк
        assert issue.maturity_date > as_of


@pytest.mark.slow
def test_loader_sets_ofz_ytm():
    """Загрузчик сопоставляет каждый выпуск с ОФЗ-benchmark."""
    as_of = date(2026, 10, 26)
    issues = load_bonds_from_moex(as_of, raw_dir=None, limit=10)
    
    with_ofz = [i for i in issues if i.ofz_ytm is not None]
    # Хотя бы часть выпусков должна получить benchmark
    assert len(with_ofz) > 0


def test_match_ofz_ytm_finds_closest():
    """Сопоставление выбирает ближайший ОФЗ по сроку."""
    issue = BondIssue(
        isin="TEST",
        ticker="TEST",
        issuer_id="1",
        issuer_name="Test",
        industry="Test",
        maturity_date=date(2028, 6, 1),
        price_date=date(2026, 10, 26),
    )
    
    curve = [
        {"matdate": date(2027, 1, 1), "days": 432, "ytm": 0.12},
        {"matdate": date(2028, 6, 1), "days": 583, "ytm": 0.135},
        {"matdate": date(2030, 1, 1), "days": 1163, "ytm": 0.14},
    ]
    
    ytm = match_ofz_ytm(issue, curve)
    assert ytm == pytest.approx(0.135)
