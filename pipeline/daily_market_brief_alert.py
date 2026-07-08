# -*- coding: utf-8 -*-
"""
daily_market_brief_alert.py

Daily Market Brief 자동 이메일 발송 스크립트.

최종 지표 구성
1) KOSPI 50일 이격도
2) KOSDAQ 50일 이격도
3) S&P500 50일 이격도
4) NASDAQ 50일 이격도
5) SOX 반도체지수 50일 이격도
6) 삼성전자 50일 이격도
7) 원/달러 환율
8) DXY 달러인덱스
9) 미국 2년물 금리
10) 미국 10년물 금리
11) 한국 10년물 금리
12) 미국 하이일드 스프레드
13) VIX 지수

[주도주 브리핑] (v4 추가)
14) KRX 거래대금 상위 종목 + 시장 폭(상승/하락 종목 수)
15) 외국인/기관 순매수 상위 종목 (KOSPI)
16) 미국 섹터 ETF 20일 상대강도 (vs S&P500)

발송 시각 (KST):
- 오전: 평일 07:20 (전영업일 한국·미국 시장 데이터 종합)
- 오후: 평일 18:06 (한국 정규장 마감 + 투자자별 잠정 데이터 확정 후)

사용자 기준
- 이격도: 100 이상 강세, 120 이상 과열, 130 이상 광기/셀 고려
- 원/달러: 1500원 이상 경고, 급등 시 경고, 1400~1440원은 달러 환전 고려
- VIX: 20 이상 경계, 30 이상 공포 확대
- 미국/한국 10년물: 하루 +5bp 이상 경계, +10bp 이상 위험 신호

GitHub Secrets
- 필수: SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, MAIL_TO
- 권장: FRED_API_KEY, BOK_API_KEY, ANTHROPIC_API_KEY
"""

import os
import sys
import ssl
import csv
import json
import time
import smtplib
import re
import datetime as dt
from dataclasses import dataclass
from email.mime.text import MIMEText
from email.header import Header
from typing import Optional, List, Dict, Any, Callable
from urllib.request import Request, urlopen
from urllib.parse import urlencode, quote
from urllib.error import HTTPError, URLError

import pandas as pd


# ─────────────────────────────────────────────────────────
# Secrets
# ─────────────────────────────────────────────────────────
SMTP_HOST = os.environ.get("SMTP_HOST")
SMTP_PORT = int(os.environ.get("SMTP_PORT") or "465")
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASS = os.environ.get("SMTP_PASS")
MAIL_TO = os.environ.get("MAIL_TO") or SMTP_USER

FRED_API_KEY = os.environ.get("FRED_API_KEY")
BOK_API_KEY = os.environ.get("BOK_API_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")


# ─────────────────────────────────────────────────────────
# User thresholds
# ─────────────────────────────────────────────────────────
CSV_LOG_PATH = "daily_market_brief_log.csv"

LOOKBACK_DAYS_PRICE = 360
LOOKBACK_DAYS_RATE = 100
MA_WINDOW = 50

# 이격도 기준
DISPARITY_STRONG = 100.0
DISPARITY_OVERHEAT = 120.0
DISPARITY_MANIA = 130.0
DISPARITY_WEAK = 95.0

# 고점/낙폭 윈도우 (거래일). LOOKBACK_DAYS_PRICE 범위 안에서 tail로 처리됨.
PEAK_WINDOW = 252

# 자산별 이격도 과열 임계값(백분위 보정, 2026-06 threshold_calibration.csv 기준).
# 과열=p90, 극단=p97. kind=stock이면 개별종목 낙폭 경고를 붙인다.
# 분포가 바뀌면 calibrate_thresholds.py로 재보정해 갱신한다.
DISPARITY_CALIB = {
    "KOSPI 50일 이격도":          {"overheat": 110, "extreme": 117, "kind": "index"},
    "KOSDAQ 50일 이격도":         {"overheat": 107, "extreme": 111, "kind": "index"},
    "S&P500 50일 이격도":         {"overheat": 105, "extreme": 107, "kind": "index"},
    "NASDAQ 50일 이격도":         {"overheat": 107, "extreme": 110, "kind": "index"},
    "SOX 반도체지수 50일 이격도":  {"overheat": 112, "extreme": 116, "kind": "index"},
    "삼성전자 50일 이격도":        {"overheat": 112, "extreme": 128, "kind": "stock"},
    "SK하이닉스 50일 이격도":      {"overheat": 120, "extreme": 138, "kind": "stock"},
}
DEFAULT_DISPARITY_CAL = {"overheat": DISPARITY_OVERHEAT, "extreme": DISPARITY_MANIA, "kind": "index"}

# 환율 기준
FX_IDEAL_LOW = 1400.0
FX_IDEAL_HIGH = 1440.0
FX_HIGH_WARNING = 1500.0
FX_SURGE_PCT = 1.0
FX_SURGE_KRW = 15.0

# 금리 기준
YIELD_CAUTION_BP = 5.0
YIELD_RISK_BP = 10.0

# VIX 기준
VIX_CAUTION = 20.0
VIX_FEAR = 30.0

# DXY 기준
DXY_SURGE_PCT = 0.5

# 하이일드 스프레드 기준
HY_SPREAD_CAUTION = 4.0
HY_SPREAD_RISK = 5.0
HY_SPREAD_SURGE_CAUTION_BP = 10.0
HY_SPREAD_SURGE_RISK_BP = 25.0

# 실질금리·채권 변동성 기준
REAL_YIELD_CAUTION_BP = 5.0
REAL_YIELD_RISK_BP = 10.0
MOVE_CAUTION = 120.0
MOVE_RISK = 150.0
MOVE_20D_SURGE_CAUTION = 10.0
MOVE_20D_SURGE_RISK = 20.0

# 환율 보조 지표 기준
CNH_SURGE_PCT = 0.4
JPY_SURGE_PCT = 0.5
WTI_SURGE_PCT = 2.0
WTI_HIGH_WARNING = 90.0

# 경기 시계 기준
CURVE_INVERSION = 0.0
CURVE_FLAT_CAUTION = 0.25

# Regime score log. Cloud Run Jobs에서는 로컬 파일이 영구 보존되지 않을 수 있으므로,
# 전일 대비 변화는 파일이 존재할 때만 표시한다.
REGIME_LOG_PATH = "daily_market_regime_log.csv"

# KRX 기술적 시장 확산도 계산은 비교적 무겁다. Cloud Run에서 시간이 오래 걸리면
# 환경변수 ENABLE_KRX_TECH_BREADTH=0 으로 끌 수 있다.
ENABLE_KRX_TECH_BREADTH = os.environ.get("ENABLE_KRX_TECH_BREADTH", "1") != "0"
KRX_TECH_BREADTH_MAX_TRADING_DAYS = int(os.environ.get("KRX_TECH_BREADTH_MAX_TRADING_DAYS", "260"))
KRX_TECH_BREADTH_MAX_SECONDS = int(os.environ.get("KRX_TECH_BREADTH_MAX_SECONDS", "150"))

# ── GCS 기반 기술적 확산도 캐시 (2번) ──────────────────────
# 무거운 전종목 50일선 계산은 별도 배치(RUN_MODE=breadth_cache)가 하루 1회 수행해
# GCS에 JSON으로 저장하고, 오전·오후 브리핑은 이 캐시를 '읽기만' 한다.
GCS_BUCKET = os.environ.get("GCS_BUCKET")
GCS_BREADTH_KEY = os.environ.get("GCS_BREADTH_KEY", "breadth_cache.json")
GCS_REGIME_LOG_KEY = os.environ.get("GCS_REGIME_LOG_KEY", "daily_market_regime_log.csv")
BREADTH_CACHE_MAX_AGE_DAYS = int(os.environ.get("BREADTH_CACHE_MAX_AGE_DAYS", "5"))
# 캐시가 없을 때 브리핑 실행 중 인라인 계산을 허용할지(기본 False: Cloud Run 시간초과 방지).
# 로컬 테스트에서만 1로 켠다.
BREADTH_COMPUTE_INLINE = os.environ.get("BREADTH_COMPUTE_INLINE", "0") == "1"
RUN_MODE = os.environ.get("RUN_MODE", "")


@dataclass
class IndicatorResult:
    name: str
    value_text: str
    date: str
    state: str
    comment: str
    change_text: str = ""
    source: str = ""
    ok: bool = True
    raw_value: Optional[float] = None
    signal_level: int = 0  # 0 정상, 1 관심, 2 경계, 3 위험
    mdd_line: str = ""  # 이격도 지표 전용: 고점대비 낙폭/MDD/지지선 한 줄

    def to_log_row(self) -> Dict[str, Any]:
        return {
            "run_date": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "indicator": self.name,
            "date": self.date,
            "value": self.raw_value,
            "value_text": self.value_text,
            "change_text": self.change_text,
            "state": self.state,
            "comment": self.comment,
            "source": self.source,
            "ok": self.ok,
            "signal_level": self.signal_level,
        }



@dataclass
class Signal:
    """지표의 의미를 방향·스트레스·타이밍으로 분리한 내부 신호."""

    direction: int = 0      # -2 Risk-Off, -1 약한 Risk-Off, 0 Neutral, +1 약한 Risk-On, +2 Risk-On
    stress: int = 0         # 0~3: 신용·변동성·환율 등 위험 스트레스
    timing: int = 0         # 0~3: 과열/침체 등 진입 타이밍 부담
    confidence: int = 100   # 0~100: 데이터 신뢰도


@dataclass
class LeadershipState:
    """한국 시장 주도주·시장 폭 상태."""

    date: str = ""
    breadth_ratio: Optional[float] = None              # 상승 종목 비율(%), 높을수록 시장 폭 양호
    top10_value_concentration: Optional[float] = None  # 전체 거래대금 중 상위 10개 비중(%)
    semiconductor_share_top10: Optional[float] = None  # 상위 10개 거래대금 중 반도체/IT 비중(%)
    kospi_kosdaq_gap: Optional[float] = None           # KOSPI 이격도 - KOSDAQ 이격도
    pct_above_20ma: Optional[float] = None             # 20일선 위 종목 비율(%)
    pct_above_50ma: Optional[float] = None             # 50일선 위 종목 비율(%)
    pct_above_200ma: Optional[float] = None            # 200일선 위 종목 비율(%)
    high_52w_count: Optional[int] = None               # 52주 신고가 종목 수(종가 기준 근사)
    low_52w_count: Optional[int] = None                # 52주 신저가 종목 수(종가 기준 근사)
    technical_breadth_note: str = ""
    leadership_score: int = 50                         # 0~100, 높을수록 쏠림 강함
    leadership_label: str = "자료 부족"
    key_points: Optional[List[str]] = None


@dataclass
class TimingScores:
    """공포-광기 기반의 진입/축소 타이밍 점수."""

    fear_buy_score: int
    mania_reduce_score: int
    final_timing: str
    fear_reasons: List[str]
    mania_reasons: List[str]
    warning: str = ""
    translation: str = ""


@dataclass
class MarketRegime:
    """메일 상단에 고정할 시장 국면 판정 결과."""

    risk_score: int
    overheating_score: int
    breadth_score: int
    fx_stress_score: int
    credit_score: int  # backward compatible: 실제 의미는 신용·변동성 안정도
    entry_score: int    # 신규 진입 매력도: 높을수록 신규 진입 부담이 낮음
    timing_scores: TimingScores
    final_label: str
    action_label: str
    headline: str
    one_liner: str
    core_question: str
    beginner_translation: str
    key_drivers: List[str]
    risks: List[str]


@dataclass
class RegimeChange:
    """이전 실행 대비 시장 국면 점수 변화."""

    previous_run_date: str
    risk_delta: int
    overheating_delta: int
    breadth_delta: int
    fx_stress_delta: int
    stability_delta: int
    entry_delta: int = 0
    summary: str = ""


# ─────────────────────────────────────────────────────────
# Common utilities
# ─────────────────────────────────────────────────────────
# GitHub Actions 러너는 UTC이므로, 한국 날짜/시각 표기는 반드시 KST 기준으로 계산
KST = dt.timezone(dt.timedelta(hours=9))

# 웹 대시보드(그래프로 보는 전체 지표) 공개 주소 — 메일 본문에 링크로 노출
DASHBOARD_URL = "https://woo-ook.github.io/market-brief-dashboard/"


def now_kst() -> dt.datetime:
    return dt.datetime.now(KST)


def today_kst_str() -> str:
    return now_kst().strftime("%Y-%m-%d")


@dataclass(frozen=True)
class BriefSession:
    """실행 시각(KST)에 따른 브리핑 세션 맥락.

    오전: 미국 정규장 마감 직후 / 한국장 개장 전 → '간밤 미국장'이 주연.
    오후: 한국 정규장 마감 직후 / 미국장 개장 전 → '오늘 한국장'이 주연.
    """

    key: str            # "morning" | "evening"
    label: str          # "오전 브리핑" | "오후 브리핑"
    primary_market: str # 방금 마감해 이번 브리핑의 주연이 되는 시장
    focus_block: str    # AI 프롬프트에 주입할 세션 관점 지시문


def current_session() -> BriefSession:
    """실행 시각(KST)을 기준으로 오전/오후 세션 맥락을 반환."""
    if now_kst().hour < 12:
        return BriefSession(
            key="morning",
            label="오전 브리핑",
            primary_market="미국",
            focus_block=(
                "[이번 브리핑 관점 — 오전: 미국장 마감 직후 / 한국장 개장 전]\n"
                "- 방금(한국시간 새벽) 마감한 '미국 정규장 결과'를 이번 브리핑의 주연으로 다루세요.\n"
                "  S&P500·NASDAQ·SOX·VIX·미국 금리·DXY의 '간밤 변화'를 가장 먼저, 가장 비중 있게 해석합니다.\n"
                "- 한국 지표(KOSPI·KOSDAQ·삼성전자 이격도, 거래대금 쏠림)는 '전일 종가 기준'이며 아직 갱신되지 않았습니다.\n"
                "  이를 '오늘의 움직임'으로 서술하지 말고, '오늘 한국장 개장 전 점검 포인트'로만 다루세요.\n"
                "- 마무리(→ 종합)는 반드시 '간밤 미국장 결과가 오늘 한국장 개장에 무엇을 시사하는가'로 연결하세요."
            ),
        )
    return BriefSession(
        key="evening",
        label="오후 브리핑",
        primary_market="한국",
        focus_block=(
            "[이번 브리핑 관점 — 오후: 한국장 마감 직후 / 미국장 개장 전]\n"
            "- 방금 마감한 '한국 정규장 결과'(당일 확정)를 이번 브리핑의 주연으로 다루세요.\n"
            "  KOSPI·KOSDAQ·삼성전자 이격도, 시장 폭, 거래대금 쏠림의 '오늘 변화'를 가장 먼저, 가장 비중 있게 해석합니다.\n"
            "- 미국 지표는 '어젯밤 마감 기준'이며, 오늘 밤 미국장 개장을 앞둔 전망 관점으로 다루세요.\n"
            "- 마무리(→ 종합)는 반드시 '오늘 한국장 결과가 오늘 밤 미국장·내일 한국장에 무엇을 시사하는가'로 연결하세요."
        ),
    )


def run_label_kst() -> str:
    """실행 시각(KST)에 따라 오전/오후 브리핑 라벨을 반환 (하위 호환용 래퍼)."""
    return current_session().label


def require_email_env() -> None:
    missing = []
    for key, value in {
        "SMTP_HOST": SMTP_HOST,
        "SMTP_USER": SMTP_USER,
        "SMTP_PASS": SMTP_PASS,
        "MAIL_TO": MAIL_TO,
    }.items():
        if not value:
            missing.append(key)

    if missing:
        raise RuntimeError(f"GitHub Secrets에 다음 값이 없습니다: {', '.join(missing)}")


def today_minus(days: int) -> str:
    return (dt.date.today() - dt.timedelta(days=days)).isoformat()


def safe_call(name: str, func: Callable[[], IndicatorResult]) -> IndicatorResult:
    try:
        return func()
    except Exception as e:
        return IndicatorResult(
            name=name,
            value_text="계산 실패",
            date=dt.date.today().isoformat(),
            state="오류",
            comment=str(e),
            ok=False,
            signal_level=3,
        )


def http_get_text(url: str, timeout: int = 60, retries: int = 3, sleep_sec: int = 3) -> str:
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            req = Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; daily-market-brief/3.0)",
                    "Accept": "text/plain, application/json, */*",
                },
            )
            with urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8")
        except Exception as e:
            last_error = e
            if attempt < retries:
                time.sleep(sleep_sec * attempt)

    raise RuntimeError(f"HTTP 요청 실패 after {retries} retries: {last_error}")


