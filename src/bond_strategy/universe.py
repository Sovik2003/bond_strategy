"""Юниверсум: все выпуски, проходящие критерии допуска, и их ранжирование.

Устройство в два этапа, и это не оптимизация ради оптимизации.

Этап 1 — отсев по данным борды. Борда отдаёт в одном запросе всё, что нужно
для грубого отсева: валюту, срок, уровень листинга, тип купона, дату оферты,
цену, НКД. Эти проверки бесплатны, поэтому применяются ко всем 3031 выпускам.
Результат — 324 выпуска на реальных данных прогона.

Этап 2 — обогащение. На каждый выпуск нужно четыре сетевых запроса:
description, bondization, рейтинг эмитента, история ликвидности.

Порядок величин на живых данных: около секунды на выпуск при нескольких
потоках, то есть весь юниверсум обогащается за считанные минуты. Точное
время зависит от отклика ISS и числа потоков, поэтому здесь не фиксируется.

Обогащение идёт параллельно и с кэшем рейтингов по EMITTER_ID: на 60
выпусках оказалось 23 уникальных эмитента, то есть 62% запросов рейтинга
лишние. Кэш хранит и отсутствие рейтинга, иначе эмитент без рейтинга
запрашивался бы на каждый свой выпуск.

Ключевое отличие от прежнего поведения: раньше конвейер брал первые N строк
борды. Порядок борды произволен — в начале списка оказывались мелкие и
неликвидные выпуски, а ликвидные в прогон не попадали вовсе. Здесь
обрабатывается весь прошедший отсев, а отбор лучших происходит уже после
обогащения, по баллу.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date

from .config import StrategyConfig
from .ingestion.liquidity import liquidity_stats
from .ingestion.mapping import build_issue, load_issuers
from .ingestion.moex import (
    IssClient,
    coupon_type_from,
    fetch_bondization,
    fetch_description,
    num,
)
from .ingestion.ratings import Rating, fetch_company_ratings, pick_rating
from .models import BondIssue
from .validation.freshness import apply_validation


@dataclass
class UniverseStats:
    """Счётчики по этапам — без них непонятно, где потерялись выпуски."""

    board_rows: int = 0
    rejected_currency: int = 0
    rejected_no_maturity: int = 0
    rejected_maturity: int = 0
    rejected_list_level: int = 0
    rejected_no_price: int = 0
    rejected_coupon_type: int = 0
    rejected_offer: int = 0
    passed_screen: int = 0
    enriched: int = 0
    build_failed: int = 0
    errors: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "board_rows": self.board_rows,
            "passed_screen": self.passed_screen,
            "enriched": self.enriched,
            "rejected_currency": self.rejected_currency,
            "rejected_maturity": self.rejected_maturity + self.rejected_no_maturity,
            "rejected_list_level": self.rejected_list_level,
            "rejected_no_price": self.rejected_no_price,
            "rejected_coupon_type": self.rejected_coupon_type,
            "rejected_offer": self.rejected_offer,
            "build_failed": self.build_failed,
            "errors": self.errors,
        }


def screen_board_row(
    row: dict, as_of: date, cfg: StrategyConfig, stats: UniverseStats
) -> bool:
    """Проверки, которые не требуют сетевых запросов.

    Возвращает True, если выпуск идёт в обогащение. Причина отказа
    записывается в stats — иначе непонятно, куда делись сотни бумаг.
    """
    if (row.get("FACEUNIT") or "SUR") not in ("SUR", "RUB"):
        stats.rejected_currency += 1
        return False

    maturity_raw = row.get("MATDATE")
    if not maturity_raw:
        stats.rejected_no_maturity += 1
        return False
    try:
        maturity = date.fromisoformat(str(maturity_raw)[:10])
    except ValueError:
        stats.rejected_no_maturity += 1
        return False

    min_days = int(cfg.get("filters", "min_days_to_maturity"))
    if (maturity - as_of).days < min_days:
        stats.rejected_maturity += 1
        return False

    max_level = int(cfg.get("filters", "max_list_level"))
    level = num(row.get("LISTLEVEL"))
    if level and level > max_level:
        stats.rejected_list_level += 1
        return False

    if not row.get("PREVPRICE") or row.get("ACCRUEDINT") is None:
        stats.rejected_no_price += 1
        return False

    # Тип купона по борде. Проверено на 40 выпусках: BONDTYPE и COUPON_DETAILS
    # борды дают тот же результат, что полный description — расхождений нет.
    # Это позволяет отсеять плавающий и структурный купон без запроса.
    coupon = coupon_type_from(row.get("BONDTYPE"), None, row.get("COUPON_DETAILS"))
    if coupon != "FIXED":
        stats.rejected_coupon_type += 1
        return False

    offer_raw = row.get("OFFERDATE")
    if offer_raw:
        try:
            offer = date.fromisoformat(str(offer_raw)[:10])
        except ValueError:
            offer = None
        if offer and offer > as_of:
            stats.rejected_offer += 1
            return False

    return True


def screen(
    rows: list[dict], as_of: date, cfg: StrategyConfig
) -> tuple[list[dict], UniverseStats]:
    stats = UniverseStats(board_rows=len(rows))
    passed = [row for row in rows if screen_board_row(row, as_of, cfg, stats)]
    stats.passed_screen = len(passed)
    return passed, stats


def _enrich_one(
    client: IssClient,
    row: dict,
    as_of: date,
    cfg: StrategyConfig,
    issuers,
    window_days: int,
    rating_cache: dict[str, Rating | None],
    cache_lock,
) -> tuple[BondIssue | None, str | None]:
    """Обогащение одного выпуска. Возвращает (выпуск, текст ошибки)."""
    secid = row["SECID"]
    try:
        description = fetch_description(client, secid)
        bondization = fetch_bondization(client, secid)
    except Exception as exc:
        return None, f"{secid}: {exc}"

    emitter_id = str(description.get("EMITTER_ID") or "")

    # Кэш рейтингов: у одного эмитента бывает более десяти выпусков,
    # и запрашивать его рейтинг на каждый — лишние секунды.
    with cache_lock:
        cached = emitter_id in rating_cache
        rating = rating_cache.get(emitter_id) if cached else None
    if not cached:
        try:
            rating = pick_rating(fetch_company_ratings(client, emitter_id))
        except Exception:
            rating = None
        with cache_lock:
            rating_cache[emitter_id] = rating

    issue = build_issue(row, description, bondization, as_of, issuers, cfg, rating=rating)
    if issue is None:
        return None, None

    try:
        turnover, days = liquidity_stats(client, secid, as_of, window_days)
        issue.avg_daily_turnover = turnover
        issue.turnover_days = days
    except Exception as exc:
        issue.avg_daily_turnover = None
        issue.turnover_days = 0
        return issue, f"{secid} (история): {exc}"

    # Статус liquidity зависит от оборота, поэтому валидация повторяется.
    apply_validation(issue, as_of, cfg)
    return issue, None


def build_universe(
    client: IssClient,
    rows: list[dict],
    as_of: date,
    cfg: StrategyConfig,
    issuers_path: str = "config/issuers.json",
    workers: int = 6,
    stats: UniverseStats | None = None,
) -> tuple[list[BondIssue], UniverseStats, list[str]]:
    """Обогащает весь прошедший отсев, параллельно и с кэшем рейтингов."""
    import threading

    stats = stats or UniverseStats(board_rows=len(rows))
    issuers = load_issuers(issuers_path)
    window_days = int(cfg.get("filters", "liquidity_history_days", default=20))
    rating_cache: dict[str, Rating | None] = {}
    lock = threading.Lock()
    errors: list[str] = []

    def work(row: dict) -> tuple[BondIssue | None, str | None]:
        return _enrich_one(
            client, row, as_of, cfg, issuers, window_days, rating_cache, lock
        )

    issues: list[BondIssue] = []
    if workers <= 1:
        results = [work(row) for row in rows]
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(work, rows))

    for issue, error in results:
        if error:
            errors.append(error)
            stats.errors += 1
        if issue is None:
            stats.build_failed += 1
            continue
        issues.append(issue)

    stats.enriched = len(issues)
    return issues, stats, errors
