#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Build flow.json — 투자자별 자금 흐름 (수급) 카드.

외국인 / 기관계 / 연기금 / 금융투자 / 보험 / 투신 / 개인 별로
 - 최근 약 1개월간 순매수/순매도 상위 종목 (돈이 어디서 어디로 흐르는지)
 - 일별 순매수 금액 추세 (누적 방향성)
 - 섹터별 자금 흐름 롤업
을 계산해 대시보드의 "자금 흐름" 탭용 flow.json 으로 저장한다.

데이터 소스: KRX 정보데이터시스템 (pykrx). 투자자별 데이터는 로그인이 필요하므로
KRX_ID / KRX_PW 환경변수가 있어야 한다 (없으면 빈/실패 카드로 graceful degrade).

저녁 루틴에서만 실행:
  python scripts/build_investor_flow.py
  python scripts/build_investor_flow.py --output flow.json --days 30 --top 15

Dependencies: pykrx, pandas, FinanceDataReader
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from pykrx import stock

try:
    import FinanceDataReader as fdr
except Exception:  # pragma: no cover
    fdr = None

KST = dt.timezone(dt.timedelta(hours=9))

# 표시명 -> pykrx investor 인자
INVESTORS: List[Dict[str, str]] = [
    {"name": "외국인", "krx": "외국인", "group": "해외"},
    {"name": "기관계", "krx": "기관합계", "group": "기관"},
    {"name": "연기금", "krx": "연기금", "group": "기관"},
    {"name": "금융투자", "krx": "금융투자", "group": "기관"},
    {"name": "보험", "krx": "보험", "group": "기관"},
    {"name": "투신", "krx": "투신", "group": "기관"},
    {"name": "개인", "krx": "개인", "group": "개인"},
]

MARKETS = ["KOSPI", "KOSDAQ"]


def kst_now_str() -> str:
    return dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")


def today_kst() -> dt.date:
    return dt.datetime.now(KST).date()


def fmt_won(x: Optional[float]) -> str:
    """원 단위 금액을 조/억 단위 한글 문자열로."""
    if x is None or not math.isfinite(x):
        return "—"
    sign = "+" if x >= 0 else "-"
    v = abs(float(x))
    jo = 1_0000_0000_0000  # 1조
    eok = 1_0000_0000      # 1억
    if v >= jo:
        return f"{sign}{v / jo:,.2f}조"
    if v >= eok:
        return f"{sign}{v / eok:,.0f}억"
    return f"{sign}{v:,.0f}원"


def daterange(days: int) -> (str, str):  # type: ignore[valid-type]
    end = today_kst()
    start = end - dt.timedelta(days=days)
    return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")


# --------------------------------------------------------------------------
# 티커 -> 섹터 매핑 (FDR KRX 상장목록)
# --------------------------------------------------------------------------
def build_sector_map() -> Dict[str, str]:
    if fdr is None:
        return {}
    # KRX-DESC 리스팅에만 Sector/Industry(업종) 컬럼이 있다.
    try:
        lst = fdr.StockListing("KRX-DESC")
    except Exception:
        return {}
    if lst is None or lst.empty:
        return {}
    code_col = next((c for c in ["Code", "Symbol", "종목코드"] if c in lst.columns), None)
    if code_col is None:
        return {}
    has_sector = "Sector" in lst.columns
    has_industry = "Industry" in lst.columns
    out: Dict[str, str] = {}
    for _, row in lst.iterrows():
        code = str(row[code_col]).zfill(6)
        sector = None
        if has_sector and pd.notna(row["Sector"]) and str(row["Sector"]) != "nan":
            sector = str(row["Sector"]).strip()
        elif has_industry and pd.notna(row["Industry"]) and str(row["Industry"]) != "nan":
            sector = str(row["Industry"]).strip()
        out[code] = sector if sector else "기타"
    return out


# --------------------------------------------------------------------------
# 투자자별 종목 순매수 (여러 시장 합산)
# --------------------------------------------------------------------------
def net_purchases(frm: str, to: str, krx_investor: str) -> pd.DataFrame:
    frames = []
    for mkt in MARKETS:
        try:
            df = stock.get_market_net_purchases_of_equities(frm, to, mkt, krx_investor)
            if df is not None and not df.empty:
                frames.append(df)
        except Exception:
            continue
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames)
    # 컬럼: 종목명, 매도/매수거래량, 순매수거래량, 매도/매수거래대금, 순매수거래대금
    return df