def to_float_series(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce").dropna()


# ─────────────────────────────────────────────────────────
# Price/index data
# ─────────────────────────────────────────────────────────
def get_price_series_fdr(symbol: str, start: str) -> pd.Series:
    import FinanceDataReader as fdr

    df = fdr.DataReader(symbol, start)
    if df is None or df.empty:
        raise RuntimeError(f"FinanceDataReader에서 빈 데이터 반환: {symbol}")

    if "Close" in df.columns:
        close = df["Close"]
    elif "종가" in df.columns:
        close = df["종가"]
    else:
        raise RuntimeError(f"종가 컬럼을 찾지 못했습니다: {symbol}, columns={list(df.columns)}")

    close = to_float_series(close)
    if close.empty:
        raise RuntimeError(f"유효한 종가 데이터가 없습니다: {symbol}")

    close.index = pd.to_datetime(close.index)
    return close.sort_index()


def get_price_series_yfinance(symbol: str, period: str = "18mo") -> pd.Series:
    import yfinance as yf

    df = yf.download(symbol, period=period, progress=False, auto_adjust=False, threads=False)
    if df is None or df.empty:
        raise RuntimeError(f"yfinance에서 빈 데이터 반환: {symbol}")

    if "Close" not in df.columns:
        raise RuntimeError(f"Close 컬럼을 찾지 못했습니다: {symbol}, columns={list(df.columns)}")

    close = df["Close"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]

    close = to_float_series(close)
    if close.empty:
        raise RuntimeError(f"유효한 종가 데이터가 없습니다: {symbol}")

    close.index = pd.to_datetime(close.index)
    return close.sort_index()


def get_price_series(
    fdr_symbols: Optional[List[str]] = None,
    yf_symbols: Optional[List[str]] = None,
) -> tuple[pd.Series, str]:
    start = today_minus(LOOKBACK_DAYS_PRICE)
    errors = []

    for symbol in fdr_symbols or []:
        try:
            return get_price_series_fdr(symbol, start), f"FinanceDataReader:{symbol}"
        except Exception as e:
            errors.append(f"FDR {symbol}: {e}")

    for symbol in yf_symbols or []:
        try:
            return get_price_series_yfinance(symbol), f"yfinance:{symbol}"
        except Exception as e:
            errors.append(f"yfinance {symbol}: {e}")

    raise RuntimeError("가격 데이터를 가져오지 못했습니다. " + " | ".join(errors))


# ─────────────────────────────────────────────────────────
# Interpretation rules
# ─────────────────────────────────────────────────────────
def interpret_disparity(
    disparity: float,
    overheat: float = DISPARITY_OVERHEAT,
    extreme: float = DISPARITY_MANIA,
) -> tuple[str, str, int]:
    if disparity >= extreme:
        return (
            "광기 / 셀 고려",
            f"이 자산의 보정 극단기준({extreme:.0f}) 이상입니다(자기 역사 상위 ~3%). 추세는 매우 강하지만 과열이 극단화된 구간이므로 일부 차익실현 또는 비중 조절을 고려할 수 있습니다.",
            3,
        )
    if disparity >= overheat:
        return (
            "과열",
            f"이 자산의 보정 과열기준({overheat:.0f}) 이상입니다(자기 역사 상위 ~10%). 강세가 매우 뚜렷하지만 단기 조정 위험이 커진 구간입니다.",
            2,
        )
    if disparity >= DISPARITY_STRONG:
        return (
            "강세",
            "50일선 위에 있습니다. 추세는 우호적이지만, 이격도가 커질수록 추격매수는 신중히 볼 필요가 있습니다.",
            1,
        )
    if disparity <= DISPARITY_WEAK:
        return (
            "약세 / 침체권",
            "50일선 아래로 의미 있게 이탈했습니다. 추세 약화 구간이며, 단기 반등 가능성과 추가 하락 위험을 함께 봐야 합니다.",
            2,
        )
    return (
        "중립 / 50일선 하회",
        "50일선 부근 또는 소폭 하회 구간입니다. 방향성이 명확하다고 보기는 어렵습니다.",
        0,
    )


def interpret_fx(level: float, pct_change: float, abs_change: float) -> tuple[str, str, int]:
    surge = pct_change >= FX_SURGE_PCT or abs_change >= FX_SURGE_KRW

    if level >= FX_HIGH_WARNING and surge:
        return (
            "고환율 + 급등 경고",
            f"원/달러가 {FX_HIGH_WARNING:,.0f}원 이상이면서 하루 변동도 큽니다. 원화 약세와 시장 불안 신호로 해석할 수 있습니다.",
            3,
        )
    if level >= FX_HIGH_WARNING:
        return (
            "고환율 경고",
            f"원/달러가 {FX_HIGH_WARNING:,.0f}원 이상입니다. 수입물가, 외국인 수급, 국내 위험자산에 부담으로 볼 수 있습니다.",
            3,
        )
    if surge:
        return (
            "환율 급등 경고",
            f"원/달러가 하루 {FX_SURGE_PCT:.1f}% 이상 또는 {FX_SURGE_KRW:.0f}원 이상 상승했습니다. 단기 위험회피 흐름을 점검해야 합니다.",
            2,
        )
    if FX_IDEAL_LOW <= level <= FX_IDEAL_HIGH:
        return (
            "환전 고려 구간",
            f"사용자가 선호한 {FX_IDEAL_LOW:,.0f}~{FX_IDEAL_HIGH:,.0f}원권입니다. 달러 환전 또는 분할 환전 검토 구간으로 볼 수 있습니다.",
            1,
        )
    if level > FX_IDEAL_HIGH:
        return (
            "높은 환율",
            f"선호 환율대인 {FX_IDEAL_LOW:,.0f}~{FX_IDEAL_HIGH:,.0f}원권보다 높습니다. 달러 환전은 분할 접근이 더 적절할 수 있습니다.",
            1,
        )
    return (
        "낮은 환율 / 우호적",
        f"선호 환율대인 {FX_IDEAL_LOW:,.0f}원보다 낮습니다. 달러 환전 관점에서는 상대적으로 유리한 구간일 수 있습니다.",
        1,
    )


def interpret_dxy(level: float, pct_change: float) -> tuple[str, str, int]:
    if pct_change >= DXY_SURGE_PCT:
        return (
            "달러 강세 경계",
            f"DXY가 하루 +{DXY_SURGE_PCT:.1f}% 이상 상승했습니다. 원/달러 상승이 원화만의 문제가 아니라 글로벌 달러 강세일 가능성을 함께 봐야 합니다.",
            2,
        )
    if pct_change <= -DXY_SURGE_PCT:
        return (
            "달러 약세",
            f"DXY가 하루 -{DXY_SURGE_PCT:.1f}% 이상 하락했습니다. 원/달러 하락 압력에는 우호적입니다.",
            1,
        )
    return (
        "중립",
        "달러인덱스의 일간 변화는 크지 않습니다. 원/달러 움직임이 DXY와 같은 방향인지 비교해볼 필요가 있습니다.",
        0,
    )


def interpret_yield(delta_bp: float, market: str) -> tuple[str, str, int]:
    if delta_bp >= YIELD_RISK_BP:
        return (
            "금리 급등 / 위험 신호",
            f"{market} 금리가 하루 +{YIELD_RISK_BP:.0f}bp 이상 급등했습니다. 성장주·장기자산 밸류에이션과 위험자산 선호에 부담입니다.",
            3,
        )
    if delta_bp >= YIELD_CAUTION_BP:
        return (
            "금리 상승 경계",
            f"{market} 금리가 하루 +{YIELD_CAUTION_BP:.0f}bp 이상 상승했습니다. 할인율 부담이 커지는 구간입니다.",
            2,
        )
    if delta_bp <= -YIELD_RISK_BP:
        return (
            "금리 급락",
            f"{market} 금리가 하루 -{YIELD_RISK_BP:.0f}bp 이상 하락했습니다. 할인율 부담은 완화되지만 경기 둔화 우려와 함께 해석해야 합니다.",
            2,
        )
    return (
        "중립",
        f"{market} 금리의 일간 변화는 제한적입니다.",
        0,
    )


def interpret_vix(vix: float) -> tuple[str, str, int]:
    if vix >= VIX_FEAR:
        return (
            "공포 확대",
            f"VIX가 {VIX_FEAR:.0f} 이상입니다. 시장 변동성 스트레스가 큰 구간이므로 리스크 관리가 우선입니다.",
            3,
        )
    if vix >= VIX_CAUTION:
        return (
            "경계",
            f"VIX가 {VIX_CAUTION:.0f} 이상입니다. 변동성 확대 가능성을 경계해야 합니다.",
            2,
        )
    return (
        "안정",
        f"VIX가 {VIX_CAUTION:.0f} 미만입니다. 변동성은 비교적 안정적인 구간입니다.",
        0,
    )


def interpret_hy_spread(level: float, delta_bp: float) -> tuple[str, str, int]:
    if level >= HY_SPREAD_RISK or delta_bp >= HY_SPREAD_SURGE_RISK_BP:
        return (
            "신용위험 확대 / 위험 신호",
            f"하이일드 스프레드가 {HY_SPREAD_RISK:.1f}% 이상이거나 하루 +{HY_SPREAD_SURGE_RISK_BP:.0f}bp 이상 확대되었습니다. 주식시장보다 신용시장이 먼저 경고를 보내는 구간일 수 있습니다.",
            3,
        )
    if level >= HY_SPREAD_CAUTION or delta_bp >= HY_SPREAD_SURGE_CAUTION_BP:
        return (
            "신용위험 경계",
            f"하이일드 스프레드가 {HY_SPREAD_CAUTION:.1f}% 이상이거나 하루 +{HY_SPREAD_SURGE_CAUTION_BP:.0f}bp 이상 확대되었습니다. 위험자산 선호 약화를 점검해야 합니다.",
            2,
        )
    return (
        "안정",
        "하이일드 스프레드는 아직 안정권입니다. 주식시장 변동이 신용위험으로 번지고 있는지는 제한적으로 보입니다.",
        0,
    )


# ─────────────────────────────────────────────────────────
# Indicator builders
# ─────────────────────────────────────────────────────────
def make_disparity_indicator(
    name: str,
    fdr_symbols: Optional[List[str]] = None,
    yf_symbols: Optional[List[str]] = None,
    unit: str = "",
) -> IndicatorResult:
    close, source = get_price_series(fdr_symbols, yf_symbols)

    df = pd.DataFrame({"Close": close})
    df["MA"] = df["Close"].rolling(MA_WINDOW).mean()
    df["Disparity"] = df["Close"] / df["MA"] * 100
    valid = df.dropna(subset=["Disparity"])

    if len(valid) < 2:
        raise RuntimeError(f"{name}: {MA_WINDOW}일 이격도 계산에 필요한 데이터가 부족합니다.")

    last = valid.iloc[-1]
    prev = valid.iloc[-2]
    date = valid.index[-1].strftime("%Y-%m-%d")
    change = float(last["Disparity"] - prev["Disparity"])

    # 자산별 보정 임계값 적용 (없으면 기본 120/130)
    cal = DISPARITY_CALIB.get(name, DEFAULT_DISPARITY_CAL)
    state, base_comment, signal = interpret_disparity(
        float(last["Disparity"]), cal["overheat"], cal["extreme"]
    )

    # 고점대비 낙폭 / 윈도우 MDD / 이그전식 조정 지지선 (-15%/-20%)
    close_s = df["Close"].dropna()
    window = close_s.tail(PEAK_WINDOW)
    peak = float(window.max())
    peak_date = window.idxmax()
    last_close = float(last["Close"])
    current_dd = (last_close / peak - 1.0) * 100.0
    mdd = float((window / window.cummax() - 1.0).min()) * 100.0
    support_15 = peak * 0.85
    support_20 = peak * 0.80
    mdd_line = (
        f"고점대비 {current_dd:+.1f}% (고점 {peak:,.0f}{unit}, {peak_date:%m-%d}) · "
        f"윈도우 MDD {mdd:.1f}% · 조정 지지선 -15% {support_15:,.0f}{unit} / -20% {support_20:,.0f}{unit}"
    )
    if cal["kind"] == "stock":
        mdd_line += " · ⚠개별종목은 역사적으로 30%+ 조정 사례도 있어 지지선은 낙관적일 수 있음"

    return IndicatorResult(
        name=name,
        value_text=f"{last['Disparity']:.2f}",
        date=date,
        change_text=f"{'+' if change >= 0 else ''}{change:.2f}p vs 전일",
        state=state,
        comment=f"{base_comment} 종가 {last['Close']:,.2f}{unit}, 50일선 {last['MA']:,.2f}{unit}.",
        source=source,
        raw_value=round(float(last["Disparity"]), 4),
        signal_level=signal,
        mdd_line=mdd_line,
    )


def make_level_indicator(
    name: str,
    fdr_symbols: Optional[List[str]],
    yf_symbols: Optional[List[str]],
    interpret_func: Callable[[float, float], tuple[str, str, int]],
    unit: str = "",
    pct_change_label: bool = True,
) -> IndicatorResult:
    close, source = get_price_series(fdr_symbols, yf_symbols)
    if len(close) < 2:
        raise RuntimeError(f"{name}: 데이터가 부족합니다.")

    last = float(close.iloc[-1])
    prev = float(close.iloc[-2])
    date = close.index[-1].strftime("%Y-%m-%d")
    pct = (last / prev - 1) * 100
    abs_change = last - prev
    state, comment, signal = interpret_func(last, pct)

    if pct_change_label:
        change_text = f"{'+' if abs_change >= 0 else ''}{abs_change:.2f}{unit} / {'+' if pct >= 0 else ''}{pct:.2f}% vs 전일"
    else:
        change_text = f"{'+' if abs_change >= 0 else ''}{abs_change:.2f}{unit} vs 전일"

    return IndicatorResult(
        name=name,
        value_text=f"{last:,.2f}{unit}",
        date=date,
        change_text=change_text,
        state=state,
        comment=comment,
        source=source,
        raw_value=round(last, 4),
        signal_level=signal,
    )


def make_fx_indicator() -> IndicatorResult:
    close, source = get_price_series(["USD/KRW"], ["KRW=X"])
    if len(close) < 2:
        raise RuntimeError("환율 데이터가 부족합니다.")

    last = float(close.iloc[-1])
    prev = float(close.iloc[-2])
    date = close.index[-1].strftime("%Y-%m-%d")
    abs_change = last - prev
    pct = (last / prev - 1) * 100
    state, comment, signal = interpret_fx(last, pct, abs_change)

    return IndicatorResult(
        name="원/달러 환율",
        value_text=f"{last:,.2f}원",
        date=date,
        change_text=f"{'+' if abs_change >= 0 else ''}{abs_change:.2f}원 / {'+' if pct >= 0 else ''}{pct:.2f}% vs 전일",
        state=state,
        comment=comment,
        source=source,
        raw_value=round(last, 4),
        signal_level=signal,
    )


# ─────────────────────────────────────────────────────────
# FRED
# ─────────────────────────────────────────────────────────
def get_fred_series(series_id: str, lookback_days: int = LOOKBACK_DAYS_RATE) -> pd.Series:
    start = (dt.date.today() - dt.timedelta(days=lookback_days)).strftime("%Y-%m-%d")

    if FRED_API_KEY:
        params = urlencode({
            "series_id": series_id,
            "api_key": FRED_API_KEY,
            "file_type": "json",
            "observation_start": start,
            "sort_order": "asc",
        })
        url = f"https://api.stlouisfed.org/fred/series/observations?{params}"
        data = json.loads(http_get_text(url, timeout=60, retries=3))
        observations = data.get("observations", [])
        values = []

        for row in observations:
            date_str = row.get("date")
            value_str = row.get("value")
            if not date_str or not value_str or value_str == ".":
                continue
            try:
                values.append((pd.Timestamp(date_str), float(value_str)))
            except ValueError:
                continue
    else:
        url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
        text = http_get_text(url, timeout=60, retries=3)
        rows = list(csv.DictReader(text.splitlines()))
        values = []
        cutoff = dt.date.today() - dt.timedelta(days=lookback_days * 3)

        for row in rows:
            date_str = row.get("observation_date")
            value_str = row.get(series_id)
            if not date_str or not value_str or value_str == ".":
                continue
            d = dt.datetime.strptime(date_str, "%Y-%m-%d").date()
            if d < cutoff:
                continue
            try:
                values.append((pd.Timestamp(d), float(value_str)))
            except ValueError:
                continue

    if not values:
        raise RuntimeError(f"FRED 유효 데이터가 없습니다: {series_id}")

    return pd.Series({d: v for d, v in values}).sort_index()



def make_us_yield_indicator(name: str, series_id: str) -> IndicatorResult:
    s = get_fred_series(series_id)
    if len(s) < 2:
        raise RuntimeError(f"{name}: 금리 데이터가 부족합니다.")

    last = float(s.iloc[-1])
    prev = float(s.iloc[-2])
    date = s.index[-1].strftime("%Y-%m-%d")
    delta_bp = (last - prev) * 100
    state, comment, signal = interpret_yield(delta_bp, name)

    return IndicatorResult(
        name=name,
        value_text=f"{last:.3f}%",
        date=date,
        change_text=f"{'+' if delta_bp >= 0 else ''}{delta_bp:.1f}bp vs 전일",
        state=state,
        comment=comment,
        source=f"FRED API:{series_id}" if FRED_API_KEY else f"FRED CSV:{series_id}",
        raw_value=round(last, 4),
        signal_level=signal,
    )


def interpret_breakeven(delta_bp: float) -> tuple[str, str, int]:
    if delta_bp >= 10:
        return ("기대인플레 급등", "미국 10년 기대인플레가 하루 +10bp 이상 상승했습니다. 명목금리 상승이 인플레 기대를 동반하는지 확인해야 합니다.", 2)
    if delta_bp >= 5:
        return ("기대인플레 상승 경계", "미국 10년 기대인플레가 하루 +5bp 이상 상승했습니다. 유가·물가 기대와 금리 상승 원인을 함께 봐야 합니다.", 1)
    if delta_bp <= -10:
        return ("기대인플레 급락", "미국 10년 기대인플레가 하루 -10bp 이상 하락했습니다. 경기 기대 약화 또는 위험회피와 함께 해석해야 합니다.", 2)
    return ("중립", "미국 10년 기대인플레의 일간 변화는 제한적입니다.", 0)


def make_breakeven_indicator(name: str, series_id: str) -> IndicatorResult:
    s = get_fred_series(series_id)
    if len(s) < 2:
        raise RuntimeError(f"{name}: 데이터가 부족합니다.")
    last = float(s.iloc[-1])
    prev = float(s.iloc[-2])
    date = s.index[-1].strftime("%Y-%m-%d")
    delta_bp = (last - prev) * 100
    state, comment, signal = interpret_breakeven(delta_bp)
    return IndicatorResult(
        name=name,
        value_text=f"{last:.3f}%",
        date=date,
        change_text=f"{'+' if delta_bp >= 0 else ''}{delta_bp:.1f}bp vs 전일",
        state=state,
        comment=comment,
        source=f"FRED API:{series_id}" if FRED_API_KEY else f"FRED CSV:{series_id}",
        raw_value=round(last, 4),
        signal_level=signal,
    )


def make_fred_spread_indicator(name: str, long_series_id: str, short_series_id: str) -> IndicatorResult:
    long_s = get_fred_series(long_series_id, lookback_days=LOOKBACK_DAYS_RATE)
    short_s = get_fred_series(short_series_id, lookback_days=LOOKBACK_DAYS_RATE)
    df = pd.concat([long_s.rename("long"), short_s.rename("short")], axis=1).dropna()
    if len(df) < 2:
        raise RuntimeError(f"{name}: 금리차 계산에 필요한 데이터가 부족합니다.")
    last = df.iloc[-1]
    prev = df.iloc[-2]
    spread = float(last["long"] - last["short"])
    prev_spread = float(prev["long"] - prev["short"])
    delta_bp = (spread - prev_spread) * 100
    date = df.index[-1].strftime("%Y-%m-%d")

    if spread < CURVE_INVERSION:
        state = "역전 / 경기 경고"
        comment = "장단기 금리차가 역전되어 경기 사이클 관점의 선행 경고가 켜진 상태입니다."
        signal = 2
    elif spread < CURVE_FLAT_CAUTION:
        state = "평탄화 경계"
        comment = "장단기 금리차가 낮아 통화정책 압박 또는 경기 둔화 우려를 계속 점검해야 합니다."
        signal = 1
    else:
        state = "정상"
        comment = "장단기 금리차가 플러스권입니다. 경기침체 선행 경고는 상대적으로 낮은 상태입니다."
        signal = 0

    return IndicatorResult(
        name=name,
        value_text=f"{spread:.2f}%p",
        date=date,
        change_text=f"{'+' if delta_bp >= 0 else ''}{delta_bp:.1f}bp vs 전일",
        state=state,
        comment=comment,
        source=f"FRED API:{long_series_id}-{short_series_id}" if FRED_API_KEY else f"FRED CSV:{long_series_id}-{short_series_id}",
        raw_value=round(spread, 4),
        signal_level=signal,
    )


def _extract_pct_from_change_text(change_text: str) -> Optional[float]:
    # 예: '+0.02 / +0.35% vs 전일' 또는 '+3.1bp vs 전일'
    m = re.search(r"([+-]?\d+(?:\.\d+)?)%", change_text or "")
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def make_yfinance_level_indicator(
    name: str,
    yf_symbols: List[str],
    interpret_func: Callable[[float, float], tuple[str, str, int]],
    unit: str = "",
    fdr_symbols: Optional[List[str]] = None,
) -> IndicatorResult:
    # FDR을 우선 시도한다(클라우드 프록시에서 yfinance/curl_cffi가 막히는 환경 대응).
    close, source = get_price_series(fdr_symbols=fdr_symbols or [], yf_symbols=yf_symbols)
    if len(close) < 2:
        raise RuntimeError(f"{name}: 데이터가 부족합니다.")
    last = float(close.iloc[-1])
    prev = float(close.iloc[-2])
    date = close.index[-1].strftime("%Y-%m-%d")
    pct = (last / prev - 1) * 100
    abs_change = last - prev
    state, comment, signal = interpret_func(last, pct)
    return IndicatorResult(
        name=name,
        value_text=f"{last:,.2f}{unit}",
        date=date,
        change_text=f"{'+' if abs_change >= 0 else ''}{abs_change:.2f}{unit} / {'+' if pct >= 0 else ''}{pct:.2f}% vs 전일",
        state=state,
        comment=comment,
        source=source,
        raw_value=round(last, 4),
        signal_level=signal,
    )


def interpret_move(level: float, pct_change: float, pct_20d: Optional[float] = None) -> tuple[str, str, int]:
    surge_20d = pct_20d is not None and pct_20d >= MOVE_20D_SURGE_RISK
    caution_20d = pct_20d is not None and pct_20d >= MOVE_20D_SURGE_CAUTION
    if level >= MOVE_RISK or surge_20d:
        return (
            "채권 변동성 위험",
            "MOVE가 위험권이거나 최근 20거래일 상승률이 큽니다. VIX가 안정적이어도 금리 민감 성장주에는 보이지 않는 스트레스가 남아 있을 수 있습니다.",
            3,
        )
    if level >= MOVE_CAUTION or caution_20d:
        return (
            "채권 변동성 경계",
            "채권시장 변동성이 높아지고 있습니다. 주식시장은 안정적으로 보여도 금리 스트레스가 성장주·반도체 추격에 부담이 될 수 있습니다.",
            2,
        )
    return (
        "채권 변동성 안정",
        "MOVE가 경계권 아래입니다. 채권시장 변동성 측면의 추가 스트레스는 제한적입니다.",
        0,
    )


def make_move_indicator() -> IndicatorResult:
    close, source = get_price_series(fdr_symbols=["^MOVE"], yf_symbols=["^MOVE", "MOVE"])
    if len(close) < 21:
        raise RuntimeError("MOVE 20일 변화율 계산에 필요한 데이터가 부족합니다.")
    last = float(close.iloc[-1])
    prev = float(close.iloc[-2])
    base20 = float(close.iloc[-21])
    date = close.index[-1].strftime("%Y-%m-%d")
    pct = (last / prev - 1) * 100
    pct20 = (last / base20 - 1) * 100
    state, comment, signal = interpret_move(last, pct, pct20)
    return IndicatorResult(
        name="MOVE 지수",
        value_text=f"{last:.2f}",
        date=date,
        change_text=f"{'+' if last - prev >= 0 else ''}{last - prev:.2f} / {'+' if pct >= 0 else ''}{pct:.2f}% vs 전일, 20일 {'+' if pct20 >= 0 else ''}{pct20:.1f}%",
        state=state,
        comment=comment,
        source=source,
        raw_value=round(last, 4),
        signal_level=signal,
    )


def interpret_usd_cnh(level: float, pct_change: float) -> tuple[str, str, int]:
    if pct_change >= CNH_SURGE_PCT:
        return ("위안화 약세 경계", "USD/CNH가 상승했습니다. 원/달러 상승이 글로벌 달러보다 중국·아시아 통화 약세와 연결되는지 확인해야 합니다.", 2)
    if pct_change <= -CNH_SURGE_PCT:
        return ("위안화 강세", "USD/CNH가 하락했습니다. 아시아 통화 압력은 완화되는 방향입니다.", 0)
    return ("중립", "USD/CNH의 일간 변화는 제한적입니다.", 0)


def interpret_usd_jpy(level: float, pct_change: float) -> tuple[str, str, int]:
    if pct_change >= JPY_SURGE_PCT:
        return ("엔화 약세 경계", "USD/JPY가 상승했습니다. 엔화 약세와 캐리 트레이드 환경이 아시아 통화 전반에 부담을 줄 수 있습니다.", 1)
    if pct_change <= -JPY_SURGE_PCT:
        return ("엔화 강세", "USD/JPY가 하락했습니다. 달러/엔 방향에서는 아시아 통화 부담이 완화되는 흐름입니다.", 0)
    return ("중립", "USD/JPY의 일간 변화는 제한적입니다.", 0)


def interpret_wti(level: float, pct_change: float) -> tuple[str, str, int]:
    if level >= WTI_HIGH_WARNING and pct_change >= WTI_SURGE_PCT:
        return ("유가 급등 부담", "WTI가 높은 레벨에서 급등했습니다. 수입물가·인플레·원화 부담을 함께 점검해야 합니다.", 2)
    if pct_change >= WTI_SURGE_PCT:
        return ("유가 상승 경계", "WTI가 하루 +2% 이상 상승했습니다. 원화와 물가 기대에 부담이 될 수 있습니다.", 1)
    if pct_change <= -WTI_SURGE_PCT:
        return ("유가 하락", "WTI가 하락했습니다. 수입물가와 인플레 기대 측면에서는 부담 완화 요인입니다.", 0)
    return ("중립", "WTI의 일간 변화는 제한적입니다.", 0)



def make_hy_spread_indicator() -> IndicatorResult:
    s = get_fred_series("BAMLH0A0HYM2", lookback_days=120)
    if len(s) < 2:
        raise RuntimeError("미국 하이일드 스프레드 데이터가 부족합니다.")

    last = float(s.iloc[-1])
    prev = float(s.iloc[-2])
    date = s.index[-1].strftime("%Y-%m-%d")
    delta_bp = (last - prev) * 100
    state, comment, signal = interpret_hy_spread(last, delta_bp)

    return IndicatorResult(
        name="미국 하이일드 스프레드",
        value_text=f"{last:.2f}%",
        date=date,
        change_text=f"{'+' if delta_bp >= 0 else ''}{delta_bp:.1f}bp vs 전일",
        state=state,
        comment=comment,
        source="FRED API:BAMLH0A0HYM2" if FRED_API_KEY else "FRED CSV:BAMLH0A0HYM2",
        raw_value=round(last, 4),
        signal_level=signal,
    )


# ─────────────────────────────────────────────────────────
# BOK ECOS for Korea 10Y
# ─────────────────────────────────────────────────────────
def get_bok_kr10y_item_code() -> str:
    if not BOK_API_KEY:
        raise RuntimeError("BOK_API_KEY가 없습니다.")

    url = f"https://ecos.bok.or.kr/api/StatisticItemList/{BOK_API_KEY}/json/kr/1/1000/817Y002"
    data = json.loads(http_get_text(url, timeout=60, retries=3))
    root = data.get("StatisticItemList")
    if not root:
        raise RuntimeError(f"ECOS StatisticItemList 응답 형식 오류: {data}")

    rows = root.get("row", [])
    for r in rows:
        code = str(r.get("ITEM_CODE", ""))
        name = " ".join(str(r.get(k, "")) for k in ["ITEM_NAME", "ITEM_NAME1", "ITEM_NAME2", "ITEM_NAME3", "ITEM_NAME4"])
        if "국고채" in name and "10" in name and "년" in name:
            return code

    # 일부 응답은 ITEM_NAME 대신 ITEM_NAME1만 사용합니다.
    for r in rows:
        code = str(r.get("ITEM_CODE", ""))
        all_text = " ".join(str(v) for v in r.values())
        if "국고채" in all_text and "10" in all_text and "년" in all_text:
            return code

    raise RuntimeError("ECOS StatisticItemList에서 국고채(10년) 항목 코드를 찾지 못했습니다.")


def make_kr10y_indicator_ecos() -> IndicatorResult:
    item_code = get_bok_kr10y_item_code()

    end = dt.date.today()
    start = end - dt.timedelta(days=120)
    start_s = start.strftime("%Y%m%d")
    end_s = end.strftime("%Y%m%d")

    item_code_encoded = quote(item_code, safe="")
    url = (
        f"https://ecos.bok.or.kr/api/StatisticSearch/"
        f"{BOK_API_KEY}/json/kr/1/1000/817Y002/D/{start_s}/{end_s}/{item_code_encoded}"
    )
    data = json.loads(http_get_text(url, timeout=60, retries=3))
    root = data.get("StatisticSearch")
    if not root:
        raise RuntimeError(f"ECOS StatisticSearch 응답 형식 오류: {data}")

    rows = root.get("row", [])
    values = []
    for r in rows:
        try:
            d = pd.Timestamp(str(r.get("TIME")))
            v = float(r.get("DATA_VALUE"))
            values.append((d, v))
        except Exception:
            continue

    if len(values) < 2:
        raise RuntimeError("ECOS에서 한국 10년물 금리 유효 데이터가 부족합니다.")

    values = sorted(values, key=lambda x: x[0])
    last_d, last_v = values[-1]
    prev_d, prev_v = values[-2]

    delta_bp = (last_v - prev_v) * 100
    state, comment, signal = interpret_yield(delta_bp, "한국 10년물")

    return IndicatorResult(
        name="한국 10년물 금리",
        value_text=f"{last_v:.3f}%",
        date=last_d.strftime("%Y-%m-%d"),
        change_text=f"{'+' if delta_bp >= 0 else ''}{delta_bp:.1f}bp vs 전일",
        state=state,
        comment=comment,
        source=f"BOK ECOS:817Y002/{item_code}",
        raw_value=round(last_v, 4),
        signal_level=signal,
    )


def make_kr10y_indicator_fred() -> IndicatorResult:
    s = get_fred_series("IRLTLT01KRM156N", lookback_days=900)
    if len(s) < 2:
        raise RuntimeError("FRED 한국 10년물 월간 데이터가 부족합니다.")

    last = float(s.iloc[-1])
    prev = float(s.iloc[-2])
    date = s.index[-1].strftime("%Y-%m-%d")
    delta_bp = (last - prev) * 100
    state, comment, signal = interpret_yield(delta_bp, "한국 10년물")

    return IndicatorResult(
        name="한국 10년물 금리",
        value_text=f"{last:.3f}%",
        date=date,
        change_text=f"{'+' if delta_bp >= 0 else ''}{delta_bp:.1f}bp vs 전월",
        state=f"{state} / 월간자료",
        comment=comment,
        source="FRED:IRLTLT01KRM156N",
        raw_value=round(last, 4),
        signal_level=signal,
    )


def make_kr10y_indicator() -> IndicatorResult:
    if BOK_API_KEY:
        try:
            return make_kr10y_indicator_ecos()
        except Exception as e:
            fallback = make_kr10y_indicator_fred()
            fallback.comment = f"ECOS 일별 조회 실패: {e} / 대체로 FRED-OECD 월간 자료를 사용했습니다. {fallback.comment}"
            return fallback

    fallback = make_kr10y_indicator_fred()
    fallback.comment = "BOK_API_KEY가 없어 FRED-OECD 월간 자료를 사용했습니다. " + fallback.comment
    return fallback


# ─────────────────────────────────────────────────────────
# 주도주(시장 주도 종목/섹터) 섹션
#  1) KRX 거래대금 상위 종목  : 자금이 몰리는 종목 확인
#  2) 외국인/기관 순매수 상위 : 주도 수급의 주체 확인
#  3) 시장 폭(상승/하락 종목 수): 주도 장세의 폭 확인
#  4) 미국 섹터 ETF 상대강도   : 미국 시장 주도 섹터 확인
# ─────────────────────────────────────────────────────────
LEADER_TOP_N = 10          # 거래대금 상위 종목 수
FLOW_TOP_N = 5             # 투자자별 순매수 상위 종목 수
INVESTOR_FLOW_LOOKBACK = int(os.environ.get("INVESTOR_FLOW_LOOKBACK", "3"))  # 투자자 데이터 날짜 폴백 일수
RS_WINDOW = 20             # 상대강도 계산 기간(거래일)

# 미국 S&P500 11개 섹터 SPDR ETF
US_SECTOR_ETFS = {
    "XLK": "기술(Technology)",
    "XLC": "커뮤니케이션",
    "XLY": "경기소비재",
    "XLP": "필수소비재",
    "XLV": "헬스케어",
    "XLF": "금융",
    "XLI": "산업재",
    "XLE": "에너지",
    "XLB": "소재",
    "XLRE": "리츠/부동산",
    "XLU": "유틸리티",
}


def get_krx_latest_business_day() -> str:
    """KRX 기준 가장 가까운 영업일(YYYYMMDD)."""
    from pykrx import stock

    return stock.get_nearest_business_day_in_a_week()


def get_krx_latest_data_date(max_lookback: int = 7) -> tuple[str, bool]:
    """
    실제로 데이터가 존재하는 가장 최근 영업일을 찾는다.
    FinanceDataReader 기반 (pykrx보다 안정적).

    Returns:
        (YYYYMMDD 문자열, 당일 데이터 여부)
    """
    import FinanceDataReader as fdr

    today_kst_dt = now_kst().date()
    today_str = today_kst_dt.strftime("%Y%m%d")
    target_dt = today_kst_dt
    attempts = []

    for _ in range(max_lookback):
        # 주말 건너뛰기
        while target_dt.weekday() >= 5:
            target_dt -= dt.timedelta(days=1)
        target = target_dt.strftime("%Y%m%d")

        try:
            # KOSPI 지수로 영업일 여부 확인 (가장 가볍고 안정적)
            df = fdr.DataReader("KS11", target, target)
            if df is not None and not df.empty:
                return target, (target == today_str)
            attempts.append(f"{target}: 빈 데이터")
        except Exception as e:
            attempts.append(f"{target}: {type(e).__name__}:{e}")

        target_dt -= dt.timedelta(days=1)

    raise RuntimeError(
        f"최근 {max_lookback}일 내 KRX 영업일 데이터를 찾을 수 없습니다. "
        f"시도 내역: {' / '.join(attempts)}"
    )


def _fetch_krx_snapshot(date: str) -> pd.DataFrame:
    """
    KRX 전체(KOSPI+KOSDAQ) 종목의 당일 스냅샷.
    FinanceDataReader의 StockListing을 이용.
    종가/등락률/거래대금 컬럼이 포함된 DataFrame 반환.
    """
    import FinanceDataReader as fdr

    # KRX 전체 상장 종목 리스트 + 당일 스냅샷
    # 'KRX' 키는 KOSPI+KOSDAQ+KONEX 전종목의 스냅샷 가격 데이터를 포함
    df = fdr.StockListing("KRX")
    if df is None or df.empty:
        raise RuntimeError(f"FDR KRX StockListing 빈 응답")

    # 컬럼 정규화 (FDR 버전에 따라 컬럼명이 다를 수 있음)
    rename_map = {
        "Code": "ticker", "Symbol": "ticker",
        "Name": "name",
        "Close": "close",
        "Changes": "change", "ChangesRatio": "change_pct", "ChagesRatio": "change_pct",
        "Volume": "volume",
        "Amount": "value",
        "Marcap": "marcap",
    }
    df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})

    required = ["ticker", "name", "close", "value"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise RuntimeError(f"FDR StockListing 컬럼 누락: {missing} / 실제 컬럼: {list(df.columns)}")

    # 숫자형 변환
    for col in ["close", "change_pct", "volume", "value", "marcap"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # 거래대금 유효 종목만
    df = df[df["value"].fillna(0) > 0].copy()
    if df.empty:
        raise RuntimeError("거래대금 > 0 종목이 없음")

    return df


def build_krx_leaders_lines() -> List[str]:
    """KRX 전체 시장 거래대금 상위 종목 + 시장 폭(상승/하락 종목 수). FDR 기반."""
    date, is_today = get_krx_latest_data_date()
    df = _fetch_krx_snapshot(date)

    # 시장 폭
    if "change_pct" in df.columns:
        chg = df["change_pct"].dropna()
        n_up = int((chg > 0).sum())
        n_down = int((chg < 0).sum())
        n_flat = int((chg == 0).sum())
    else:
        n_up = n_down = n_flat = 0
    breadth_ratio = n_up / max(n_up + n_down, 1) * 100

    lines = []
    date_fmt = f"{date[:4]}-{date[4:6]}-{date[6:]}"
    if is_today:
        lines.append(f"기준일: {date_fmt} (당일 확정 데이터)")
    else:
        lines.append(f"기준일: {date_fmt} (전영업일 확정 — 당일 데이터 미확정으로 폴백)")
    lines.append("")
    lines.append(
        f"■ 시장 폭: 상승 {n_up:,} / 하락 {n_down:,} / 보합 {n_flat:,} 종목 "
        f"(상승 비율 {breadth_ratio:.1f}%)"
    )
    if n_up + n_down > 0:
        if breadth_ratio >= 60:
            lines.append("  → 상승 종목이 다수입니다. 주도주의 폭이 넓은 장세로 볼 수 있습니다.")
        elif breadth_ratio <= 40:
            lines.append("  → 하락 종목이 다수입니다. 지수가 올랐다면 소수 대형주 주도의 좁은 장세를 의심해야 합니다.")
        else:
            lines.append("  → 상승/하락이 혼재합니다. 종목 장세 가능성을 함께 봐야 합니다.")
    lines.append("")
    lines.append(f"■ 거래대금 상위 {LEADER_TOP_N}종목 (자금이 몰리는 종목)")

    top = df.sort_values("value", ascending=False).head(LEADER_TOP_N)
    for _, row in top.iterrows():
        ticker = str(row.get("ticker", ""))
        name = str(row.get("name", ticker))
        close = float(row["close"]) if pd.notna(row["close"]) else 0
        rate = float(row.get("change_pct", float("nan")))
        value = float(row["value"]) / 1e8  # 억원
        rate_str = f"{'+' if rate >= 0 else ''}{rate:.2f}%" if pd.notna(rate) else "N/A"
        lines.append(
            f"  - {name}({ticker}): 종가 {close:,.0f}원, {rate_str}, 거래대금 {value:,.0f}억원"
        )

    return lines

    # 시장 폭 (등락률 기준)
    return lines


def _try_pykrx_net_purchases(date: str, market: str, inv_key: str):
    """pykrx 투자자별 순매수 DataFrame 반환. 실패 시 (None, 사유문자열).

    과거 except:pass가 원인을 통째로 삼켜 진단이 불가능했던 문제를 해결하기 위해,
    실패 단계를 import / API부재 / 호출예외 / 결과없음 / 컬럼불일치로 구분해 돌려준다.
    """
    try:
        from pykrx import stock as _krx
    except Exception as e:
        return None, f"import 실패: {type(e).__name__}: {e}"
    fn = getattr(_krx, "get_market_net_purchases_of_equities", None)
    if fn is None:
        return None, "API 없음(get_market_net_purchases_of_equities) — pykrx 버전 불일치"
    try:
        df = fn(date, date, market, inv_key)
    except Exception as e:
        return None, f"{inv_key} 조회 예외: {type(e).__name__}: {e}"
    if df is None or len(df) == 0:
        return None, f"{inv_key} 결과 비어 있음(date={date}) — KRX 인증/미확정/네트워크 의심"
    if "순매수거래대금" not in getattr(df, "columns", []):
        return None, f"{inv_key} 컬럼 불일치: {list(df.columns)[:6]}"
    return df, ""


def _investor_flow_hint(reasons: List[str]) -> str:
    """실패 사유 모음을 보고 사용자 메일에 넣을 한 줄 원인 힌트를 만든다."""
    blob = " ".join(reasons)
    if "import 실패" in blob:
        return "원인 추정: pykrx 미설치 — requirements.txt 확인 필요"
    if "API 없음" in blob:
        return "원인 추정: pykrx 버전 불일치 — 버전 고정 필요"
    if "비어 있음" in blob:
        if not (os.environ.get("KRX_ID") and os.environ.get("KRX_PW")):
            return "원인 추정: KRX 인증 미설정(KRX_ID/KRX_PW) 또는 해당 일자 데이터 미확정"
        return "원인 추정: 해당 일자 투자자 데이터 미확정 또는 KRX 응답 지연"
    return ""


def build_krx_investor_flow_lines() -> List[str]:
    """투자자별 순매수·순매도 상위 종목.

    개선점:
    - 실패 원인을 stderr에 로깅(과거 except:pass로 원인이 보이지 않던 문제 해결)
    - 투자자 데이터는 지수보다 확정이 늦을 수 있어 날짜를 며칠 폴백
    - 외국인/기관/개인 부분 성공 허용(일부만 돼도 표시)
    - 순매수뿐 아니라 순매도 상위도 표시(매도 주체·이탈 종목 파악)
    - 모두 실패 시 시가총액 상위로 대체하되 원인 힌트를 함께 노출
    """
    date, is_today = get_krx_latest_data_date()
    lines: List[str] = []
    date_fmt = f"{date[:4]}-{date[4:6]}-{date[6:]}"
    note = "당일 확정" if is_today else "전영업일 확정"
    lines.append(f"기준일: {date_fmt} ({note})")
    lines.append("")

    has_krx_cred = bool(os.environ.get("KRX_ID") and os.environ.get("KRX_PW"))
    print(f"[investor flow] start date={date} krx_cred={'set' if has_krx_cred else 'unset'}", file=sys.stderr)

    # 날짜 폴백 후보(영업일만)
    candidate_dates: List[str] = []
    d = dt.datetime.strptime(date, "%Y%m%d").date()
    while len(candidate_dates) < INVESTOR_FLOW_LOOKBACK:
        if d.weekday() < 5:
            candidate_dates.append(d.strftime("%Y%m%d"))
        d -= dt.timedelta(days=1)

    investors = [("외국인", "외국인"), ("기관합계", "기관"), ("개인", "개인")]
    reasons: List[str] = []
    success_count = 0
    used_date = None

    for cand in candidate_dates:
        block: List[str] = []
        local_success = 0
        for inv_key, label in investors:
            df, reason = _try_pykrx_net_purchases(cand, "KOSPI", inv_key)
            if df is None:
                reasons.append(reason)
                continue
            df_sorted = df.sort_values("순매수거래대금", ascending=False)
            buys = df_sorted.head(FLOW_TOP_N)
            block.append(f"■ {label} 순매수 상위 {FLOW_TOP_N} (KOSPI)")
            for ticker, row in buys.iterrows():
                name = str(row.get("종목명", ticker))
                net = float(row["순매수거래대금"]) / 1e8
                block.append(f"  - {name}({ticker}): +{net:,.0f}억원")
            # 순매도(가장 음수) 상위 — 외국인 매도폭탄 등 이탈 주체 파악
            sells = df_sorted.tail(FLOW_TOP_N).iloc[::-1]
            if float(sells["순매수거래대금"].min()) < 0:
                block.append(f"■ {label} 순매도 상위 {FLOW_TOP_N} (KOSPI)")
                for ticker, row in sells.iterrows():
                    net = float(row["순매수거래대금"]) / 1e8
                    if net >= 0:
                        continue
                    name = str(row.get("종목명", ticker))
                    block.append(f"  - {name}({ticker}): {net:,.0f}억원")
            block.append("")
            local_success += 1
        if local_success > 0:
            used_date = cand
            success_count = local_success
            lines.extend(block)
            break

    if success_count == 0:
        print(f"[investor flow] 전 시도 실패. 사유: {reasons[:6]}", file=sys.stderr)
        lines.append("■ 투자자별 순매수 데이터를 가져오지 못했습니다.")
        hint = _investor_flow_hint(reasons)
        if hint:
            lines.append(f"  ({hint})")
        lines.append("  (거래대금 상위·시가총액 상위 종목을 함께 참고하세요)")
        lines.append("")
        try:
            df = _fetch_krx_snapshot(date)
            if "marcap" in df.columns and df["marcap"].notna().any():
                top_mc = df.sort_values("marcap", ascending=False).head(FLOW_TOP_N)
                lines.append(f"■ 시가총액 상위 {FLOW_TOP_N}종목")
                for _, row in top_mc.iterrows():
                    ticker = str(row.get("ticker", ""))
                    name = str(row.get("name", ticker))
                    mc = float(row["marcap"]) / 1e12  # 조원
                    close = float(row.get("close", 0))
                    rate = row.get("change_pct", float("nan"))
                    rate_str = f", {'+' if rate >= 0 else ''}{rate:.2f}%" if pd.notna(rate) else ""
                    lines.append(
                        f"  - {name}({ticker}): 시총 {mc:,.1f}조원, "
                        f"종가 {close:,.0f}원{rate_str}"
                    )
                lines.append("")
        except Exception as e:
            print(f"[investor flow] 시총 폴백도 실패: {type(e).__name__}: {e}", file=sys.stderr)
    else:
        if used_date and used_date != date:
            print(f"[investor flow] 날짜 폴백 사용: {used_date} (요청 {date})", file=sys.stderr)
        print(f"[investor flow] 성공 investors={success_count}", file=sys.stderr)

    if lines and lines[-1] == "":
        lines.pop()
    return lines


def build_us_sector_rs_lines() -> List[str]:
    """미국 섹터 ETF의 SPY 대비 20일 상대강도 (주도 섹터 확인)."""
    import yfinance as yf

    tickers = list(US_SECTOR_ETFS.keys()) + ["SPY"]
    df = yf.download(tickers, period="3mo", progress=False, auto_adjust=True, threads=False)
    if df is None or df.empty:
        raise RuntimeError("yfinance 섹터 ETF 데이터가 비어 있습니다.")

    close = df["Close"] if "Close" in df.columns else df
    if isinstance(close, pd.Series):
        raise RuntimeError("섹터 ETF 데이터 형식이 예상과 다릅니다.")

    close = close.dropna(how="all")
    if len(close) < RS_WINDOW + 1:
        raise RuntimeError(f"섹터 RS 계산에 필요한 {RS_WINDOW + 1}일 데이터가 부족합니다.")

    last = close.iloc[-1]
    base = close.iloc[-(RS_WINDOW + 1)]
    ret = (last / base - 1) * 100  # 20일 수익률(%)

    if "SPY" not in ret.index or pd.isna(ret["SPY"]):
        raise RuntimeError("SPY 기준 수익률을 계산하지 못했습니다.")

    spy_ret = float(ret["SPY"])
    rel = (ret.drop(labels=["SPY"]) - spy_ret).dropna().sort_values(ascending=False)

    date = close.index[-1].strftime("%Y-%m-%d")
    lines = []
    lines.append(f"기준일: {date} / 최근 {RS_WINDOW}거래일 수익률, S&P500(SPY {spy_ret:+.2f}%) 대비 초과수익")
    lines.append("")
    lines.append("■ 주도 섹터 상위 3 (상대강도 +)")
    for sym in rel.head(3).index:
        lines.append(f"  - {US_SECTOR_ETFS.get(sym, sym)}({sym}): {ret[sym]:+.2f}% (vs SPY {rel[sym]:+.2f}%p)")
    lines.append("")
    lines.append("■ 소외 섹터 하위 3 (상대강도 -)")
    for sym in rel.tail(3).index[::-1]:
        lines.append(f"  - {US_SECTOR_ETFS.get(sym, sym)}({sym}): {ret[sym]:+.2f}% (vs SPY {rel[sym]:+.2f}%p)")
    lines.append("")
    lines.append("  → 상대강도가 지속적으로 양(+)인 섹터가 현재 미국 시장의 주도 섹터입니다.")

    return lines


def _is_semiconductor_or_it(name: str, ticker: str = "") -> bool:
    """거래대금 상위 종목의 반도체/IT 쏠림을 거칠게 판별한다.

    KRX 스냅샷에는 표준 업종 컬럼이 항상 포함되지 않으므로 종목명 기반 휴리스틱을 사용한다.
    정확한 산업분류가 필요한 경우 KRX 업종 코드 또는 WICS/GICS 매핑 테이블을 별도로 붙이면 된다.
    """
    text = f"{name} {ticker}".lower()
    keywords = [
        "삼성전자", "sk하이닉스", "하이닉스", "반도체", "전자", "전기", "테크",
        "테크놀로지", "it", "소프트웨어", "시스템", "솔루션", "이닉스", "한미반도체",
        "리노공업", "이수페타시스", "심텍", "하나마이크론", "원익", "db하이텍",
        "두산테스나", "에스앤에스텍", "동진쎄미켐", "주성엔지니어링", "hpsp",
    ]
    return any(k.lower() in text for k in keywords)



def _gcs_bucket():
    """google-cloud-storage 버킷 핸들을 반환. 미설정/실패 시 None."""
    if not GCS_BUCKET:
        return None
    try:
        from google.cloud import storage
        return storage.Client().bucket(GCS_BUCKET)
    except Exception as e:
        print(f"[gcs] 클라이언트 초기화 실패: {type(e).__name__}: {e}", file=sys.stderr)
        return None


def _gcs_download_text(key: str) -> Optional[str]:
    """GCS 객체를 텍스트로 다운로드. 미설정/없음/실패 시 None."""
    bucket = _gcs_bucket()
    if bucket is None:
        return None
    try:
        blob = bucket.blob(key)
        if not blob.exists():
            return None
        return blob.download_as_text()
    except Exception as e:
        print(f"[gcs] {key} 다운로드 실패: {type(e).__name__}: {e}", file=sys.stderr)
        return None


def _gcs_upload_text(key: str, text: str, content_type: str = "text/csv") -> bool:
    """텍스트를 GCS 객체로 업로드."""
    bucket = _gcs_bucket()
    if bucket is None:
        return False
    try:
        bucket.blob(key).upload_from_string(text, content_type=content_type)
        return True
    except Exception as e:
        print(f"[gcs] {key} 업로드 실패: {type(e).__name__}: {e}", file=sys.stderr)
        return False


def _breadth_cache_read() -> Dict[str, Any]:
    """GCS에서 기술적 확산도 캐시를 읽어 dict로 반환. 없거나 너무 오래되면 {}."""
    bucket = _gcs_bucket()
    if bucket is None:
        return {}
    try:
        blob = bucket.blob(GCS_BREADTH_KEY)
        if not blob.exists():
            return {}
        payload = json.loads(blob.download_as_text())
    except Exception as e:
        print(f"[gcs] 캐시 읽기 실패: {type(e).__name__}: {e}", file=sys.stderr)
        return {}

    cached_date = payload.get("as_of_date")
    if cached_date:
        try:
            age = (dt.date.today() - dt.datetime.strptime(cached_date, "%Y%m%d").date()).days
            if age > BREADTH_CACHE_MAX_AGE_DAYS:
                print(f"[gcs] 캐시가 오래됨({cached_date}, {age}일 경과) — 폴백", file=sys.stderr)
                return {}
        except Exception:
            pass
    payload["note"] = f"GCS 캐시({cached_date}) 사용"
    return payload


def _breadth_cache_write(data: Dict[str, Any]) -> bool:
    """기술적 확산도 계산 결과를 GCS에 JSON으로 저장."""
    bucket = _gcs_bucket()
    if bucket is None:
        print("[gcs] GCS_BUCKET 미설정 — 캐시 저장 생략", file=sys.stderr)
        return False
    try:
        blob = bucket.blob(GCS_BREADTH_KEY)
        blob.upload_from_string(
            json.dumps(data, ensure_ascii=False),
            content_type="application/json",
        )
        return True
    except Exception as e:
        print(f"[gcs] 캐시 저장 실패: {type(e).__name__}: {e}", file=sys.stderr)
        return False


def build_breadth_cache() -> Dict[str, Any]:
    """배치 진입점: 전종목 기술적 확산도를 계산해 GCS 캐시에 저장한다.

    Cloud Run Job에서 RUN_MODE=breadth_cache 또는 인자 'breadth-cache'로 실행한다.
    브리핑과 분리돼 있어 계산이 오래 걸려도 메일 발송에 영향을 주지 않는다.
    """
    latest_date, _is_today = get_krx_latest_data_date()
    data = _compute_krx_technical_breadth_full(latest_date)
    data["as_of_date"] = latest_date
    data["computed_at"] = now_kst().isoformat()
    ok = _breadth_cache_write(data)
    print(f"[breadth-cache] date={latest_date} ok={ok} keys={list(data.keys())}")
    return data


def _compute_krx_technical_breadth(latest_date: str) -> Dict[str, Any]:
    """기술적 확산도(50일선 위 종목 비율 등)를 GCS 캐시에서 읽는다(브리핑용).

    브리핑 실행 중에는 무거운 전종목 계산을 하지 않는다(Cloud Run 시간초과 방지).
    캐시가 없거나 오래되면 빈 dict를 반환하고, 상위 로직은 상승종목 비율 폴백으로 동작한다.
    """
    if not ENABLE_KRX_TECH_BREADTH:
        return {"note": "기술적 시장 확산도 계산 비활성화"}

    cached = _breadth_cache_read()
    if cached:
        return cached

    if BREADTH_COMPUTE_INLINE:
        return _compute_krx_technical_breadth_full(latest_date)

    return {"note": "기술적 확산도 캐시 없음(배치 미수행) — 상승종목 비율로 폴백"}


def _compute_krx_technical_breadth_full(latest_date: str) -> Dict[str, Any]:
    """KRX 종목의 20/50/200일선 위 비율과 52주 신고가/신저가 수를 계산한다.

    주의: KRX 전체 종목의 과거 종가를 날짜별로 수집하므로 실행 시간이 늘어날 수 있다.
    Cloud Run에서 과도하게 느리면 ENABLE_KRX_TECH_BREADTH=0으로 비활성화한다.
    """
    if not ENABLE_KRX_TECH_BREADTH:
        return {"note": "기술적 시장 확산도 계산 비활성화"}

    try:
        from pykrx import stock as _krx
    except Exception as e:
        return {"note": f"pykrx 사용 불가: {type(e).__name__}"}

    start_time = time.time()
    latest_dt = dt.datetime.strptime(latest_date, "%Y%m%d").date()
    frames: List[pd.Series] = []
    current = latest_dt
    checked = 0

    while len(frames) < KRX_TECH_BREADTH_MAX_TRADING_DAYS and checked < 430:
        if time.time() - start_time > KRX_TECH_BREADTH_MAX_SECONDS:
            break
        if current.weekday() < 5:
            date_s = current.strftime("%Y%m%d")
            try:
                daily = _krx.get_market_ohlcv_by_ticker(date_s, market="ALL")
                if daily is not None and not daily.empty and "종가" in daily.columns:
                    close = pd.to_numeric(daily["종가"], errors="coerce")
                    close.name = pd.Timestamp(current)
                    close = close[close > 0]
                    if len(close) > 100:
                        frames.append(close)
            except Exception:
                pass
        current -= dt.timedelta(days=1)
        checked += 1

    if len(frames) < 60:
        return {"note": f"기술적 시장 확산도 데이터 부족({len(frames)}거래일)"}

    close_df = pd.DataFrame(frames).sort_index()
    latest = close_df.iloc[-1]
    result: Dict[str, Any] = {"note": f"{len(close_df)}거래일 기준"}

    for window, key in [(20, "pct_above_20ma"), (50, "pct_above_50ma"), (200, "pct_above_200ma")]:
        if len(close_df) >= window:
            ma = close_df.tail(window).mean(skipna=True)
            valid = latest.notna() & ma.notna() & (ma > 0)
            if valid.sum() > 0:
                result[key] = float((latest[valid] > ma[valid]).mean() * 100)

    # 52주 고저는 확보된 종가 기준 근사치다. 장중 고가/저가 기준과는 다를 수 있다.
    if len(close_df) >= 200:
        window_df = close_df.tail(min(252, len(close_df)))
        high = window_df.max(skipna=True)
        low = window_df.min(skipna=True)
        valid_h = latest.notna() & high.notna() & (high > 0)
        valid_l = latest.notna() & low.notna() & (low > 0)
        result["high_52w_count"] = int((latest[valid_h] >= high[valid_h] * 0.999).sum())
        result["low_52w_count"] = int((latest[valid_l] <= low[valid_l] * 1.001).sum())

    return result

def _analyze_krx_leadership(date: str, is_today: bool, df: pd.DataFrame) -> tuple[List[str], LeadershipState]:
    """KRX 시장 폭·거래대금 쏠림을 계산하고 설명 라인을 생성."""
    lines: List[str] = []
    date_fmt = f"{date[:4]}-{date[4:6]}-{date[6:]}"
    if is_today:
        lines.append(f"기준일: {date_fmt} (당일 확정 데이터)")
    else:
        lines.append(f"기준일: {date_fmt} (전영업일 확정 — 당일 데이터 미확정으로 폴백)")
    lines.append("")

    n_up = n_down = n_flat = 0
    breadth_ratio = None
    if "change_pct" in df.columns:
        chg = df["change_pct"].dropna()
        n_up = int((chg > 0).sum())
        n_down = int((chg < 0).sum())
        n_flat = int((chg == 0).sum())
        if n_up + n_down > 0:
            breadth_ratio = n_up / (n_up + n_down) * 100

    total_value = float(df["value"].sum()) if "value" in df.columns else 0.0
    top = df.sort_values("value", ascending=False).head(LEADER_TOP_N).copy()
    top10_value = float(top["value"].sum()) if not top.empty else 0.0
    top10_conc = top10_value / total_value * 100 if total_value > 0 else None

    if not top.empty:
        top["is_semi_it"] = top.apply(
            lambda r: _is_semiconductor_or_it(str(r.get("name", "")), str(r.get("ticker", ""))), axis=1
        )
        semi_value = float(top.loc[top["is_semi_it"], "value"].sum())
        semi_share = semi_value / top10_value * 100 if top10_value > 0 else None
        semi_count = int(top["is_semi_it"].sum())
    else:
        semi_share = None
        semi_count = 0

    # 쏠림 점수: 높을수록 소수 종목/반도체·IT에 거래대금이 몰린 상태
    leadership_score = 50
    key_points: List[str] = []

    tech = _compute_krx_technical_breadth(date)
    pct_above_20ma = tech.get("pct_above_20ma")
    pct_above_50ma = tech.get("pct_above_50ma")
    pct_above_200ma = tech.get("pct_above_200ma")
    high_52w_count = tech.get("high_52w_count")
    low_52w_count = tech.get("low_52w_count")
    technical_breadth_note = str(tech.get("note", ""))

    if top10_conc is not None:
        if top10_conc >= 35:
            leadership_score += 20
            key_points.append(f"상위 10개 거래대금 비중 {top10_conc:.1f}%로 쏠림 강함")
        elif top10_conc >= 25:
            leadership_score += 10
            key_points.append(f"상위 10개 거래대금 비중 {top10_conc:.1f}%로 쏠림 존재")
        elif top10_conc <= 15:
            leadership_score -= 10
            key_points.append(f"상위 10개 거래대금 비중 {top10_conc:.1f}%로 거래 확산 양호")

    if semi_share is not None:
        if semi_share >= 55:
            leadership_score += 20
            key_points.append(f"거래대금 상위 10개 중 반도체/IT 비중 {semi_share:.1f}%")
        elif semi_share >= 35:
            leadership_score += 10
            key_points.append(f"반도체/IT 거래대금 비중 {semi_share:.1f}%")

    if breadth_ratio is not None:
        if breadth_ratio <= 40:
            leadership_score += 20
            key_points.append(f"상승 종목 비율 {breadth_ratio:.1f}%로 시장 폭 약함")
        elif breadth_ratio < 50:
            leadership_score += 10
            key_points.append(f"상승 종목 비율 {breadth_ratio:.1f}%로 시장 폭 제한적")
        elif breadth_ratio >= 60:
            leadership_score -= 15
            key_points.append(f"상승 종목 비율 {breadth_ratio:.1f}%로 시장 폭 양호")

    if pct_above_50ma is not None:
        if pct_above_50ma < 40:
            leadership_score += 15
            key_points.append(f"50일선 위 종목 비율 {pct_above_50ma:.1f}%로 중기 체력 약함")
        elif pct_above_50ma >= 60:
            leadership_score -= 10
            key_points.append(f"50일선 위 종목 비율 {pct_above_50ma:.1f}%로 중기 체력 양호")

    if low_52w_count is not None and high_52w_count is not None:
        if low_52w_count > high_52w_count * 2 and low_52w_count >= 30:
            leadership_score += 10
            key_points.append(f"52주 신저가 {low_52w_count}개 > 신고가 {high_52w_count}개")
        elif high_52w_count > low_52w_count * 2 and high_52w_count >= 30:
            leadership_score -= 5
            key_points.append(f"52주 신고가 {high_52w_count}개 > 신저가 {low_52w_count}개")

    leadership_score = max(0, min(100, int(round(leadership_score))))
    if leadership_score >= 75:
        leadership_label = "강한 쏠림장"
    elif leadership_score >= 60:
        leadership_label = "쏠림 우세"
    elif leadership_score <= 35:
        leadership_label = "확산형 장세"
    else:
        leadership_label = "혼재"

    lines.append(
        f"■ 시장 폭: 상승 {n_up:,} / 하락 {n_down:,} / 보합 {n_flat:,} 종목 "
        f"(상승 비율 {breadth_ratio:.1f}%)" if breadth_ratio is not None else
        f"■ 시장 폭: 상승 {n_up:,} / 하락 {n_down:,} / 보합 {n_flat:,} 종목"
    )
    if breadth_ratio is not None:
        if breadth_ratio >= 60:
            lines.append("  → 상승 종목이 다수입니다. 지수 상승이 시장 전반으로 확산되는 장세입니다.")
        elif breadth_ratio <= 40:
            lines.append("  → 하락 종목이 다수입니다. 지수가 강해도 소수 대형주 의존 장세일 수 있습니다.")
        else:
            lines.append("  → 상승/하락이 혼재합니다. 종목·업종 선택의 영향이 큰 장세입니다.")
    lines.append("")
    lines.append(f"■ 주도주 쏠림 점수: {leadership_score}/100 ({leadership_label})")
    if top10_conc is not None:
        lines.append(f"  - 전체 거래대금 중 상위 10개 비중: {top10_conc:.1f}%")
    if semi_share is not None:
        lines.append(f"  - 상위 10개 거래대금 중 반도체/IT 추정 비중: {semi_share:.1f}% ({semi_count}/{len(top)}개)")
    has_technical_breadth = any(v is not None for v in [
        pct_above_20ma, pct_above_50ma, pct_above_200ma, high_52w_count, low_52w_count
    ])
    if has_technical_breadth:
        lines.append("")
        lines.append("■ 시장 확산도(이동평균·52주 고저, 종가 기준 근사)")
        if pct_above_20ma is not None:
            lines.append(f"  - 20일선 위 종목 비율: {pct_above_20ma:.1f}%")
        if pct_above_50ma is not None:
            lines.append(f"  - 50일선 위 종목 비율: {pct_above_50ma:.1f}%")
        if pct_above_200ma is not None:
            lines.append(f"  - 200일선 위 종목 비율: {pct_above_200ma:.1f}%")
        if high_52w_count is not None and low_52w_count is not None:
            lines.append(f"  - 52주 신고가/신저가: {high_52w_count:,} / {low_52w_count:,}")
        if key_points:
            lines.append("  - 핵심 판단: " + " / ".join(key_points[:3]))
    elif key_points:
        lines.append("")
        lines.append("■ 시장 확산도 요약")
        lines.append("  - 핵심 판단: " + " / ".join(key_points[:3]))
    lines.append("")

    lines.append(f"■ 거래대금 상위 {LEADER_TOP_N}종목 (자금이 몰리는 종목)")
    for _, row in top.iterrows():
        ticker = str(row.get("ticker", ""))
        name = str(row.get("name", ticker))
        close = float(row["close"]) if pd.notna(row.get("close")) else 0
        rate = float(row.get("change_pct", float("nan")))
        value = float(row["value"]) / 1e8  # 억원
        rate_str = f"{'+' if rate >= 0 else ''}{rate:.2f}%" if pd.notna(rate) else "N/A"
        tag = " / IT·반도체" if bool(row.get("is_semi_it", False)) else ""
        lines.append(f"  - {name}({ticker}): 종가 {close:,.0f}원, {rate_str}, 거래대금 {value:,.0f}억원{tag}")

    state = LeadershipState(
        date=date_fmt,
        breadth_ratio=breadth_ratio,
        top10_value_concentration=top10_conc,
        semiconductor_share_top10=semi_share,
        pct_above_20ma=pct_above_20ma,
        pct_above_50ma=pct_above_50ma,
        pct_above_200ma=pct_above_200ma,
        high_52w_count=high_52w_count,
        low_52w_count=low_52w_count,
        technical_breadth_note=technical_breadth_note,
        leadership_score=leadership_score,
        leadership_label=leadership_label,
        key_points=key_points,
    )
    return lines, state


def build_krx_leaders_with_state() -> tuple[List[str], Optional[LeadershipState]]:
    """KRX 거래대금·시장 폭 라인과 LeadershipState를 함께 반환."""
    date, is_today = get_krx_latest_data_date()
    df = _fetch_krx_snapshot(date)
    return _analyze_krx_leadership(date, is_today, df)


def build_leading_stock_report() -> tuple[str, Optional[LeadershipState]]:
    """주도주 섹션 전체 텍스트와 주도주 상태.

    원칙: 데이터 소스 실패의 기술적 예외 메시지를 독자 메일에 직접 노출하지 않는다.
    """
    blocks: List[str] = []
    leadership_state: Optional[LeadershipState] = None

    blocks.append("--- [한국] 거래대금 상위 / 시장 폭 ---")
    try:
        lines, leadership_state = build_krx_leaders_with_state()
        blocks.extend(lines)
    except Exception:
        blocks.append("이번 브리핑에서는 한국 거래대금·시장 폭 데이터가 제외되었습니다.")
    blocks.append("")

    blocks.append("--- [한국] 투자자별 순매수 상위 ---")
    try:
        blocks.extend(build_krx_investor_flow_lines())
    except Exception:
        blocks.append("이번 브리핑에서는 투자자별 순매수 데이터가 제외되었습니다.")
    blocks.append("")

    blocks.append("--- [미국] 섹터 상대강도 (20일, vs S&P500) ---")
    try:
        blocks.extend(build_us_sector_rs_lines())
    except Exception:
        blocks.append("이번 브리핑에서는 미국 섹터 상대강도 데이터가 제외되었습니다.")
    blocks.append("")

    blocks.append("※ 주도주 판단 가이드")
    blocks.append("- 거래대금 상위에 같은 업종 종목이 반복 등장하면 해당 업종이 주도 섹터일 가능성이 높습니다.")
    blocks.append("- 상승 종목 비율이 낮은데 지수만 오르면 소수 대형주 의존 장세일 수 있습니다.")
    blocks.append("- 주도주 쏠림 점수가 높을수록 시장 전체보다 특정 업종·대형주에 자금이 집중된 상태입니다.")

    return "\n".join(blocks), leadership_state


# ─────────────────────────────────────────────────────────
# Regime engine + Email body
# ─────────────────────────────────────────────────────────
def get_result(results: List[IndicatorResult], name: str) -> Optional[IndicatorResult]:
    return next((r for r in results if r.name == name and r.ok), None)


def get_raw_value(results: List[IndicatorResult], name: str) -> Optional[float]:
    r = get_result(results, name)
    return r.raw_value if r and r.raw_value is not None else None


def _trend_word(disparity: Optional[float]) -> Optional[str]:
    """이격도 값을 추세 표현으로 변환."""
    if disparity is None:
        return None
    if disparity >= DISPARITY_MANIA:
        return "극단적 과열"
    if disparity >= DISPARITY_OVERHEAT:
        return "과열"
    if disparity >= DISPARITY_STRONG:
        return "강세"
    if disparity <= DISPARITY_WEAK:
        return "약세"
    return "중립(50일선 부근)"


def clamp_score(x: float, low: int = 0, high: int = 100) -> int:
    return max(low, min(high, int(round(x))))


def score_light(score: int, mode: str = "positive") -> str:
    """점수를 신호등 문자열로 변환.

    positive: 높을수록 좋음 / negative: 높을수록 부담
    """
    if mode == "negative":
        if score >= 75:
            return "🔴 높음"
        if score >= 55:
            return "🟡 부담"
        if score >= 35:
            return "🟡 보통"
        return "🟢 낮음"
    if score >= 70:
        return "🟢 좋음"
    if score >= 45:
        return "🟡 보통"
    return "🔴 약함"


def score_light_entry(score: int) -> str:
    """신규 진입 매력도 신호등. 높을수록 신규 진입 부담이 낮다."""
    if score >= 65:
        return "🟢 좋음"
    if score >= 40:
        return "🟡 보통"
    return "🔴 낮음"


def breadth_label(score: int) -> str:
    """시장 확산도 라벨. 낮을수록 소수 종목·업종 쏠림이 강하다."""
    if score >= 70:
        return "🟢 넓음"
    if score >= 45:
        return "🟡 보통"
    if score >= 30:
        return "🔴 좁음"
    return "🔴 매우 좁음"


def timing_light(score: int, mode: str = "fear") -> str:
    """공포 매수/광기 축소 점수 신호등.

    fear: 높을수록 공포 매수 후보 성격이 강함.
    mania: 높을수록 추격/과열 축소 부담이 강함.
    """
    if mode == "mania":
        if score >= 75:
            return "🔴 높음"
        if score >= 60:
            return "🟠 경계"
        if score >= 40:
            return "🟡 보통"
        return "🟢 낮음"
    if score >= 75:
        return "🟢 높음"
    if score >= 60:
        return "🟢 관심"
    if score >= 40:
        return "🟡 보통"
    return "🟢 낮음"


def _first_number_from_text(text: str, unit_hint: str = "") -> Optional[float]:
    if not text:
        return None
    if unit_hint:
        pattern = rf"([+-]?\d+(?:\.\d+)?)\s*{re.escape(unit_hint)}"
        m = re.search(pattern, text)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                return None
    m = re.search(r"([+-]?\d+(?:\.\d+)?)", text)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def get_change_bp(r: Optional[IndicatorResult]) -> Optional[float]:
    if not r:
        return None
    return _first_number_from_text(r.change_text, "bp")


def get_change_pct(r: Optional[IndicatorResult]) -> Optional[float]:
    if not r:
        return None
    return _extract_pct_from_change_text(r.change_text)


def get_change_points(r: Optional[IndicatorResult]) -> Optional[float]:
    if not r:
        return None
    # 이격도 변화: '+1.43p vs 전일' 형식
    return _first_number_from_text(r.change_text, "p")


def calculate_fear_buy_score(
    results: List[IndicatorResult],
    breadth_score: int,
    fx_stress_score: int,
    leadership: Optional[LeadershipState] = None,
) -> tuple[int, List[str], str]:
    """공포가 매수 기회로 바뀌고 있는지 평가한다.

    높은 공포 자체가 매수 신호는 아니다. 공포 압력에 가격 침체와 진정 신호가
    함께 붙을 때 점수가 올라가며, 떨어지는 칼날 방지장치를 둔다.
    """
    score = 0
    reasons: List[str] = []

    vix_r = get_result(results, "VIX 지수")
    move_r = get_result(results, "MOVE 지수")
    hy_r = get_result(results, "미국 하이일드 스프레드")
    kospi_r = get_result(results, "KOSPI 50일 이격도")
    kosdaq_r = get_result(results, "KOSDAQ 50일 이격도")
    spx_r = get_result(results, "S&P500 50일 이격도")
    nasdaq_r = get_result(results, "NASDAQ 50일 이격도")

    vix = vix_r.raw_value if vix_r else None
    move = move_r.raw_value if move_r else None
    hy = hy_r.raw_value if hy_r else None
    kospi = kospi_r.raw_value if kospi_r else None
    kosdaq = kosdaq_r.raw_value if kosdaq_r else None
    spx = spx_r.raw_value if spx_r else None
    nasdaq = nasdaq_r.raw_value if nasdaq_r else None

    # A. 공포 압력 45점
    if vix is not None:
        if vix >= 40:
            score += 18; reasons.append(f"VIX {vix:.1f}: 극단 공포")
        elif vix >= 30:
            score += 15; reasons.append(f"VIX {vix:.1f}: 공포 구간")
        elif vix >= 25:
            score += 10; reasons.append(f"VIX {vix:.1f}: 변동성 경계")
        elif vix >= 20:
            score += 6; reasons.append(f"VIX {vix:.1f}: 공포 초기")

    if move is not None:
        if move >= 130:
            score += 10; reasons.append(f"MOVE {move:.1f}: 채권시장 공포")
        elif move >= 110:
            score += 7; reasons.append(f"MOVE {move:.1f}: 채권 변동성 경계")
        elif move >= 90:
            score += 4; reasons.append(f"MOVE {move:.1f}: 채권 변동성 상승")

    if hy is not None:
        if hy >= 6.0:
            score += 17; reasons.append(f"HY 스프레드 {hy:.2f}%: 신용위험 급등")
        elif hy >= 5.0:
            score += 13; reasons.append(f"HY 스프레드 {hy:.2f}%: 신용위험 확대")
        elif hy >= 4.0:
            score += 8; reasons.append(f"HY 스프레드 {hy:.2f}%: 신용 경계")
        elif hy >= 3.5:
            score += 4; reasons.append(f"HY 스프레드 {hy:.2f}%: 신용 스트레스 초기")

    # B. 가격 하락/침체 25점
    if kospi is not None:
        if kospi <= 90:
            score += 10; reasons.append(f"KOSPI 이격도 {kospi:.1f}: 깊은 침체")
        elif kospi <= 95:
            score += 6; reasons.append(f"KOSPI 이격도 {kospi:.1f}: 50일선 하회")
    if kosdaq is not None:
        if kosdaq <= 90:
            score += 5; reasons.append(f"KOSDAQ 이격도 {kosdaq:.1f}: 깊은 침체")
        elif kosdaq <= 95:
            score += 3; reasons.append(f"KOSDAQ 이격도 {kosdaq:.1f}: 약세권")
    if spx is not None and spx <= 95:
        score += 5; reasons.append(f"S&P500 이격도 {spx:.1f}: 약세권")
    if nasdaq is not None and nasdaq <= 95:
        score += 5; reasons.append(f"NASDAQ 이격도 {nasdaq:.1f}: 약세권")

    # C. 시장 내부 붕괴 15점
    if breadth_score <= 20:
        score += 7; reasons.append(f"시장 확산도 {breadth_score}/100: 내부 붕괴권")
    elif breadth_score <= 35:
        score += 4; reasons.append(f"시장 확산도 {breadth_score}/100: 좁은 장")

    if leadership and leadership.breadth_ratio is not None:
        if leadership.breadth_ratio <= 30:
            score += 5; reasons.append(f"상승 종목 비율 {leadership.breadth_ratio:.1f}%: 광범위한 하락")
        elif leadership.breadth_ratio <= 40:
            score += 3; reasons.append(f"상승 종목 비율 {leadership.breadth_ratio:.1f}%: 약한 시장 폭")
    if leadership and leadership.high_52w_count is not None and leadership.low_52w_count is not None:
        if leadership.low_52w_count > leadership.high_52w_count * 2 and leadership.low_52w_count >= 30:
            score += 3; reasons.append(f"52주 신저가 {leadership.low_52w_count}개: 내부 훼손")

    # D. 진정 신호 15점
    vix_pct = get_change_pct(vix_r)
    hy_bp = get_change_bp(hy_r)
    kospi_chg = get_change_points(kospi_r)
    if vix_pct is not None:
        if vix_pct <= -5:
            score += 4; reasons.append(f"VIX {vix_pct:+.1f}%: 공포 진정")
        elif vix_pct < 0:
            score += 2; reasons.append("VIX 소폭 하락")
    if hy_bp is not None:
        if hy_bp <= -5:
            score += 4; reasons.append(f"HY 스프레드 {hy_bp:+.1f}bp: 신용 스트레스 완화")
        elif hy_bp <= 0:
            score += 2; reasons.append("HY 스프레드 안정")
    if kospi_chg is not None and kospi_chg > 0:
        score += 3; reasons.append(f"KOSPI 이격도 {kospi_chg:+.2f}p: 반등 확인")
    # Cloud Run 로컬 로그가 유지되지 않을 수 있어 점수 변화는 없으면 생략한다.

    warning = ""
    if vix is not None and hy_bp is not None and kospi_chg is not None:
        if vix >= 30 and hy_bp > 0 and kospi_chg < 0:
            score = min(score, 55)
            warning = "공포는 크지만 VIX·신용·지수 진정이 부족해 떨어지는 칼날 위험이 있습니다."

    return clamp_score(score), reasons[:7], warning


def calculate_mania_reduce_score(
    results: List[IndicatorResult],
    breadth_score: int,
    fx_stress_score: int,
    leadership: Optional[LeadershipState] = None,
) -> tuple[int, List[str]]:
    """너무 많이 달려 추격·과열 축소 부담이 커졌는지 평가한다."""
    score = 0
    reasons: List[str] = []

    kospi = get_raw_value(results, "KOSPI 50일 이격도")
    spx = get_raw_value(results, "S&P500 50일 이격도")
    nasdaq = get_raw_value(results, "NASDAQ 50일 이격도")
    sox = get_raw_value(results, "SOX 반도체지수 50일 이격도")
    samsung = get_raw_value(results, "삼성전자 50일 이격도")
    kosdaq = get_raw_value(results, "KOSDAQ 50일 이격도")
    fx = get_raw_value(results, "원/달러 환율")
    real10 = get_raw_value(results, "미국 10년 실질금리")
    usdjpy = get_raw_value(results, "USD/JPY")
    wti_r = get_result(results, "WTI 유가")
    vix = get_raw_value(results, "VIX 지수")
    move = get_raw_value(results, "MOVE 지수")
    hy = get_raw_value(results, "미국 하이일드 스프레드")

    # A. 가격 과열 40점
    if kospi is not None:
        if kospi >= 130:
            score += 15; reasons.append(f"KOSPI 이격도 {kospi:.1f}: 극단 과열")
        elif kospi >= 120:
            score += 12; reasons.append(f"KOSPI 이격도 {kospi:.1f}: 과열")
        elif kospi >= 110:
            score += 6; reasons.append(f"KOSPI 이격도 {kospi:.1f}: 높음")
    if samsung is not None:
        if samsung >= 130:
            score += 12; reasons.append(f"삼성전자 이격도 {samsung:.1f}: 극단 과열")
        elif samsung >= 120:
            score += 8; reasons.append(f"삼성전자 이격도 {samsung:.1f}: 과열")
        elif samsung >= 110:
            score += 4; reasons.append(f"삼성전자 이격도 {samsung:.1f}: 높음")
    if sox is not None:
        if sox >= 120:
            score += 8; reasons.append(f"SOX 이격도 {sox:.1f}: 과열")
        elif sox >= 115:
            score += 5; reasons.append(f"SOX 이격도 {sox:.1f}: 높은 수준")
    if nasdaq is not None and nasdaq >= 110:
        score += 5; reasons.append(f"NASDAQ 이격도 {nasdaq:.1f}: 과열권 접근")
    elif spx is not None and spx >= 108:
        score += 3; reasons.append(f"S&P500 이격도 {spx:.1f}: 높음")

    # B. 쏠림/확산 부진 25점
    if leadership:
        if leadership.leadership_score >= 90:
            score += 10; reasons.append(f"주도주 쏠림 {leadership.leadership_score}/100: 극단 쏠림")
        elif leadership.leadership_score >= 75:
            score += 8; reasons.append(f"주도주 쏠림 {leadership.leadership_score}/100: 강한 쏠림")
        elif leadership.leadership_score >= 60:
            score += 5; reasons.append(f"주도주 쏠림 {leadership.leadership_score}/100: 쏠림")
        if leadership.top10_value_concentration is not None:
            if leadership.top10_value_concentration >= 45:
                score += 6; reasons.append(f"상위 10개 거래대금 비중 {leadership.top10_value_concentration:.1f}%")
            elif leadership.top10_value_concentration >= 35:
                score += 4; reasons.append(f"상위 10개 거래대금 비중 {leadership.top10_value_concentration:.1f}%")
        if leadership.semiconductor_share_top10 is not None:
            if leadership.semiconductor_share_top10 >= 70:
                score += 5; reasons.append(f"상위 10개 내 반도체/IT {leadership.semiconductor_share_top10:.1f}%")
            elif leadership.semiconductor_share_top10 >= 50:
                score += 3; reasons.append(f"상위 10개 내 반도체/IT {leadership.semiconductor_share_top10:.1f}%")
    if breadth_score <= 40:
        score += 4; reasons.append(f"시장 확산도 {breadth_score}/100: 좁은 장")
    elif breadth_score <= 50:
        score += 2; reasons.append(f"시장 확산도 {breadth_score}/100: 확산 제한")

    # C. 안심 과다 15점
    if vix is not None:
        if vix < 15:
            score += 5; reasons.append(f"VIX {vix:.1f}: 안심 과다 가능성")
        elif vix < 18:
            score += 3; reasons.append(f"VIX {vix:.1f}: 낮은 공포")
    if hy is not None:
        if hy < 3.0:
            score += 5; reasons.append(f"HY {hy:.2f}%: 신용시장 과도한 평온")
        elif hy < 3.5:
            score += 3; reasons.append(f"HY {hy:.2f}%: 신용 안정")
    if move is not None:
        if move < 80:
            score += 5; reasons.append(f"MOVE {move:.1f}: 채권시장 평온")
        elif move < 100:
            score += 3; reasons.append(f"MOVE {move:.1f}: 채권 변동성 낮음")

    # D. 매크로 부담 15점
    if fx is not None:
        if fx >= FX_HIGH_WARNING:
            score += 5; reasons.append(f"원/달러 {fx:,.0f}원: 고환율")
        elif fx >= 1450:
            score += 3; reasons.append(f"원/달러 {fx:,.0f}원: 환율 부담")
    if real10 is not None:
        if real10 >= 2.2:
            score += 5; reasons.append(f"실질금리 {real10:.2f}%: 성장주 부담")
        elif real10 >= 2.0:
            score += 3; reasons.append(f"실질금리 {real10:.2f}%: 높은 수준")
    if usdjpy is not None and usdjpy >= 160:
        score += 3; reasons.append(f"USD/JPY {usdjpy:.1f}: 엔화 약세·캐리 점검")
    wti_pct = get_change_pct(wti_r)
    if wti_pct is not None and wti_pct >= 5:
        score += 2; reasons.append(f"WTI {wti_pct:+.1f}%: 유가 급등")

    # E. 미국/한국 괴리 5점
    if kospi is not None and spx is not None and kospi - spx >= 15:
        score += 3; reasons.append(f"KOSPI-S&P500 이격도 차 {kospi-spx:.1f}p")
    if kospi is not None and kosdaq is not None and kospi - kosdaq >= 20:
        score += 2; reasons.append(f"KOSPI-KOSDAQ 이격도 차 {kospi-kosdaq:.1f}p")

    return clamp_score(score), reasons[:9]


def classify_timing(fear_score: int, mania_score: int) -> str:
    if fear_score >= 75 and mania_score <= 40:
        return "적극 분할매수 후보"
    if 60 <= fear_score < 75 and mania_score <= 50:
        return "분할매수 관심"
    if fear_score >= 60 and mania_score >= 60:
        return "공포는 있으나 변동성 큼, 확인 후 분할"
    if fear_score <= 40 and mania_score >= 70:
        return "추격 금지 / 비중 점검"
    if fear_score <= 40 and 50 <= mania_score < 70:
        return "관망 / 눌림 대기"
    if 40 <= fear_score <= 60 and 40 <= mania_score <= 60:
        return "선별 접근"
    if fear_score <= 30 and mania_score <= 30:
        return "특별한 기회 없음"
    if mania_score >= 60:
        return "눌림 대기"
    if fear_score >= 60:
        return "분할매수 관심"
    return "관망 / 선별 접근"


def build_timing_scores(
    results: List[IndicatorResult],
    breadth_score: int,
    fx_stress_score: int,
    leadership: Optional[LeadershipState] = None,
) -> TimingScores:
    fear_score, fear_reasons, warning = calculate_fear_buy_score(results, breadth_score, fx_stress_score, leadership)
    mania_score, mania_reasons = calculate_mania_reduce_score(results, breadth_score, fx_stress_score, leadership)
    final_timing = classify_timing(fear_score, mania_score)

    if fear_score < 40 and mania_score >= 70:
        translation = "지금은 공포 세일장이 아니라, 일부 주도주가 너무 빨리 달린 구간입니다."
    elif fear_score >= 60 and mania_score < 60:
        translation = "공포가 커졌고 일부 진정 신호가 보여 분할매수 후보를 검토할 수 있는 구간입니다."
    elif fear_score >= 60 and mania_score >= 60:
        translation = "공포와 변동성이 모두 커서 기회는 생기지만 확인 없는 진입은 위험한 구간입니다."
    elif mania_score >= 60:
        translation = "공포 세일장은 아니고 가격·쏠림 부담이 커 눌림을 기다리는 편이 유리한 구간입니다."
    else:
        translation = "공포 매수 기회도, 광기 축소 신호도 강하지 않은 선별 접근 구간입니다."

    return TimingScores(
        fear_buy_score=fear_score,
        mania_reduce_score=mania_score,
        final_timing=final_timing,
        fear_reasons=fear_reasons or ["공포 압력은 제한적"],
        mania_reasons=mania_reasons or ["광기·과열 축소 신호는 제한적"],
        warning=warning,
        translation=translation,
    )


def format_timing_scores(timing: TimingScores) -> str:
    lines = []
    lines.append("[공포-광기 타이밍]")
    lines.append(f"- 공포 매수 점수: {timing_light(timing.fear_buy_score, 'fear')} ({timing.fear_buy_score}/100)")
    lines.append(f"- 광기 축소 점수: {timing_light(timing.mania_reduce_score, 'mania')} ({timing.mania_reduce_score}/100)")
    lines.append(f"- 최종 타이밍: {timing.final_timing}")
    lines.append("")
    lines.append(f"해석: {timing.translation}")
    if timing.warning:
        lines.append(f"주의: {timing.warning}")
    if timing.mania_reasons:
        lines.append("- 광기/축소 근거: " + " / ".join(timing.mania_reasons[:4]))
    if timing.fear_reasons and timing.fear_buy_score >= 40:
        lines.append("- 공포 매수 근거: " + " / ".join(timing.fear_reasons[:4]))
    return "\n".join(lines)


def signal_from_indicator(r: IndicatorResult) -> Signal:
    """기존 signal_level과 별도로 direction/stress/timing을 분리한다.

    이 함수는 AI 프롬프트 및 내부 점수화의 보조 자료로 사용한다.
    """
    if not r.ok:
        return Signal(direction=0, stress=0, timing=0, confidence=0)

    name = r.name
    value = r.raw_value
    if value is None:
        return Signal(confidence=70)

    # 가격·지수 이격도: 방향성과 타이밍을 분리한다.
    if "이격도" in name:
        direction = 0
        timing = 0
        if value >= 105:
            direction = 2
        elif value >= 100:
            direction = 1
        elif value <= 95:
            direction = -1
        if value >= DISPARITY_MANIA:
            timing = 3
        elif value >= DISPARITY_OVERHEAT:
            timing = 3
        elif value >= 110:
            timing = 2
        elif value <= DISPARITY_WEAK:
            timing = 1
        return Signal(direction=direction, stress=0, timing=timing, confidence=100)

    if name == "VIX 지수":
        if value >= VIX_FEAR:
            return Signal(direction=-2, stress=3, timing=0, confidence=100)
        if value >= VIX_CAUTION:
            return Signal(direction=-1, stress=2, timing=0, confidence=100)
        return Signal(direction=1, stress=0, timing=0, confidence=100)

    if name == "MOVE 지수":
        if value >= MOVE_RISK or "위험" in r.state:
            return Signal(direction=-2, stress=3, timing=0, confidence=90)
        if value >= MOVE_CAUTION or "경계" in r.state:
            return Signal(direction=-1, stress=2, timing=0, confidence=90)
        return Signal(direction=1, stress=0, timing=0, confidence=90)

    if name == "미국 하이일드 스프레드":
        if value >= HY_SPREAD_RISK:
            return Signal(direction=-2, stress=3, timing=0, confidence=100)
        if value >= HY_SPREAD_CAUTION:
            return Signal(direction=-1, stress=2, timing=0, confidence=100)
        return Signal(direction=1, stress=0, timing=0, confidence=100)

    if name == "원/달러 환율":
        if value >= FX_HIGH_WARNING:
            return Signal(direction=-1, stress=3, timing=0, confidence=100)
        if value >= FX_IDEAL_HIGH:
            return Signal(direction=0, stress=2, timing=0, confidence=100)
        return Signal(direction=0, stress=0, timing=0, confidence=100)

    if name in ["USD/CNH", "USD/JPY", "WTI 유가"]:
        if "경계" in r.state or "부담" in r.state or "약세" in r.state:
            return Signal(direction=-1, stress=1, timing=0, confidence=80)
        return Signal(direction=0, stress=0, timing=0, confidence=80)

    if name == "미국 10년 실질금리":
        if "급등" in r.state or "위험" in r.state:
            return Signal(direction=-2, stress=2, timing=1, confidence=90)
        if "상승 경계" in r.state:
            return Signal(direction=-1, stress=1, timing=1, confidence=90)
        if "급락" in r.state:
            return Signal(direction=1, stress=0, timing=0, confidence=90)
        return Signal(direction=0, stress=0, timing=0, confidence=90)

    if "금리차" in name:
        if "역전" in r.state:
            return Signal(direction=-1, stress=2, timing=0, confidence=90)
        if "평탄화" in r.state:
            return Signal(direction=0, stress=1, timing=0, confidence=90)
        return Signal(direction=0, stress=0, timing=0, confidence=90)

    if "금리" in name:
        # 금리 레벨 자체보다 급등락이 중요하므로 기존 state를 보조로 사용한다.
        if "급등" in r.state or "위험" in r.state:
            return Signal(direction=-1, stress=2, timing=0, confidence=80)
        if "상승 경계" in r.state:
            return Signal(direction=-1, stress=1, timing=0, confidence=80)
        return Signal(direction=0, stress=0, timing=0, confidence=80)

    return Signal(direction=0, stress=0, timing=0, confidence=80)


def signal_to_text(sig: Signal) -> str:
    direction = "추세 우호" if sig.direction > 0 else "추세 부담" if sig.direction < 0 else "추세 중립"
    stress = "스트레스 높음" if sig.stress >= 2 else "스트레스 보통" if sig.stress == 1 else "스트레스 낮음"
    timing = "타이밍 부담 높음" if sig.timing >= 2 else "타이밍 부담 보통" if sig.timing == 1 else "타이밍 부담 낮음"
    return f"{direction} / {stress} / {timing}"


def build_session_core_question(theme: str, session: BriefSession) -> str:
    """국면이 정한 '주제'(theme)에 세션 방향 프레임을 입혀 핵심 질문을 만든다.

    같은 국면이라도 오전(간밤 미국장 → 오늘 한국 개장)과
    오후(오늘 한국장 → 오늘 밤 미국·내일 한국)에서 질문이 다르게 나온다.
    theme: "concentration" | "fx" | "overheating" | "breadth"
    """
    morning = session.key == "morning"
    if theme == "concentration":
        return (
            "간밤 미국 반도체·기술주 흐름이 오늘 한국장의 반도체·대형주 쏠림을 더 키울까, 아니면 과열을 식힐까?"
            if morning else
            "오늘 한국장의 반도체·대형주 쏠림이 오늘 밤 미국 반도체(SOX)로 확인될까, 아니면 한국만의 과열로 끝날까?"
        )
    if theme == "fx":
        return (
            "간밤 달러 강세·미국 금리 흐름이 원화 부담을 키울까, 아니면 오늘 한국장 개장에서 환율 부담이 완화될까?"
            if morning else
            "오늘 한국장이 버틴 위험선호가 오늘 밤 글로벌 달러 강세에도 유지될까, 아니면 환율이 내일 한국장의 발목을 잡을까?"
        )
    if theme == "overheating":
        return (
            "간밤 미국장의 추세가 오늘 한국장의 과열을 더 끌어올릴까, 아니면 개장과 함께 이격도 해소가 시작될까?"
            if morning else
            "오늘 한국장의 과열 이격도가 오늘 밤 미국장에서도 정당화될까, 아니면 내일 한국장에서 이격도 해소가 먼저 나올까?"
        )
    # breadth / default
    return (
        "간밤 미국장의 위험선호가 오늘 한국장 전체로 확산될 수 있을까?"
        if morning else
        "오늘 한국장의 위험선호가 오늘 밤 미국장으로 확산될 수 있을까?"
    )


def classify_market_regime(
    results: List[IndicatorResult],
    leadership: Optional[LeadershipState] = None,
    session: Optional[BriefSession] = None,
) -> MarketRegime:
    """지표별 상태를 종합해 시장 국면을 점수화한다.

    핵심 원칙:
    - VIX/HY/MOVE/실질금리는 시장 심장박동(스트레스)로 본다.
    - 이격도는 방향 판단이 아니라 진입 타이밍/과열 지표로 분리한다.
    - 시장 폭과 주도주 쏠림은 상승의 건강도를 판단하는 별도 축으로 둔다.
    """
    session = session or current_session()
    kospi = get_raw_value(results, "KOSPI 50일 이격도")
    kosdaq = get_raw_value(results, "KOSDAQ 50일 이격도")
    spx = get_raw_value(results, "S&P500 50일 이격도")
    nasdaq = get_raw_value(results, "NASDAQ 50일 이격도")
    sox = get_raw_value(results, "SOX 반도체지수 50일 이격도")
    samsung = get_raw_value(results, "삼성전자 50일 이격도")
    fx = get_raw_value(results, "원/달러 환율")
    dxy = get_result(results, "DXY 달러인덱스")
    cnh = get_result(results, "USD/CNH")
    jpy = get_result(results, "USD/JPY")
    wti = get_result(results, "WTI 유가")
    vix = get_raw_value(results, "VIX 지수")
    move = get_result(results, "MOVE 지수")
    hy = get_raw_value(results, "미국 하이일드 스프레드")
    us10 = get_result(results, "미국 10년물 금리")
    real10 = get_result(results, "미국 10년 실질금리")
    breakeven10 = get_result(results, "미국 10년 기대인플레")
    spread_10y_3m = get_result(results, "미국 10Y-3M 금리차")
    spread_10y_ff = get_result(results, "미국 10Y-Fed Funds 금리차")

    risk_score = 50
    overheating = 0
    breadth_score = 50
    fx_stress = 0
    stability_score = 50
    key_drivers: List[str] = []
    risks: List[str] = []

    # 1군: 신용·변동성·채권시장 스트레스
    if vix is not None:
        if vix < VIX_CAUTION:
            risk_score += 15
            stability_score += 18
            key_drivers.append(f"VIX {vix:.1f}: 주식 변동성 안정")
        elif vix >= VIX_FEAR:
            risk_score -= 30
            stability_score -= 30
            risks.append(f"VIX {vix:.1f}: 공포 구간")
        else:
            risk_score -= 10
            stability_score -= 10
            risks.append(f"VIX {vix:.1f}: 주식 변동성 경계")

    if move and move.raw_value is not None:
        if "위험" in move.state:
            risk_score -= 18
            stability_score -= 25
            overheating += 5
            risks.append(f"MOVE {move.raw_value:.1f}: 채권 변동성 위험")
        elif "경계" in move.state:
            risk_score -= 8
            stability_score -= 12
            risks.append(f"MOVE {move.raw_value:.1f}: 채권 변동성 경계")
        else:
            stability_score += 10
            key_drivers.append(f"MOVE {move.raw_value:.1f}: 채권 변동성 안정")

    if hy is not None:
        if hy < HY_SPREAD_CAUTION:
            risk_score += 15
            stability_score += 22
            key_drivers.append(f"HY 스프레드 {hy:.2f}%: 신용시장 안정")
        elif hy >= HY_SPREAD_RISK:
            risk_score -= 30
            stability_score -= 35
            risks.append(f"HY 스프레드 {hy:.2f}%: 신용위험 확대")
        else:
            risk_score -= 12
            stability_score -= 15
            risks.append(f"HY 스프레드 {hy:.2f}%: 신용위험 경계")

    # 2군: 위험자산 추세
    trend_items = [
        ("KOSPI", kospi, 8),
        ("KOSDAQ", kosdaq, 6),
        ("S&P500", spx, 8),
        ("NASDAQ", nasdaq, 8),
        ("SOX", sox, 8),
    ]
    strong_trends = []
    weak_trends = []
    for label, val, weight in trend_items:
        if val is None:
            continue
        if val >= 105:
            risk_score += weight
            strong_trends.append(f"{label} {val:.1f}")
        elif val >= DISPARITY_STRONG:
            risk_score += max(3, weight // 2)
            strong_trends.append(f"{label} {val:.1f}")
        elif val <= DISPARITY_WEAK:
            risk_score -= weight
            weak_trends.append(f"{label} {val:.1f}")
            if label == "KOSDAQ":
                breadth_score -= 15
    if strong_trends:
        key_drivers.append("주요 지수 50일선 우위: " + ", ".join(strong_trends[:4]))
    if weak_trends:
        risks.append("추세 약화 지수: " + ", ".join(weak_trends[:4]))

    # 4군: 이격도는 과열/타이밍 부담으로 별도 반영
    for label, val in [
        ("KOSPI", kospi), ("KOSDAQ", kosdaq), ("S&P500", spx),
        ("NASDAQ", nasdaq), ("SOX", sox), ("삼성전자", samsung)
    ]:
        if val is None:
            continue
        if val >= DISPARITY_MANIA:
            overheating += 35
            risks.append(f"{label} 이격도 {val:.1f}: 극단적 과열")
        elif val >= DISPARITY_OVERHEAT:
            overheating += 25
            risks.append(f"{label} 이격도 {val:.1f}: 과열")
        elif val >= 110:
            overheating += 12
        elif val >= 105:
            overheating += 6
        elif val <= DISPARITY_WEAK:
            overheating += 5

    # 명목금리와 실질금리 분해
    if us10 and ("급등" in us10.state or "위험" in us10.state):
        risk_score -= 8
        risks.append(f"미국 10년물: {us10.state}")
    elif us10 and "상승 경계" in us10.state:
        risk_score -= 4
        risks.append(f"미국 10년물: {us10.state}")

    if real10:
        if "급등" in real10.state or "위험" in real10.state:
            risk_score -= 8
            overheating += 5
            risks.append(f"미국 10년 실질금리: {real10.state} — 성장주 할인율 부담")
        elif "상승 경계" in real10.state:
            risk_score -= 5
            overheating += 3
            risks.append(f"미국 10년 실질금리: {real10.state}")
        elif "급락" in real10.state:
            risk_score += 5
            key_drivers.append("미국 10년 실질금리 하락: 성장주 할인율 부담 완화")

    if breakeven10 and ("기대인플레" in breakeven10.state and "상승" in breakeven10.state):
        risks.append(f"미국 10년 기대인플레: {breakeven10.state}")

    if real10 and sox is not None and sox >= 110 and ("상승" in real10.state or "급등" in real10.state):
        risks.append("실질금리 상승과 SOX 과열이 겹쳐 반도체 추격매수 부담 증가")
        overheating += 5

    # 경기 시계: 장단기 금리차
    for curve in [spread_10y_3m, spread_10y_ff]:
        if not curve or curve.raw_value is None:
            continue
        if curve.raw_value < CURVE_INVERSION:
            risk_score -= 8
            risks.append(f"{curve.name} {curve.raw_value:.2f}%p: 역전")
        elif curve.raw_value < CURVE_FLAT_CAUTION:
            risk_score -= 3
            risks.append(f"{curve.name} {curve.raw_value:.2f}%p: 평탄화 경계")
        else:
            key_drivers.append(f"{curve.name} {curve.raw_value:.2f}%p: 경기 시계 정상권")

    # 환율/DXY/아시아 통화/유가 스트레스
    fx_result = get_result(results, "원/달러 환율")
    if fx is not None:
        fx_surge = bool(fx_result and ("급등" in fx_result.state or "급등" in fx_result.comment))
        if fx >= FX_HIGH_WARNING:
            # 1,500원 이상은 급등이 아니어도 정상 체온이 아니다. 최소 노란불 이상으로 고정한다.
            fx_stress = max(fx_stress, 60)
            risk_score -= 5
            risks.append(f"원/달러 {fx:,.0f}원: 고환율 경고")
            if fx_surge:
                fx_stress = max(fx_stress, 80)
                risks.append("원/달러 고환율에 일간 급등까지 겹쳐 원화 부담 확대")
        elif fx >= FX_IDEAL_HIGH:
            fx_stress = max(fx_stress, 35)
            risks.append(f"원/달러 {fx:,.0f}원: 환율 부담")
        elif FX_IDEAL_LOW <= fx <= FX_IDEAL_HIGH:
            fx_stress = max(fx_stress, 20)
            key_drivers.append(f"원/달러 {fx:,.0f}원: 환전 고려 구간")

    if dxy and dxy.raw_value is not None:
        if "강세" in dxy.state or "경계" in dxy.state:
            fx_stress += 15
            risks.append(f"DXY {dxy.raw_value:.1f}: 글로벌 달러 강세 부담")
        elif "약세" in dxy.state:
            fx_stress -= 10
            key_drivers.append(f"DXY {dxy.raw_value:.1f}: 달러 약세")

    if cnh and ("경계" in cnh.state or "약세" in cnh.state):
        fx_stress += 12
        risks.append(f"USD/CNH {cnh.value_text}: 중국·아시아 통화 약세 부담")
    if jpy and ("경계" in jpy.state or "약세" in jpy.state):
        fx_stress += 8
        risks.append(f"USD/JPY {jpy.value_text}: 엔화 약세·캐리 환경 점검")
    if wti and ("부담" in wti.state or "경계" in wti.state):
        fx_stress += 8
        risks.append(f"WTI {wti.value_text}: 유가 상승에 따른 수입물가 부담")

    # 시장 폭·쏠림·이동평균 체력
    if leadership:
        leadership.kospi_kosdaq_gap = None
        if kospi is not None and kosdaq is not None:
            leadership.kospi_kosdaq_gap = kospi - kosdaq
            if leadership.kospi_kosdaq_gap >= 5:
                breadth_score -= 10
                risks.append(f"KOSPI-KOSDAQ 이격도 차 {leadership.kospi_kosdaq_gap:.1f}p: 대형주 편중")

        if leadership.breadth_ratio is not None:
            if leadership.breadth_ratio >= 60:
                breadth_score += 25
            elif leadership.breadth_ratio >= 50:
                breadth_score += 10
            elif leadership.breadth_ratio < 40:
                breadth_score -= 25
            elif leadership.breadth_ratio < 45:
                breadth_score -= 15

        if leadership.pct_above_50ma is not None:
            if leadership.pct_above_50ma >= 60:
                breadth_score += 15
                key_drivers.append(f"50일선 위 종목 비율 {leadership.pct_above_50ma:.1f}%: 중기 체력 양호")
            elif leadership.pct_above_50ma < 40:
                breadth_score -= 20
                risks.append(f"50일선 위 종목 비율 {leadership.pct_above_50ma:.1f}%: 중기 체력 약함")

        if leadership.pct_above_200ma is not None:
            if leadership.pct_above_200ma >= 55:
                breadth_score += 8
            elif leadership.pct_above_200ma < 40:
                breadth_score -= 10
                risks.append(f"200일선 위 종목 비율 {leadership.pct_above_200ma:.1f}%: 장기 체력 약함")

        if leadership.high_52w_count is not None and leadership.low_52w_count is not None:
            if leadership.low_52w_count > leadership.high_52w_count * 2 and leadership.low_52w_count >= 30:
                breadth_score -= 10
                risks.append(f"52주 신저가 {leadership.low_52w_count}개가 신고가 {leadership.high_52w_count}개를 크게 상회")
            elif leadership.high_52w_count > leadership.low_52w_count * 2 and leadership.high_52w_count >= 30:
                breadth_score += 5
                key_drivers.append(f"52주 신고가 {leadership.high_52w_count}개가 신저가 {leadership.low_52w_count}개를 상회")

        if leadership.leadership_score >= 75:
            breadth_score -= 20
            risks.append(f"주도주 쏠림 점수 {leadership.leadership_score}/100: {leadership.leadership_label}")
        elif leadership.leadership_score >= 60:
            breadth_score -= 10
            risks.append(f"주도주 쏠림 점수 {leadership.leadership_score}/100: {leadership.leadership_label}")
        elif leadership.leadership_score <= 35:
            breadth_score += 10
            key_drivers.append(f"주도주 쏠림 낮음: {leadership.leadership_label}")

    risk_score = clamp_score(risk_score)
    overheating = clamp_score(overheating)
    breadth_score = clamp_score(breadth_score)
    fx_stress = clamp_score(fx_stress)
    stability_score = clamp_score(stability_score)

    # 기술적 확산도(20/50/200일선 위 종목 비율)가 비활성화된 경우,
    # 하루 상승 종목 비율이 40%대라면 "붕괴"가 아니라 "좁은 장"으로 해석한다.
    if leadership:
        technical_available = any(v is not None for v in [
            leadership.pct_above_20ma, leadership.pct_above_50ma, leadership.pct_above_200ma,
            leadership.high_52w_count, leadership.low_52w_count,
        ])
        if not technical_available and leadership.breadth_ratio is not None:
            # 기술적 확산도(50일선 위 종목 비율)가 비면 당일 상승종목 비율로 폴백한다.
            # 과거 캡(상한 40)은 상승종목 90%인 날도 40에 묶여 항상 '쏠림장(<45)'으로
            # 떨어지게 만들었다. 시장 폭이 실제로 넓은 날은 캡을 풀어 확산 경로를 연다.
            if leadership.breadth_ratio >= 60:
                breadth_score = max(breadth_score, 58)
            elif leadership.breadth_ratio >= 50:
                breadth_score = max(breadth_score, 48)
            elif leadership.breadth_ratio >= 45:
                breadth_score = max(breadth_score, 32)
            elif leadership.breadth_ratio >= 40:
                breadth_score = max(breadth_score, 22)
            else:
                breadth_score = max(breadth_score, 12)

    # Risk-On과 신규 진입 매력도는 다르다.
    # 위험선호가 높아도 과열·좁은 확산·원화 부담이 겹치면 신규 추격 매력도는 낮아진다.
    entry_score = clamp_score(
        risk_score
        - overheating * 0.45
        - (100 - breadth_score) * 0.30
        - fx_stress * 0.15
    )

    # 시장 폭(당일 상승종목 비율)과 거래대금 집중을 분리해서 본다.
    # 한국 시장은 삼성전자·SK하이닉스만으로 거래대금이 상시 쏠리므로,
    # 거래대금 집중(leadership_score)만으로 '쏠림장'을 판정하면 시장 폭이
    # 90%로 뒤집힌 날도 매일 같은 결론이 나온다. 둘을 분리해 판정한다.
    breadth_ratio_val = leadership.breadth_ratio if leadership else None
    leadership_conc = leadership.leadership_score if leadership else None
    broad_day = breadth_ratio_val is not None and breadth_ratio_val >= 55
    narrow_day = breadth_ratio_val is not None and breadth_ratio_val < 45
    heavy_conc = leadership_conc is not None and leadership_conc >= 70

    # 최종 판정: AI가 바꾸면 안 되는 고정 결론
    if risk_score >= 65 and overheating >= 70:
        final = "Risk-On 과열"
        action = "추격매수보다 눌림 대기"
    elif risk_score >= 65 and broad_day and heavy_conc:
        # 상승 종목은 넓게 퍼졌지만(순환매) 거래대금은 여전히 대형주에 집중된 날.
        final = "Risk-On 순환·확산"
        action = "주도주 외 확산 종목까지 분산 접근"
    elif risk_score >= 65 and (narrow_day or breadth_score < 45):
        final = "Risk-On 쏠림장"
        action = "주도주 쏠림 확인 후 선별 접근"
    elif risk_score >= 65:
        final = "Risk-On"
        action = "분할 접근 가능"
    elif risk_score <= 40:
        final = "Risk-Off"
        action = "방어 우선"
    else:
        final = "Neutral"
        action = "관망 또는 확인 후 접근"

    if final == "Risk-On 순환·확산":
        one_liner = "위험선호가 유지되는 가운데 상승 종목이 넓게 퍼진 순환매 장입니다. 다만 거래대금은 여전히 대형주에 집중돼 있습니다."
    elif final.startswith("Risk-On") and overheating >= 70 and breadth_score < 45:
        one_liner = "위험선호는 살아 있지만 과열과 쏠림이 겹쳐 신규 추격은 불리한 장입니다."
    elif final.startswith("Risk-On") and breadth_score < 45:
        one_liner = "위험선호는 유지되지만 상승이 일부 업종·대형주에 집중된 장입니다."
    elif final.startswith("Risk-On") and stability_score < 50:
        one_liner = "지수 추세는 우호적이지만 채권·금리 스트레스가 함께 올라 추격에는 선별이 필요한 장입니다."
    elif final.startswith("Risk-On"):
        one_liner = "신용·변동성 안정도와 주요 지수 추세가 함께 우호적인 장입니다."
    elif final == "Risk-Off":
        one_liner = "신용·변동성·금리 또는 추세 지표가 방어적 대응을 요구하는 장입니다."
    else:
        one_liner = "위험선호와 부담 요인이 엇갈려 방향 확인이 필요한 중립 장세입니다."

    headline = f"{final}: {action}"

    if final == "Risk-On 순환·확산":
        question_theme = "breadth"
    elif leadership and leadership.leadership_score >= 70:
        question_theme = "concentration"
    elif fx_stress >= 55:
        question_theme = "fx"
    elif overheating >= 70:
        question_theme = "overheating"
    else:
        question_theme = "breadth"
    core_question = build_session_core_question(question_theme, session)

    if final == "Risk-On 순환·확산":
        beginner_translation = "오늘은 큰 주도주 몇 개만 오른 게 아니라 여러 종목이 고르게 오른 날입니다. 주도주만 좇기보다, 함께 오른 다른 업종까지 넓게 살펴볼 만한 구간입니다."
    elif final.startswith("Risk-On") and overheating >= 70 and breadth_score < 45:
        beginner_translation = "시장은 아직 분위기가 좋지만, 좋은 종목 몇 개만 너무 빨리 달린 장입니다. 지금 따라 사기보다는 쉬어갈 때를 기다리는 편이 낫습니다."
    elif final.startswith("Risk-On") and entry_score < 40:
        beginner_translation = "전체 분위기는 괜찮지만 새로 들어가기에는 가격 부담이 큽니다. 좋은 장과 좋은 진입 가격은 다릅니다."
    elif final.startswith("Risk-On"):
        beginner_translation = "시장의 큰 분위기는 우호적입니다. 다만 이미 많이 오른 종목은 나눠서 접근하는 편이 안전합니다."
    elif final == "Risk-Off":
        beginner_translation = "시장이 불안정합니다. 수익보다 손실을 줄이는 쪽이 먼저인 장입니다."
    else:
        beginner_translation = "좋은 신호와 부담 신호가 섞여 있습니다. 방향이 더 분명해질 때까지 확인이 필요한 장입니다."

    if not key_drivers:
        key_drivers.append("명확한 우호 요인은 제한적")
    if not risks:
        risks.append("뚜렷한 단기 위험 신호는 제한적")

    timing_scores = build_timing_scores(results, breadth_score, fx_stress, leadership)

    return MarketRegime(
        risk_score=risk_score,
        overheating_score=overheating,
        breadth_score=breadth_score,
        fx_stress_score=fx_stress,
        credit_score=stability_score,
        entry_score=entry_score,
        timing_scores=timing_scores,
        final_label=final,
        action_label=action,
        headline=headline,
        one_liner=one_liner,
        core_question=core_question,
        beginner_translation=beginner_translation,
        key_drivers=key_drivers[:6],
        risks=risks[:6],
    )


def _read_regime_log_df(path: str = REGIME_LOG_PATH):
    """regime 로그를 DataFrame으로 읽는다. GCS 우선, 없으면 로컬 파일."""
    import io
    text = _gcs_download_text(GCS_REGIME_LOG_KEY)
    if text is not None:
        try:
            return pd.read_csv(io.StringIO(text))
        except Exception as e:
            print(f"[regime log] GCS 파싱 실패: {type(e).__name__}: {e}", file=sys.stderr)
    if os.path.exists(path):
        try:
            return pd.read_csv(path)
        except Exception:
            return None
    return None


def load_previous_regime_change(regime: MarketRegime, path: str = REGIME_LOG_PATH) -> Optional[RegimeChange]:
    df = _read_regime_log_df(path)
    if df is None or df.empty:
        return None
    try:
        prev = df.iloc[-1]
        risk_delta = int(round(regime.risk_score - float(prev.get("risk_score", regime.risk_score))))
        overheating_delta = int(round(regime.overheating_score - float(prev.get("overheating_score", regime.overheating_score))))
        breadth_delta = int(round(regime.breadth_score - float(prev.get("breadth_score", regime.breadth_score))))
        fx_delta = int(round(regime.fx_stress_score - float(prev.get("fx_stress_score", regime.fx_stress_score))))
        stability_delta = int(round(regime.credit_score - float(prev.get("stability_score", regime.credit_score))))
        entry_delta = int(round(regime.entry_score - float(prev.get("entry_score", regime.entry_score))))
        parts = []
        if abs(risk_delta) >= 3:
            parts.append(f"위험선호 {'개선' if risk_delta > 0 else '악화'} {risk_delta:+d}p")
        if abs(overheating_delta) >= 3:
            parts.append(f"과열도 {'상승' if overheating_delta > 0 else '완화'} {overheating_delta:+d}p")
        if abs(breadth_delta) >= 3:
            parts.append(f"시장 확산도 {'개선' if breadth_delta > 0 else '약화'} {breadth_delta:+d}p")
        if abs(fx_delta) >= 3:
            parts.append(f"원화 부담 {'상승' if fx_delta > 0 else '완화'} {fx_delta:+d}p")
        if abs(entry_delta) >= 3:
            parts.append(f"신규 진입 매력도 {'개선' if entry_delta > 0 else '악화'} {entry_delta:+d}p")
        summary = " / ".join(parts) if parts else "전일 대비 점수 변화는 제한적입니다."
        return RegimeChange(
            previous_run_date=str(prev.get("run_date", "이전 실행")),
            risk_delta=risk_delta,
            overheating_delta=overheating_delta,
            breadth_delta=breadth_delta,
            fx_stress_delta=fx_delta,
            stability_delta=stability_delta,
            entry_delta=entry_delta,
            summary=summary,
        )
    except Exception:
        return None


def format_regime_change(change: Optional[RegimeChange]) -> str:
    if not change:
        return ""
    lines = []
    lines.append("[오늘의 변화]")
    lines.append(f"- 이전 기준: {change.previous_run_date}")
    lines.append(f"- 글로벌 위험선호 변화: {change.risk_delta:+d}p")
    lines.append(f"- 추격매수 부담 변화: {change.overheating_delta:+d}p")
    lines.append(f"- 시장 확산도 변화: {change.breadth_delta:+d}p")
    lines.append(f"- 원화 부담 변화: {change.fx_stress_delta:+d}p")
    lines.append(f"- 신용·변동성 안정도 변화: {change.stability_delta:+d}p")
    lines.append(f"- 신규 진입 매력도 변화: {change.entry_delta:+d}p")
    lines.append(f"→ {change.summary}")
    return "\n".join(lines)


def format_regime_scoreboard(regime: MarketRegime, change: Optional[RegimeChange] = None) -> str:
    lines = []
    lines.append("[오늘의 결론]")
    lines.append(regime.headline)
    lines.append(f"한 줄: {regime.one_liner}")
    lines.append("")
    lines.append("[오늘의 핵심 질문]")
    lines.append(regime.core_question)
    lines.append("")
    lines.append("[초보자 번역]")
    lines.append(regime.beginner_translation)
    lines.append("")
    lines.append("[📊 그래프로 보는 전체 지표]")
    lines.append(f"카드+차트로 한눈에 보기 → {DASHBOARD_URL}")
    lines.append("")
    lines.append("[시장 신호등]")
    lines.append(f"- 글로벌 위험선호: {score_light(regime.risk_score)} ({regime.risk_score}/100)")
    lines.append(f"- 신용·변동성 안정도: {score_light(regime.credit_score)} ({regime.credit_score}/100)")
    lines.append(f"- 시장 확산도: {breadth_label(regime.breadth_score)} ({regime.breadth_score}/100)")
    lines.append(f"- 원화 부담: {score_light(regime.fx_stress_score, 'negative')} ({regime.fx_stress_score}/100)")
    lines.append(f"- 추격매수 부담: {score_light(regime.overheating_score, 'negative')} ({regime.overheating_score}/100)")
    lines.append(f"- 신규 진입 매력도: {score_light_entry(regime.entry_score)} ({regime.entry_score}/100)")
    lines.append("")
    lines.append(format_timing_scores(regime.timing_scores))
    if change:
        lines.append("")
        lines.append(format_regime_change(change))
    lines.append("")
    lines.append("[핵심 근거]")
    for item in regime.key_drivers:
        lines.append(f"- {item}")
    lines.append("")
    lines.append("[핵심 위험]")
    for item in regime.risks:
        lines.append(f"- {item}")
    return "\n".join(lines)


def format_action_plan(regime: MarketRegime) -> str:
    if regime.final_label.startswith("Risk-On") and regime.overheating_score >= 70:
        items = [
            "신규 진입: 급등 구간 추격보다 눌림목 또는 과열 완화 확인",
            "보유자: 주도 업종 비중이 과도한지 점검",
            "확인 지표: 시장 확산도 회복, VIX/HY 안정 유지, 원화 부담 완화",
        ]
    elif regime.final_label == "Risk-On 순환·확산":
        items = [
            "신규 진입: 주도 대형주 외에 함께 오른 확산 업종에서 분산 후보 탐색",
            "보유자: 주도주 쏠림 비중을 확산 종목으로 일부 분산 검토",
            "확인 지표: 상승 종목 비율 55% 이상 지속 + 거래대금 쏠림 완화 여부",
        ]
    elif regime.final_label.startswith("Risk-On") and regime.breadth_score < 45:
        items = [
            "신규 진입: 지수보다 주도 업종·종목의 지속성 확인",
            "보유자: 소수 대형주 의존 장세인지 점검",
            "확인 지표: 상승 종목 비율 50% 이상 회복 여부",
        ]
    elif regime.final_label.startswith("Risk-On"):
        items = [
            "신규 진입: 분할 접근 가능, 단 이격도 높은 종목은 가격 부담 확인",
            "보유자: 추세 유지 종목은 보유하되 과열 구간은 비중 관리",
            "확인 지표: VIX 20 이하, HY 스프레드 안정 지속 여부",
        ]
    elif regime.final_label == "Risk-Off":
        items = [
            "신규 진입: 방어 우선, 현금 비중과 손실 제한 기준 확인",
            "보유자: 변동성 큰 종목과 레버리지 노출 축소 검토",
            "확인 지표: VIX·HY·원화 부담 완화 여부",
        ]
    else:
        items = [
            "신규 진입: 방향 확인 전까지 관망 또는 소액 분할",
            "보유자: 지수보다 개별 보유 종목의 추세와 실적 모멘텀 점검",
            "확인 지표: 위험선호 점수 65 이상 회복 또는 40 이하 이탈 여부",
        ]
    return "\n".join(f"{i}. {item}" for i, item in enumerate(items, start=1))




def format_regime_change_conditions(regime: MarketRegime) -> str:
    lines = []
    lines.append("[판정이 바뀌는 조건]")
    if regime.final_label.startswith("Risk-On"):
        lines.append("- Risk-On 유지 조건: VIX 20 이하, HY 스프레드 4% 이하, MOVE 경계권 미진입, 주요 지수 50일선 유지")
        lines.append("- 쏠림 경고 완화 조건: 시장 확산도 50점 이상 회복, 상승 종목 비율 55% 이상, 50일선 위 종목 비율 개선")
        lines.append("- 경계 강화 조건: 시장 확산도 20점 이하 지속 + 원/달러 추가 상승 + SOX 과열 지속")
        lines.append("- Risk-Off 전환 조건: VIX 20 돌파 + HY 스프레드 확대 + KOSPI 50일선 이탈")
    elif regime.final_label == "Risk-Off":
        lines.append("- 방어 유지 조건: VIX/HY/MOVE 중 2개 이상이 경계권에 머무는 경우")
        lines.append("- 중립 회복 조건: VIX 20 이하, HY 안정, 원/달러 진정, 주요 지수 50일선 회복")
        lines.append("- Risk-On 전환 조건: 신용·변동성 안정도 60점 이상 + 글로벌 위험선호 65점 이상 + 시장 확산도 회복")
    else:
        lines.append("- Risk-On 전환 조건: 글로벌 위험선호 65점 이상, 신용·변동성 안정도 개선, 주요 지수 50일선 회복")
        lines.append("- 쏠림 완화 조건: 시장 확산도 50점 이상, 상승 종목 비율 55% 이상")
        lines.append("- Risk-Off 전환 조건: VIX 20 돌파 + HY 스프레드 확대 + KOSPI/KOSDAQ 50일선 이탈")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────
# AI 기반 시장 코멘트 (Claude API)
# ─────────────────────────────────────────────────────────
CLAUDE_MODEL = "claude-sonnet-4-6"
CLAUDE_MAX_TOKENS = 3000

AI_SYSTEM_PROMPT = """\
당신은 20년 경력의 매크로 시장 전략가이며, 매일 오전·오후 시장 브리핑을 작성합니다.
이 브리핑은 투자 전문가인 구독자와 경제 지식이 많지 않은 일반인 구독자가 함께 읽습니다.

[절대 규칙]
아래의 시스템 판정을 바꾸지 마세요.
당신은 판정을 새로 내리는 사람이 아니라, 시스템 판정을 사람이 이해하기 쉽게 설명하는 해설자입니다.
최종 국면, 점수, 오늘의 액션은 반드시 제공된 시스템 판정을 그대로 따르세요.

[출력 형식 — 반드시 이 구조를 따르세요]

◆ 오늘의 한눈 요약
(경제 지식이 없는 사람도 이해할 수 있는 쉬운 설명. 3~5문장.
 "오늘 시장은 왜 이런 판정인지"와 "오늘 무엇을 조심해야 하는지"를 일상 언어로 전달하세요.
 전일 점수 변화 데이터가 제공되면 오늘 판정이 어제보다 좋아졌는지, 나빠졌는지, 변화가 제한적인지를 반드시 1문장으로 설명하세요.
 전일 점수 데이터가 없으면 이 문장은 생략하세요.)

◆ 전체 시장 환경 (금리·환율·변동성·신용)
(VIX, MOVE, 하이일드 스프레드, 명목금리, 실질금리, 기대인플레, 환율, DXY를 연결해서 2~4문장으로 해석하세요.
 명목금리 상승이 실질금리 상승인지 기대인플레 상승인지 구분하고, 성장주/반도체 할인율 부담을 설명하세요.
 VIX는 안정적이나 MOVE가 상승하면 "주식시장 표정은 안정적이나 채권시장 스트레스는 남아 있다"는 구조로 설명하세요.)

◆ 한국 시장
(KOSPI·KOSDAQ 이격도, 시장 폭, 거래대금 쏠림, 20/50/200일선 위 종목 비율, 신고가/신저가를 3~5문장으로 해석하세요.
 대형주/중소형주 괴리와 주도 업종 쏠림을 반드시 언급하세요.)

◆ 미국 시장
(S&P500·NASDAQ·SOX 이격도, 실질금리, 기대인플레, MOVE, 섹터 상대강도를 2~4문장으로 해석하세요.
 한국 시장과의 동조화/괴리도 언급하세요.)

→ 종합
(시스템의 최종 국면과 오늘의 액션을 1~2문장으로 다시 정리하세요.)

[분석 원칙]
- 숫자를 단순 나열하지 말고 지표 간 인과관계와 맥락을 연결하세요.
- 이격도는 위험 신호 자체가 아니라 추세 강도와 진입 타이밍 부담을 분리해서 설명하세요.
- 특정 종목의 매수/매도를 추천하지 마세요. 시장 구조와 환경에 대한 분석만 합니다.
- 데이터에 없는 뉴스, 이벤트, 실적을 추측하지 마세요.
- 데이터에 '[이번 브리핑 관점]' 블록이 있으면, 그 지시에 따라 어느 시장을 주연으로 둘지·어느 시장을 먼저·더 비중 있게 서술할지를 결정하세요. 방금 마감한 시장의 '오늘 변화'를 우선하고, 아직 개장 전이라 전일 종가에 머문 시장은 '개장 전 점검' 관점으로만 다루세요.
- 인사말, 머리말, 면책조항은 쓰지 마세요. 바로 "◆ 오늘의 한눈 요약"부터 시작하세요.
"""


def _build_indicator_data_text(
    results: List[IndicatorResult],
    leading_report: str = "",
    regime: Optional[MarketRegime] = None,
    leadership: Optional[LeadershipState] = None,
    regime_change: Optional[RegimeChange] = None,
) -> str:
    lines = []
    lines.append(f"실행 시각: {now_kst().strftime('%Y-%m-%d %H:%M')} KST ({run_label_kst()})")
    lines.append("")
    lines.append(current_session().focus_block)
    lines.append("")
    if regime:
        lines.append("[시스템 판정]")
        lines.append(f"- 최종 국면: {regime.final_label}")
        lines.append(f"- Risk-On 점수: {regime.risk_score}/100")
        lines.append(f"- 과열 점수: {regime.overheating_score}/100")
        lines.append(f"- 시장 확산도 점수: {regime.breadth_score}/100")
        lines.append(f"- 원화 부담 점수: {regime.fx_stress_score}/100")
        lines.append(f"- 신용·변동성 안정도: {regime.credit_score}/100")
        lines.append(f"- 신규 진입 매력도: {regime.entry_score}/100")
        lines.append(f"- 공포 매수 점수: {regime.timing_scores.fear_buy_score}/100")
        lines.append(f"- 광기 축소 점수: {regime.timing_scores.mania_reduce_score}/100")
        lines.append(f"- 최종 타이밍: {regime.timing_scores.final_timing}")
        lines.append(f"- 공포-광기 해석: {regime.timing_scores.translation}")
        lines.append(f"- 오늘의 핵심 질문: {regime.core_question}")
        lines.append(f"- 초보자 번역: {regime.beginner_translation}")
        lines.append(f"- 오늘의 액션: {regime.action_label}")
        lines.append(f"- 한 줄 판단: {regime.one_liner}")
        lines.append(f"- 핵심 근거: {' / '.join(regime.key_drivers)}")
        lines.append(f"- 핵심 위험: {' / '.join(regime.risks)}")
        lines.append("")
    if regime_change:
        lines.append("[전일 대비 변화]")
        lines.append(f"- {regime_change.summary}")
        lines.append(f"- 글로벌 위험선호 {regime_change.risk_delta:+d}p, 추격매수 부담 {regime_change.overheating_delta:+d}p, 시장 확산도 {regime_change.breadth_delta:+d}p, 원화 부담 {regime_change.fx_stress_delta:+d}p, 신규 진입 매력도 {regime_change.entry_delta:+d}p")
        lines.append("")
    if leadership:
        lines.append("[주도주/시장 폭 점수]")
        lines.append(f"- 주도주 쏠림 점수: {leadership.leadership_score}/100 ({leadership.leadership_label})")
        if leadership.breadth_ratio is not None:
            lines.append(f"- 상승 종목 비율: {leadership.breadth_ratio:.1f}%")
        if leadership.pct_above_20ma is not None:
            lines.append(f"- 20일선 위 종목 비율: {leadership.pct_above_20ma:.1f}%")
        if leadership.pct_above_50ma is not None:
            lines.append(f"- 50일선 위 종목 비율: {leadership.pct_above_50ma:.1f}%")
        if leadership.pct_above_200ma is not None:
            lines.append(f"- 200일선 위 종목 비율: {leadership.pct_above_200ma:.1f}%")
        if leadership.high_52w_count is not None and leadership.low_52w_count is not None:
            lines.append(f"- 52주 신고가/신저가: {leadership.high_52w_count} / {leadership.low_52w_count}")
        if leadership.top10_value_concentration is not None:
            lines.append(f"- 상위 10개 거래대금 비중: {leadership.top10_value_concentration:.1f}%")
        if leadership.semiconductor_share_top10 is not None:
            lines.append(f"- 상위 10개 내 반도체/IT 거래대금 비중: {leadership.semiconductor_share_top10:.1f}%")
        if leadership.kospi_kosdaq_gap is not None:
            lines.append(f"- KOSPI-KOSDAQ 이격도 차: {leadership.kospi_kosdaq_gap:.1f}p")
        lines.append("")

    lines.append("[수집 지표]")
    for r in results:
        if r.ok:
            sig = signal_from_indicator(r)
            chg = f" | 변화: {r.change_text}" if r.change_text else ""
            lines.append(
                f"- {r.name}: {r.value_text} | 상태: {r.state}{chg} | 내부 신호: {signal_to_text(sig)}"
            )
            lines.append(f"  해석: {r.comment}")
        else:
            lines.append(f"- {r.name}: 수집 실패")
    if leading_report:
        lines.append("")
        lines.append("[주도주 브리핑 데이터]")
        lines.append(leading_report)
    return "\n".join(lines)


def build_market_comment_ai(
    results: List[IndicatorResult],
    leading_report: str = "",
    regime: Optional[MarketRegime] = None,
    leadership: Optional[LeadershipState] = None,
    regime_change: Optional[RegimeChange] = None,
) -> str:
    """Claude API를 호출하여 자연어 시장 코멘트를 생성."""
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY가 설정되지 않았습니다.")

    user_msg = _build_indicator_data_text(results, leading_report, regime, leadership, regime_change)

    payload = json.dumps({
        "model": CLAUDE_MODEL,
        "max_tokens": CLAUDE_MAX_TOKENS,
        "system": AI_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_msg}],
    }).encode("utf-8")

    req = Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )

    with urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    content = data.get("content", [])
    texts = [block["text"] for block in content if block.get("type") == "text"]
    if not texts:
        raise RuntimeError("Claude API 응답에 텍스트가 없습니다.")

    return "\n".join(texts).strip()


