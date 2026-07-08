#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Append/replace commodity indicators in data.json for Daily Market Brief.

Adds category: 원자재
Adds indicators: 금, 은, 구리
Signal metric: 50-day disparity = close / MA50 * 100
Thresholds: each indicator's own 5-year disparity distribution p70 / p90 / p97
Chart shown on dashboard: raw closing price (not the disparity series)

Usage:
  python scripts/build_macro_commodities.py
  python scripts/build_macro_commodities.py --input data.json --output data.json

Dependencies:
  pandas, yfinance
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import yfinance as yf

try:
    import FinanceDataReader as fdr
except Exception:  # noqa: BLE001
    fdr = None

KST = dt.timezone(dt.timedelta(hours=9))


@dataclass(frozen=True)
class CommoditySpec:
    name: str
    ticker: str
    fallback_ticker: str
    unit: str
    source_label: str
    comment_prefix: str


COMMODITIES: List[CommoditySpec] = [
    CommoditySpec(
        name="금 50일 이격도",
        ticker="GC=F",
        fallback_ticker="GLD",
        unit="달러/온스",
        source_label="yfinance:GC=F",
        comment_prefix="금 가격은 실질금리, 달러, 지정학 리스크, 안전자산 선호를 함께 반영합니다.",
    ),
    CommoditySpec(
        name="은 50일 이격도",
        ticker="SI=F",
        fallback_ticker="SLV",
        unit="달러/온스",
        source_label="yfinance:SI=F",
        comment_prefix="은 가격은 귀금속 성격과 산업재 성격을 함께 가지며, 경기·유동성·달러 방향에 민감합니다.",
    ),
    CommoditySpec(
        name="구리 50일 이격도",
        ticker="HG=F",
        fallback_ticker="CPER",
        unit="달러/파운드",
        source_label="yfinance:HG=F",
        comment_prefix="구리 가격은 제조업·전력망·중국 수요를 보는 대표 경기민감 원자재 지표입니다.",
    ),
]


def kst_now_str() -> str:
    return dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")


def today_kst() -> dt.date:
    return dt.datetime.now(KST).date()


def fmt_signed_pct(x: Optional[float], digits: int = 2) -> str:
    if x is None or not math.isfinite(x):
        return "—"
    return f"{x:+.{digits}f}%"


def fmt_signed_point(x: Optional[float], digits: int = 1) -> str:
    if x is None or not math.isfinite(x):
        return "—"
    return f"{x:+.{digits}f}p"


def fmt_price(x: float, unit: str) -> str:
    if "파운드" in unit:
        return f"${x:,.2f}"
    return f"${x:,.1f}"


def pct_change(curr: Optional[float], prev: Optional[float]) -> Optional[float]:
    if curr is None or prev in (None, 0) or not math.isfinite(curr) or not math.isfinite(prev):
        return None
    return (curr / prev - 1.0) * 100


def load_close_series(ticker: str, fallback_ticker: str, years: int = 5) -> Tuple[pd.Series, str]:
    end = today_kst() + dt.timedelta(days=1)
    start = end - dt.timedelta(days=int(365.25 * years) + 120)

    def _via_fdr(tk: str) -> Optional[pd.Series]:
        # FinanceDataReader는 requests 기반이라 클라우드 프록시를 통과한다
        # (yfinance/curl_cffi가 막히는 환경 대응). GC=F/SI=F/HG=F 등 동일 심볼 지원.
        if fdr is None:
            return None
        df = fdr.DataReader(tk, start.isoformat(), end.isoformat())
        if df is None or df.empty or "Close" not in df.columns:
            return None
        return df["Close"].dropna().astype(float)

    def _via_yf(tk: str) -> Optional[pd.Series]:
        df = yf.download(
            tk,
            start=start.isoformat(),
            end=end.isoformat(),
            auto_adjust=True,
            progress=False,
            threads=False,
        )
        if df is None or df.empty:
            return None
        close = df["Close"].iloc[:, 0] if isinstance(df.columns, pd.MultiIndex) else df["Close"]
        return close.dropna().astype(float)

    for tk in (ticker, fallback_ticker):
        for loader in (_via_fdr, _via_yf):
            try:
                close = loader(tk)
                if close is not None and len(close) >= 260:
                    return close, tk
            except Exception:
                continue
    raise RuntimeError(f"No usable price data for {ticker} or {fallback_ticker}")


def percentile(series: pd.Series, q: float) -> float:
    return float(series.quantile(q))


def classify_disparity(disparity: float, strong: float, overheat: float, extreme: float) -> Tuple[str, int]:
    if disparity >= extreme:
        return "극단 과열", 3
    if disparity >= overheat:
        return "과열", 2
    if disparity >= strong:
        return "강세", 1
    if disparity >= 100:
        return "50일선 상회", 0
    return "50일선 하회", 1


