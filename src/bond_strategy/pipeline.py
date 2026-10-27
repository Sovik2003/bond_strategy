"""Конвейер: загрузка из ISS -> отбор -> нормализация -> скрининг -> отчёт.

Сеть используется только в fetch-слое. Всё остальное работает на
переданных словарях, поэтому поддаётся офлайн-тестированию.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .config import StrategyConfig
from .ingestion.liquidity import liquidity_stats
from .ingestion.ratings import fetch_company_ratings, pick_rating
from .ingestion.mapping import build_issue, load_issuers
from .ingestion.moex import (
    BOARD_CORP,
    BOARD_OFZ,
    IssClient,
    fetch_board,
    fetch_bondization,
    fetch_description,
    num,
)
from .models import BondIssue, SignalType
from .validation.freshness import apply_validation
from .portfolio.engine import Portfolio
from .signals.generator import evaluate_issue


@dataclass
class PipelineResult:
    as_of: date
    config_id: str
    signals: list[dict] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def ofz_curve(client: IssClient) -> list[tuple[float, float]]:
    """Кривая ОФЗ: (дюрация в годах, доходность долей).

    Доходность из ISS приходит в процентах — переводим в доли.
    """
    rows = fetch_board(client, BOARD_OFZ)
    points: list[tuple[float, float]] = []
    for row in rows:
        duration = num(row.get("DURATION"))
        yld = num(row.get("YIELD"))
        if duration and duration > 0 and yld and yld > 0:
            points.append((duration / 365.0, yld / 100.0))
    return sorted(points)


def benchmark_ofz(points: list[tuple[float, float]], years: float | None) -> float | None:
    """Линейная интерполяция по дюрации; за границами — крайние точки."""
    if not points or years is None:
        return None
    if years <= points[0][0]:
        return points[0][1]
    if years >= points[-1][0]:
        return points[-1][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x0 <= years <= x1:
            if x1 == x0:
                return y0
            return y0 + (y1 - y0) * (years - x0) / (x1 - x0)
    return None


def years_to_maturity(issue: BondIssue, as_of: date) -> float | None:
    if issue.maturity_date is None:
        return None
    return max((issue.maturity_date - as_of).days, 0) / 365.0


def preselect(rows: list[dict], as_of: date, cfg: StrategyConfig) -> list[dict]:
    """Отсев до сетевых запросов: валюта, срок, уровень листинга.

    Детали запрашиваются только по прошедшим отсев — это экономит
    сотни запросов к ISS.
    """
    max_level = int(cfg.get("filters", "max_list_level"))
    max_years = float(cfg.get("filters", "preferred_max_years"))
    min_days = int(cfg.get("filters", "min_days_to_maturity"))

    selected = []
    for row in rows:
        if (row.get("FACEUNIT") or "SUR") not in ("SUR", "RUB"):
            continue
        maturity = row.get("MATDATE")
        if not maturity:
            continue
        try:
            mat = date.fromisoformat(maturity[:10])
        except ValueError:
            continue
        days = (mat - as_of).days
        if days < min_days:
            continue
        level = num(row.get("LISTLEVEL"))
        if level and level > max_level:
            continue
        # Срок сильно за пределами горизонта: не тратим запросы
        if days > max_years * 365 * 2:
            continue
        selected.append(row)
    return selected


def run_pipeline(
    as_of: date,
    cfg: StrategyConfig,
    client: IssClient,
    issuers_path: str | Path = "config/issuers.json",
    limit: int | None = None,
) -> PipelineResult:
    result = PipelineResult(as_of=as_of, config_id=cfg.config_id)

    issuers = load_issuers(issuers_path)
    window_days = int(cfg.get("filters", "liquidity_history_days", default=20))
    curve = ofz_curve(client)

    rows = fetch_board(client, BOARD_CORP)
    candidates = preselect(rows, as_of, cfg)
    if limit is not None:
        candidates = candidates[:limit]

    portfolio = Portfolio(cfg=cfg, cash=cfg.initial_capital)
    issues: list[BondIssue] = []

    for row in candidates:
        secid = row["SECID"]
        try:
            description = fetch_description(client, secid)
            bondization = fetch_bondization(client, secid)
        except Exception as exc:  # сеть или структура
            result.errors.append(f"{secid}: {exc}")
            continue

        emitter_id = description.get("EMITTER_ID")
        try:
            rating = pick_rating(fetch_company_ratings(client, emitter_id))
        except Exception as exc:
            result.errors.append(f"{secid} (рейтинг): {exc}")
            rating = None

        issue = build_issue(row, description, bondization, as_of, issuers, cfg, rating=rating)
        if issue is None:
            continue

        try:
            turnover, days = liquidity_stats(client, secid, as_of, window_days)
            issue.avg_daily_turnover = turnover
            issue.turnover_days = days
        except Exception as exc:
            result.errors.append(f"{secid} (история): {exc}")
            issue.avg_daily_turnover = None
            issue.turnover_days = 0

        # Статусы зависят от liq, поэтому валидация повторяется после
        # заполнения оборота.
        apply_validation(issue, as_of, cfg)

        years = years_to_maturity(issue, as_of)
        issue.ofz_ytm = benchmark_ofz(curve, years)
        issues.append(issue)

    prices = {
        i.isin: (i.clean_price, i.accrued_interest)
        for i in issues
        if i.clean_price is not None and i.accrued_interest is not None
    }

    for issue in issues:
        signal = evaluate_issue(issue, portfolio, prices, as_of, cfg, cfg.broker_rate)
        result.signals.append(
            {
                "isin": issue.isin,
                "secid": issue.secid,
                "issuer": issue.issuer_name,
                "maturity": issue.maturity_date.isoformat() if issue.maturity_date else None,
                "coupon_type": issue.coupon_type,
                "clean_price": issue.clean_price,
                "nkd": issue.accrued_interest,
                "avg_turnover": issue.avg_daily_turnover,
                "turnover_days": issue.turnover_days,
                "avg_spread": issue.avg_spread,
                "rating": issue.rating,
                "rating_agency": issue.rating_agency,
                "rating_date": issue.rating_date.isoformat() if issue.rating_date else None,
                "ofz_ytm": issue.ofz_ytm,
                "signal": signal.signal_type.value,
                "score": signal.score,
                "suggested_amount": signal.suggested_amount,
                "suggested_quantity": signal.suggested_quantity,
                "reasons": signal.reasons,
                "failed": signal.failed_conditions,
            }
        )

    counts = {t.value: 0 for t in SignalType}
    for s in result.signals:
        counts[s["signal"]] += 1

    result.summary = {
        "as_of": as_of.isoformat(),
        "config_id": cfg.config_id,
        "board_rows": len(rows),
        "preselected": len(candidates),
        "evaluated": len(issues),
        "ofz_points": len(curve),
        "counts": counts,
        "errors": len(result.errors),
    }
    return result


def export(result: PipelineResult, path: str | Path) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "summary": result.summary,
                "signals": result.signals,
                "errors": result.errors,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return out
