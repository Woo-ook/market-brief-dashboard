#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Build sector.json with 5/20/60/120-day moving-average status cards.

This replaces the old sector stock card logic based on 50-day disparity.
The frontend must support chart.type == "multi_ma". The patched app.js does.

Usage:
  python scripts/build_sector_ma.py
  python scripts/build_sector_ma.py --config config/sector_price_universe.json --output sector.json

Dependencies:
  pandas, yfinance, FinanceDataReader
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
except Exception:  # pragma: no cover
    fdr = None

KST = dt.timezone(dt.timedelta(hours=9))
MA_WINDOWS = [5, 20, 60, 120]


DEFAULT_UNIVERSE = {
    "note": "산업별 주가 모니터링 대상. source는 FDR 또는 yfinance.",
    "indicators": [
        {"category": "반도체", "name": "삼성전자", "source": "FDR", "ticker": "005930", "currency": "KRW"},
        {"category": "반도체", "name": "SK하이닉스", "source": "FDR", "ticker": "000660", "currency": "KRW"},
        {"category": "반도체", "name": "마이크론 (MU)", "source": "yfinance", "ticker": "MU", "currency": "USD"},
        {"category": "반도체", "name": "샌디스크 (SNDK)", "source": "yfinance", "ticker": "SNDK", "currency": "USD"},
        {"category": "조선", "name": "HD한국조선해양", "source": "FDR", "ticker": "009540", "currency": "KRW"},
        {"category": "조선", "name": "HD현대중공업", "source": "FDR", "ticker": "329180", "currency": "KRW"},
        {"category": "조선", "name": "삼성중공업", "source": "FDR", "ticker": "010140", "currency": "KRW"},
        {"category": "조선", "name": "한화오션", "source": "FDR", "ticker": "042660", "currency": "KRW"},
        {"category": "소프트웨어", "name": "알파벳 (GOOGL)", "source": "yfinance", "ticker": "GOOGL", "currency": "USD"},
        {"category": "소프트웨어", "name": "마이크로소프트 (MSFT)", "source": "yfinance", "ticker": "MSFT", "currency": "USD"},
        {"category": "소프트웨어", "name": "메타 (META)", "source": "yfinance", "ticker": "META", "currency": "USD"},
        {"category": "소프트웨어", "name": "오라클 (ORCL)", "source": "yfinance", "ticker": "ORCL", "currency": "USD"},
        {"category": "소프트웨어", "name": "팔란티어 (PLTR)", "source": "yfinance", "ticker": "PLTR", "currency": "USD"},
        {"category": "소프트웨어", "name": "NAVER", "source": "FDR", "ticker": "035420", "currency": "KRW"},
        {"category": "소프트웨어", "name": "삼성에스디에스", "source": "FDR", "ticker": "018260", "currency": "KRW"},
        {"category": "소프트웨어", "name": "LG CNS", "source": "FDR", "ticker": "064400", "currency": "KRW"},
    ],
}


def kst_now_str() -> str:
    return dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")


def today_kst() -> dt.date:
    return dt.datetime.now(KST).date()


def fmt_price(x: float, currency: str) -> str:
    if currency.upper() == "KRW":
        return f"{x:,.0f}원"
    return f"${x:,.2f}"


def fmt_pct(x: Optional[float], digits: int = 2) -> str:
    if x is None or not math.isfinite(x):
        return "—"
    return f"{x:+.{digits}f}%"


def pct_change(curr: Optional[float], prev: Optional[float]) -> Optional[float]:
    if curr is None or prev in (None, 0) or not math.isfinite(curr) or not math.isfinite(prev):
        return None
    return (curr / prev - 1.0) * 100


def source_label(source: str, ticker: str) -> str:
    return f"{source}:{ticker}"


def load_close(spec: Dict[str, Any], lookback_days: int = 900) -> pd.Series:
    source = str(spec["source"]).lower()
    ticker = spec["ticker"]
    end = today_kst() + dt.timedelta(days=1)
    start = end - dt.timedelta(days=lookback_days)

    if source == "fdr":
        if fdr is None:
            raise RuntimeError("FinanceDataReader is not installed")
        df = fdr.DataReader(ticker, start.isoformat(), end.isoformat())
        if df is None or df.empty or "Close" not in df.columns:
            raise RuntimeError(f"No FDR Close data for {ticker}")
        return df["Close"].dropna().astype(float)

    if source == "yfinance":
        df = yf.download(
            ticker,
            start=start.isoformat(),
            end=end.isoformat(),
            auto_adjust=True,
            progress=False,
            threads=False,
        )
        if df is None or df.empty:
            raise RuntimeError(f"No yfinance data for {ticker}")
        if isinstance(df.columns, pd.MultiIndex):
            close = df["Close"].iloc[:, 0]
        else:
            close = df["Close"]
        return close.dropna().astype(float)

    raise ValueError(f"Unsupported source: {spec['source']}")


def moving_averages(close: pd.Series) -> Dict[int, pd.Series]:
    return {w: close.rolling(w).mean() for w in MA_WINDOWS}