def mdd_line(close: pd.Series) -> str:
    one_year = close.tail(260)
    if one_year.empty:
        return ""
    peak_val = float(one_year.max())
    peak_date = one_year.idxmax()
    last = float(one_year.iloc[-1])
    mdd = (last / peak_val - 1.0) * 100 if peak_val else None
    date_txt = peak_date.strftime("%m-%d") if hasattr(peak_date, "strftime") else str(peak_date)
    return f"최근 1년 고점대비 {fmt_signed_pct(mdd, 1)} (고점 {peak_val:,.2f}, {date_txt})"


def make_card(spec: CommoditySpec) -> Dict[str, Any]:
    close, used_ticker = load_close_series(spec.ticker, spec.fallback_ticker)
    ma50 = close.rolling(50).mean()
    disparity = (close / ma50 * 100).dropna()
    if disparity.empty:
        raise RuntimeError(f"Not enough MA50 data for {spec.name}")

    # Use the most recent five years of valid 50-day disparity observations.
    hist = disparity.tail(252 * 5)
    strong = round(percentile(hist, 0.70), 1)
    overheat = round(percentile(hist, 0.90), 1)
    extreme = round(percentile(hist, 0.97), 1)

    last_close = float(close.iloc[-1])
    prev_close = float(close.iloc[-2]) if len(close) >= 2 else None
    last_disp = float(disparity.iloc[-1])
    prev_disp = float(disparity.iloc[-2]) if len(disparity) >= 2 else None
    state, signal_level = classify_disparity(last_disp, strong, overheat, extreme)

    price_labels = [idx.strftime("%Y-%m-%d") for idx in close.tail(260).index]
    price_tail = [round(float(x), 4) for x in close.tail(260).values]

    source = spec.source_label if used_ticker == spec.ticker else f"yfinance:{used_ticker} fallback"
    date_txt = close.index[-1].strftime("%Y-%m-%d")
    one_day = pct_change(last_close, prev_close)
    disp_delta = None if prev_disp is None else last_disp - prev_disp

    return {
        "name": spec.name,
        "category": "원자재",
        "value_text": f"{fmt_price(last_close, spec.unit)} · 이격도 {last_disp:.1f}",
        "date": date_txt,
        "change_text": f"{fmt_signed_pct(one_day)} vs 전일 · 이격도 {fmt_signed_point(disp_delta)}",
        "state": state,
        "signal_level": signal_level,
        "comment": (
            f"{spec.comment_prefix} 50일 이격도 {last_disp:.1f}. "
            f"임계값(강세 {strong} / 과열 {overheat} / 극단 {extreme})은 최근 5년 이격도 분포의 "
            "70·90·97퍼센타일로 지표별 산출한 값입니다."
        ),
        "mdd_line": mdd_line(close),
        "source": source,
        "ok": True,
        "chart": {
            "type": "value",
            "unit": spec.unit,
            "labels": price_labels,
            "series": {"value": price_tail},
        },
    }


def failure_card(spec: CommoditySpec, reason: str) -> Dict[str, Any]:
    return {
        "name": spec.name,
        "category": "원자재",
        "value_text": "수집 실패",
        "date": today_kst().isoformat(),
        "change_text": "",
        "state": "오류",
        "signal_level": 3,
        "comment": str(reason)[:240],
        "mdd_line": "",
        "source": spec.source_label,
        "ok": False,
        "chart": None,
    }


def update_data_json(input_path: Path, output_path: Path) -> None:
    data = json.loads(input_path.read_text(encoding="utf-8"))
    indicators = data.get("indicators", [])
    keep = [x for x in indicators if x.get("category") != "원자재"]

    new_cards = []
    for spec in COMMODITIES:
        try:
            new_cards.append(make_card(spec))
        except Exception as exc:
            new_cards.append(failure_card(spec, str(exc)))

    # Keep existing macro categories in their original order and append commodities near the end.
    data["indicators"] = keep + new_cards
    note = data.get("note", "")
    add_note = "원자재 3종(금·은·구리)은 50일 이격도와 최근 5년 지표별 분위수 임계값으로 판단합니다."
    if add_note not in note:
        data["note"] = (note + " " + add_note).strip()

    output_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {output_path} with {len(new_cards)} commodity cards at {kst_now_str()}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="data.json", help="Existing macro data.json path")
    ap.add_argument("--output", default="data.json", help="Output data.json path")
    args = ap.parse_args()
    update_data_json(Path(args.input), Path(args.output))


if __name__ == "__main__":
    main()