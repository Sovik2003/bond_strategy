"""Массовый загрузчик облигаций из MOEX ISS."""
from __future__ import annotations

from datetime import date
from pathlib import Path

from ..models import BondIssue
from .moex import MoexClient
from ..normalization.moex import moex_to_bond_issue


def load_bonds_from_moex_bulk(
    as_of: date,
    raw_dir: Path | None = None,
    min_maturity_days: int = 180,
    min_daily_turnover: float = 300_000,
) -> list[BondIssue]:
    """Массовая загрузка корпоративных облигаций из MOEX ISS.

    Один запрос на весь board, затем индивидуальные bondization только для
    прошедших первичный фильтр выпусков.
    
    Args:
        as_of: дата снимка
        raw_dir: каталог для сохранения исходных JSON
        min_maturity_days: минимальный остаточный срок
        min_daily_turnover: минимальный оборот для первичного фильтра
    
    Returns:
        Список BondIssue, готовых к анализу
    """
    client = MoexClient(raw_dir=raw_dir)
    
    # 1. Массовый запрос: securities + marketdata за один раз
    data = client.get(
        "/engines/stock/markets/bonds/boards/TQCB/securities.json",
        **{"iss.only": "securities,marketdata"}
    )
    
    sc_cols = data["securities"]["columns"]
    md_cols = data["marketdata"]["columns"]
    
    securities = [dict(zip(sc_cols, row)) for row in data["securities"]["data"]]
    marketdata = [dict(zip(md_cols, row)) for row in data["marketdata"]["data"]]
    
    # Объединить по SECID
    by_secid = {s["SECID"]: dict(s) for s in securities}
    for md in marketdata:
        secid = md["SECID"]
        if secid in by_secid:
            by_secid[secid].update(md)
    
    # 2. Первичный фильтр: рубли, срок, оборот
    candidates = []
    for secid, merged in by_secid.items():
        # Только рубли (по первой проверке — поле FACEUNIT нет в securities, проверим позже)
        matdate_str = merged.get("MATDATE")
        if not matdate_str:
            continue
        try:
            matdate = date.fromisoformat(matdate_str)
        except (ValueError, TypeError):
            continue
        
        if (matdate - as_of).days < min_maturity_days:
            continue
        
        # Минимальный оборот (грубый фильтр до запроса bondization)
        valtoday = merged.get("VALTODAY")
        try:
            if valtoday is None or float(valtoday) < min_daily_turnover:
                continue
        except (ValueError, TypeError):
            continue
        
        candidates.append((secid, merged))
    
    print(f"Первичный фильтр: {len(candidates)} из {len(by_secid)} выпусков")
    
    # 3. Для каждого кандидата: description (FACEUNIT, BOND_TYPE) + bondization
    issues: list[BondIssue] = []
    for secid, merged in candidates:
        try:
            desc = client.get_security_description(secid)
            
            # Проверка валюты
            if desc.get("FACEUNIT") != "SUR":
                continue
            
            bondz = client.get_bondization(secid)
            
            # Сборка BondIssue из объединённых данных
            issue = moex_to_bond_issue(
                security=merged,
                description=desc,
                bondization=bondz,
                marketdata=merged,  # уже объединено
                as_of=as_of
            )
            
            if issue is None:
                continue
            
            issues.append(issue)
            
        except Exception as e:
            print(f"WARN: {secid} пропущен: {e}")
            continue
    
    # 4. Загрузить ОФЗ и сопоставить
    ofz_curve = load_ofz_curve(client, as_of)
    for issue in issues:
        issue.ofz_ytm = match_ofz_ytm(issue, ofz_curve)
    
    print(f"Загружено {len(issues)} выпусков с ОФЗ-benchmark")
    return issues


def load_ofz_curve(client: MoexClient, as_of: date) -> list[dict]:
    """Загрузка кривой доходности ОФЗ."""
    ofz = client.list_ofz()
    curve = []
    for o in ofz:
        matdate = o.get("MATDATE")
        ytm = o.get("YIELD")
        if not matdate or ytm is None:
            continue
        try:
            mat = date.fromisoformat(matdate)
            days = (mat - as_of).days
            if days > 0 and float(ytm) > 0:
                curve.append({"matdate": mat, "days": days, "ytm": float(ytm) / 100.0})
        except (ValueError, TypeError):
            continue
    return sorted(curve, key=lambda x: x["days"])


def match_ofz_ytm(issue: BondIssue, curve: list[dict]) -> float | None:
    """Сопоставление корпоративного выпуска с ближайшим ОФЗ по сроку."""
    if not issue.maturity_date or not curve:
        return None
    
    as_of = issue.price_date or date.today()
    days = (issue.maturity_date - as_of).days
    
    if days <= 0:
        return None
    
    closest = min(curve, key=lambda x: abs(x["days"] - days))
    ytm = closest.get("ytm")
    return ytm if ytm and ytm > 0 else None
