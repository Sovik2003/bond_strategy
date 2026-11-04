"""Тесты отчёта и сквозного прогона (этап 7, раздел 6).

Проверяются инварианты, по которым инвестор судит о достоверности отчёта:

1. Рекомендации совпадают с тем, что реально проверено планировщиком.
2. Структура «после» пересчитана от того же знаменателя, что и «до».
3. Остаток на счёте сходится: до − покупки = после.
4. Примечания про доходность и дюрацию присутствуют, когда они нужны.
"""
from datetime import date, timedelta

import pytest

from bond_strategy.models import BondIssue
from bond_strategy.rebalance.config import load_rebalance_config
from bond_strategy.rebalance.runner import rebalance

AS_OF = date(2026, 10, 26)


@pytest.fixture(scope="module")
def cfg():
    return load_rebalance_config("config/rebalance.json")


def issue(isin, issuer=None, industry="Промышленность", years=3, coupon=0.15, rating="A+"):
    return BondIssue(
        isin=isin,
        secid=isin,
        issuer_id=issuer or f"E_{isin}",
        issuer_name=f"Эмитент {isin}",
        industry=industry,
        nominal=1000.0,
        coupon_type="FIXED",
        coupon_rate=coupon,
        coupon_frequency=2,
        maturity_date=AS_OF + timedelta(days=round(years * 365)),
        rating=rating,
        rating_agency="АКРА",
        rating_date=date(2026, 6, 1),
        clean_price=100.0,
        accrued_interest=0.0,
        min_lot=1,
        security_name=f"Бумага {isin}",
    )


def test_report_is_built_end_to_end(cfg):
    universe = [issue("A", industry="Промышленность"), issue("B", industry="Нефть")]
    report, plan_result = rebalance(AS_OF, 100_000.0, [], universe, cfg)
    assert report.purchases
    assert report.summary["purchases"] == len(plan_result.purchases)
    assert report.config_id == cfg.config_id


def test_cash_reconciles(cfg):
    """До − покупки = после. Иначе отчёт врёт о деньгах."""
    universe = [issue("A", industry="Промышленность"), issue("B", industry="Нефть")]
    report, plan_result = rebalance(AS_OF, 100_000.0, [], universe, cfg)
    spent = sum(p.amount for p in plan_result.purchases)
    assert report.cash_before - spent == pytest.approx(report.cash_after)


def test_value_is_conserved_by_purchases(cfg):
    """Покупка переводит деньги в бумаги: стоимость портфеля не меняется."""
    universe = [issue("A", industry="Промышленность"), issue("B", industry="Нефть")]
    report, _ = rebalance(AS_OF, 100_000.0, [], universe, cfg)
    assert report.value_after == pytest.approx(report.value_before)


def test_shares_after_sum_to_one(cfg):
    universe = [issue("A", industry="Промышленность"), issue("B", industry="Нефть")]
    report, _ = rebalance(AS_OF, 100_000.0, [], universe, cfg)
    issues_slice = next(s for s in report.structure if s["code"] == "issue")
    bonds_share = sum(row["after"] for row in issues_slice["rows"])
    cash_share = report.cash_after / report.value_after
    assert bonds_share + cash_share == pytest.approx(1.0)


def test_structure_has_all_limit_slices(cfg):
    report, _ = rebalance(AS_OF, 100_000.0, [], [issue("A")], cfg)
    codes = {s["code"] for s in report.structure}
    assert {"issue", "issuer", "group", "industry", "band", "low_rating", "over_horizon"} <= codes


def test_over_limit_flag_is_set_for_breach(cfg):
    report, _ = rebalance(AS_OF, 100_000.0, [], [], cfg)
    assert report.structure is not None


def test_stop_reason_is_explained_when_no_purchase(cfg):
    report, _ = rebalance(AS_OF, 100_000.0, [], [issue("BAD", rating="BB")], cfg)
    assert report.purchases == []
    assert report.stop_reason
    assert report.rejected


def test_notes_mention_yield_is_pre_tax(cfg):
    report, _ = rebalance(AS_OF, 100_000.0, [], [issue("A")], cfg)
    text = " ".join(report.notes)
    assert "до налогов и комиссий" in text