# ── 오늘의 핵심 이슈 (웹 검색 그라운딩) ─────────────────────────────────
DAILY_ISSUES_MODEL = "claude-sonnet-4-6"
DAILY_ISSUES_MAX_TOKENS = 1500
# 기본 웹 검색 버전(추가 의존성 없음). 동적 필터링이 필요하면
# web_search_20260209 + 코드 실행 도구를 함께 활성화해야 한다.
WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 6}

DAILY_ISSUES_SYSTEM_PROMPT = """\
당신은 한국·미국 증시를 담당하는 뉴스 큐레이터입니다.
반드시 web_search 도구로 '실제 검색해 확인된' 사실만 쓰세요. 검색으로 확인되지 않은 내용은 절대 쓰지 마세요.

[임무]
최근 약 24~36시간 동안 한국·미국 증시에 영향이 큰 이슈에서
긍정적 이슈 정확히 2개, 부정적 이슈 정확히 2개를 고르세요.

[선별 기준]
- 거시(FOMC·금리·CPI·고용·중앙은행), 지정학(전쟁·협상·제재), 산업(반도체·AI·에너지), 정책·규제 중심.
- 개별 종목 등락이 아니라 '시장 전체 방향'에 영향을 줄 사건을 우선합니다.
- 확인된 대형 이슈가 한쪽에서 2개 미만이면 억지로 채우지 말고 그 줄에 "확인된 추가 이슈 부족"이라고 적으세요.

[출력 형식 — 정확히 이 구조, 다른 말 없이 바로 시작]
■ 긍정 이슈 (+)
1. {헤드라인} — {왜 시장에 중요한가, 1문장} | 영향: {지수/자산} | 출처: {매체}, {날짜}
2. ...
■ 부정 이슈 (−)
1. {헤드라인} — {왜 시장에 중요한가, 1문장} | 영향: {지수/자산} | 출처: {매체}, {날짜}
2. ...

[금지]
- 검색되지 않은 추측·예상·루머 작성 금지.
- 특정 종목 매수/매도 추천 금지.
- 머리말·맺음말·면책조항 금지. 바로 "■ 긍정 이슈 (+)"부터 시작.
"""


