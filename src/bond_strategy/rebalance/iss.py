"""Загрузка рынка для ребалансировки.

Вынесено из скринера намеренно. Скринер отвечает «что интересно купить» и
отсекает по порогам ликвидности и спреда. Ребалансировка — «что купить и в
каком количестве на уже собранном портфеле», и здесь порог ликвидности не
нужен: если бумага уже в портфеле, её надо оценить даже при низком обороте.

Поэтому здесь переиспользуются те же вызовы ISS (доска, отсев по формальным
признакам, обогащение), но без отсева по ликвидности и спреду. Всё, что
требуется по разделу 3, проверяет допуск, а не загрузчик.
"""
from __future__ import annotations

from datetime import date

from ..config import StrategyConfig
from ..ingestion.moex import BOARD_CORP, IssClient, fetch_board
from ..models import BondIssue
from ..universe import build_universe, screen


def load_market(
    as_of: date,
    strategy_cfg: StrategyConfig,
    client: IssClient,
    issuers_path: str = "config/issuers.json",
    workers: int = 6,
) -> tuple[list[BondIssue], dict[str, str]]:
    """Выпуски, доступные на рынке, и ошибки загрузки по ISIN."""
    rows = fetch_board(client, BOARD_CORP)
    passed, _stats = screen(rows, as_of, strategy_cfg)
    issues, _stats2, errors = build_universe(
        client, passed, as_of, strategy_cfg, issuers_path, workers=workers
    )
    by_isin = {err.split(":")[0]: err for err in errors}
    return issues, by_isin
