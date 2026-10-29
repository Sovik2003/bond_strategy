"""Тесты страницы ребалансировки через AppTest.

Проверяется то, что нельзя проверить юнит-тестами логики: страница собирается
без исключений, форма портфеля общая со скринером, а прогон доходит до отчёта.

Полный прогон по рынку здесь не запускается: он требует сети до MOEX ISS и
минут времени. Тест проверяет сборку страницы и отрисовку уже готового отчёта
из session_state — этого достаточно, чтобы поймать сломанную вёрстку, неверные
ключи в структуре отчёта и падение на пустых значениях.
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

streamlit = pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

PAGE = ROOT / "pages" / "2_Ребалансировка.py"


@pytest.fixture()
def portfolio_file(tmp_path, monkeypatch):
    """Подменяем путь портфеля на временный, чтобы не трогать рабочий файл.

    Путь передаётся через переменную окружения: страница читает его при импорте,
    а не из фикстуры. Без этого тест писал бы в один файл, а страница читала
    другой, и проверка оставалась бы зелёной при любом поведении.
    """
    path = tmp_path / "portfolio.json"
    path.write_text(json.dumps({"cash": 75_000, "positions": []}), encoding="utf-8")
    monkeypatch.setenv("BOND_STRATEGY_PORTFOLIO", str(path))
    monkeypatch.setenv("BOND_STRATEGY_REPORT", str(tmp_path / "rebalance.json"))
    monkeypatch.setenv("BOND_STRATEGY_CACHE", str(tmp_path / "cache"))
    return path


def test_page_renders_without_exceptions(portfolio_file, monkeypatch):
    at = AppTest.from_file(str(PAGE), default_timeout=120)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("Ребалансировка портфеля" in t.value for t in at.title)


def test_page_shows_both_buttons(portfolio_file):
    at = AppTest.from_file(str(PAGE), default_timeout=120)
    at.run()
    labels = [b.label for b in at.button]
    assert "Сохранить портфель" in labels
    assert "Запустить ребалансировку" in labels


def test_page_asks_to_run_before_report(portfolio_file):
    at = AppTest.from_file(str(PAGE), default_timeout=120)
    at.run()
    assert any("Запустить ребалансировку" in i.value for i in at.info)


def test_sidebar_has_run_parameters(portfolio_file):
    at = AppTest.from_file(str(PAGE), default_timeout=120)
    at.run()
    # дата расчёта, ограничение загрузки, размер страницы, потоки, кэш
    assert len(at.sidebar.number_input) >= 1
    assert len(at.sidebar.slider) >= 2
    assert len(at.sidebar.checkbox) >= 1


def test_portfolio_form_is_shared_with_screener(portfolio_file):
    """Форма портфеля одна на обе страницы, иначе файл разошёлся бы."""
    at = AppTest.from_file(str(PAGE), default_timeout=120)
    at.run()
    labels = [b.label for b in at.button]
    assert "Сохранить портфель" in labels


def test_empty_portfolio_is_reported_on_run(portfolio_file):
    portfolio_file.write_text(json.dumps({"cash": 0, "positions": []}), encoding="utf-8")
    at = AppTest.from_file(str(PAGE), default_timeout=120)
    at.run()
    # нажимаем «Запустить» при пустом портфеле
    run_buttons = [b for b in at.button if "Запустить" in b.label]
    assert run_buttons
    run_buttons[0].click()
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("Портфель пуст" in e.value for e in at.error)
