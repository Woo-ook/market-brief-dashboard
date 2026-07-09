#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Build liquidity.json — 시장 유동성 (Market Liquidity) 카드.

미국/한국 시장의 "돈의 양"을 보는 지표들. 매일 확인할 필요는 없지만(대부분
주간·월간 갱신) 매일 재빌드해도 각 지표는 자기 native 주기로만 값이 바뀌므로
저녁 루틴에 얹어 매일 돌려도 안전하다. 실패해도 비치명적(보조 데이터)이라
기존 liquidity.json 을 덮어쓰지 않도록 루틴이 처리한다(flow.json 과 동일).

카드 구성
  [미국]  (FRED — urllib, 프록시 친화적)
    - Fed 대차대조표          WALCL       (주간, 수)
    - 은행 지급준비금          WRESBAL     (주간)
    - 재무부 일반계정(TGA)     WTREGEN     (주간)   ← 유동성 흡수(역방향)
    - 역레포(Reverse Repo)    RRPONTSYD   (영업일)  ← 유동성 흡수(역방향)
    - 미국 M2 증가율          M2SL        (월간, YoY)
    - SOFR–EFFR 스프레드      SOFR/EFFR   (영업일)  ← 단기자금 스트레스
  [한국]
    - 원/달러 환율            FinanceDataReader (영업일)
    - 외국인/기관/개인 20일 순매수  KRX pykrx (영업일, 로그인 필요)
    - 한국 M2 증가율          BOK ECOS    (월간, YoY)

* KOFIA(투자자예탁금/신용융자/CMA·MMF·RP)는 eXBuilder6 SPA라 별도 XHR 관찰이
  필요해 이번 단계에선 제외(2단계).

실행:
  python scripts/build_liquidity.py
  python scripts/build_liquidity.py --output liquidity.json

Dependencies: pandas, FinanceDataReader, pykrx (KRX 카드에만). FRED/ECOS 는 stdlib.
환경변수(선택): FRED_API_KEY, BOK_API_KEY, KRX_ID/KRX_PW.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import socket
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

KST = dt.timezone(dt.timedelta(hours=9))
socket.setdefaulttimeout(60)

# 로컬 Windows(cp949) 콘솔에서도 로그 print 가 깨지거나 크래시나지 않도록 utf-8 고정.
try:  # pragma: no cover
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

FRED_API_KEY = os.environ.get("FRED_API_KEY")
BOK_API_KEY = os.environ.get("BOK_API_KEY")

UA = {"User-Agent": "Mozilla/5.0 (liquidity-builder)"}


# ==========================================================================
# 공통 유틸
# ==========================================================================
def kst_now_str() -> str:
    return dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")


def http_get_text(url: str, timeout: int = 60, retries: int = 3) -> str:
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001
            last = exc
    raise RuntimeError(f"GET 실패({url[:80]}...): {last}")


def fmt_usd(d: Optional[float], signed: bool = False) -> str:
    """달러 금액(절대 USD)을 $X.XXT / $XXXB 로."""
    if d is None:
        return "—"
    sign = ""
    if signed:
        sign = "+" if d >= 0 else "-"
    v = abs(float(d))
    if v >= 1e12:
        return f"{sign}${v / 1e12:.2f}T"
    if v >= 1e9:
        return f"{sign}${v / 1e9:,.0f}B"
    if v >= 1e6:
        return f"{sign}${v / 1e6:,.0f}M"
    return f"{sign}${v:,.0f}"


def fmt_won(x: Optional[float]) -> str:
    """원 단위 금액을 조/억 문자열로."""
    if x is None:
        return "—"
    sign = "+" if x >= 0 else "-"
    v = abs(float(x))
    if v >= 1e12:
        return f"{sign}{v / 1e12:,.2f}조"
    if v >= 1e8:
        return f"{sign}{v / 1e8:,.0f}억"
    return f"{sign}{v:,.0f}원"


def base_card(name: str, category: str, freq: str, source: str) -> Dict[str, Any]:
    """실패 시에도 유효한 최소 카드 골격."""
    return {
        "name": name,
        "category": category,
        "freq": freq,
        "value_text": "수집 실패",
        "change_text": "",
        "state": "오류",
        "signal_level": 3,
        "date": "",
        "source": source,
        "ok": False,
        "comment": "",
        "mdd_line": "",
        "top_buys": [],
        "top_sells": [],
        "sector_flow": [],
        "chart": None,
    }