def classify(close: float, ma_vals: Dict[int, Optional[float]]) -> Tuple[str, int, List[str]]:
    above = {w: (ma_vals.get(w) is not None and math.isfinite(ma_vals[w]) and close >= ma_vals[w]) for w in MA_WINDOWS}

    signals = []
    if above[5]:
        signals.append("5일선 위: 단기 상승 가능성")
    else:
        signals.append("5일선 아래: 초단기 탄력 약화")

    if above[20]:
        signals.append("20일선 위: 단기 강세")
    else:
        signals.append("20일선 아래: 단기 조정")

    if above[60]:
        signals.append("60일선 위: 중기 추세 유지")
    else:
        signals.append("60일선 아래: 중기 추세 약화")

    if above[120]:
        signals.append("120일선 위: 장기 추세 유지")
    else:
        signals.append("120일선 아래: 장기 추세 약화")

    count = sum(above.values())
    if count == 4:
        return "전구간 상승 추세", 0, signals
    if above[20] and above[60] and above[120]:
        return "단기 조정 / 중장기 강세", 1, signals
    if above[20] and above[60]:
        return "단기·중기 강세", 1, signals
    if above[20]:
        return "단기 강세", 1, signals
    if above[5]:
        return "단기 상승 가능성", 1, signals
    if above[60] or above[120]:
        return "단기 조정", 2, signals
    return "전구간 이탈", 3, signals


def mdd_line(close: pd.Series) -> str:
    one_year = close.tail(260)
    if one_year.empty:
        return ""
    peak_val = float(one_year.max())
    peak_date = one_year.idxmax()
    last = float(one_year.iloc[-1])
    mdd = (last / peak_val - 1.0) * 100 if peak_val else None
    date_txt = peak_date.strftime("%m-%d") if hasattr(peak_date, "strftime") else str(peak_date)
    return f"최근 1년 고점대비 {fmt_pct(mdd, 1)} (고점 {peak_val:,.0f}, {date_txt})"


def ma_gap_text(close: float, ma_vals: Dict[int, Optional[float]]) -> str:
    parts = []
    for w in MA_WINDOWS:
        ma = ma_vals.get(w)
        if ma is None or not math.isfinite(ma):
            parts.append(f"{w}D —")
            continue
        gap = (close / ma - 1.0) * 100
        mark = "위" if close >= ma else "아래"
        parts.append(f"{w}D {mark} {fmt_pct(gap, 1)}")
    return " · ".join(parts)


def make_chart(close: pd.Series, ma: Dict[int, pd.Series], n: int = 180) -> Dict[str, Any]:
    df = pd.DataFrame({"close": close})
    for w, s in ma.items():
        df[f"ma{w}"] = s
    df = df.tail(n)
    labels = [idx.strftime("%Y-%m-%d") for idx in df.index]

    def to_list(col: str) -> List[Optional[float]]:
        out: List[Optional[float]] = []
        for x in df[col].values:
            out.append(None if pd.isna(x) else round(float(x), 2))
        return out

    return {
        "type": "multi_ma",
        "unit": "가격",
        "labels": labels,
        "series": {
            "close": to_list("close"),
            "ma5": to_list("ma5"),
            "ma20": to_list("ma20"),
            "ma60": to_list("ma60"),
            "ma120": to_list("ma120"),
        },
    }


def make_card(spec: Dict[str, Any]) -> Dict[str, Any]:
    close = load_close(spec)
    if len(close) < 125:
        raise RuntimeError(f"Not enough price history for {spec['name']}")
    ma = moving_averages(close)
    last_close = float(close.iloc[-1])
    prev_close = float(close.iloc[-2]) if len(close) >= 2 else None
    ma_vals = {w: (None if pd.isna(ma[w].iloc[-1]) else float(ma[w].iloc[-1])) for w in MA_WINDOWS}
    state, signal_level, signals = classify(last_close, ma_vals)

    date_txt = close.index[-1].strftime("%Y-%m-%d")
    one_day = pct_change(last_close, prev_close)
    change_text = ma_gap_text(last_close, ma_vals)

    return {
        "name": spec["name"],
        "category": spec["category"],
        "value_text": fmt_price(last_close, spec.get("currency", "USD")),
        "date": date_txt,
        "change_text": f"{fmt_pct(one_day)} vs 전일 · {change_text}",
        "state": state,
        "signal_level": signal_level,
        "comment": (
            "5·20·60·120일 이동평균 기준의 추세 판단입니다. "
            + " / ".join(signals)
            + ". 50일 이격도 기반 과열 판단보다, 단기·중기·장기 추세의 배열을 보기 위한 카드입니다."
        ),
        "mdd_line": mdd_line(close),
        "source": source_label(spec["source"], spec["ticker"]),
        "ok": True,
        "chart": make_chart(close, ma),
    }


def failure_card(spec: Dict[str, Any], reason: str) -> Dict[str, Any]:
    return {
        "name": spec.get("name", spec.get("ticker", "unknown")),
        "category": spec.get("category", "기타"),
        "value_text": "수집 실패",
        "date": today_kst().isoformat(),
        "change_text": "",
        "state": "오류",
        "signal_level": 3,
        "comment": str(reason)[:240],
        "mdd_line": "",
        "source": source_label(spec.get("source", "unknown"), spec.get("ticker", "")),
        "ok": False,
        "chart": None,
    }


def ensure_default_config(path: Path) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(DEFAULT_UNIVERSE, ensure_ascii=False, indent=2), encoding="utf-8")


def build(config_path: Path, output_path: Path) -> None:
    ensure_default_config(config_path)
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    cards = []
    for spec in cfg.get("indicators", []):
        try:
            cards.append(make_card(spec))
        except Exception as exc:
            cards.append(failure_card(spec, exc))

    out = {
        "generated_at": kst_now_str(),
        "note": "산업별 주가 모니터링. 50일 이격도 대신 5·20·60·120일 이동평균 기준으로 단기·중기·장기 추세를 판단합니다.",
        "indicators": cards,
    }
    output_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {output_path} with {len(cards)} sector MA cards at {kst_now_str()}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/sector_price_universe.json")
    ap.add_argument("--output", default="sector.json")
    args = ap.parse_args()
    build(Path(args.config), Path(args.output))


if __name__ == "__main__":
    main()