def top_lists(df: pd.DataFrame, sector_map: Dict[str, str], top: int) -> Dict[str, Any]:
    if df.empty or "순매수거래대금" not in df.columns:
        return {"top_buys": [], "top_sells": [], "sector_flow": [], "net_value": None}

    def row_to_item(idx, r) -> Dict[str, Any]:
        ticker = str(idx).zfill(6)
        return {
            "ticker": ticker,
            "name": str(r.get("종목명", ticker)),
            "net_value": int(r["순매수거래대금"]),
            "sector": sector_map.get(ticker, "기타"),
        }

    ordered = df.sort_values("순매수거래대금", ascending=False)
    buys = [row_to_item(i, r) for i, r in ordered.head(top).iterrows() if r["순매수거래대금"] > 0]
    sells_src = ordered.tail(top).iloc[::-1]
    sells = [row_to_item(i, r) for i, r in sells_src.iterrows() if r["순매수거래대금"] < 0]

    # 섹터 롤업
    sec_tot: Dict[str, int] = {}
    for i, r in df.iterrows():
        ticker = str(i).zfill(6)
        sec = sector_map.get(ticker, "기타")
        sec_tot[sec] = sec_tot.get(sec, 0) + int(r["순매수거래대금"])
    sector_flow = sorted(
        [{"sector": k, "net_value": v} for k, v in sec_tot.items()],
        key=lambda d: abs(d["net_value"]),
        reverse=True,
    )[:12]

    net_value = int(df["순매수거래대금"].sum())
    return {"top_buys": buys, "top_sells": sells, "sector_flow": sector_flow, "net_value": net_value}


# --------------------------------------------------------------------------
# 투자자별 일별 순매수 추세 (시장 전체, detail 세분화)
# --------------------------------------------------------------------------
def daily_series(frm: str, to: str) -> pd.DataFrame:
    """시장 전체(KOSPI+KOSDAQ) 일별 투자자별 순매수. 컬럼=투자자, 인덱스=날짜.

    detail=False 는 기관합계/외국인합계 등 집계 컬럼을, detail=True 는
    금융투자/보험/투신/연기금등 세부 컬럼을 준다. 둘 다 받아 합쳐서
    모든 투자자 컬럼을 확보한다.
    """
    per_market = []
    for mkt in MARKETS:
        frames = []
        for det in (False, True):
            try:
                df = stock.get_market_trading_value_by_date(frm, to, mkt, detail=det)
                if df is not None and not df.empty:
                    frames.append(df)
            except Exception:
                continue
        if not frames:
            continue
        combined = pd.concat(frames, axis=1)
        combined = combined.loc[:, ~combined.columns.duplicated()]
        per_market.append(combined)
    if not per_market:
        return pd.DataFrame()
    total = per_market[0].copy()
    for f in per_market[1:]:
        total = total.add(f, fill_value=0)
    return total


def pick_daily(daily_df: pd.DataFrame, krx_investor: str) -> Dict[str, Any]:
    if daily_df.empty:
        return {"labels": [], "value": [], "cum": []}
    # 컬럼 매칭: '기관합계' vs '기관계', '연기금' vs '연기금등', '외국인' vs '외국인합계'
    cols = list(daily_df.columns)
    target = None
    candidates = [krx_investor]
    if krx_investor == "기관합계":
        candidates += ["기관계", "기관"]
    if krx_investor == "연기금":
        candidates += ["연기금등"]
    if krx_investor == "외국인":
        candidates += ["외국인합계"]
    for cand in candidates:
        for c in cols:
            if str(c).replace(" ", "") == cand.replace(" ", ""):
                target = c
                break
        if target:
            break
    if target is None:
        # 부분일치 폴백
        for c in cols:
            if krx_investor in str(c):
                target = c
                break
    if target is None:
        return {"labels": [], "value": [], "cum": []}

    s = daily_df[target].astype(float)
    labels = [idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx) for idx in s.index]
    values = [round(float(v) / 1_0000_0000, 1) for v in s.values]  # 억 단위
    cum, run = [], 0.0
    for v in values:
        run += v
        cum.append(round(run, 1))
    return {"labels": labels, "value": values, "cum": cum}


