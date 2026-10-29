"""Страница ребалансировки в Streamlit (пункт меню «Ребалансировка»).

Streamlit подхватывает файлы из `pages/` автоматически: достаточно держать
`app.py` запущенным, отдельная команда не нужна.

Почему отдельная страница, а не вкладка. `app.py` — линейный скрипт с десятком
`st.stop()`. Вкладки в Streamlit не изолируют исполнение: скрипт один и идёт
сверху вниз, поэтому остановка в одной вкладке погасила бы и соседнюю. Чтобы
не переписывать скринер целиком, ребалансировка живёт рядом.

Что показывает страница, по разделу 6 требований:

    рекомендации  — что сделать: купить, держать, не докупать, проверить риск,
                    рассмотреть продажу;
    структура     — доли до и после по выпускам, эмитентам, группам, отраслям,
                    срокам, категории BBB/BBB+ и сроку свыше горизонта;
    состояние     — дюрация с покрытием, ориентир доходности, остаток, купоны
                    по месяцам.

Порядок вывода повторяет логику инвестора: что не так с портфелем, что
предлагается сделать, что станет после.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path

# Страницы из `pages/` исполняются с sys.path, где нет корня проекта: без этой
# строки `ui_portfolio` и `portfolio_inputs` не находятся.
ROOT = Path(__file__).resolve().parent.parent
for _path in (str(ROOT / "src"), str(ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import pandas as pd
import streamlit as st

from bond_strategy.config import load_config
from bond_strategy.ingestion.moex import IssClient, IssError
from bond_strategy.rebalance.config import load_rebalance_config
from bond_strategy.rebalance.iss import load_market
from bond_strategy.rebalance.planner import plan
from bond_strategy.rebalance.report import build_report
from bond_strategy.rebalance.runner import build_state
from ui_portfolio import render_portfolio_form

CONFIG_PATH = Path(os.environ.get("BOND_STRATEGY_CONFIG", ROOT / "config/strategy.json"))
REBALANCE_CONFIG_PATH = Path(
    os.environ.get("BOND_STRATEGY_REBALANCE_CONFIG", ROOT / "config/rebalance.json")
)
# Путь к портфелю настраивается переменной окружения: без этого тест страницы
# писал бы во временный файл, а страница читала бы рабочий, и проверка была бы
# ложной — зелёной независимо от того, что происходит с портфелем.
PORTFOLIO_PATH = Path(
    os.environ.get("BOND_STRATEGY_PORTFOLIO", ROOT / "data/portfolio.json")
)
REPORT_PATH = Path(os.environ.get("BOND_STRATEGY_REPORT", ROOT / "data/reports/rebalance.json"))
CACHE_ROOT = str(Path(os.environ.get("BOND_STRATEGY_CACHE", ROOT / "data/cache")))


def fmt_rub(value) -> str:
    if value is None:
        return "—"
    return f"{value:,.2f}".replace(",", "\u00a0").replace(".", ",") + " ₽"


def fmt_pct(value, digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{value * 100:,.{digits}f}".replace(",", "\u00a0").replace(".", ",") + "%"


def fmt_years(value) -> str:
    if value is None:
        return "—"
    return f"{value:,.2f}".replace(",", "\u00a0").replace(".", ",") + " г."


def fmt_text(value) -> str:
    return "—" if value is None or value == "" else str(value)


@st.cache_data(show_spinner=False)
def run_cached(
    as_of_iso: str,
    cash: float,
    positions: tuple,
    limit: int | None,
    page_size: int,
    workers: int,
    cache_root: str | None,
    strategy_config_path: str,
    rebalance_config_path: str,
) -> dict:
    """Полный прогон под кэш Streamlit.

    IssClient создаётся внутри: он держит открытые соединения и не хешируется.
    Кэш обогащения лежит отдельно в `cache_root` и переживает перезапуск
    приложения — поэтому повторный запуск не качает рынок заново.
    """
    as_of = date.fromisoformat(as_of_iso)
    strategy_cfg = load_config(strategy_config_path)
    rcfg = load_rebalance_config(rebalance_config_path)
    client = IssClient(raw_root=None)

    universe, load_errors = load_market(
        as_of,
        strategy_cfg,
        client,
        workers=workers,
        cache_root=cache_root,
        page_size=page_size,
        limit=limit,
    )
    by_secid = {issue.secid: issue for issue in universe}

    holdings = []
    unmatched = []
    for secid, quantity in positions:
        issue = by_secid.get(secid)
        if issue is None:
            unmatched.append(secid)
        else:
            holdings.append((issue, quantity))

    state = build_state(as_of, cash, holdings, rcfg)
    plan_result = plan(state, universe, rcfg)
    report = build_report(
        as_of=as_of,
        state=state,
        plan_result=plan_result,
        cfg=rcfg,
        after_state=plan_result.after_state or state,
    )
    payload = report.as_dict()
    payload["unmatched"] = unmatched
    payload["load_errors"] = dict(load_errors)
    payload["universe_size"] = len(universe)
    return payload


st.set_page_config(page_title="Ребалансировка портфеля", page_icon="⚖️", layout="wide")
st.title("Ребалансировка портфеля")
st.caption(
    "Алгоритм предлагает покупки на доступный остаток, не нарушая лимиты "
    "концентрации. Продажа — только при ухудшении риска. Заявки не отправляются."
)

snapshot = render_portfolio_form(
    PORTFOLIO_PATH, names=st.session_state.get("rebalance_names")
)

capital = snapshot["cash"] + sum(p["market_value"] for p in snapshot["positions"])
cols = st.columns(3)
cols[0].metric("Сумма портфеля", fmt_rub(capital))
cols[1].metric("Позиций", len(snapshot["positions"]))
cols[2].metric("Свободные деньги", fmt_rub(snapshot["cash"]))

st.divider()

with st.sidebar:
    st.header("Параметры ребалансировки")
    as_of = st.date_input("Дата расчёта", value=date.today())
    limit_raw = st.number_input(
        "Ограничить загрузку, выпусков",
        min_value=0,
        value=0,
        step=25,
        help=(
            "0 — весь рынок после отсева (около 325 выпусков на 2026 год). "
            "Полная загрузка идёт минутами; кэш позволяет добрать позже."
        ),
    )
    page_size = st.slider(
        "Размер страницы загрузки",
        min_value=5,
        max_value=50,
        value=25,
        help="Сколько выпусков обогащать за один заход. Кэш сохраняется после каждой страницы.",
    )
    workers = st.slider("Потоков", min_value=1, max_value=16, value=8)
    use_cache = st.checkbox(
        "Использовать кэш рынка",
        value=True,
        help="Повторный запуск продолжит с места остановки вместо новой загрузки.",
    )

    st.divider()
    run_clicked = st.button("Запустить ребалансировку", type="primary", width="stretch")
    st.caption(
        "Пороги — `config/rebalance.json`. Менять лимиты можно только там: "
        "правка на странице сделала бы отчёт невоспроизводимым."
    )

if run_clicked:
    if not snapshot["positions"] and snapshot["cash"] <= 0:
        st.error("Портфель пуст: укажите свободные деньги или позиции.")
        st.stop()

    try:
        with st.spinner(
            "Загрузка рынка и подбор покупок. Первый запуск на полном рынке "
            "занимает несколько минут; повторный идёт из кэша."
        ):
            payload = run_cached(
                as_of.isoformat(),
                float(snapshot["cash"]),
                tuple((p["secid"], p["quantity"]) for p in snapshot["positions"]),
                int(limit_raw) if limit_raw else None,
                page_size,
                workers,
                CACHE_ROOT if use_cache else None,
                str(CONFIG_PATH),
                str(REBALANCE_CONFIG_PATH),
            )
    except IssError as exc:
        st.error(f"MOEX ISS недоступен: {exc}")
        st.info(
            "Проверьте сеть и повторите запуск. Уже загруженные выпуски остались "
            "в кэше — повтор не начнётся с нуля."
        )
        st.stop()
    except Exception as exc:  # noqa: BLE001 — UI не должен падать трейсбеком
        st.error(f"Прогон не удался: {type(exc).__name__}: {exc}")
        st.stop()

    st.session_state["rebalance_report"] = payload
    st.session_state["rebalance_snapshot"] = json.loads(json.dumps(snapshot))
    st.session_state["rebalance_names"] = {
        p["secid"]: p.get("security_name") for p in payload["purchases"]
    }

report = st.session_state.get("rebalance_report")
if report is None:
    st.info("Укажите параметры в боковой панели и нажмите «Запустить ребалансировку».")
    st.stop()

if st.session_state.get("rebalance_snapshot") != snapshot:
    st.warning(
        "Портфель изменён после последнего расчёта. Результаты ниже относятся "
        "к предыдущему составу — запустите расчёт заново."
    )

summary = report.get("summary", {})
purchases = report.get("purchases", [])
positions = report.get("positions", [])
structure = report.get("structure", [])

st.subheader(f"Ребалансировка на {report['as_of']}")
row = st.columns(5)
row[0].metric("Рекомендаций купить", fmt_text(summary.get("purchases", 0)))
row[1].metric("Рассмотреть продажу", fmt_text(summary.get("sell_candidates", 0)))
row[2].metric("Не докупать", fmt_text(summary.get("do_not_add", 0)))
row[3].metric("Дюрация после", fmt_years((report.get("duration") or {}).get("after")))
row[4].metric("Остаток", fmt_rub((report.get("cash") or {}).get("after")))

row2 = st.columns(4)
row2[0].metric("Стоимость до", fmt_rub((report.get("value") or {}).get("before")))
row2[1].metric("Стоимость после", fmt_rub((report.get("value") or {}).get("after")))
row2[2].metric("YTM-ориентир", fmt_pct((report.get("ytm_orientation") or {}).get("after")))
row2[3].metric("config_id", report.get("config_id") or "—")

duration = report.get("duration") or {}
if duration.get("after") is not None and duration.get("coverage_after") is not None:
    st.caption(
        f"Дюрация посчитана по {fmt_pct(duration['coverage_after'], 0)} стоимости портфеля. "
        "Это чувствительность цены к ставке, а не обещание предельной просадки."
    )

st.divider()
st.subheader("Рекомендации")

if purchases:
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "SECID": p.get("secid"),
                    "Наименование": fmt_text(p.get("security_name")),
                    "Лотов": p.get("lots"),
                    "Бумаг": p.get("quantity"),
                    "Сумма": fmt_rub(p.get("amount")),
                    "Рейтинг": fmt_text(p.get("rating")),
                    "Срок": fmt_years(p.get("years_to_maturity")),
                    "YTM": fmt_pct(p.get("ytm")),
                    "Доля после": fmt_pct(p.get("share_after")),
                    "Причина": "; ".join(p.get("reasons") or []) or "—",
                }
                for p in purchases
            ]
        ),
        width="stretch",
        hide_index=True,
    )
    spent = sum(p.get("amount") or 0 for p in purchases)
    st.caption(
        f"Итого предлагается купить на {fmt_rub(spent)} "
        f"из {fmt_rub((report.get('cash') or {}).get('before'))} доступных."
    )
else:
    st.info(report.get("stop_reason") or "Покупки не предложены.")

if report.get("stop_reason") and purchases:
    st.caption(f"Остановка: {report['stop_reason']}")

if report.get("diagnostics"):
    with st.expander("Почему часть выпусков не рассматривалась", expanded=False):
        for note in report["diagnostics"][:50]:
            st.caption(note)

if report.get("rejected"):
    with st.expander(f"Отклонено выпусков: {len(report['rejected'])}", expanded=False):
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "SECID": r.get("secid"),
                        "Наименование": fmt_text(r.get("security_name")),
                        "Причина": r.get("reason"),
                        "Коды": ", ".join(r.get("codes") or []),
                    }
                    for r in report["rejected"][:200]
                ]
            ),
            width="stretch",
            hide_index=True,
        )
        st.caption(
            "Отказ по разделу 3: бумага не соответствует условиям покупки. "
            "Причины сгруппированы по коду — по ним видно, чего не хватает в данных."
        )

if report.get("unmatched"):
    st.warning(
        "Не найдены на рынке и не оценены: "
        + ", ".join(report["unmatched"])
        + ". Проверьте SECID вручную: позиции не потеряны, но в расчёте не участвуют."
    )

st.divider()
st.subheader("Действующие позиции")

if positions:
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "SECID": p.get("secid"),
                    "Наименование": fmt_text(p.get("security_name")),
                    "Статус": p.get("status_label"),
                    "Доля": fmt_pct(p.get("share")),
                    "Рейтинг": fmt_text(p.get("rating")),
                    "Срок": fmt_years(p.get("years_to_maturity")),
                    "YTM": fmt_pct(p.get("ytm")),
                    "Можно докупать": "да" if p.get("can_add") else "нет",
                }
                for p in positions
            ]
        ),
        width="stretch",
        hide_index=True,
    )

    with_notes = [p for p in positions if p.get("limit_notes")]
    if with_notes:
        with st.expander("Исчерпанные лимиты по позициям", expanded=False):
            for p in with_notes:
                st.markdown(f"**{p.get('secid')}** — {p.get('status_label')}")
                for note in p["limit_notes"]:
                    st.caption(f"• {note}")

    with_reasons = [p for p in positions if p.get("reasons")]
    if with_reasons:
        with st.expander("Причины статусов", expanded=False):
            for p in with_reasons:
                st.markdown(f"**{p.get('secid')}** — {p.get('status_label')}")
                for reason in p["reasons"]:
                    st.caption(f"• {reason}")

    if report.get("breaches"):
        st.warning(
            "В портфеле уже есть превышения лимитов. Они не блокируют покупки "
            "бумаг других отраслей: правило — не ухудшать превышение, а не "
            "запрещать любые сделки."
        )
        for item in report["breaches"].values():
            st.caption("• " + (item.get("text") if isinstance(item, dict) else str(item)))
else:
    st.caption("Позиций нет: портфель состоит только из денег.")

st.divider()
st.subheader("Структура: до и после")

SLICE_TITLES = {
    "issue": "Выпуски",
    "issuer": "Эмитенты",
    "group": "Группы компаний",
    "industry": "Отрасли",
    "band": "Сроки погашения",
    "low_rating": "Категория BBB/BBB+",
    "over_horizon": "Срок свыше горизонта",
}

for block in structure:
    rows = block.get("rows") or []
    if not rows:
        continue
    limit = block.get("limit")
    header = SLICE_TITLES.get(block.get("code"), block.get("name"))
    if limit:
        header += f" — лимит {fmt_pct(limit, 0)}"
    with st.expander(header, expanded=block.get("code") in {"industry", "band"}):
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Позиция": fmt_text(r.get("label")),
                        "До": fmt_pct(r.get("before")),
                        "После": fmt_pct(r.get("after")),
                        "Лимит": fmt_pct(r.get("limit"), 0) if r.get("limit") else "—",
                        "Сверх лимита": "да" if r.get("over_limit") else "—",
                    }
                    for r in rows
                ]
            ),
            width="stretch",
            hide_index=True,
        )

flows_before = (report.get("flows") or {}).get("before") or {}
flows_after = (report.get("flows") or {}).get("after") or {}
if flows_before or flows_after:
    st.divider()
    st.subheader("Ожидаемые купоны и погашения по месяцам")
    months = sorted(set(flows_before) | set(flows_after))
    st.bar_chart(
        pd.DataFrame(
            {
                "Месяц": months,
                "До": [flows_before.get(m, 0.0) for m in months],
                "После": [flows_after.get(m, 0.0) for m in months],
            }
        ).set_index("Месяц")
    )
    st.caption(
        "Прогноз строится по условиям выпусков. Фактические выплаты зависят от "
        "исполнения обязательств эмитентами."
    )

notes = report.get("notes") or []
if notes:
    st.divider()
    st.subheader("Что важно знать")
    for note in notes:
        st.markdown(f"- {note}")

st.divider()
st.subheader("Экспорт")

try:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload_text = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    REPORT_PATH.write_text(payload_text, encoding="utf-8")
    st.download_button(
        "Скачать JSON",
        payload_text,
        file_name=f"rebalance_{report['as_of']}.json",
        mime="application/json",
    )
    st.caption(f"Файл сформирован: `{REPORT_PATH}`")
except OSError as exc:
    st.error(f"Не удалось подготовить файл отчёта: {exc}")