def build_daily_issues_ai(session: Optional[BriefSession] = None) -> str:
    """web_search로 그라운딩된 '오늘의 핵심 이슈 2+2'를 생성한다.

    검색으로 확인된 사실만 사용하도록 제약하며, 실패 시 호출부에서
    try/except로 감싸 메일 전체 발송에는 영향을 주지 않는다.
    """
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY가 설정되지 않았습니다.")

    session = session or current_session()
    today = today_kst_str()
    if session.key == "morning":
        frame = (
            "지금은 한국시간 오전입니다. 미국 정규장이 막 마감했고 한국장은 개장 전입니다. "
            "간밤 미국장·글로벌 지정학 이슈가 '오늘 한국장 개장'에 줄 영향 위주로 선별하세요."
        )
    else:
        frame = (
            "지금은 한국시간 오후입니다. 한국 정규장이 막 마감했고 미국장은 개장 전입니다. "
            "오늘 한국장 이슈와, '오늘 밤 미국장'에 영향을 줄 이슈 위주로 선별하세요."
        )

    user_msg = (
        f"오늘 날짜: {today} (KST)\n{frame}\n\n"
        "web_search로 최신 뉴스를 검색해, 위 기준에 맞는 긍정 2개·부정 2개 이슈를 정리해 주세요. "
        "각 이슈는 반드시 검색 결과로 확인된 것이어야 하며 출처와 날짜를 명시하세요."
    )

    payload = json.dumps({
        "model": DAILY_ISSUES_MODEL,
        "max_tokens": DAILY_ISSUES_MAX_TOKENS,
        "system": DAILY_ISSUES_SYSTEM_PROMPT,
        "tools": [WEB_SEARCH_TOOL],
        "messages": [{"role": "user", "content": user_msg}],
    }).encode("utf-8")

    req = Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )

    # 웹 검색은 단일 요청 내에서 여러 번 검색을 왕복하므로 타임아웃을 넉넉히 둔다.
    # HTTPError(예: 웹 검색 미활성 시 400)는 본문에 실패 원인이 들어있으므로 그대로 노출한다.
    try:
        with urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        raise RuntimeError(f"Claude API 이슈 호출 실패 (HTTP {e.code}): {body[:500]}") from e

    content = data.get("content", [])
    texts = [b["text"] for b in content if b.get("type") == "text"]
    result = "\n".join(texts).strip()
    if not result:
        # 웹 검색이 서버에서 실패하면 텍스트 없이 에러 블록만 올 수 있다. 진단을 위해 블록 종류를 남긴다.
        block_types = [b.get("type") for b in content]
        raise RuntimeError(f"Claude API 응답(이슈)에 텍스트가 없습니다. 블록 종류={block_types}")
    return result


