"""Загрузка рынка для ребалансировки — постранично и с продолжением.

Три отличия от скринера, и все три вынужденные.

1. Без отсева по ликвидности и спреду. Скринер отвечает «что интересно
   купить» и отбрасывает низкооборотные бумаги. Ребалансировка отвечает «что
   купить и в каком количестве на уже собранном портфеле»: если бумага уже
   есть в портфеле, её надо оценить даже при низком обороте. Всё остальное по
   разделу 3 проверяет допуск, а не загрузчик.

2. Постранично, с кэшем после каждого выпуска. Четыре сетевых запроса на
   выпуск при 325 выпусках — это больше тысячи запросов, и один обрыв рвал
   весь прогон. Теперь обработанное сразу ложится на диск, а следующий запуск
   продолжает с места остановки.

3. Прогресс печатается наружу, а не копится молча. Загрузка длится минуты, и
   без вывода нельзя отличить «работает» от «повисло».

Отсев по формальным признакам (`screen`) здесь остаётся: валюта, срок,
листинг, тип купона и оферта не зависят от того, что бумага уже в портфеле.
"""
from __future__ import annotations

import sys
from datetime import date
from typing import Callable

from ..config import StrategyConfig
from ..ingestion.moex import BOARD_CORP, IssClient, fetch_board
from ..models import BondIssue
from ..universe import build_universe, screen
from .market_cache import MarketCache

Progress = Callable[[str], None]


def _silent(_message: str) -> None:
    """Прогресс по умолчанию не печатается: библиотека не должна шуметь."""


def load_market(
    as_of: date,
    strategy_cfg: StrategyConfig,
    client: IssClient,
    issuers_path: str = "config/issuers.json",
    workers: int = 6,
    cache_root: str | None = "data/cache",
    page_size: int = 50,
    progress: Progress | None = None,
) -> tuple[list[BondIssue], dict[str, str]]:
    """Выпуски, доступные на рынке, и ошибки загрузки по ISIN.

    `page_size` — сколько выпусков обогащать за один заход. Размер страницы
    выбран так, чтобы одна страница укладывалась в разумное время и кэш
    сохранялся часто: при обрыве теряется не больше одной страницы.

    `cache_root=None` отключает кэш — нужно, когда важна свежесть любой ценой
    или когда прогон разовый.
    """
    say = progress or _silent
    cache = MarketCache(cache_root, MarketCache.make_fingerprint(as_of, strategy_cfg.config_id)) if cache_root else None

    rows = fetch_board(client, BOARD_CORP)
    say(f"Доска: {len(rows)} строк")

    passed, stats = screen(rows, as_of, strategy_cfg)
    say(f"Прошло отсев: {len(passed)} из {len(rows)}")

    if cache is not None:
        cached = sum(1 for row in passed if cache.has(row["SECID"]))
        if cached:
            say(f"В кэше уже есть: {cached} из {len(passed)}")

    issues: list[BondIssue] = []
    errors: dict[str, str] = {}

    for start in range(0, len(passed), page_size):
        page = passed[start : start + page_size]
        pending = [row for row in page if cache is None or not cache.has(row["SECID"])]

        # Уже загруженное берём из кэша, не трогая сеть.
        for row in page:
            if cache is not None and cache.has(row["SECID"]):
                issue = cache.get(row["SECID"])
                if issue is not None:
                    issues.append(issue)

        if pending:
            loaded, page_errors, page_stats = build_universe(
                client,
                pending,
                as_of,
                strategy_cfg,
                issuers_path,
                workers=workers,
                stats=stats,
            )
            issues.extend(loaded)
            errors.update(page_errors)
            if cache is not None:
                cache.put_many(loaded)
                cache.save()

            say(
                f"Страница {start // page_size + 1}: "
                f"+{len(loaded)} выпусков, ошибок {len(page_errors)}, "
                f"в кэше {len(cache) if cache else 0}"
            )
        else:
            say(f"Страница {start // page_size + 1}: целиком из кэша")

    if cache is not None:
        # Полный набор — из кэша: туда попало всё, что удалось загрузить за все
        # запуски, включая выпуски, которые в текущей странице не попали.
        issues = cache.all_issues() or issues

    return issues, errors
