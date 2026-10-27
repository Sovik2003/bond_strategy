"""Обогащение данных кредитной информацией, которой нет в MOEX ISS.

ISS отдаёт условия выпуска и рыночные данные, но не рейтинг, не отрасль
и не факт достаточности раскрытия. Эти поля берутся из отдельного
справочника с явным происхождением.

Принцип: если рейтинга нет в справочнике — поле остаётся MISSING.
Подставлять «примерный» рейтинг нельзя: это изменило бы решение о покупке.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from ..models import BondIssue, DataStatus, Quality


@dataclass(frozen=True)
class IssuerProfile:
    """Кредитный профиль эмитента из внешнего справочника."""

    emitter_id: str
    name: str
    industry: str
    rating: str | None
    rating_agency: str | None
    rating_date: date | None
    reporting_sufficient: bool
    source: str
    updated_at: date | None


# Соответствие буквенных рейтингов категориям стратегии.
# BBB- и выше — основная категория, ниже — среднее качество.
_HIGH_RATINGS = {
    "AAA", "AA+", "AA", "AA-", "A+", "A", "A-", "BBB+", "BBB", "BBB-",
}


def quality_from_rating(rating: str | None) -> Quality:
    if rating and rating.upper() in _HIGH_RATINGS:
        return Quality.HIGH
    return Quality.MEDIUM


def load_issuer_registry(path: str | Path) -> dict[str, IssuerProfile]:
    """Читает справочник эмитентов. Пустой файл — допустимое состояние."""
    file = Path(path)
    if not file.exists():
        return {}

    raw = json.loads(file.read_text(encoding="utf-8"))
    registry: dict[str, IssuerProfile] = {}

    for item in raw.get("issuers", []):
        emitter_id = str(item.get("emitter_id") or "").strip()
        if not emitter_id:
            continue
        rating_date = item.get("rating_date")
        updated = item.get("updated_at")
        registry[emitter_id] = IssuerProfile(
            emitter_id=emitter_id,
            name=item.get("name") or f"EMITTER_{emitter_id}",
            industry=item.get("industry") or "UNKNOWN",
            rating=item.get("rating"),
            rating_agency=item.get("rating_agency"),
            rating_date=date.fromisoformat(rating_date) if rating_date else None,
            reporting_sufficient=bool(item.get("reporting_sufficient", False)),
            source=item.get("source") or "не указан",
            updated_at=date.fromisoformat(updated) if updated else None,
        )
    return registry


def enrich_issue(
    issue: BondIssue,
    registry: dict[str, IssuerProfile],
    settlement: date,
    rating_max_age_days: int = 400,
) -> BondIssue:
    """Дополняет выпуск кредитными данными. Возвращает тот же объект.

    Поля, которых нет в справочнике, остаются в статусе MISSING — фильтр
    тогда не пропустит выпуск к покупке.
    """
    profile = registry.get(issue.issuer_id)
    if profile is None:
        issue.status["rating"] = DataStatus.MISSING
        issue.status["industry"] = DataStatus.MISSING
        return issue

    issue.issuer_name = profile.name

    if profile.industry and profile.industry != "UNKNOWN":
        issue.industry = profile.industry
        issue.status["industry"] = DataStatus.FRESH
    else:
        issue.status["industry"] = DataStatus.MISSING

    if not profile.rating:
        issue.status["rating"] = DataStatus.MISSING
        return issue

    # Устаревший рейтинг не равен отсутствующему, но и не считается свежим
    if profile.rating_date is None:
        rating_status = DataStatus.UNVERIFIED
    elif (settlement - profile.rating_date).days > rating_max_age_days:
        rating_status = DataStatus.STALE
    else:
        rating_status = DataStatus.FRESH

    issue.rating = profile.rating
    issue.rating_agency = profile.rating_agency
    issue.rating_date = profile.rating_date
    issue.quality = quality_from_rating(profile.rating)
    issue.status["rating"] = rating_status

    issue.reporting_sufficient = profile.reporting_sufficient
    return issue


def enrich_all(
    issues: list[BondIssue],
    registry: dict[str, IssuerProfile],
    settlement: date,
    rating_max_age_days: int = 400,
) -> tuple[list[BondIssue], dict[str, int]]:
    """Обогащает список и возвращает статистику покрытия."""
    stats = {"total": len(issues), "with_rating": 0, "with_industry": 0, "unknown_issuer": 0}

    for issue in issues:
        enrich_issue(issue, registry, settlement, rating_max_age_days)
        if issue.rating:
            stats["with_rating"] += 1
        if issue.industry != "UNKNOWN":
            stats["with_industry"] += 1
        if issue.issuer_id not in registry:
            stats["unknown_issuer"] += 1

    return issues, stats