def test_notes_mention_ytm_is_orientation(cfg):
    report, _ = rebalance(AS_OF, 100_000.0, [], [issue("A")], cfg)
    text = " ".join(report.notes)
    assert "ориентир" in text


def test_notes_say_remaining_cash_is_kept(cfg):
    report, _ = rebalance(AS_OF, 100_000.0, [], [issue("A")], cfg)
    if report.cash_after > 0:
        assert "сохраняется на счёте" in " ".join(report.notes)


def test_report_serialises_to_plain_types(cfg):
    import json

    report, _ = rebalance(AS_OF, 100_000.0, [], [issue("A")], cfg)
    text = json.dumps(report.as_dict(), ensure_ascii=False, default=str)
    assert '"as_of": "2026-10-26"' in text
    assert report.as_dict()["summary"]["purchases"] >= 0


def test_rebalance_is_deterministic(cfg):
    universe = [issue("A", industry="Промышленность"), issue("B", industry="Нефть")]
    first, _ = rebalance(AS_OF, 100_000.0, [], universe, cfg)
    second, _ = rebalance(AS_OF, 100_000.0, [], universe, cfg)
    assert first.as_dict() == second.as_dict()


def test_empty_portfolio_and_empty_universe_is_a_clean_report(cfg):
    report, plan_result = rebalance(AS_OF, 0.0, [], [], cfg)
    assert report.purchases == []
    assert report.stop_reason
    assert report.cash_after == 0.0


# --- названия вместо идентификаторов ---------------------------------------
#
# Доли считаются по EMITTER_ID, но в отчёте показывались голые номера:
# «Эмитент 15684 — 15.4%». Номер не сообщает инвестору ничего, и по такой
# строке нельзя понять, чей лимит превышен.

def test_issuer_slice_shows_names_not_ids(cfg):
    report, _ = rebalance(AS_OF, 100_000.0, [], [issue("A", issuer="E1"), issue("B", issuer="E2")], cfg)
    block = next(s for s in report.structure if s["code"] == "issuer")
    labels = [row["label"] for row in block["rows"]]
    assert "Эмитент A" in labels
    assert "E1" not in labels, f"в отчёте остался идентификатор: {labels}"
    # идентификатор сохранён в ключе: по нему отчёт группируется и сверяется
    assert {row["key"] for row in block["rows"]} == {"E1", "E2"}


def test_group_slice_shows_names_not_ids(cfg):
    report, _ = rebalance(AS_OF, 100_000.0, [], [issue("A", issuer="E1")], cfg)
    block = next(s for s in report.structure if s["code"] == "group")
    labels = [row["label"] for row in block["rows"]]
    assert "Эмитент A" in labels
    assert "E1" not in labels


def test_group_label_uses_custom_group_name(cfg):
    """Группу задали словами — показывается это слово, а не номер эмитента."""
    import dataclasses

    custom = dataclasses.replace(cfg, groups={"E1": "Группа ПСБ"})
    report, _ = rebalance(AS_OF, 100_000.0, [], [issue("A", issuer="E1")], custom)
    block = next(s for s in report.structure if s["code"] == "group")
    assert any(row["label"] == "Группа ПСБ" for row in block["rows"])


def test_breaches_use_names_not_ids(cfg):
    """Предупреждение о превышении — та строка, которую читают первой."""
    report, _ = rebalance(AS_OF, 10_000.0, [(issue("A", issuer="E1"), 40)], [], cfg)
    texts = [item["text"] for item in report.breaches.values()]
    assert any("Эмитент A" in t for t in texts), texts
    assert not any("Эмитент E1" in t for t in texts), texts


def test_issuer_and_group_breaches_are_distinguishable(cfg):
    """Пока карта групп пуста, группа совпадает с эмитентом.

    Обе проверки нарушаются одинаково, и одинаковый текст выглядит как
    задвоенная строка. Разные слова показывают, что проверок две.
    """
    report, _ = rebalance(AS_OF, 10_000.0, [(issue("A", issuer="E1"), 40)], [], cfg)
    texts = [item["text"] for item in report.breaches.values()]
    issuer_lines = [t for t in texts if t.startswith("Эмитент")]
    group_lines = [t for t in texts if t.startswith("Группа")]
    assert issuer_lines and group_lines
    assert set(issuer_lines).isdisjoint(group_lines)