# ==========================================================================
# FRED
# ==========================================================================
def fred_series(series_id: str, lookback_days: int = 1500) -> List[Tuple[str, float]]:
    """FRED 관측치를 (YYYY-MM-DD, value) 오름차순 리스트로. 키 있으면 JSON API,
    없으면 fredgraph CSV 폴백(둘 다 urllib)."""
    start = (dt.date.today() - dt.timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    out: List[Tuple[str, float]] = []
    if FRED_API_KEY:
        params = urllib.parse.urlencode({
            "series_id": series_id,
            "api_key": FRED_API_KEY,
            "file_type": "json",
            "observation_start": start,
            "sort_order": "asc",
        })
        url = f"https://api.stlouisfed.org/fred/series/observations?{params}"
        data = json.loads(http_get_text(url))
        for row in data.get("observations", []):
            d, v = row.get("date"), row.get("value")
            if not d or not v or v == ".":
                continue
            try:
                out.append((d, float(v)))
            except ValueError:
                continue
    else:
        url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
        text = http_get_text(url)
        for row in csv.DictReader(text.splitlines()):
            d = row.get("observation_date")
            v = row.get(series_id)
            if not d or not v or v == ".":
                continue
            if d < start:
                continue
            try:
                out.append((d, float(v)))
            except ValueError:
                continue
    if not out:
        raise RuntimeError(f"FRED 유효 데이터 없음: {series_id}")
    out.sort(key=lambda t: t[0])
    return out


# 미국 유동성 레벨 카드 정의
# scale: 원계열 → 절대 USD 로 바꾸는 배수. polarity: +1 = 값↑이 유동성 완화(호재),
#        -1 = 값↑이 유동성 흡수(경계).
US_LEVEL_CARDS = [
    {"name": "Fed 대차대조표", "id": "WALCL", "freq": "주간", "scale": 1e6,
     "polarity": +1, "tail": 78,
     "desc": "연준 총자산(중앙은행 물탱크). 확대=양적완화/유동성 공급, 축소=QT."},
    {"name": "은행 지급준비금", "id": "WRESBAL", "freq": "주간", "scale": 1e6,
     "polarity": +1, "tail": 78,
     "desc": "은행이 연준에 맡긴 지준(금융시스템의 피). 줄면 자금경색 위험↑."},
    {"name": "재무부 일반계정(TGA)", "id": "WTREGEN", "freq": "주간", "scale": 1e6,
     "polarity": -1, "tail": 78,
     "desc": "정부의 연준 예치금. 늘면 시중 유동성 흡수, 줄면 시장으로 방출."},
    {"name": "역레포(Reverse Repo)", "id": "RRPONTSYD", "freq": "영업일", "scale": 1e9,
     "polarity": -1, "tail": 120,
     "desc": "시장 밖에 대기 중인 현금(ON RRP). 줄면 자금이 시장으로 유입."},
]


def make_fred_level_card(cfg: Dict[str, Any]) -> Dict[str, Any]:
    card = base_card(cfg["name"], "미국", cfg["freq"], f"FRED:{cfg['id']}")
    obs = fred_series(cfg["id"])
    dates = [d for d, _ in obs]
    dollars = [v * cfg["scale"] for _, v in obs]
    latest, prev = dollars[-1], (dollars[-2] if len(dollars) > 1 else None)
    delta = (latest - prev) if prev is not None else None
    pol = cfg["polarity"]

    # 방향 판정: 최근 변화 * polarity 부호
    rising = (delta is not None and delta > 0)
    liq_up = (rising and pol > 0) or ((delta is not None and delta < 0) and pol < 0)
    if delta is None or abs(delta) < 1e-9:
        state, sig = "보합", 1
    elif liq_up:
        state, sig = ("확대" if rising else "감소") + " · 완화", 0
    else:
        state, sig = ("확대" if rising else "감소") + " · 흡수", 2

    tail = int(cfg.get("tail", 90))
    card.update({
        "value_text": fmt_usd(latest),
        "change_text": f"전기 대비 {fmt_usd(delta, signed=True)}" if delta is not None else "",
        "state": state,
        "signal_level": sig,
        "date": dates[-1],
        "ok": True,
        "comment": f"{cfg['desc']} 최근값 {fmt_usd(latest)}, 직전 대비 {fmt_usd(delta, signed=True)}.",
        "mdd_line": f"최근 {min(tail, len(obs))}개 관측 · 라인=레벨($B)",
        "chart": {
            "type": "level",
            "unit": "$B",
            "labels": dates[-tail:],
            "series": {"value": [round(x / 1e9, 1) for x in dollars[-tail:]]},
        },
    })
    return card


def make_m2_growth_card() -> Dict[str, Any]:
    card = base_card("미국 M2 증가율", "미국", "월간", "FRED:M2SL")
    obs = fred_series("M2SL", lookback_days=2200)  # ~6년
    dates = [d for d, _ in obs]
    vals = [v for _, v in obs]  # $Billions
    if len(vals) < 14:
        raise RuntimeError("M2 데이터 부족")
    yoy: List[Optional[float]] = []
    for i in range(len(vals)):
        if i >= 12 and vals[i - 12]:
            yoy.append(round((vals[i] / vals[i - 12] - 1) * 100, 2))
        else:
            yoy.append(None)
    # 유효 구간만
    idx = [i for i, y in enumerate(yoy) if y is not None]
    d2 = [dates[i] for i in idx]
    y2 = [yoy[i] for i in idx]
    last_y = y2[-1]
    prev_y = y2[-2] if len(y2) > 1 else None
    mom = round((vals[-1] / vals[-2] - 1) * 100, 2) if len(vals) > 1 and vals[-2] else None

    if last_y is None:
        state, sig = "—", 1
    elif last_y < 0:
        state, sig = "수축", 3
    elif prev_y is not None and last_y >= prev_y:
        state, sig = "확대 가속", 0
    else:
        state, sig = "완만", 1

    card.update({
        "value_text": f"{last_y:+.1f}% YoY",
        "change_text": (f"전월비 {mom:+.2f}%" if mom is not None else ""),
        "state": state,
        "signal_level": sig,
        "date": dates[-1],
        "ok": True,
        "comment": ("시중 통화량(M2)의 큰 흐름. YoY 증가율이 (+)로 가속하면 유동성 팽창, "
                    f"(-)면 수축. 현재 {last_y:+.1f}% YoY."),
        "mdd_line": f"M2 레벨 ${vals[-1]/1e3:,.2f}T (월간)",
        "chart": {
            "type": "level", "unit": "%",
            "labels": d2[-72:], "series": {"value": y2[-72:]},
        },
    })
    return card


def make_sofr_spread_card() -> Dict[str, Any]:
    card = base_card("SOFR–EFFR 스프레드", "미국", "영업일", "FRED:SOFR-EFFR")
    sofr = dict(fred_series("SOFR", lookback_days=400))
    effr = dict(fred_series("EFFR", lookback_days=400))
    common = sorted(set(sofr) & set(effr))
    if len(common) < 5:
        raise RuntimeError("SOFR/EFFR 공통일자 부족")
    spread_bp = [(sofr[d] - effr[d]) * 100 for d in common]  # basis points
    last = spread_bp[-1]
    prev = spread_bp[-2] if len(spread_bp) > 1 else None
    delta = (last - prev) if prev is not None else None

    a = abs(last)
    if a <= 5:
        state, sig = "안정", 0
    elif a <= 12:
        state, sig = "다소 확대", 1
    elif a <= 25:
        state, sig = "스트레스", 2
    else:
        state, sig = "경색 신호", 3

    card.update({
        "value_text": f"{last:+.1f}bp",
        "change_text": (f"전일 대비 {delta:+.1f}bp" if delta is not None else ""),
        "state": state,
        "signal_level": sig,
        "date": common[-1],
        "ok": True,
        "comment": ("담보부(SOFR)와 무담보(EFFR) 익일물 금리 차. 0 근처면 단기자금시장 정상, "
                    "크게 벌어지면(특히 +) 레포시장 담보·현금 스트레스 신호. "
                    f"현재 {last:+.1f}bp."),
        "mdd_line": f"SOFR {sofr[common[-1]]:.2f}% · EFFR {effr[common[-1]]:.2f}%",
        "chart": {
            "type": "level", "unit": "bp",
            "labels": common[-120:],
            "series": {"value": [round(x, 1) for x in spread_bp[-120:]]},
        },
    })
    return card


# ==========================================================================
# 한국 — USD/KRW (FinanceDataReader)
# ==========================================================================
def make_usdkrw_card() -> Dict[str, Any]:
    card = base_card("원/달러 환율", "한국", "영업일", "FinanceDataReader")
    import FinanceDataReader as fdr  # 지연 import
    end = dt.datetime.now(KST).date()
    start = end - dt.timedelta(days=200)
    df = fdr.DataReader("USD/KRW", start.strftime("%Y-%m-%d"))
    if df is None or df.empty or "Close" not in df.columns:
        raise RuntimeError("USD/KRW 데이터 없음")
    df = df.dropna(subset=["Close"])
    closes = [float(x) for x in df["Close"].values]
    dates = [i.strftime("%Y-%m-%d") if hasattr(i, "strftime") else str(i) for i in df.index]
    last = closes[-1]
    prev = closes[-2] if len(closes) > 1 else last
    ref20 = closes[-21] if len(closes) > 21 else closes[0]
    trend_pct = (last / ref20 - 1) * 100 if ref20 else 0.0

    # 원화 약세(환율↑)=경계
    if trend_pct >= 2:
        state, sig = "원화 약세(경계)", 2
    elif trend_pct <= -2:
        state, sig = "원화 강세", 0
    else:
        state, sig = "중립", 1
    if last >= 1400:
        sig = max(sig, 2)
        if state == "중립":
            state = "고환율 경계"

    card.update({
        "value_text": f"{last:,.1f}원",
        "change_text": f"20일 추세 {trend_pct:+.1f}% · 전일 {last - prev:+.1f}",
        "state": state,
        "signal_level": sig,
        "date": dates[-1],
        "ok": True,
        "comment": ("원/달러 환율. 상승(원화 약세)은 외국인 자금 이탈·수입물가·위험자산 부담으로 "
                    f"이어질 수 있는 유동성 압력. 20일 추세 {trend_pct:+.1f}%."),
        "mdd_line": f"최근 {min(120, len(closes))} 영업일 · 라인=환율(원)",
        "chart": {
            "type": "level", "unit": "원",
            "labels": dates[-120:], "series": {"value": [round(x, 1) for x in closes[-120:]]},
        },
    })
    return card


# ==========================================================================
# 한국 — 투자자 20일 순매수 (KRX pykrx)
# ==========================================================================
KR_INVESTORS = [
    {"name": "외국인 20일 순매수", "krx": "외국인", "alt": ["외국인합계"]},
    {"name": "기관 20일 순매수", "krx": "기관합계", "alt": ["기관계", "기관"]},
    {"name": "개인 20일 순매수", "krx": "개인", "alt": []},
]
KR_MARKETS = ["KOSPI", "KOSDAQ"]


def kr_daily_netvalue():
    """KOSPI+KOSDAQ 일별 투자자별 순매수(원). 컬럼=투자자, 인덱스=날짜."""
    import pandas as pd
    from pykrx import stock
    end = dt.datetime.now(KST).date()
    start = end - dt.timedelta(days=40)
    frm, to = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
    per_market = []
    for mkt in KR_MARKETS:
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
        return None
    total = per_market[0].copy()
    for f in per_market[1:]:
        total = total.add(f, fill_value=0)
    return total


def make_kr_flow_card(inv: Dict[str, Any], daily) -> Dict[str, Any]:
    card = base_card(inv["name"], "한국", "영업일", "KRX")
    if daily is None or daily.empty:
        raise RuntimeError("KRX 일별 순매수 데이터 없음(로그인 필요할 수 있음)")
    cols = list(daily.columns)
    target = None
    for cand in [inv["krx"], *inv["alt"]]:
        for c in cols:
            if str(c).replace(" ", "") == cand.replace(" ", ""):
                target = c
                break
        if target:
            break
    if target is None:
        for c in cols:
            if inv["krx"] in str(c):
                target = c
                break
    if target is None:
        raise RuntimeError(f"{inv['krx']} 컬럼 없음: {cols}")

    s = daily[target].astype(float).tail(20)  # 최근 약 20거래일
    labels = [i.strftime("%Y-%m-%d") if hasattr(i, "strftime") else str(i) for i in s.index]
    daily_eok = [round(v / 1e8, 1) for v in s.values]      # 억원
    cum, run = [], 0.0
    for v in daily_eok:
        run += v
        cum.append(round(run, 1))
    total_won = float(s.sum())

    is_buy = total_won >= 0
    if inv["krx"] == "개인":
        # 개인은 종종 역지표 → 색은 중립(노랑)로
        state, sig = ("순매수" if is_buy else "순매도"), 1
    else:
        state = "순매수" if is_buy else "순매도"
        sig = 0 if is_buy else 2

    card.update({
        "value_text": f"{state} {fmt_won(total_won)}",
        "change_text": f"20거래일 누적 {fmt_won(total_won)}",
        "state": state,
        "signal_level": sig,
        "date": labels[-1] if labels else "",
        "ok": bool(labels),
        "comment": (f"{inv['name'].replace(' 20일 순매수','')}의 최근 약 20거래일 KOSPI+KOSDAQ "
                    f"누적 순매수 {fmt_won(total_won)}. 막대=일별 순매수(억), 라인=누적. "
                    "종목·섹터 상세는 '자금 흐름' 탭 참고."),
        "mdd_line": "",
        "chart": {
            "type": "flow_daily", "unit": "억원",
            "labels": labels, "series": {"value": daily_eok, "cum": cum},
        },
    })
    return card


# ==========================================================================
# 한국 — M2 증가율 (BOK ECOS)
# ==========================================================================
ECOS_M2_TABLES = ["101Y003", "101Y004", "101Y002"]  # 통화 및 유동성 후보


def ecos_json(path: str) -> Dict[str, Any]:
    url = f"https://ecos.bok.or.kr/api/{path}"
    return json.loads(http_get_text(url))


def make_kr_m2_card() -> Dict[str, Any]:
    card = base_card("한국 M2 증가율", "한국", "월간", "BOK ECOS")
    if not BOK_API_KEY:
        raise RuntimeError("BOK_API_KEY 없음")

    # 1) M2(광의통화) 항목코드 탐색
    table = item_code = item_name = None
    for tbl in ECOS_M2_TABLES:
        try:
            data = ecos_json(f"StatisticItemList/{BOK_API_KEY}/json/kr/1/1000/{tbl}")
            rows = data.get("StatisticItemList", {}).get("row", [])
        except Exception:
            continue
        for r in rows:
            nm = str(r.get("ITEM_NAME", ""))
            if ("M2" in nm and "광의" in nm) or nm.strip() in ("M2(광의통화)", "M2"):
                table, item_code, item_name = tbl, r.get("ITEM_CODE"), nm
                break
        if item_code:
            break
        # 느슨한 매칭
        for r in rows:
            if "광의통화" in str(r.get("ITEM_NAME", "")):
                table, item_code, item_name = tbl, r.get("ITEM_CODE"), str(r.get("ITEM_NAME"))
                break
        if item_code:
            break
    if not item_code:
        raise RuntimeError("ECOS에서 M2 항목코드를 찾지 못함")

    # 2) 월간 시계열(최근 ~6년) 조회
    end = dt.datetime.now(KST).date()
    start_ym = (end.replace(day=1) - dt.timedelta(days=2200)).strftime("%Y%m")
    end_ym = end.strftime("%Y%m")
    ic = urllib.parse.quote(str(item_code))
    data = ecos_json(f"StatisticSearch/{BOK_API_KEY}/json/kr/1/1000/{table}/M/{start_ym}/{end_ym}/{ic}")
    rows = data.get("StatisticSearch", {}).get("row", [])
    pts = []
    for r in rows:
        t, v = r.get("TIME"), r.get("DATA_VALUE")
        if t and v not in (None, "", "-"):
            try:
                pts.append((t, float(v)))
            except ValueError:
                continue
    pts.sort(key=lambda x: x[0])
    if len(pts) < 14:
        raise RuntimeError("ECOS M2 데이터 부족")
    times = [p[0] for p in pts]
    vals = [p[1] for p in pts]
    yoy = []
    for i in range(len(vals)):
        if i >= 12 and vals[i - 12]:
            yoy.append(round((vals[i] / vals[i - 12] - 1) * 100, 2))
        else:
            yoy.append(None)
    idx = [i for i, y in enumerate(yoy) if y is not None]
    d2 = [f"{times[i][:4]}-{times[i][4:6]}" for i in idx]
    y2 = [yoy[i] for i in idx]
    last_y = y2[-1]
    prev_y = y2[-2] if len(y2) > 1 else None

    if last_y is None:
        state, sig = "—", 1
    elif last_y < 3:
        state, sig = "둔화", 2
    elif prev_y is not None and last_y >= prev_y:
        state, sig = "확대", 0
    else:
        state, sig = "완만", 1

    card.update({
        "value_text": f"{last_y:+.1f}% YoY",
        "change_text": (f"전월 {prev_y:+.1f}%" if prev_y is not None else ""),
        "state": state,
        "signal_level": sig,
        "date": f"{times[-1][:4]}-{times[-1][4:6]}",
        "ok": True,
        "comment": (f"한국 거시 유동성 M2({item_name}) 전년동월비 증가율. "
                    f"가속하면 유동성 팽창, 둔화하면 위축. 현재 {last_y:+.1f}% YoY."),
        "mdd_line": f"ECOS {table}/{item_code} · 월간",
        "chart": {
            "type": "level", "unit": "%",
            "labels": d2[-72:], "series": {"value": y2[-72:]},
        },
    })
    return card


# ==========================================================================
# 조립
# ==========================================================================
def build(output_path: Path) -> None:
    cards: List[Dict[str, Any]] = []

    def run(label: str, fn, *a):
        try:
            c = fn(*a)
        except Exception as exc:  # noqa: BLE001
            # base 카드 확보용: fn 이 첫 인자로 이름을 알 수 있으면 좋지만,
            # 여기선 실패 메시지를 담은 최소 카드를 만든다.
            c = None
            print(f"[liq] {label}: FAIL {exc}")
        else:
            print(f"[liq] {label}: ok={c.get('ok')} {c.get('value_text')}")
        return c

    # 미국 레벨 4종
    for cfg in US_LEVEL_CARDS:
        c = run(cfg["name"], make_fred_level_card, cfg)
        if c is None:
            c = base_card(cfg["name"], "미국", cfg["freq"], f"FRED:{cfg['id']}")
            c["comment"] = "FRED 수집 실패"
        cards.append(c)
    # 미국 M2 증가율 · SOFR 스프레드
    for label, fn, fallback in [
        ("미국 M2 증가율", make_m2_growth_card, ("미국 M2 증가율", "미국", "월간", "FRED:M2SL")),
        ("SOFR–EFFR 스프레드", make_sofr_spread_card, ("SOFR–EFFR 스프레드", "미국", "영업일", "FRED:SOFR-EFFR")),
    ]:
        c = run(label, fn)
        if c is None:
            c = base_card(*fallback)
        cards.append(c)

    # 한국 — USD/KRW
    c = run("원/달러 환율", make_usdkrw_card)
    if c is None:
        c = base_card("원/달러 환율", "한국", "영업일", "FinanceDataReader")
    cards.append(c)

    # 한국 — 투자자 20일 순매수 (일별 데이터 1회 조회 후 3개 카드)
    try:
        daily = kr_daily_netvalue()
        print(f"[liq] KRX daily: {None if daily is None else daily.shape}")
    except Exception as exc:  # noqa: BLE001
        daily = None
        print(f"[liq] KRX daily FAIL {exc}")
    for inv in KR_INVESTORS:
        c = run(inv["name"], make_kr_flow_card, inv, daily)
        if c is None:
            c = base_card(inv["name"], "한국", "영업일", "KRX")
            c["comment"] = "KRX 수집 실패(로그인 필요할 수 있음)"
        cards.append(c)

    # 한국 — M2 증가율
    c = run("한국 M2 증가율", make_kr_m2_card)
    if c is None:
        c = base_card("한국 M2 증가율", "한국", "월간", "BOK ECOS")
        c["comment"] = "ECOS 수집 실패"
    cards.append(c)

    out = {
        "generated_at": kst_now_str(),
        "note": ("시장 유동성(Market Liquidity). 미국(Fed 대차대조표·지준·TGA·역레포·M2·"
                 "SOFR스프레드)과 한국(환율·투자자 수급·M2)의 '돈의 양'을 봅니다. "
                 "대부분 주간·월간 갱신이라 매일 값이 바뀌지 않는 게 정상입니다."),
        "indicators": cards,
    }
    output_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    ok_n = sum(1 for c in cards if c["ok"])
    print(f"Wrote {output_path} with {len(cards)} cards ({ok_n} ok) at {kst_now_str()}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default="liquidity.json")
    args = ap.parse_args()
    build(Path(args.output))


if __name__ == "__main__":
    main()