def build_market_comment_rule_based(
    results: List[IndicatorResult],
    regime: MarketRegime,
    leadership: Optional[LeadershipState] = None,
) -> str:
    """AI 실패 시에도 시스템 판정을 유지하는 규칙 기반 코멘트."""
    lines: List[str] = []
    lines.append("◆ 오늘의 한눈 요약")
    lines.append(regime.one_liner)
    lines.append(f"최종 판정은 {regime.final_label}이며, 오늘의 액션은 '{regime.action_label}'입니다.")
    lines.append("")

    lines.append("◆ 전체 시장 환경 (금리·환율·변동성·신용)")
    macro = []
    for name in ["VIX 지수", "MOVE 지수", "미국 하이일드 스프레드", "원/달러 환율", "DXY 달러인덱스", "미국 10년물 금리", "미국 10년 실질금리", "미국 10년 기대인플레"]:
        r = get_result(results, name)
        if r:
            macro.append(f"{name} {r.value_text}({r.state})")
    lines.append(" / ".join(macro) if macro else "매크로 핵심 지표 일부가 수집되지 않았습니다.")
    lines.append("")

    lines.append("◆ 한국 시장")
    kr_parts = []
    for name in ["KOSPI 50일 이격도", "KOSDAQ 50일 이격도", "삼성전자 50일 이격도"]:
        r = get_result(results, name)
        if r:
            kr_parts.append(f"{name.replace(' 50일 이격도', '')} {r.value_text}({r.state})")
    if leadership:
        kr_parts.append(f"시장 폭 {leadership.breadth_ratio:.1f}%" if leadership.breadth_ratio is not None else "시장 폭 자료 제한")
        kr_parts.append(f"주도주 쏠림 {leadership.leadership_score}/100({leadership.leadership_label})")
    lines.append(" / ".join(kr_parts) if kr_parts else "한국 시장 핵심 지표 일부가 수집되지 않았습니다.")
    lines.append("")

    lines.append("◆ 미국 시장")
    us_parts = []
    for name in ["S&P500 50일 이격도", "NASDAQ 50일 이격도", "SOX 반도체지수 50일 이격도", "미국 10년 실질금리", "MOVE 지수"]:
        r = get_result(results, name)
        if r:
            us_parts.append(f"{name.replace(' 50일 이격도', '')} {r.value_text}({r.state})")
    lines.append(" / ".join(us_parts) if us_parts else "미국 시장 핵심 지표 일부가 수집되지 않았습니다.")
    lines.append("")

    lines.append("→ 종합")
    lines.append(f"{regime.headline}. 핵심 근거는 {' / '.join(regime.key_drivers[:3])}이며, 핵심 위험은 {' / '.join(regime.risks[:3])}입니다.")
    return "\n".join(lines)


