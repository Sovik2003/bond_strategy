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
from ..ingestion.mapping import load_issuers
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
    limit: int | None = None,
    held_secids: set[str] | None = None,
    progress: Progress | None = None,
) -> tuple[list[BondIssue], dict[str, str]]:
    """Выпуски, доступные на рынке, и ошибки загрузки по ISIN.

    `page_size` — сколько выпусков обогащать за один заход. Размер страницы
    выбран так, чтобы одна страница укладывалась в разумное время и кэш
    сохранялся часто: при обрыве теряется не больше одной страницы.

    `cache_root=None` отключает кэш — нужно, когда важна свежесть любой ценой
    или когда прогон разовый.

    `limit` ограничивает число обогащаемых выпусков. Это не «режим для тестов»
    только: на медленном канале полезно сначала собрать часть рынка, посмотреть
    отчёт и продолжить позже — кэш делает второй запуск продолжением первого.

    `held_secids` — бумаги, которые уже есть в портфеле. Они загружаются в обход
    отсева, и это не поблажка, а требование смысла.

    Отсев скринера отвечает на вопрос «стоит ли покупать», и его пороги (срок до
    погашения, оферта, листинг) относятся к решению о покупке. Но бумага, которая
    уже куплена, никуда не денется от того, что до погашения осталось 34 дня или
    что объявлена оферта: её всё равно надо оценить — посчитать долю в портфеле,
    дюжину вклада в дюрацию, размер купона. Отбросив её на входе, программа
    сообщала «не найдено на рынке» про бумагу, которая на рынке есть.

    Продавать такую бумагу при этом никто не предлагает: причины продажи живут в
    `check_risk_quality` и не пересекаются с порогами покупки.
    """
    say = progress or _silent
    cache = (
        MarketCache(
            cache_root,
            MarketCache.make_fingerprint(
                as_of, strategy_cfg.config_id, MarketCache.issuers_digest(issuers_path)
            ),
        )
        if cache_root
        else None
    )

    rows = fetch_board(client, BOARD_CORP)
    say(f"Доска: {len(rows)} строк")

    passed, stats = screen(rows, as_of, strategy_cfg)
    say(f"Прошло отсев: {len(passed)} из {len(rows)}")

    # Бумаги из портфеля — в начало и в обход отсева. В начало, потому что при
    # `limit` они не должны отсекаться первыми: оценка того, что уже куплено,
    # важнее поиска нового.
    if held_secids:
        passed_ids = {row["SECID"] for row in passed}
        held_rows = [row for row in rows if row["SECID"] in held_secids]
        bypassed = [row for row in held_rows if row["SECID"] not in passed_ids]
        if bypassed:
            passed = bypassed + passed
            say(
                f"Из портфеля взяты в обход отсева: "
                f"{', '.join(row['SECID'] for row in bypassed)}"
            )
        elsewhere = held_secids - {row["SECID"] for row in held_rows}
        if elsewhere:
            say(f"Нет на доске: {', '.join(sorted(elsewhere))}")
    if limit is not None:
        passed = passed[:limit]
        say(f"Ограничение выборки: {len(passed)} выпусков")

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
            # build_universe возвращает (выпуски, статистика, ошибки) — порядок
            # именно такой, и перепутать второй с третьим легко.
            loaded, _page_stats, page_errors = build_universe(
                client,
                pending,
                as_of,
                strategy_cfg,
                issuers_path,
                workers=workers,
                stats=stats,
            )
            issues.extend(loaded)
            for error in page_errors:
                errors[error.split(":")[0]] = error
            if cache is not None:
                cache.put_many(loaded)
                cache.save()

            say(
                f"Страница {start // page_size + 1} из "
                f"{(len(passed) + page_size - 1) // page_size}: "
                f"+{len(loaded)} выпусков, ошибок {len(page_errors)}, "
                f"в кэше {len(cache) if cache else 0}"
            )
        else:
            say(f"Страница {start // page_size + 1}: целиком из кэша")

    # Возвращается ровно то, что относится к текущему отбору. Раньше здесь
    # брался весь кэш целиком, и `limit` переставал работать: к выборке из
    # пяти бумаг подмешивались все, загруженные когда-либо. Кэш — это источник
    # данных, а не источник списка: список задаёт доска и отсев.
    allowed = {row["SECID"] for row in passed}
    issues = [issue for issue in issues if issue.secid in allowed]

    # Справочник применяется заново поверх загруженного. Отрасль и название
    # эмитента приходят из `config/issuers.json`, но попадают в объект при
    # обогащении — то есть кэшируются вместе с ним. Без переприменения правка
    # справочника не действовала бы на уже загруженный рынок: выпуски из кэша
    # пришли бы со старой отраслью, и отчёт показал бы прежнюю картину.
    #
    # Дешевле, чем заставлять перекачивать рынок ради одной строки в справочнике:
    # присваивание в памяти вместо сотен сетевых запросов.
    try:
        directory = load_issuers(issuers_path)
    except (OSError, ValueError):
        directory = {}
    if directory:
        refreshed = 0
        for issue in issues:
            record = directory.get(issue.issuer_id)
            if record is None:
                continue
            if (issue.industry, issue.issuer_name) != (record.industry, record.name):
                refreshed += 1
            issue.industry = record.industry
            issue.issuer_name = record.name
        if refreshed:
            say(f"Справочник эмитентов применён заново: {refreshed} выпусков")

    return issues, errors
