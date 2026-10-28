"""Streamlit-витрина над аналитическим ядром bond_strategy.

Приложение только читает и считает: заявки на биржу не отправляются,
config/strategy.json на диске не изменяется.

Запуск:
    PYTHONPATH=src streamlit run app.py
"""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import date
from pathlib import Path

import streamlit as st
import pandas as pd

from portfolio_inputs import load_portfolio, save_portfolio, validate_portfolio

from bond_strategy.config import StrategyConfig, load_config
from bond_strategy.ingestion.moex import IssClient, IssError
from bond_strategy.pipeline import export, run_pipeline

CONFIG_PATH = Path("config/strategy.json")
REPORT_PATH = Path("data/reports/report.json")
PORTFOLIO_PATH = Path("data/portfolio.json")

MIN_CAPITAL = 1_000
DEFAULT_CAPITAL = 50_000

REJECT_LABELS = {
    "rejected_list_level": "Уровень листинга",
    "rejected_maturity": "Срок до погашения",
    "rejected_coupon_type": "Тип купона",
    "rejected_currency": "Валюта",
    "rejected_offer": "Оферта",
    "rejected_no_price": "Нет цены",
    "build_failed": "Не удалось разобрать",
    "errors": "Ошибки загрузки",
}

SIGNAL_ORDER = ["BUY", "WATCH", "REVIEW", "SELL_REVIEW", "DATA_BLOCKED"]


# --------------------------------------------------------------------------
# Масштабирование капитала
# --------------------------------------------------------------------------

def scale_config(cfg: StrategyConfig, amount: float) -> StrategyConfig:
    """Вернуть копию конфига с капиталом `amount` и пропорциональными порогами.

    reserve_base и min_position заданы в конфиге абсолютными числами (20% и 16%
    от initial). Если подменить только capital.initial, эти доли схлопнутся:
    при капитале 500 000 резерв превратился бы в 2%, а минимальная позиция
    в 1,6%, и лимиты начали бы пропускать заведомо мелкие сделки.
    Поэтому оба абсолютных порога масштабируются тем же коэффициентом.

    config_id пересчитывается по той же канонизации, что и в load_config,
    иначе хеш перестал бы соответствовать фактическим параметрам прогона.
    """
    if amount <= 0:
        raise ValueError("Сумма портфеля должна быть больше нуля")
    if amount < MIN_CAPITAL:
        raise ValueError(f"Минимальная сумма портфеля — {MIN_CAPITAL:,} ₽".replace(",", " "))

    raw = copy.deepcopy(cfg.raw)
    base = float(raw["capital"]["initial"])
    if base <= 0:
        raise ValueError("В конфиге некорректный capital.initial — масштабирование невозможно")

    # Когда сумма совпадает с базовой, возвращаем конфиг как есть: масштабирование
    # с коэффициентом 1.0 превратило бы 10000 в 10000.0 и дало бы другой хеш для
    # того же набора параметров. config_id обязан совпадать с load_config.
    if amount == base:
        return cfg

    factor = amount / base
    raw["capital"]["initial"] = float(amount)
    raw["capital"]["reserve_base"] = float(raw["capital"]["reserve_base"]) * factor
    raw["filters"]["min_position"] = float(raw["filters"]["min_position"]) * factor

    canonical = json.dumps(raw, sort_keys=True, ensure_ascii=False)
    config_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    return StrategyConfig(raw=raw, config_id=config_id)


# --------------------------------------------------------------------------
# Прогон
# --------------------------------------------------------------------------

@st.cache_data(show_spinner=False)
def run_cached(as_of_iso: str, capital: float, enrich_limit: int | None, workers: int, cash: float, positions: tuple):
    """Полный прогон под кэш. IssClient создаётся внутри — он не хешируется."""
    cfg = scale_config(load_config(CONFIG_PATH), capital)
    client = IssClient(raw_root="data/raw")
    return run_pipeline(
        date.fromisoformat(as_of_iso),
        cfg,
        client,
        top=None,
        workers=workers,
        enrich_limit=enrich_limit,
        cash=cash,
        holdings=[{"secid": s, "quantity": q, "market_value": v} for s, q, v in positions],
    )


# --------------------------------------------------------------------------
# Форматирование (русская локаль: пробел в тысячах, запятая в дробях)
# --------------------------------------------------------------------------

def fmt_rub(value) -> str:
    if value is None:
        return "—"
    return f"{value:,.0f}".replace(",", "\u00a0") + " ₽"


def fmt_num(value, digits: int = 0) -> str:
    if value is None:
        return "—"
    return f"{value:,.{digits}f}".replace(",", "\u00a0").replace(".", ",")


