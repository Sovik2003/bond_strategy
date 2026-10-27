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
from .ingestion.moex import (
    BOARD_CORP,
    BOARD_OFZ,
    IssClient,
    fetch_board,
    num,
)
from .models import BondIssue, SignalType
from .portfolio.engine import Portfolio
from .signals.generator import evaluate_issue
from .universe import UniverseStats, build_universe, screen


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


def run_pipeline(
    as_of: date,
    cfg: StrategyConfig,
    client: IssClient,
    issuers_path: str | Path = "config/issuers.json",
    top: int | None = None,
    workers: int = 6,
    enrich_limit: int | None = None,
) -> PipelineResult:
    """Полный прогон: борда -> отсев -> обогащение -> ранжирование -> сигналы.

    Порядок важен. Сначала строится ВЕСЬ юниверсум, прошедший критерии, и
    только потом он ранжируется по баллу. Прежний параметр limit брал первые
    N строк борды, а порядок там произволен: в прогон попадали мелкие бумаги
    с начала списка, а ликвидные не попадали вовсе. Из-за этого отказы по
    ликвидности составляли 11 из 14 — это был артефакт выборки, а не рынка.

    top — сколько лучших оставить в отчёте ПОСЛЕ ранжирования.
    enrich_limit — техническое ограничение на число обогащаемых выпусков,
    для быстрых проверок. На смысл не влияет: ранжирование всё равно по баллу.
    """
    result = PipelineResult(as_of=as_of, config_id=cfg.config_id)

    curve = ofz_curve(client)
    rows = fetch_board(client, BOARD_CORP)

    candidates, stats = screen(rows, as_of, cfg)
    if enrich_limit is not None:
        candidates = candidates[:enrich_limit]

    issues, stats, errors = build_universe(
        client, candidates, as_of, cfg, str(issuers_path), workers=workers, stats=stats
    )
    result.errors.extend(errors)

    # Бенчмарк ОФЗ считается после обогащения: для него нужен срок до погашения.
    for issue in issues:
        issue.ofz_ytm = benchmark_ofz(curve, years_to_maturity(issue, as_of))

    prices = {
        i.isin: (i.clean_price, i.accrued_interest)
        for i in issues
        if i.clean_price is not None and i.accrued_interest is not None
    }

    portfolio = Portfolio(cfg=cfg, cash=cfg.initial_capital)
    evaluated = [
        (issue, evaluate_issue(issue, portfolio, prices, as_of, cfg, cfg.broker_rate))
        for issue in issues
    ]

    # Ранжирование: лучшие по баллу первыми. При равном балле — по убыванию
    # ликвидности, затем по secid, чтобы порядок не зависел от выдачи ISS.
    evaluated.sort(
        key=lambda pair: (
            -pair[1].score,
            -(pair[0].avg_daily_turnover or 0.0),
            pair[0].secid,
        )
    )

    ranked = evaluated[:top] if top is not None else evaluated

    for rank, (issue, signal) in enumerate(ranked, start=1):
        result.signals.append(
            {
                "rank": rank,
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
        "ofz_points": len(curve),
        "universe": stats.as_dict(),
        "ranked_total": len(evaluated),
        "reported": len(result.signals),
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
