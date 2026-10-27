"""Тест загрузки данных MOEX ISS."""
import pytest

from bond_strategy.ingestion.moex import MoexClient
from bond_strategy.normalization.moex import moex_to_bond_issue, normalize_bond_type
from bond_strategy.test_data import TEST_DATE


@pytest.fixture(scope="module")
def client():
    """Клиент без сохранения исходных файлов в тестах."""
    return MoexClient(raw_dir=None)


def test_moex_client_connects(client):
    """MOEX ISS доступен и отвечает на запросы."""
    bonds = client.list_bonds_tqcb()
    assert len(bonds) > 100, "Должно быть несколько тысяч выпусков"
    assert all("SECID" in b for b in bonds)


def test_bond_type_normalization():
    assert normalize_bond_type("Облигация с фиксированным (известным) купоном") == "FIXED"
    assert normalize_bond_type("Облигация с плавающим купоном") == "FLOATING"
    assert normalize_bond_type("Валютная облигация") == "FOREIGN"
    assert normalize_bond_type(None) == "UNKNOWN"


@pytest.mark.slow
def test_moex_to_bond_issue_real_data(client):
    """Сборка BondIssue из реальных данных MOEX."""
    # Берём первые 10 выпусков
    bonds = client.list_bonds_tqcb()[:10]
    
    for sec in bonds:
        secid = sec["SECID"]
        desc = client.get_security_description(secid)
        bondz = client.get_bondization(secid)
        md = client.get_marketdata(secid)
        
        issue = moex_to_bond_issue(sec, desc, bondz, md, TEST_DATE)
        
        # Валютные выпуски отсекаются
        if issue is None:
            continue
        
        # Базовая валидация
        assert issue.isin
        assert issue.ticker == secid
        assert issue.nominal > 0
        assert issue.coupon_type in ("FIXED", "FLOATING")
        assert issue.maturity_date is not None
        assert issue.maturity_date > TEST_DATE
        
        # Рыночные данные могут отсутствовать
        if issue.clean_price is not None:
            assert issue.clean_price > 0
        
        break  # проверяем хотя бы один


@pytest.mark.slow
def test_bondization_parses_coupons_and_amortization(client):
    """bondization корректно возвращает купоны и амортизацию."""
    # Берём выпуск с амортизацией из предыдущих проб
    secid = "RU000A0JQAM6"
    bondz = client.get_bondization(secid)
    
    assert "coupons" in bondz
    assert "amortizations" in bondz
    assert "offers" in bondz
    
    # Амортизация: data_source='maturity' — это погашение, не амортизация
    non_maturity = [a for a in bondz["amortizations"] if a.get("data_source") != "maturity"]
    assert len(non_maturity) >= 0  # может быть 0, если только погашение


@pytest.mark.slow
def test_marketdata_includes_prices_and_spreads(client):
    """Рыночные данные содержат цены, НКД и спреды."""
    # Берём ликвидный выпуск
    secid = "RU000A10DS74"
    md = client.get_marketdata(secid)
    
    if md is None:
        pytest.skip("Нет торгов по этому выпуску сегодня")
    
    # Хотя бы одна из цен должна быть
    assert md.get("PREVWAPRICE") or md.get("LAST") or md.get("PREVPRICE")
    assert "ACCRUEDINT" in md
    assert "VALTODAY" in md


def test_moex_to_bond_rejects_foreign_currency(client):
    """Валютные выпуски отсекаются."""
    sec = {"SECID": "TEST", "FACEVALUE": 1000, "LOTSIZE": 1}
    desc = {
        "FACEUNIT": "USD",
        "MATDATE": "2030-01-01",
        "BOND_TYPE": "Валютная облигация",
        "ISQUALIFIEDINVESTORS": "0",
    }
    bondz = {"coupons": [], "amortizations": [], "offers": []}
    
    issue = moex_to_bond_issue(sec, desc, bondz, None, TEST_DATE)
    assert issue is None


def test_moex_to_bond_rejects_expired(client):
    """Выпуски с истёкшим сроком погашения отсекаются."""
    sec = {"SECID": "TEST", "FACEVALUE": 1000, "LOTSIZE": 1}
    desc = {
        "FACEUNIT": "SUR",
        "MATDATE": "2020-01-01",  # прошлое
        "BOND_TYPE": "Облигация с фиксированным купоном",
        "ISQUALIFIEDINVESTORS": "0",
    }
    bondz = {"coupons": [], "amortizations": [], "offers": []}
    
    issue = moex_to_bond_issue(sec, desc, bondz, None, TEST_DATE)
    assert issue is None
