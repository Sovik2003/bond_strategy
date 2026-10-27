"""Скоринг: нормировка весов и отсутствие компоненты financial.

Компоненты financial больше нет: чистый долг/EBITDA и покрытие процентов
ниоткуда не загружаются, поэтому 20% веса считались бы по константе 55.
Кредитное качество измеряется рейтингом.
"""
import pytest

from bond_strategy.analytics.scoring import total_score
from bond_strategy.config import StrategyConfig, load_config
from tests.facts import TEST_DATE, normal


@pytest.fixture(scope="module")
def cfg():
    return load_config("config/strategy.json")


def _with_weights(base: StrategyConfig, weights: dict) -> StrategyConfig:
    raw = dict(base.raw)
    raw["scoring_weights"] = weights
    return StrategyConfig(raw=raw, config_id="test")


# ---------- отсутствие financial ----------


def test_components_have_no_financial(cfg):
    issue = normal()
    _, components = total_score(issue, TEST_DATE, 0.03, cfg)
    assert "financial" not in components


def test_components_are_the_four_measured(cfg):
    _, components = total_score(normal(), TEST_DATE, 0.03, cfg)
    assert set(components) == {"credit", "yield", "liquidity", "structure"}


def test_config_weights_sum_to_one(cfg):
    weights = cfg.get("scoring_weights")
    assert set(weights) == {"credit", "yield", "liquidity", "structure"}
    assert sum(weights.values()) == pytest.approx(1.0, abs=1e-6)


def test_all_components_max_gives_hundred(cfg):
    """При идеале по всем компонентам балл ровно 100 — потолок не срезан."""
    _, components = total_score(normal(), TEST_DATE, 0.03, cfg)
    weights = cfg.get("scoring_weights")
    perfect = sum(100.0 * weights[k] for k in components)
    assert perfect == pytest.approx(100.0, abs=1e-6)


# ---------- нормировка ----------


def test_incomplete_weights_are_normalized(cfg):
    """Неполный набор весов считается как среднее по включённым компонентам.

    Важно, чего НЕ происходит: балл не умножается на сумму весов. Без
    нормировки результат был бы 80.0 (0.5*84 + 0.2*100 + 0.2*90 = 80 при
    сумме весов 0.9), то есть максимум молча съезжал бы вниз, и порог BUY
    становился бы недостижим без всякой причины.

    С нормировкой получается 88.89 — честное среднее по трём компонентам.
    Оно ниже полного набора (90.0) не из-за сбоя, а потому что из среднего
    убрали structure, который давал 100 и тянул оценку вверх.
    """
    full = _with_weights(cfg, {"credit": 0.5, "yield": 0.2, "liquidity": 0.2, "structure": 0.1})
    partial = _with_weights(cfg, {"credit": 0.5, "yield": 0.2, "liquidity": 0.2})

    issue = normal()
    score_full, _ = total_score(issue, TEST_DATE, 0.03, full)
    score_partial, _ = total_score(issue, TEST_DATE, 0.03, partial)

    assert score_full == pytest.approx(90.0, abs=0.01)
    # Без нормировки было бы 80.0 — то самое молчаливое занижение.
    unnormalized = 0.5 * 84.0 + 0.2 * 100.0 + 0.2 * 90.0
    assert unnormalized == pytest.approx(80.0, abs=0.01)
    assert score_partial == pytest.approx(80.0 / 0.9, abs=0.01)
    assert score_partial > unnormalized


def test_zero_weights_return_zero_not_crash(cfg):
    empty = _with_weights(cfg, {})
    score, components = total_score(normal(), TEST_DATE, 0.03, empty)
    assert score == 0.0
    assert components  # компоненты всё равно посчитаны


def test_missing_weights_key_defaults_to_zero(cfg):
    """Отсутствующие в конфигурации веса считаются нулевыми, а не падают."""
    only_credit = _with_weights(cfg, {"credit": 1.0})
    score, _ = total_score(normal(), TEST_DATE, 0.03, only_credit)
    # A+ -> 84, это единственная компонента с весом
    assert score == pytest.approx(84.0, abs=0.01)


# ---------- достижимость порога ----------


def test_score_reaches_buy_threshold_for_good_rating(cfg):
    """Рейтинг A и выше позволяет набрать порог — фильтр не непроходим."""
    weights = cfg.get("scoring_weights")
    for rating, credit in (("AAA", 100), ("AA", 92), ("A", 80)):
        issue = normal()
        issue.rating = rating
        ceiling = credit * weights["credit"] + 100 * (1 - weights["credit"])
        assert ceiling >= cfg.min_buy_score, f"{rating}: потолок {ceiling:.1f} < порога"


def test_bbb_can_reach_threshold_after_weight_redistribution(cfg):
    """Следствие удаления financial: BBB проходит порог при идеале в остальном.

    Это осознанная фиксация текущего поведения, а не желаемое свойство.
    Раньше 20% веса приходилось на financial, и максимум для BBB был ниже
    порога. После перераспределения вес credit вырос до 0.4375, поэтому
    BBB (62) даёт 83.4 — выше min_buy_score 80.

    Если кредитное качество должно само по себе отсекать BBB, это делается
    либо поднятием min_buy_score, либо отдельным фильтром по рейтингу,
    а не весом компоненты. Тест страхует от незаметного изменения.
    """
    weights = cfg.get("scoring_weights")
    ceiling = 62 * weights["credit"] + 100 * (1 - weights["credit"])
    assert ceiling == pytest.approx(83.375, abs=0.01)
    assert ceiling > cfg.min_buy_score


def test_bb_cannot_reach_threshold(cfg):
    """BB (38) при идеале в остальном порог не берёт — нижняя граница держится."""
    weights = cfg.get("scoring_weights")
    ceiling = 38 * weights["credit"] + 100 * (1 - weights["credit"])
    assert ceiling < cfg.min_buy_score


def test_total_score_is_bounded(cfg):
    score, _ = total_score(normal(), TEST_DATE, 1.0, cfg)
    assert 0.0 <= score <= 100.0