def build_market_comment(
    results: List[IndicatorResult],
    leading_report: str,
    regime: MarketRegime,
    leadership: Optional[LeadershipState] = None,
    regime_change: Optional[RegimeChange] = None,
) -> str:
    """시장 코멘트 생성: 시스템 판정은 고정하고 AI는 표현만 담당."""
    if ANTHROPIC_API_KEY:
        try:
            return build_market_comment_ai(results, leading_report, regime, leadership, regime_change)
        except Exception:
            # 독자 메일에는 기술적 오류를 노출하지 않는다.
            return build_market_comment_rule_based(results, regime, leadership)

    return build_market_comment_rule_based(results, regime, leadership)


def build_summary(results: List[IndicatorResult], regime: Optional[MarketRegime] = None) -> str:
    lines = []
    if regime:
        lines.append(f"- 최종 국면: {regime.final_label}")
        lines.append(f"- 오늘의 액션: {regime.action_label}")
        lines.append(f"- 글로벌 위험선호 {regime.risk_score}/100, 추격매수 부담 {regime.overheating_score}/100, 시장 확산도 {regime.breadth_score}/100, 신규 진입 매력도 {regime.entry_score}/100")
        lines.append(f"- 공포 매수 {regime.timing_scores.fear_buy_score}/100, 광기 축소 {regime.timing_scores.mania_reduce_score}/100, 최종 타이밍: {regime.timing_scores.final_timing}")
        lines.append("")

    ok = [r for r in results if r.ok]
    stress_items = []
    timing_items = []
    direction_items = []
    for r in ok:
        sig = signal_from_indicator(r)
        if sig.stress >= 2:
            stress_items.append(f"{r.name}: {r.state} ({r.value_text})")
        if sig.timing >= 2:
            timing_items.append(f"{r.name}: {r.state} ({r.value_text})")
        if sig.direction >= 1:
            direction_items.append(f"{r.name}: {r.value_text}")

    if stress_items:
        lines.append("[스트레스 신호]")
        lines.extend(f"- {x}" for x in stress_items[:6])
        lines.append("")
    if timing_items:
        lines.append("[타이밍/과열 신호]")
        lines.extend(f"- {x}" for x in timing_items[:6])
        lines.append("")
    if direction_items:
        lines.append("[우호적 추세 신호]")
        lines.extend(f"- {x}" for x in direction_items[:6])
        lines.append("")

    us2 = get_result(results, "미국 2년물 금리")
    us10 = get_result(results, "미국 10년물 금리")
    us3m = get_result(results, "미국 3개월물 금리")
    fedfunds = get_result(results, "Fed Funds 금리")
    real10 = get_result(results, "미국 10년 실질금리")
    breakeven10 = get_result(results, "미국 10년 기대인플레")
    curve_3m = get_result(results, "미국 10Y-3M 금리차")
    curve_ff = get_result(results, "미국 10Y-Fed Funds 금리차")
    move = get_result(results, "MOVE 지수")
    kr10 = get_result(results, "한국 10년물 금리")
    fx = get_result(results, "원/달러 환율")
    dxy = get_result(results, "DXY 달러인덱스")
    cnh = get_result(results, "USD/CNH")
    jpy = get_result(results, "USD/JPY")
    wti = get_result(results, "WTI 유가")

    if us2 and us10 and us2.raw_value is not None and us10.raw_value is not None:
        spread = us10.raw_value - us2.raw_value
        curve_state = "역전" if spread < 0 else "정상"
        lines.append("[미국 장단기금리차]")
        lines.append(f"- 10년-2년 스프레드: {spread:.2f}%p / {curve_state}")
        if spread < 0:
            lines.append("- 장단기금리 역전은 경기 둔화 우려와 함께 해석해야 합니다.")
        lines.append("")

    if curve_3m or curve_ff:
        lines.append("[경기 시계]")
        if curve_3m:
            lines.append(f"- 10Y-3M 금리차: {curve_3m.value_text} / {curve_3m.state}")
        if curve_ff:
            lines.append(f"- 10Y-Fed Funds 금리차: {curve_ff.value_text} / {curve_ff.state}")
        lines.append("- 금리차가 플러스권이면 경기침체 선행 경고는 낮고, 역전되면 경기 사이클 경계가 커집니다.")
        lines.append("")

    if real10 or breakeven10 or move:
        lines.append("[성장주 할인율/채권 변동성]")
        if real10:
            lines.append(f"- 미국 10년 실질금리: {real10.value_text} / {real10.state}")
        if breakeven10:
            lines.append(f"- 미국 10년 기대인플레: {breakeven10.value_text} / {breakeven10.state}")
        if move:
            lines.append(f"- MOVE: {move.value_text} / {move.state}")
        lines.append("- 명목금리보다 실질금리와 MOVE가 성장주·반도체 추격 부담을 더 직접적으로 보여줍니다.")
        lines.append("")

    if kr10 and us10 and kr10.raw_value is not None and us10.raw_value is not None:
        kr_us_spread = kr10.raw_value - us10.raw_value
        lines.append("[한미 10년물 금리차]")
        lines.append(f"- 한국 10년 - 미국 10년: {kr_us_spread:.2f}%p")
        if kr_us_spread < 0 and fx and fx.raw_value is not None and fx.raw_value >= FX_IDEAL_HIGH:
            lines.append("- 한국 10년물이 미국 10년물보다 낮고 환율도 높은 편이면 원화 약세 압력을 함께 점검해야 합니다.")
        lines.append("")

    if fx and dxy:
        lines.append("[환율 해석 보조]")
        lines.append(f"- 원/달러 상태: {fx.state}")
        lines.append(f"- DXY 상태: {dxy.state}")
        if cnh:
            lines.append(f"- USD/CNH 상태: {cnh.state}")
        if jpy:
            lines.append(f"- USD/JPY 상태: {jpy.state}")
        if wti:
            lines.append(f"- WTI 상태: {wti.state}")
        lines.append("- 원/달러와 DXY가 함께 상승하면 글로벌 달러 강세, DXY가 잠잠한데 CNH/JPY가 오르면 아시아 통화 약세, 유가까지 오르면 수입물가 부담을 함께 봅니다.")

    return "\n".join(lines).strip() or "- 특별한 위험/경계 신호는 없습니다."


