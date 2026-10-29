"""Допуск выпуска к покупке — раздел 3 требований.

Здесь проверяется только соответствие бумаги условиям покупки. Лимиты
концентрации и деньги живут в других модулях: бумага может быть безупречной,
но не влезать в портфель.

Две разные проверки, и их нельзя смешивать:

    check_eligibility — можно ли покупать ЭТУ бумагу сегодня;
    check_risk_quality — не ухудшился ли риск по УЖЕ КУПЛЕННОЙ бумаге.

Разница принципиальна. Рейтинг ниже BBB запрещает покупку, но не является
причиной продажи: правила говорят «продажа только при ухудшении риска», и
снижение рейтинга — ухудшение, а вот отсутствие рейтинга у старой позиции —
пробел в данных. Владелец такой бумаги не должен продавать её из-за того, что
агентство не обновило страницу.

Каждый отказ несёт машинный код и человеческую причину. Код нужен, чтобы отчёт
можно было группировать («сколько отказов из-за рейтинга»), причина — чтобы
инвестор понимал, что именно произошло.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from ..ingestion.ratings import _RANK
from ..models import BondIssue
from .config import RebalanceConfig


@dataclass(frozen=True)
class Check:
    code: str
    message: str


@dataclass
class Eligibility:
    """Результат проверки: прошла ли бумага и почему нет."""

    checks: list[Check] = field(default_factory=list)
    warnings: list[Check] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.checks

    @property
    def codes(self) -> list[str]:
        return [c.code for c in self.checks]

    @property
    def reason(self) -> str:
        return "; ".join(c.message for c in self.checks)

    def reject(self, code: str, message: str) -> None:
        self.checks.append(Check(code, message))

    def warn(self, code: str, message: str) -> None:
        self.warnings.append(Check(code, message))


def _rating_check(
    issue: BondIssue, cfg: RebalanceConfig, as_of: date
) -> Check | None:
    """Действующий рейтинг не ниже порога.

    Три отдельные причины отказа, а не одна общая: «нет рейтинга», «рейтинг от
    неутверждённого агентства» и «рейтинг ниже BBB» требуют от инвестора разных
    действий. Первое — обновить данные, второе — утвердить агентство, третье —
    не покупать.
    """
    if issue.rating is None:
        return Check("EXCLUDED_NO_RATING", "Нет действующего рейтинга")

    approved = cfg.approved_agencies
    if approved and issue.rating_agency not in approved:
        return Check(
            "EXCLUDED_UNAPPROVED_AGENCY",
            f"Рейтинг от агентства «{issue.rating_agency or '—'}» вне утверждённого списка",
        )

    if issue.rating_date is None:
        return Check("EXCLUDED_RATING_UNVERIFIED", "Дата рейтинга не подтверждена")

    age = (as_of - issue.rating_date).days
    if age < 0:
        return Check("EXCLUDED_RATING_UNVERIFIED", "Дата рейтинга в будущем")
    if age > cfg.rating_max_age_days:
        return Check(
            "EXCLUDED_RATING_STALE",
            f"Рейтинг от {issue.rating_date.isoformat()} старше {cfg.rating_max_age_days} дн.",
        )

    rank = _RANK.get(issue.rating)
    if rank is None:
        return Check("EXCLUDED_RATING_UNRECOGNIZED", f"Рейтинг «{issue.rating}» не разобран")
    if rank > cfg.rating_floor_rank:
        return Check(
            "EXCLUDED_RATING_BELOW_FLOOR",
            f"Рейтинг {issue.rating} ниже минимального {cfg.rating_floor}",
        )
    return None


def check_eligibility(issue: BondIssue, cfg: RebalanceConfig, as_of: date) -> Eligibility:
    """Проверка требований раздела 3 для новой покупки."""
    result = Eligibility()

    # Свежесть данных проверяется первой: остальные поля могут быть просто не
    # заполнены, и отказ по ним был бы отказом не по той причине.
    missing = issue.missing_fields()
    if missing:
        result.reject(
            "EXCLUDED_DATA_INCOMPLETE",
            f"Нет данных для расчёта: {', '.join(sorted(missing))}",
        )
    else:
        # Полнота считается по тем же полям, поэтому проверяется только когда
        # список пуст: иначе одна причина отказа попала бы в отчёт дважды.
        completeness = issue.data_completeness()
        if completeness < cfg.min_data_completeness:
            result.reject(
                "EXCLUDED_DATA_INCOMPLETE",
                f"Полнота данных {completeness:.0%} ниже требуемых {cfg.min_data_completeness:.0%}",
            )

    if issue.default_flag:
        result.reject("EXCLUDED_DEFAULT", "Объявлен дефолт")
    if issue.restructuring_flag:
        result.reject("EXCLUDED_RESTRUCTURING", "Объявлена реструктуризация")

    # Рублёвая корпоративная бумага: валюта и тип бумаги отсеяны на борде, но
    # купон проверяется здесь, потому что ребалансировке нужен именно
    # фиксированный.
    if cfg.require_fixed_coupon and (issue.coupon_type or "").upper() != "FIXED":
        result.reject("EXCLUDED_COUPON_TYPE", f"Купон «{issue.coupon_type}» вместо фиксированного")

    if cfg.require_maturity and issue.maturity_date is None:
        result.reject("EXCLUDED_NO_MATURITY", "Нет конечной даты погашения")
    elif issue.maturity_date is not None and issue.maturity_date <= as_of:
        result.reject("EXCLUDED_MATURED", "Срок погашения уже наступил")

    # Действующая оферта — отказ. Плановая амортизация по известному графику
    # допускается, поэтому проверяется оферта, а не наличие амортизации.
    if cfg.reject_offer:
        if issue.offer_date is not None:
            result.reject("EXCLUDED_OFFER", f"Действует оферта от {issue.offer_date.isoformat()}")
        elif issue.status.get("offer") is not None and issue.status["offer"].blocks_buy:
            result.reject("EXCLUDED_OFFER", "Условия оферты не подтверждены")

    rating_check = _rating_check(issue, cfg, as_of)
    if rating_check is not None:
        # Рейтинг входит в обязательные поля, поэтому при его отсутствии уже
        # была бы причина «нет данных». Оставляем более конкретную: инвестору
        # нужно знать, что дело именно в рейтинге, а не в цене или сроке.
        if issue.rating is None:
            result.checks = [c for c in result.checks if c.code != "EXCLUDED_DATA_INCOMPLETE"]
        result.reject(rating_check.code, rating_check.message)

    # Субординация и право эмитента на досрочный выкуп: в ISS этих полей нет.
    # По умолчанию такие выпуски допускаются с замечанием — иначе блокировался
    # бы почти весь рынок из-за отсутствия данных, а не из-за риска. Флаг
    # require_unknown_flags_verified переключает поведение на строгое.
    if cfg.require_unknown_flags_verified:
        result.reject(
            "EXCLUDED_FLAGS_UNVERIFIED",
            "Субординация и право досрочного выкупа не подтверждены",
        )
    else:
        result.warn(
            "FLAGS_UNVERIFIED",
            "Субординация и право досрочного выкупа не проверяются — в ISS этих полей нет",
        )

    return result


def check_risk_quality(issue: BondIssue, cfg: RebalanceConfig, as_of: date) -> Eligibility:
    """Признаки ухудшения риска по уже купленной бумаге.

    Отличается от `check_eligibility` тем, что здесь нет отсева по цене. Позиция
    уже есть; вопрос не «можно ли купить», а «не пора ли рассматривать продажу».

    Три уровня, и их нельзя смешивать:

        checks   — подтверждённое ухудшение: дефолт, реструктуризация, рейтинг
                   ниже BBB. Это повод рассмотреть продажу;
        warnings — недостаточно данных: рейтинга нет, дата не подтверждена,
                   рейтинг не обновлялся. Это «проверить риск», а не продажа.

    Отсутствие рейтинга намеренно не считается продажей. Правило говорит
    «рейтинг снизился ниже BBB», а не «рейтинга нет»: отозванный или просроченный
    рейтинг действующим не считается, но ухудшением кредитного качества он не
    является. Продавать бумагу из-за того, что агентство не обновило страницу, —
    ровно то, чего правила делать не велят.
    """
    result = Eligibility()

    if issue.default_flag:
        result.reject("DEFAULT_CONFIRMED", "Подтверждён дефолт")
    if issue.restructuring_flag:
        result.reject("RESTRUCTURING_CONFIRMED", "Объявлена реструктуризация")

    if issue.rating is None:
        result.warn("NO_RATING", "Нет действующего рейтинга — риск не подтверждён")
        return result

    rank = _RANK.get(issue.rating)
    if rank is None:
        result.warn(
            "RATING_UNRECOGNIZED", f"Рейтинг «{issue.rating}» не разобран — риск не подтверждён"
        )
        return result

    if issue.rating_date is None:
        result.warn("RATING_UNVERIFIED", "Дата рейтинга не подтверждена")
    elif (as_of - issue.rating_date).days > cfg.rating_max_age_days:
        result.warn(
            "RATING_STALE",
            f"Рейтинг не обновлялся {(as_of - issue.rating_date).days} дн.",
        )

    if rank > cfg.rating_floor_rank:
        result.reject(
            "RATING_BELOW_FLOOR",
            f"Рейтинг {issue.rating} ниже минимального {cfg.rating_floor}",
        )

    return result