def fmt_pct(value, digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{value * 100:,.{digits}f}".replace(",", "\u00a0").replace(".", ",") + "%"


def fmt_text(value) -> str:
    return "—" if value is None or value == "" else str(value)


# --------------------------------------------------------------------------
# Экран 1 — параметры прогона
# --------------------------------------------------------------------------

st.set_page_config(page_title="bond_strategy", page_icon="📊", layout="wide")

if "portfolio_snapshot" not in st.session_state:
    try:
        st.session_state["portfolio_snapshot"] = load_portfolio(PORTFOLIO_PATH)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        st.error(f"Не удалось прочитать сохранённый портфель: {exc}. Файл не изменён.")
        st.stop()

st.title("bond_strategy")
st.subheader("Купленные позиции")
st.caption("Введите SECID с борда TQCB, количество бумаг и текущую стоимость всей позиции в рублях с НКД. Цены нужно обновлять вручную. Сохранение — только на этом компьютере.")
with st.form("portfolio_form"):
    saved = st.session_state["portfolio_snapshot"]
    free_cash = st.number_input("Свободные деньги, ₽", min_value=0.0, value=float(saved["cash"]), step=1000.0)
    frame = pd.DataFrame(saved["positions"], columns=["secid", "quantity", "market_value"])
    editor = st.data_editor(
        frame, num_rows="dynamic", hide_index=True, width="stretch",
        column_config={
            "secid": st.column_config.TextColumn("SECID / ISIN", required=True),
            "quantity": st.column_config.NumberColumn("Количество, шт.", min_value=1, step=1, required=True),
            "market_value": st.column_config.NumberColumn("Стоимость позиции с НКД, ₽", min_value=0.01, step=100.0, required=True),
        },
    )
    saved_clicked = st.form_submit_button("Сохранить портфель")
if saved_clicked:
    try:
        snapshot = validate_portfolio(free_cash, editor.to_dict("records"))
        save_portfolio(PORTFOLIO_PATH, snapshot)
        st.session_state["portfolio_snapshot"] = snapshot
        st.success("Портфель сохранён. Запустите расчёт, чтобы учесть изменения.")
    except (ValueError, OSError) as exc:
        st.error(f"Портфель не сохранён: {exc}")

snapshot = st.session_state["portfolio_snapshot"]
capital = snapshot["cash"] + sum(p["market_value"] for p in snapshot["positions"])
st.metric("Сумма портфеля: облигации с НКД + свободные деньги", fmt_rub(capital))
st.caption("Изменения в форме применяются только после нажатия «Сохранить портфель». Расчёт использует сохранённые данные.")
if snapshot["positions"]:
    st.warning("Стоимость позиций введена вручную и не обновляется автоматически. Проверьте её на дату расчёта. Без рейтинга используется среднее качество. Если отрасль не определена, отраслевой лимит может быть неполным — проверьте его вручную.")

with st.sidebar:
    st.header("Параметры прогона")

    st.metric("Сумма портфеля, ₽", fmt_rub(capital))
    st.caption("Облигации с НКД + свободные деньги; ввод — в форме портфеля.")

    as_of = st.date_input("Дата расчёта", value=date.today())

    enrich_raw = st.number_input(
        "Лимит обогащения",
        min_value=0,
        value=None,
        step=10,
        placeholder="все",
        help="0 или пусто — весь юниверсум",
    )

    workers = st.slider("Потоков", min_value=1, max_value=16, value=6)

    st.divider()
    run_clicked = st.button("Запустить", type="primary", width="stretch")

    st.divider()
    st.warning(
        "Полный прогон занимает **несколько минут**: обогащение — около "
        "секунды на выпуск. Нужна сеть до MOEX ISS. Повторный запуск быстрее "
        "за счёт кэша ISS в `data/raw`."
    )
    st.caption(
        "Приложение только читает и считает — заявки на биржу не отправляются."
    )


# 0 обнулил бы юниверсум: pipeline делает candidates[:enrich_limit] без проверки на ноль.
enrich_limit: int | None = int(enrich_raw) if enrich_raw else None

if run_clicked:
    try:
        cfg_preview = scale_config(load_config(CONFIG_PATH), float(capital))
    except ValueError as exc:
        st.error(f"Проверьте сумму портфеля: {exc}")
        st.stop()

    try:
        with st.spinner("Идёт прогон: борда → отсев → обогащение → ранжирование…"):
            result = run_cached(as_of.isoformat(), float(capital), enrich_limit, workers,
                                snapshot["cash"], tuple((p["secid"], p["quantity"], p["market_value"]) for p in snapshot["positions"]))
    except IssError as exc:
        st.error(f"MOEX ISS недоступен: {exc}")
        st.info("Проверьте сеть и повторите запуск. Уже загруженные данные остались в кэше.")
        st.stop()
    except ValueError as exc:
        st.error(f"Проверьте данные портфеля или параметры расчёта: {exc}")
        st.stop()
    except Exception as exc:  # noqa: BLE001 — UI не должен падать трейсбеком
        st.error(f"Прогон не удался: {type(exc).__name__}: {exc}")
        st.stop()

    st.session_state["result"] = result
    st.session_state["capital"] = float(capital)
    st.session_state["cfg"] = cfg_preview
    st.session_state["run_snapshot"] = copy.deepcopy(snapshot)


result = st.session_state.get("result")

st.caption("Кандидаты в портфель корпоративных облигаций по данным MOEX ISS")

if result is None:
    st.info("Задайте параметры в боковой панели и нажмите «Запустить».")
    st.stop()

if st.session_state.get("run_snapshot") != snapshot:
    st.warning("Портфель изменён после последнего расчёта. Результаты ниже относятся к предыдущему составу — запустите расчёт заново.")
used_capital = st.session_state.get("capital", float(capital))
signals = result.signals or []
summary = result.summary or {}
universe = summary.get("universe", {}) or {}
counts = summary.get("counts", {}) or {}

# --------------------------------------------------------------------------
# Экран 2 — сводка
# --------------------------------------------------------------------------

st.subheader(f"Сводка на {result.as_of}")

row1 = st.columns(5)
row1[0].metric("Выпусков на борде", fmt_num(universe.get("board_rows")))
row1[1].metric("Прошло отсев", fmt_num(universe.get("passed_screen")))
row1[2].metric("Обогащено", fmt_num(universe.get("enriched")))
row1[3].metric("Точек кривой ОФЗ", fmt_num(summary.get("ofz_points")))
row1[4].metric("config_id", result.config_id or "—")

row2 = st.columns(4)
row2[0].metric("BUY", fmt_num(counts.get("BUY", 0)))
row2[1].metric("WATCH", fmt_num(counts.get("WATCH", 0)))
row2[2].metric("DATA_BLOCKED", fmt_num(counts.get("DATA_BLOCKED", 0)))
row2[3].metric("Ошибок загрузки", fmt_num(summary.get("errors", 0)))

st.caption(
    f"Капитал прогона: **{fmt_rub(used_capital)}** · "
    f"ранжировано {fmt_num(summary.get('ranked_total'))}, "
    f"показано {fmt_num(summary.get('reported'))}"
)
if result.config_id:
    st.caption(
        "config_id — хеш параметров прогона. Меняется при смене суммы портфеля; "
        "укажите его вместе с датой, чтобы результат воспроизводился."
    )

with st.expander("Воронка отсева", expanded=False):
    funnel = {
        REJECT_LABELS[key]: value
        for key, value in universe.items()
        if key in REJECT_LABELS and value
    }
    if funnel:
        ordered = dict(sorted(funnel.items(), key=lambda kv: kv[1], reverse=True))
        st.bar_chart(ordered, horizontal=True)
        st.dataframe(
            [{"Причина": k, "Выпусков": fmt_num(v)} for k, v in ordered.items()],
            width="stretch",
            hide_index=True,
        )
    else:
        st.caption("Отсевов не зафиксировано.")

if result.errors:
    with st.expander(f"Ошибки прогона ({len(result.errors)})", expanded=False):
        for err in result.errors[:100]:
            st.text(err)
        if len(result.errors) > 100:
            st.caption(f"…и ещё {len(result.errors) - 100}")

if not signals:
    st.warning(
        "Прогон завершён, но сигналов нет. Возможные причины: слишком строгие "
        "фильтры, неполные данные по выпускам или лимит обогащения отсёк всё. "
        "Попробуйте увеличить лимит обогащения или снизить минимальный балл."
    )
    st.stop()

# --------------------------------------------------------------------------
# Ограничение ядра: сигналы не применяются последовательно
# --------------------------------------------------------------------------

buy_signals = [s for s in signals if s.get("signal") == "BUY"]
buy_total = sum(float(s.get("suggested_amount") or 0) for s in buy_signals)

st.divider()
st.subheader("Кандидаты")
st.caption(
    "Размер позиции рассчитан с учётом сохранённого портфеля. Одновременно исполнить все "
    "сигналы нельзя — конвейер не применяет сделки последовательно."
)

if buy_signals:
    overhang = buy_total / used_capital if used_capital else 0
    cols = st.columns(3)
    cols[0].metric("Сигналов BUY", fmt_num(len(buy_signals)))
    cols[1].metric("Сумма suggested_amount", fmt_rub(buy_total))
    cols[2].metric("К капиталу", f"×{fmt_num(overhang, 1)}")
    st.error(
        f"Сумма всех предложенных позиций — {fmt_rub(buy_total)} при капитале "
        f"{fmt_rub(used_capital)} (отношение ×{fmt_num(overhang, 1)}). "
        "`suggested_amount` рассчитан для каждого выпуска отдельно от текущего портфеля, "
        "а не исполнимая заявка: ядро оценивает каждый выпуск против одного и "
        "того же портфеля и не расходует кэш и лимиты между выпусками. "
        "«Итого к покупке» здесь не выводится — это была бы неверная цифра."
    )

# --------------------------------------------------------------------------
# Экран 3 — таблица кандидатов
# --------------------------------------------------------------------------

filters = st.columns(3)

all_signals = sorted(
    {s.get("signal") for s in signals if s.get("signal")},
    key=lambda s: SIGNAL_ORDER.index(s) if s in SIGNAL_ORDER else len(SIGNAL_ORDER),
)
selected_signals = filters[0].multiselect(
    "Сигнал", all_signals, default=["BUY"] if "BUY" in all_signals else all_signals[:1]
)

all_ratings = sorted({s.get("rating") for s in signals if s.get("rating")})
selected_ratings = filters[1].multiselect("Рейтинг", all_ratings, default=[])

min_score = filters[2].slider("Минимальный балл", 0, 100, 0)

view = [
    s
    for s in signals
    if (not selected_signals or s.get("signal") in selected_signals)
    and (not selected_ratings or s.get("rating") in selected_ratings)
    and float(s.get("score") or 0) >= min_score
]
view.sort(key=lambda s: -float(s.get("score") or 0))

if not view:
    st.info("Под выбранные фильтры не попал ни один выпуск. Ослабьте условия.")
    st.stop()

table = [
    {
        "№": s.get("rank"),
        "secid": fmt_text(s.get("secid")),
        "Эмитент": fmt_text(s.get("issuer")),
        "Рейтинг": fmt_text(s.get("rating")),
        "Агентство": fmt_text(s.get("rating_agency")),
        "Балл": round(float(s.get("score") or 0), 1),
        "Сигнал": fmt_text(s.get("signal")),
        "Сумма": fmt_rub(s.get("suggested_amount")),
        "Оборот/день": fmt_rub(s.get("avg_turnover")),
        "Спред": fmt_pct(s.get("avg_spread")),
        "Погашение": fmt_text(s.get("maturity")),
        "Цена": fmt_num(s.get("clean_price"), 2),
        "ОФЗ YTM": fmt_pct(s.get("ofz_ytm")),
        "Обоснование": " | ".join(s.get("reasons") or []) or "—",
        "Не пройдены проверки": " | ".join(s.get("failed") or []) or "—",
    }
    for s in view
]

st.dataframe(
    table,
    width="stretch",
    hide_index=True,
    column_config={"Балл": st.column_config.NumberColumn(format="%.1f")},
)

expander_cap = len(view)
st.caption(
    f"Показано {len(view)} из {len(signals)}. "
    f"Разбор причин — для первых {min(expander_cap, len(view))} строк."
)

for s in view[:expander_cap]:
    header = (
        f"{fmt_text(s.get('secid'))} · {fmt_text(s.get('issuer'))} · "
        f"балл {fmt_num(s.get('score'), 1)} · {fmt_text(s.get('signal'))}"
    )
    with st.expander(header):
        left, right = st.columns(2)
        with left:
            st.markdown("**Обоснование**")
            reasons = s.get("reasons") or []
            if reasons:
                for reason in reasons:
                    st.markdown(f"- {reason}")
            else:
                st.caption("Пусто")
        with right:
            st.markdown("**Проверки, которые не прошли**")
            failed = s.get("failed") or []
            if failed:
                for item in failed:
                    st.code(item, language=None)
            else:
                st.caption("Все проверки пройдены")

# --------------------------------------------------------------------------
# Экран 4 — экспорт
# --------------------------------------------------------------------------

st.divider()
st.subheader("Экспорт")

try:
    path = export(result, str(REPORT_PATH))
    payload = path.read_text(encoding="utf-8")
    st.download_button(
        "Скачать JSON",
        payload,
        file_name=f"report_{result.as_of}.json",
        mime="application/json",
    )
    st.caption(f"Файл сформирован: `{path}`")
except OSError as exc:
    st.error(f"Не удалось подготовить файл отчёта: {exc}")