def subject_prefix_from_regime(regime: MarketRegime) -> str:
    if regime.final_label == "Risk-Off" or regime.risk_score <= 40:
        return "🚨"
    if regime.overheating_score >= 75 or regime.fx_stress_score >= 75 or regime.breadth_score <= 35:
        return "⚠️"
    if regime.final_label.startswith("Risk-On"):
        return "🟢"
    return "📊"


def build_email(
    results: List[IndicatorResult],
    leading_report: str = "",
    regime: Optional[MarketRegime] = None,
    leadership: Optional[LeadershipState] = None,
    regime_change: Optional[RegimeChange] = None,
    daily_issues: str = "",
) -> tuple[str, str]:
    today = today_kst_str()
    label = run_label_kst()
    if regime is None:
        regime = classify_market_regime(results, leadership)

    subject_prefix = subject_prefix_from_regime(regime)
    subject = f"{subject_prefix} [Daily Market Brief · {label}] {today} {regime.final_label} · {regime.action_label}"

    lines = []
    lines.append(f"Daily Market Brief · {label} ({today} KST)")
    lines.append("=" * 58)
    lines.append("")
    lines.append(format_regime_scoreboard(regime, regime_change))
    lines.append("")
    lines.append("-" * 58)
    lines.append("")
    if daily_issues:
        lines.append("[오늘의 핵심 이슈] (웹 검색 기반)")
        lines.append(daily_issues)
        lines.append("")
        lines.append("※ 이슈는 웹 검색으로 자동 수집되며 부정확할 수 있으니 반드시 원문 출처를 확인하세요.")
        lines.append("")
        lines.append("-" * 58)
        lines.append("")
    lines.append("[시장 해석]")
    lines.append(build_market_comment(results, leading_report, regime, leadership, regime_change))
    lines.append("")
    lines.append("-" * 58)
    lines.append("")
    lines.append("[오늘의 액션]")
    lines.append(format_action_plan(regime))
    lines.append("")
    lines.append(format_regime_change_conditions(regime))
    lines.append("")
    lines.append("-" * 58)
    lines.append("")
    lines.append("[종합 요약]")
    lines.append(build_summary(results, regime))
    lines.append("")
    lines.append("[지표별 상세]")

    ok_detail_results = [r for r in results if r.ok]
    for i, r in enumerate(ok_detail_results, start=1):
        lines.append("")
        lines.append(f"{i}. {r.name}")
        lines.append(f"- 기준일: {r.date}")
        lines.append(f"- 값: {r.value_text}")
        if r.change_text:
            lines.append(f"- 변화: {r.change_text}")
        sig = signal_from_indicator(r)
        lines.append(f"- 상태: {r.state}")
        lines.append(f"- 내부 신호: {signal_to_text(sig)}")
        lines.append(f"- 해석: {r.comment}")
        if r.mdd_line:
            lines.append(f"- 과열·조정: {r.mdd_line}")
        if r.source:
            lines.append(f"- 출처: {r.source}")

    errors = [r for r in results if not r.ok]
    if errors:
        lines.append("")
        lines.append("[수집 제외]")
        for r in errors:
            # 독자 메일에는 기술적 예외 전체를 노출하지 않는다.
            lines.append(f"- {r.name}: 이번 브리핑에서 제외")

    if leading_report:
        lines.append("")
        lines.append("=" * 58)
        lines.append("[주도주 브리핑]")
        lines.append("")
        lines.append(leading_report)
        lines.append("=" * 58)

    lines.append("")
    lines.append("[기준]")
    lines.append(f"- 이격도: {DISPARITY_STRONG:.0f}+ 추세 우위, {DISPARITY_OVERHEAT:.0f}+ 과열, {DISPARITY_MANIA:.0f}+ 극단 과열")
    lines.append(f"- 원/달러: {FX_HIGH_WARNING:,.0f}원+ 경고, 하루 +{FX_SURGE_PCT:.1f}% 또는 +{FX_SURGE_KRW:.0f}원+ 급등 경고, {FX_IDEAL_LOW:,.0f}~{FX_IDEAL_HIGH:,.0f}원 환전 고려")
    lines.append(f"- VIX: {VIX_CAUTION:.0f}+ 경계, {VIX_FEAR:.0f}+ 공포 확대")
    lines.append(f"- 10년물 금리: 하루 +{YIELD_CAUTION_BP:.0f}bp+ 경계, +{YIELD_RISK_BP:.0f}bp+ 위험 신호")
    lines.append(f"- 실질금리: 하루 +{REAL_YIELD_CAUTION_BP:.0f}bp+ 성장주 할인율 부담, MOVE {MOVE_CAUTION:.0f}+ 채권 변동성 경계")
    lines.append("")
    lines.append("- 이 메일은 자동 생성되었습니다.")
    lines.append("- 지표 해석은 참고용이며 투자 판단의 근거로 단독 사용하면 안 됩니다.")

    return subject, "\n".join(lines)