# --------------------------------------------------------------------------
# 카드 조립
# --------------------------------------------------------------------------
def make_card(inv: Dict[str, str], frm: str, to: str,
              sector_map: Dict[str, str], daily_df: pd.DataFrame, top: int) -> Dict[str, Any]:
    df = net_purchases(frm, to, inv["krx"])
    lists = top_lists(df, sector_map, top)
    daily = pick_daily(daily_df, inv["krx"])

    net = lists["net_value"]
    is_buy = (net is not None and net >= 0)
    state = "순매수" if is_buy else "순매도"
    signal_level = 0 if is_buy else 3

    top_buy_name = lists["top_buys"][0]["name"] if lists["top_buys"] else "—"
    top_sell_name = lists["top_sells"][0]["name"] if lists["top_sells"] else "—"

    comment = (
        f"최근 약 1개월 {inv['name']} 순매수 총액 {fmt_won(net)}. "
        f"가장 많이 담은 종목: {top_buy_name}, 가장 많이 판 종목: {top_sell_name}. "
        "순매수 상위(돈이 들어간 곳)와 순매도 상위(돈이 빠진 곳), 일별 누적 추세, "
        "섹터별 흐름으로 자금의 방향을 봅니다."
    )

    ok = bool(df.shape[0]) or bool(daily["labels"])
    return {
        "name": inv["name"],
        "category": inv["group"],
        "value_text": f"{state} {fmt_won(net)}" if net is not None else "수집 실패",
        "change_text": f"순매수 {len(lists['top_buys'])}선두 · 순매도 {len(lists['top_sells'])}선두",
        "state": state if ok else "오류",
        "signal_level": signal_level,
        "date": to[:4] + "-" + to[4:6] + "-" + to[6:],
        "source": "KRX",
        "ok": ok,
        "comment": comment,
        "mdd_line": "",
        "top_buys": lists["top_buys"],
        "top_sells": lists["top_sells"],
        "sector_flow": lists["sector_flow"],
        "chart": {
            "type": "flow_daily",
            "unit": "억원",
            "labels": daily["labels"],
            "series": {"value": daily["value"], "cum": daily["cum"]},
        },
    }


def failure_card(inv: Dict[str, str], reason: str, to: str) -> Dict[str, Any]:
    return {
        "name": inv["name"],
        "category": inv["group"],
        "value_text": "수집 실패",
        "change_text": "",
        "state": "오류",
        "signal_level": 3,
        "date": to[:4] + "-" + to[4:6] + "-" + to[6:],
        "source": "KRX",
        "ok": False,
        "comment": str(reason)[:240],
        "mdd_line": "",
        "top_buys": [],
        "top_sells": [],
        "sector_flow": [],
        "chart": None,
    }


def build(output_path: Path, days: int, top: int) -> None:
    frm, to = daterange(days)
    print(f"[flow] range {frm}~{to}, KRX login={'yes' if os.getenv('KRX_ID') else 'NO'}")

    sector_map = build_sector_map()
    print(f"[flow] sector map: {len(sector_map)} tickers")
    daily_df = daily_series(frm, to)
    print(f"[flow] daily series: {daily_df.shape}")

    cards = []
    for inv in INVESTORS:
        try:
            card = make_card(inv, frm, to, sector_map, daily_df, top)
        except Exception as exc:
            card = failure_card(inv, exc, to)
        cards.append(card)
        print(f"[flow] {inv['name']}: ok={card['ok']} net={card.get('value_text')}")

    out = {
        "generated_at": kst_now_str(),
        "period": {"from": frm, "to": to},
        "note": "투자자별 자금 흐름 (KRX 수급). 최근 약 1개월간 투자자 유형별 순매수/순매도 상위 종목과 섹터 흐름, 일별 누적 추세.",
        "indicators": cards,
    }
    output_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    ok_n = sum(1 for c in cards if c["ok"])
    print(f"Wrote {output_path} with {len(cards)} investor cards ({ok_n} ok) at {kst_now_str()}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default="flow.json")
    ap.add_argument("--days", type=int, default=30, help="조회 캘린더 일수 (약 1개월)")
    ap.add_argument("--top", type=int, default=15, help="순매수/순매도 상위 종목 수")
    args = ap.parse_args()
    build(Path(args.output), args.days, args.top)


if __name__ == "__main__":
    main()
