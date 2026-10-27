"""Скринер облигаций на реальных данных MOEX ISS.

Порядок работы соответствует воронке из спецификации:
  универсум -> предфильтр -> детали+график+ликвидность -> домен -> сигнал

Дорогие запросы выполняются только для выпусков, прошедших предфильтр
и проверку сегодняшнего оборота.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .config import StrategyConfig, load_config
from .models import BondIssue, SignalType
from .moex.client import IssClient, block, num
from .moex.details import fetch_details, fetch_schedule
from .moex.liquidity import compute_liquidity
from .moex.mapping import build_bond_issue
from .moex.ofz import OfzCurve, fetch_ofz_curve
from .moex.universe import fetch_universe, prefilter, summarize_prefilter
from .normalization.enrichment import enrich_issue, load_issuer_registry
from .portfolio.engine import Portfolio
from .signals.generator import evaluate_issue

MARKET_PATH = "/engines/stock/markets/bonds/boards/{board}/securities.json"
EXPORT_DIR = Path("data/exports")


@dataclass
class ScreenRow:
    secid: str
    isin: str
    issuer: str
    signal: str
    score: float
    ytm: float | None
    premium: float | None
    turnover: float | None
    spread: float | None
    amount: float | None
    quality: str
    failed: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class ScreenResult:
    as_of: date
    config_id: str
    universe_total: int
    prefilter_passed: int
    prefilter_reasons: dict[str, int]
    analysed: int
    rows: list[ScreenRow]
    errors: list[dict]
    max_score: float = 0.0
    score_ceiling: dict = field(default_factory=dict)

    def by_signal(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in self.rows:
            counts[row.signal] = counts.get(row.signal, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    def to_dict(self) -> dict:
        return {
            "as_of": self.as_of.isoformat(),
            "config_id": self.config_id,
            "universe_total": self.universe_total,
            "prefilter_passed": self.prefilter_passed,
            "prefilter_reasons": self.prefilter_reasons,
            "analysed": self.analysed,
            "counts": self.by_signal(),
            "max_score": self.max_score,
            "score_ceiling": self.score_ceiling,
            "errors": self.errors,
            "rows": [
                {
                    "secid": r.secid,
                    "isin": r.isin,
                    "issuer": r.issuer,
                    "signal": r.signal,
                    "score": r.score,
                    "ytm": r.ytm,
                    "premium": r.premium,
                    "turnover": r.turnover,
                    "spread": r.spread,
                    "amount": r.amount,
                    "quality": r.quality,
                    "failed": r.failed,
                    "notes": r.notes[:6],
                }
                for r in self.rows
            ],
        }


def _market_snapshot(client: IssClient, board: str, stamp: str | None) -> dict[str, dict]:
    payload = client.get(
        MARKET_PATH.format(board=board), stamp=stamp, **{"iss.only": "marketdata,securities"}
    )
    md = {r.get("SECID"): r for r in block(payload, "marketdata")}
    sec = {r.get("SECID"): r for r in block(payload, "securities")}
    merged: dict[str, dict] = {}
    for secid, row in sec.items():
        # marketdata перекрывает securities, но только непустыми значениями:
        # в marketdata нет ACCRUEDINT, и слепое слияние затирает НКД в None.
        combined = dict(row)
        for key, value in (md.get(secid) or {}).items():
            if value is not None and value != "":
                combined[key] = value
        merged[secid] = combined
    return merged


def run_screen(
    as_of: date,
    cfg: StrategyConfig | None = None,
    client: IssClient | None = None,
    limit: int | None = None,
    min_turnover: float | None = None,
    board: str = "TQCB",
    commission_rate: float = 0.003,
    stamp: str | None = None,
    export: bool = False,
    registry_file: str | Path = Path("config/issuers.json"),
) -> ScreenResult:
    cfg = cfg or load_config()
    client = client or IssClient()
    stamp = stamp or as_of.isoformat()

    universe = fetch_universe(client, stamp=stamp)
    min_days = int(cfg.get("filters", "min_days_to_maturity"))
    passed, rejected = prefilter(universe, as_of, min_days, boards=(board,))
    reasons = summarize_prefilter(passed, rejected)

    snapshot = _market_snapshot(client, board, stamp)

    # отсечение по обороту ДО дорогих запросов
    turnover_floor = min_turnover if min_turnover is not None else float(
        cfg.get("filters", "min_daily_turnover")
    )
    candidates = []
    for row in passed:
        quote = snapshot.get(row.secid, {})
        val = num(quote.get("VALTODAY")) or 0.0
        if val >= turnover_floor:
            candidates.append(row)

    if limit is not None:
        candidates = candidates[:limit]

    curve: OfzCurve = fetch_ofz_curve(client, stamp=stamp)
    portfolio = Portfolio(cfg=cfg, cash=cfg.initial_capital)

    # Справочник рейтингов: ISS его не отдаёт, поэтому кредитное качество
    # берётся из отдельного файла. Пустой справочник — валидное состояние,
    # при котором ни один выпуск не получит подтверждённый рейтинг.
    registry_path = Path(registry_file)
    registry = load_issuer_registry(registry_path) if registry_path.exists() else {}
    rating_max_age = int(cfg.get("freshness", "rating_max_age_days"))

    rows: list[ScreenRow] = []
    errors: list[dict] = []

    for raw in candidates:
        try:
            details = fetch_details(client, raw.secid, stamp=stamp)
            schedule = fetch_schedule(client, raw.secid, stamp=stamp)
            liquidity = compute_liquidity(client, raw.secid, as_of, board=board, stamp=stamp)
            quote = snapshot.get(raw.secid, {})

            clean = num(quote.get("LAST")) or num(quote.get("MARKETPRICE")) or num(quote.get("PREVPRICE"))

            # ISS отдаёт ACCRUEDINT в РУБЛЯХ на одну бумагу, а доменная модель
            # хранит НКД в процентах от номинала (как в котировках).
            nkd_rub = num(quote.get("ACCRUEDINT"))
            face = details.facevalue or 1000.0
            nkd_pct = (nkd_rub / face * 100.0) if (nkd_rub is not None and face > 0) else None

            ytm_market = num(quote.get("YIELD"))
            ytm_frac = ytm_market / 100.0 if ytm_market else None

            mapped = build_bond_issue(
                details, schedule, liquidity, curve, as_of,
                clean_price=clean,
                accrued_interest=nkd_pct,
                price_date=as_of,
                ytm_from_market=ytm_frac,
            )
            issue = mapped.issue
            enrich_issue(issue, registry, as_of, rating_max_age)

            prices = (
                {issue.isin: (issue.clean_price, issue.accrued_interest)}
                if issue.clean_price is not None and issue.accrued_interest is not None
                else {}
            )
            signal = evaluate_issue(issue, portfolio, prices, as_of, cfg, commission_rate)

            rows.append(
                ScreenRow(
                    secid=issue.ticker,
                    isin=issue.isin,
                    issuer=issue.issuer_name,
                    signal=signal.signal_type.value,
                    score=signal.score,
                    ytm=issue.ytm,
                    premium=(issue.ytm - issue.ofz_ytm) if (issue.ytm and issue.ofz_ytm) else None,
                    turnover=issue.avg_daily_turnover,
                    spread=issue.avg_spread,
                    amount=signal.suggested_amount,
                    quality=issue.quality.value,
                    failed=signal.failed_conditions,
                    notes=mapped.notes,
                )
            )
        except Exception as exc:  # одна бумага не должна ронять весь прогон
            errors.append({"secid": raw.secid, "error": f"{type(exc).__name__}: {exc}"})

    result = ScreenResult(
        as_of=as_of,
        config_id=cfg.config_id,
        universe_total=len(universe),
        prefilter_passed=len(passed),
        prefilter_reasons=reasons,
        analysed=len(rows),
        rows=rows,
        errors=errors,
        max_score=max((r.score for r in rows), default=0.0),
        score_ceiling=score_ceiling(cfg),
    )

    if export:
        EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        out = EXPORT_DIR / f"screen_moex_{as_of.isoformat()}.json"
        out.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    return result


def score_ceiling(cfg: StrategyConfig) -> dict:
    """Верхняя граница балла при данных, доступных из MOEX ISS.

    Считается честно: кредитный компонент ограничен прокси по листингу,
    финансовый — прокси без отчётности, остальные три могут дать максимум.
    Если потолок ниже порога покупки, стратегия в принципе не может
    выдать BUY на одном ISS — это диагноз, а не повод снизить порог.
    """
    from .analytics.scoring import LISTING_PROXY, financial_score
    from .models import BondIssue, Quality

    weights = cfg.get("scoring_weights", default={})
    probe = BondIssue(
        isin="PROBE", ticker="PROBE", issuer_id="x", issuer_name="x",
        industry="x", list_level=1, credit_basis="LISTING_PROXY",
        quality=Quality.HIGH, reporting_sufficient=True,
    )
    from datetime import date as _date
    components = {
        "credit": LISTING_PROXY[1],
        "financial": financial_score(probe),
        "yield": 100.0,
        "liquidity": 100.0,
        "structure": 100.0,
    }
    ceiling = sum(components[k] * float(weights.get(k, 0.0)) for k in components)
    return {
        "components": {k: round(v, 1) for k, v in components.items()},
        "weights": {k: float(weights.get(k, 0.0)) for k in components},
        "max_possible": round(ceiling, 2),
        "threshold": cfg.min_buy_score,
        "buy_reachable": ceiling >= cfg.min_buy_score,
        "missing_inputs": [
            "кредитный рейтинг (ISS не предоставляет)",
            "финансовая отчётность эмитента (ISS не предоставляет)",
        ],
    }