def append_log(results: List[IndicatorResult], path: str = CSV_LOG_PATH) -> None:
    rows = [r.to_log_row() for r in results]
    df_new = pd.DataFrame(rows)

    if os.path.exists(path):
        df_old = pd.read_csv(path)
        df = pd.concat([df_old, df_new], ignore_index=True)
    else:
        df = df_new

    df.to_csv(path, index=False, encoding="utf-8-sig")


def append_regime_log(
    regime: MarketRegime,
    leadership: Optional[LeadershipState] = None,
    path: str = REGIME_LOG_PATH,
) -> None:
    row = {
        "run_date": now_kst().strftime("%Y-%m-%d %H:%M:%S"),
        "final_label": regime.final_label,
        "action_label": regime.action_label,
        "risk_score": regime.risk_score,
        "overheating_score": regime.overheating_score,
        "breadth_score": regime.breadth_score,
        "fx_stress_score": regime.fx_stress_score,
        "stability_score": regime.credit_score,
        "entry_score": regime.entry_score,
        "fear_buy_score": regime.timing_scores.fear_buy_score,
        "mania_reduce_score": regime.timing_scores.mania_reduce_score,
        "final_timing": regime.timing_scores.final_timing,
        "leadership_score": leadership.leadership_score if leadership else None,
        "breadth_ratio": leadership.breadth_ratio if leadership else None,
        "pct_above_50ma": leadership.pct_above_50ma if leadership else None,
    }
    df_new = pd.DataFrame([row])
    df_old = _read_regime_log_df(path)
    if df_old is not None and not df_old.empty:
        df = pd.concat([df_old, df_new], ignore_index=True)
    else:
        df = df_new

    # GCS에 영속 저장(컨테이너 재시작에도 보존) — 전일 대비 변화 비교의 핵심
    wrote_gcs = _gcs_upload_text(GCS_REGIME_LOG_KEY, df.to_csv(index=False))
    # 로컬에도 best-effort 저장(같은 컨테이너 내 재참조용)
    try:
        df.to_csv(path, index=False, encoding="utf-8-sig")
    except Exception:
        pass
    if not wrote_gcs and not GCS_BUCKET:
        print("[regime log] GCS 미설정 — 로컬 저장만(컨테이너 재시작 시 휘발, 전일 비교 불가)", file=sys.stderr)


def send_email(subject: str, body: str) -> None:
    require_email_env()

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = SMTP_USER
    msg["To"] = MAIL_TO

    context = ssl.create_default_context()
    recipients = [a.strip() for a in MAIL_TO.split(",") if a.strip()]

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context, timeout=30) as server:
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, recipients, msg.as_string())


def collect_indicators() -> List[IndicatorResult]:
    return [
        safe_call(
            "KOSPI 50일 이격도",
            lambda: make_disparity_indicator(
                "KOSPI 50일 이격도",
                fdr_symbols=["KS11"],
                yf_symbols=["^KS11"],
            ),
        ),
        safe_call(
            "KOSDAQ 50일 이격도",
            lambda: make_disparity_indicator(
                "KOSDAQ 50일 이격도",
                fdr_symbols=["KQ11"],
                yf_symbols=["^KQ11"],
            ),
        ),
        safe_call(
            "S&P500 50일 이격도",
            lambda: make_disparity_indicator(
                "S&P500 50일 이격도",
                fdr_symbols=["US500", "S&P500"],
                yf_symbols=["^GSPC"],
            ),
        ),
        safe_call(
            "NASDAQ 50일 이격도",
            lambda: make_disparity_indicator(
                "NASDAQ 50일 이격도",
                fdr_symbols=["IXIC", "NASDAQCOM"],
                yf_symbols=["^IXIC"],
            ),
        ),
        safe_call(
            "SOX 반도체지수 50일 이격도",
            lambda: make_disparity_indicator(
                "SOX 반도체지수 50일 이격도",
                fdr_symbols=["^SOX"],
                yf_symbols=["^SOX"],
            ),
        ),
        safe_call(
            "삼성전자 50일 이격도",
            lambda: make_disparity_indicator(
                "삼성전자 50일 이격도",
                fdr_symbols=["005930"],
                yf_symbols=["005930.KS"],
                unit="원",
            ),
        ),
        safe_call(
            "SK하이닉스 50일 이격도",
            lambda: make_disparity_indicator(
                "SK하이닉스 50일 이격도",
                fdr_symbols=["000660"],
                yf_symbols=["000660.KS"],
                unit="원",
            ),
        ),
        safe_call("원/달러 환율", make_fx_indicator),
        safe_call(
            "DXY 달러인덱스",
            lambda: make_level_indicator(
                "DXY 달러인덱스",
                fdr_symbols=["DX-Y.NYB"],
                yf_symbols=["DX-Y.NYB", "^NYICDX"],
                interpret_func=interpret_dxy,
                unit="",
            ),
        ),
        safe_call("USD/CNH", lambda: make_yfinance_level_indicator("USD/CNH", ["CNH=X"], interpret_usd_cnh, unit="", fdr_symbols=["USD/CNY"])),
        safe_call("USD/JPY", lambda: make_yfinance_level_indicator("USD/JPY", ["JPY=X"], interpret_usd_jpy, unit="", fdr_symbols=["USD/JPY"])),
        safe_call("WTI 유가", lambda: make_yfinance_level_indicator("WTI 유가", ["CL=F"], interpret_wti, unit="달러", fdr_symbols=["CL=F"])),
        safe_call("미국 2년물 금리", lambda: make_us_yield_indicator("미국 2년물 금리", "DGS2")),
        safe_call("미국 3개월물 금리", lambda: make_us_yield_indicator("미국 3개월물 금리", "DGS3MO")),
        safe_call("Fed Funds 금리", lambda: make_us_yield_indicator("Fed Funds 금리", "DFF")),
        safe_call("미국 10년물 금리", lambda: make_us_yield_indicator("미국 10년물 금리", "DGS10")),
        safe_call("미국 10년 실질금리", lambda: make_us_yield_indicator("미국 10년 실질금리", "DFII10")),
        safe_call("미국 10년 기대인플레", lambda: make_breakeven_indicator("미국 10년 기대인플레", "T10YIE")),
        safe_call("미국 10Y-3M 금리차", lambda: make_fred_spread_indicator("미국 10Y-3M 금리차", "DGS10", "DGS3MO")),
        safe_call("미국 10Y-Fed Funds 금리차", lambda: make_fred_spread_indicator("미국 10Y-Fed Funds 금리차", "DGS10", "DFF")),
        safe_call("한국 10년물 금리", make_kr10y_indicator),
        safe_call("미국 하이일드 스프레드", make_hy_spread_indicator),
        safe_call("MOVE 지수", make_move_indicator),
        safe_call(
            "VIX 지수",
            lambda: make_level_indicator(
                "VIX 지수",
                fdr_symbols=["VIX"],
                yf_symbols=["^VIX"],
                interpret_func=lambda level, pct: interpret_vix(level),
                unit="",
            ),
        ),
    ]


def main() -> None:
    # 배치 모드: 기술적 확산도만 계산해 GCS 캐시에 저장하고 종료(브리핑 미발송).
    # Cloud Run Job에서 RUN_MODE=breadth_cache 또는 인자 'breadth-cache'로 실행한다.
    if RUN_MODE == "breadth_cache" or (len(sys.argv) > 1 and sys.argv[1] == "breadth-cache"):
        build_breadth_cache()
        return

    try:
        results = collect_indicators()

        # 주도주 섹션은 실패해도 메일 전체가 죽지 않도록 별도 처리
        try:
            leading_report, leadership_state = build_leading_stock_report()
        except Exception as e:
            leading_report, leadership_state = "", None
            print(f"[leading skip] {type(e).__name__}: {e}", file=sys.stderr)

        # 오늘의 핵심 이슈(웹 검색)도 실패해도 메일 전체가 죽지 않도록 별도 처리.
        # 단, 실패 원인은 로그로 남긴다(과거 except:pass로 원인이 보이지 않던 문제 해결).
        try:
            daily_issues = build_daily_issues_ai()
        except Exception as e:
            daily_issues = ""
            print(f"[issues skip] {type(e).__name__}: {e}", file=sys.stderr)

        regime = classify_market_regime(results, leadership_state)
        regime_change = load_previous_regime_change(regime)
        subject, body = build_email(
            results, leading_report, regime, leadership_state, regime_change, daily_issues
        )

        print(subject)
        print(body)

        append_log(results)
        append_regime_log(regime, leadership_state)
        send_email(subject, body)

        # 웹 대시보드 갱신(선택 섹션: 실패해도 메일 발송은 죽지 않는다)
        try:
            import web_export
            where = web_export.publish(results, regime, brief_module=sys.modules[__name__])
            print(f"[web publish] {where}")
        except Exception as e:
            print(f"[web publish skip] {e}", file=sys.stderr)

    except Exception as e:
        err_subject = "⚠️ [Daily Market Brief 오류]"
        err_body = f"스크립트 실행 중 치명적 오류가 발생했습니다:\n{e}"
        print(err_subject, err_body, file=sys.stderr)

        try:
            send_email(err_subject, err_body)
        except Exception:
            pass

        sys.exit(1)


if __name__ == "__main__":
    main()
