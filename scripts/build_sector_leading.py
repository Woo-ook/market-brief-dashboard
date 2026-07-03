#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Build weekly sector leading indicators for Daily Market Brief.

Output: leading.json

Required/optional environment variables
- FRED_API_KEY      : required for FRED cards
- DART_API_KEY      : required for Korean shipbuilding disclosure cards
- SEC_USER_AGENT    : strongly recommended/required by SEC, e.g. "Your Name your@email.com"
- OUTPUT_PATH       : optional, default "leading.json"
- MANUAL_INPUT_PATH : optional, default "config/sector_manual_inputs.json"

Design principle
- Keep frontend contract identical to existing app.js card schema.
- Prefer official/free sources.
- If a source fails, emit an explicit "수집 실패" card instead of breaking the whole build.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import re
import sys
import time
import zipfile
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlencode

import requests

KST = dt.timezone(dt.timedelta(hours=9))

FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"
DART_BASE = "https://opendart.fss.or.kr/api"
SEC_BASE = "https://data.sec.gov/api/xbrl/companyfacts"

# ──────────────────────────────────────────────────────────────────────────────
# Shared card helpers
# ──────────────────────────────────────────────────────────────────────────────

def kst_now_str() -> str:
    return dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")


def today_kst() -> dt.date:
    return dt.datetime.now(KST).date()


def fmt_num(x: Optional[float], digits: int = 1) -> str:
    if x is None:
        return "—"
    if abs(x) >= 1000:
        return f"{x:,.{digits}f}"
    return f"{x:.{digits}f}"


def fmt_pct(x: Optional[float], digits: int = 1, signed: bool = True) -> str:
    if x is None:
        return "—"
    sign = "+" if signed and x >= 0 else ""
    return f"{sign}{x:.{digits}f}%"


def fmt_krw_amount(won: Optional[float]) -> str:
    if won is None:
        return "—"
    jo = won / 1_0000_0000_0000
    eok = won / 1_0000_0000
    if jo >= 1:
        return f"약 {jo:.2f}조원"
    return f"약 {eok:,.0f}억원"


def signal_from_yoy(yoy: Optional[float], strong: float = 10, weak: float = 0, contraction: float = -10) -> Tuple[str, int]:
    """Return Korean state and signal_level. Lower signal_level = better/greener."""
    if yoy is None:
        return "판단 보류", 1
    if yoy >= strong:
        return "확장", 0
    if yoy >= weak:
        return "완만한 확장", 1
    if yoy >= contraction:
        return "둔화", 2
    return "침체", 3


def failure_card(name: str, category: str, freq: str, source: str, reason: str) -> Dict[str, Any]:
    return {
        "name": name,
        "category": category,
        "freq": freq,
        "value_text": "수집 실패",
        "date": today_kst().isoformat(),
        "change_text": "",
        "state": "오류",
        "signal_level": 3,
        "comment": reason[:220],
        "mdd_line": "",
        "source": source,
        "ok": False,
        "chart": None,
    }


# ──────────────────────────────────────────────────────────────────────────────
# FRED monthly cards: semiconductor proxy indicators
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class FredSpec:
    series_id: str
    name: str
    category: str
    unit: str
    comment_prefix: str


FRED_SPECS = [
    FredSpec(
        "PCU33443344",
        "반도체·전자부품 생산자물가(PPI)",
        "반도체",
        "지수",
        "반도체·전자부품 출하가격 방향. 상승은 가격 강세(제조사에 우호적)를 시사합니다.",
    ),
    FredSpec(
        "IPG3344S",
        "반도체·전자부품 산업생산지수",
        "반도체",
        "지수",
        "반도체·전자부품 생산 활동 수준. 상승은 가동·출하 확대를 시사합니다.",
    ),
    FredSpec(
        "A34SNO",
        "컴퓨터·전자제품 신규주문",
        "반도체",
        "백만 달러",
        "수요 선행지표. 반도체 단독 주문은 FRED에 없어 상위 범주(컴퓨터·전자제품)로 대체합니다.",
    ),
]


def fetch_fred_series(series_id: str, api_key: str, months: int = 48) -> List[Tuple[str, float]]:
    end = today_kst()
    start = (end.replace(day=1) - dt.timedelta(days=months * 31)).replace(day=1)
    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "observation_start": start.isoformat(),
        "sort_order": "asc",
    }
    r = requests.get(FRED_BASE, params=params, timeout=30)
    r.raise_for_status()
    obs = r.json().get("observations", [])
    out: List[Tuple[str, float]] = []
    for row in obs:
        val = row.get("value")
        if val in (None, "."):
            continue
        try:
            out.append((row["date"], float(val)))
        except Exception:
            continue
    return out[-36:]


def fred_card(spec: FredSpec, api_key: Optional[str]) -> Dict[str, Any]:
    if not api_key:
        return failure_card(spec.name, spec.category, "월간", f"FRED:{spec.series_id}", "FRED_API_KEY가 없어 수집하지 못했습니다.")
    try:
        data = fetch_fred_series(spec.series_id, api_key)
        if not data:
            raise ValueError("FRED observations가 비어 있습니다.")
        labels = [d for d, _ in data]
        values = [v for _, v in data]
        latest = values[-1]
        mom = (latest / values[-2] - 1) * 100 if len(values) >= 2 and values[-2] else None
        yoy = (latest / values[-13] - 1) * 100 if len(values) >= 13 and values[-13] else None
        state, sig = signal_from_yoy(yoy)
        return {
            "name": spec.name,
            "category": spec.category,
            "freq": "월간",
            "value_text": fmt_num(latest, 1),
            "date": labels[-1],
            "change_text": f"전월 {fmt_pct(mom)} · 전년 {fmt_pct(yoy)}",
            "state": state,
            "signal_level": sig,
            "comment": (
                f"{spec.comment_prefix} 카드 색상은 과열도가 아니라 전년동월대비 방향(모멘텀)을 나타냅니다. "
                f"최신값 {fmt_num(latest, 1)} ({spec.unit}), 전월 {fmt_pct(mom)}, 전년 {fmt_pct(yoy)}."
            ),
            "mdd_line": "",
            "source": f"FRED:{spec.series_id}",
            "ok": True,
            "chart": {"type": "value", "unit": spec.unit, "labels": labels, "series": {"value": values}},
        }
    except Exception as e:
        return failure_card(spec.name, spec.category, "월간", f"FRED:{spec.series_id}", f"FRED 수집 실패: {e}")


# ──────────────────────────────────────────────────────────────────────────────
# OpenDART shipbuilding: recent contract count and amount heuristic
# ──────────────────────────────────────────────────────────────────────────────

SHIPBUILDERS = [
    {"name": "HD한국조선해양", "stock_code": "009540"},
    {"name": "HD현대중공업", "stock_code": "329180"},
    {"name": "삼성중공업", "stock_code": "010140"},
    {"name": "한화오션", "stock_code": "042660"},
]

CONTRACT_KEYWORDS = ("단일판매", "공급계약", "수주")
EXCLUDE_KEYWORDS = ("정정", "첨부정정")


def dart_get(endpoint: str, params: Dict[str, Any], timeout: int = 30) -> Any:
    r = requests.get(f"{DART_BASE}/{endpoint}", params=params, timeout=timeout)
    r.raise_for_status()
    return r


def load_dart_corp_map(api_key: str) -> Dict[str, str]:
    """Return stock_code -> corp_code from OpenDART corpCode.xml."""
    r = dart_get("corpCode.xml", {"crtfc_key": api_key})
    z = zipfile.ZipFile(io.BytesIO(r.content))
    xml_name = z.namelist()[0]
    raw = z.read(xml_name).decode("utf-8", errors="ignore")
    rows = re.findall(r"<list>(.*?)</list>", raw, flags=re.S)
    out: Dict[str, str] = {}
    for row in rows:
        corp_code = re.search(r"<corp_code>(.*?)</corp_code>", row)
        stock_code = re.search(r"<stock_code>(.*?)</stock_code>", row)
        if corp_code and stock_code and stock_code.group(1).strip():
            out[stock_code.group(1).strip()] = corp_code.group(1).strip()
    return out


def search_dart_filings(api_key: str, corp_code: str, days: int = 90) -> List[Dict[str, Any]]:
    end = today_kst()
    start = end - dt.timedelta(days=days)
    params = {
        "crtfc_key": api_key,
        "corp_code": corp_code,
        "bgn_de": start.strftime("%Y%m%d"),
        "end_de": end.strftime("%Y%m%d"),
        "page_count": 100,
        "sort": "date",
        "sort_mth": "desc",
    }
    r = dart_get("list.json", params)
    data = r.json()
    if data.get("status") not in ("000", "013"):
        raise ValueError(f"OpenDART status={data.get('status')} message={data.get('message')}")
    rows = data.get("list", []) or []
    keep = []
    for x in rows:
        nm = x.get("report_nm", "")
        if any(k in nm for k in CONTRACT_KEYWORDS) and not any(k in nm for k in EXCLUDE_KEYWORDS):
            keep.append(x)
    return keep


def dart_document_text(api_key: str, rcept_no: str) -> str:
    """Fetch original disclosure document text. Returns empty string on non-critical failures."""
    try:
        r = dart_get("document.xml", {"crtfc_key": api_key, "rcept_no": rcept_no}, timeout=60)
        content = r.content
        # OpenDART usually returns zip; sometimes XML/text error.
        if zipfile.is_zipfile(io.BytesIO(content)):
            z = zipfile.ZipFile(io.BytesIO(content))
            texts = []
            for name in z.namelist():
                raw = z.read(name)
                for enc in ("utf-8", "cp949", "euc-kr"):
                    try:
                        texts.append(raw.decode(enc))
                        break
                    except Exception:
                        continue
            text = "\n".join(texts)
        else:
            text = content.decode("utf-8", errors="ignore")
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text)
        return text
    except Exception:
        return ""


def parse_krw_amount(text: str) -> Optional[float]:
    if not text:
        return None
    # Prefer explicit won/원 contract amount rows.
    patterns = [
        r"계약금액\s*\(?원\)?\s*([0-9,]{6,})",
        r"계약금액[^0-9]{0,40}([0-9,]{6,})\s*원",
        r"계약금액[^0-9]{0,80}([0-9,]{6,})",
    ]
    candidates: List[float] = []
    for pat in patterns:
        for m in re.finditer(pat, text):
            num = re.sub(r"[^0-9]", "", m.group(1))
            if len(num) >= 7:
                try:
                    candidates.append(float(num))
                except Exception:
                    pass
    if not candidates:
        return None
    # Avoid tiny false positives; choose largest plausible contract amount.
    candidates = [x for x in candidates if x >= 1_000_000]
    return max(candidates) if candidates else None


def dart_shipbuilder_card(company: Dict[str, str], api_key: Optional[str], corp_map: Optional[Dict[str, str]]) -> Dict[str, Any]:
    nm = company["name"]
    stock = company["stock_code"]
    if not api_key:
        return failure_card(f"{nm} 수주", "조선", "주간", f"DART:{stock}", "DART_API_KEY가 없어 수집하지 못했습니다.")
    try:
        if not corp_map or stock not in corp_map:
            raise ValueError(f"corpCode.xml에서 {stock}의 corp_code를 찾지 못했습니다.")
        filings = search_dart_filings(api_key, corp_map[stock], days=90)
        amounts = []
        latest_nm = filings[0].get("report_nm", "") if filings else ""
        for f in filings[:10]:  # enough for weekly dashboard; protects rate/latency
            txt = dart_document_text(api_key, f.get("rcept_no", ""))
            amount = parse_krw_amount(txt)
            if amount:
                amounts.append(amount)
            time.sleep(0.15)
        total_amount = sum(amounts) if amounts else None
        count = len(filings)
        if count == 0:
            state, sig = "최근 없음", 2
        elif count >= 3 or (total_amount and total_amount >= 1_0000_0000_0000):
            state, sig = "수주 활발", 0
        else:
            state, sig = "수주 확인", 1
        value = f"{count}건"
        if total_amount:
            value += f" / {fmt_krw_amount(total_amount)}"
        change = "최근 90일 수주 관련 공시"
        if latest_nm:
            change += f" · 최근: {latest_nm[:35]}"
        return {
            "name": f"{nm} 수주",
            "category": "조선",
            "freq": "주간",
            "value_text": value,
            "date": today_kst().isoformat(),
            "change_text": change,
            "state": state,
            "signal_level": sig,
            "comment": (
                "DART 공시명에 '단일판매/공급계약/수주'가 포함된 신규 공시를 집계합니다. "
                "가능한 경우 공시 원문에서 계약금액(원)을 정규식으로 추출해 합산합니다. "
                "정정공시·자회사 연결 여부·통화 환산은 후속 검증이 필요하므로, 수주잔고의 방향을 보는 보조지표로 사용하세요."
            ),
            "mdd_line": "",
            "source": f"DART:{stock}",
            "ok": True,
            "chart": None,
        }
    except Exception as e:
        return failure_card(f"{nm} 수주", "조선", "주간", f"DART:{stock}", f"DART 수집 실패: {e}")


# ──────────────────────────────────────────────────────────────────────────────
# SEC companyfacts: software revenue growth, margins, RPO where available
# ──────────────────────────────────────────────────────────────────────────────

SOFTWARE_COMPANIES = [
    {"ticker": "MSFT", "name": "Microsoft", "cik": "0000789019"},
    {"ticker": "ORCL", "name": "Oracle", "cik": "0001341439"},
    {"ticker": "CRM", "name": "Salesforce", "cik": "0001108524"},
    {"ticker": "NOW", "name": "ServiceNow", "cik": "0001373715"},
    {"ticker": "ADBE", "name": "Adobe", "cik": "0000796343"},
    {"ticker": "PLTR", "name": "Palantir", "cik": "0001321655"},
]

BIGTECH_CAPEX = [
    {"ticker": "MSFT", "name": "Microsoft", "cik": "0000789019"},
    {"ticker": "GOOGL", "name": "Alphabet", "cik": "0001652044"},
    {"ticker": "AMZN", "name": "Amazon", "cik": "0001018724"},
    {"ticker": "META", "name": "Meta", "cik": "0001326801"},
    {"ticker": "ORCL", "name": "Oracle", "cik": "0001341439"},
]

REVENUE_CONCEPTS = [
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
]
OPERATING_INCOME_CONCEPTS = ["OperatingIncomeLoss"]
CFO_CONCEPTS = [
    "NetCashProvidedByUsedInOperatingActivities",
    "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
]
CAPEX_CONCEPTS = [
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "PaymentsToAcquireProductiveAssets",
]


def sec_headers() -> Dict[str, str]:
    ua = os.getenv("SEC_USER_AGENT", "DailyMarketBrief/0.1 contact@example.com")
    return {"User-Agent": ua, "Accept-Encoding": "gzip, deflate", "Host": "data.sec.gov"}


def fetch_companyfacts(cik: str) -> Dict[str, Any]:
    cik10 = str(cik).zfill(10)
    url = f"{SEC_BASE}/CIK{cik10}.json"
    r = requests.get(url, headers=sec_headers(), timeout=45)
    r.raise_for_status()
    return r.json()


def concept_entries(facts: Dict[str, Any], concept_candidates: Iterable[str]) -> List[Dict[str, Any]]:
    usgaap = facts.get("facts", {}).get("us-gaap", {})
    for concept in concept_candidates:
        obj = usgaap.get(concept)
        if not obj:
            continue
        units = obj.get("units", {})
        # USD is preferred; shares/per-share concepts are intentionally ignored.
        if "USD" in units:
            return units["USD"]
    return []


def rpo_entries(facts: Dict[str, Any]) -> List[Dict[str, Any]]:
    usgaap = facts.get("facts", {}).get("us-gaap", {})
    candidates = [k for k in usgaap.keys() if "RemainingPerformanceObligation" in k]
    for k in candidates:
        units = usgaap[k].get("units", {})
        if "USD" in units:
            return units["USD"]
    return []


def quarter_key(e: Dict[str, Any]) -> Optional[str]:
    frame = e.get("frame") or ""
    m = re.match(r"CY(\d{4})Q([1-4])$", frame)
    if m:
        return f"{m.group(1)}Q{m.group(2)}"
    fy, fp = e.get("fy"), e.get("fp")
    if fy and fp in {"Q1", "Q2", "Q3", "Q4"}:
        return f"{fy}{fp}"
    return None


def latest_quarter_values(entries: List[Dict[str, Any]]) -> Dict[str, float]:
    """Return latest filed value per CY/FY quarter key. Quarterly frames are preferred."""
    rows = []
    for e in entries:
        if e.get("form") not in {"10-Q", "10-K", "20-F", "40-F"}:
            continue
        key = quarter_key(e)
        if not key:
            continue
        val = e.get("val")
        if val is None:
            continue
        filed = e.get("filed") or e.get("end") or ""
        rows.append((key, filed, float(val)))
    # Keep last filed value for duplicate keys.
    out: Dict[str, Tuple[str, float]] = {}
    for key, filed, val in sorted(rows, key=lambda x: (x[0], x[1])):
        out[key] = (filed, val)
    return {k: v for k, (_, v) in out.items()}


def prev_year_quarter(key: str) -> Optional[str]:
    m = re.match(r"(\d{4})Q([1-4])", key)
    if not m:
        return None
    return f"{int(m.group(1))-1}Q{m.group(2)}"


def latest_yoy(values: Dict[str, float]) -> Tuple[Optional[str], Optional[float], Optional[float]]:
    keys = sorted(values.keys())
    for key in reversed(keys):
        pk = prev_year_quarter(key)
        if pk and pk in values and values[pk]:
            yoy = (values[key] / values[pk] - 1) * 100
            return key, values[key], yoy
    return (keys[-1], values[keys[-1]], None) if keys else (None, None, None)


def sec_company_metric(company: Dict[str, str]) -> Dict[str, Any]:
    facts = fetch_companyfacts(company["cik"])
    rev = latest_quarter_values(concept_entries(facts, REVENUE_CONCEPTS))
    op = latest_quarter_values(concept_entries(facts, OPERATING_INCOME_CONCEPTS))
    cfo = latest_quarter_values(concept_entries(facts, CFO_CONCEPTS))
    capex = latest_quarter_values(concept_entries(facts, CAPEX_CONCEPTS))
    rpo = latest_quarter_values(rpo_entries(facts))

    q, revenue, rev_yoy = latest_yoy(rev)
    op_margin = None
    fcf_margin = None
    rpo_yoy = None

    if q and revenue:
        if q in op:
            op_margin = op[q] / revenue * 100
        if q in cfo and q in capex:
            # Capex is often reported as positive cash outflow. If negative, normalize.
            fcf = cfo[q] - abs(capex[q])
            fcf_margin = fcf / revenue * 100
    rq, _, rpo_yoy = latest_yoy(rpo)
    return {
        "ticker": company["ticker"],
        "name": company["name"],
        "quarter": q,
        "revenue": revenue,
        "revenue_yoy": rev_yoy,
        "op_margin": op_margin,
        "fcf_margin": fcf_margin,
        "rpo_quarter": rq,
        "rpo_yoy": rpo_yoy,
    }


def mean(xs: Iterable[Optional[float]]) -> Optional[float]:
    vals = [x for x in xs if x is not None]
    return sum(vals) / len(vals) if vals else None


def sec_software_cards() -> List[Dict[str, Any]]:
    metrics = []
    errors = []
    for c in SOFTWARE_COMPANIES:
        try:
            metrics.append(sec_company_metric(c))
            time.sleep(0.15)
        except Exception as e:
            errors.append(f"{c['ticker']}:{e}")
    if not metrics:
        return [failure_card("미국 소프트웨어 대형주 펀더멘털", "소프트웨어", "분기", "SEC EDGAR", "; ".join(errors) or "SEC 수집 실패")]

    rev_avg = mean(m["revenue_yoy"] for m in metrics)
    op_avg = mean(m["op_margin"] for m in metrics)
    fcf_avg = mean(m["fcf_margin"] for m in metrics)
    rpo_avg = mean(m["rpo_yoy"] for m in metrics)
    latest_q = max([m["quarter"] for m in metrics if m.get("quarter")] or [today_kst().isoformat()])
    tickers = ", ".join(m["ticker"] for m in metrics)

    rev_state, rev_sig = signal_from_yoy(rev_avg, strong=15, weak=5, contraction=0)
    margin_state, margin_sig = ("고수익", 0) if op_avg and op_avg >= 25 else (("수익성 양호", 1) if op_avg and op_avg >= 15 else ("수익성 점검", 2))
    rpo_state, rpo_sig = signal_from_yoy(rpo_avg, strong=20, weak=5, contraction=0)

    cards = [
        {
            "name": "미국 소프트웨어 대형주 매출 성장률",
            "category": "소프트웨어",
            "freq": "분기",
            "value_text": fmt_pct(rev_avg, signed=True),
            "date": latest_q,
            "change_text": f"대상: {tickers}",
            "state": rev_state,
            "signal_level": rev_sig,
            "comment": "SEC companyfacts에서 주요 소프트웨어 기업의 분기 매출 전년동기 대비 성장률을 계산한 평균입니다. 기업별 회계연도와 태그 차이 때문에 방향성 지표로 사용하세요.",
            "mdd_line": "",
            "source": "SEC EDGAR companyfacts",
            "ok": True,
            "chart": None,
        },
        {
            "name": "미국 소프트웨어 대형주 영업이익률",
            "category": "소프트웨어",
            "freq": "분기",
            "value_text": fmt_pct(op_avg, signed=False),
            "date": latest_q,
            "change_text": f"FCF margin 평균 {fmt_pct(fcf_avg, signed=False)}",
            "state": margin_state,
            "signal_level": margin_sig,
            "comment": "성장주의 질을 확인하는 지표입니다. 매출 성장률이 높아도 영업이익률과 FCF margin이 훼손되면 업황의 질은 낮게 봅니다.",
            "mdd_line": "",
            "source": "SEC EDGAR companyfacts",
            "ok": True,
            "chart": None,
        },
    ]
    # RPO is not consistently tagged. Emit only when at least some companies are available.
    if rpo_avg is not None:
        cards.append(
            {
                "name": "미국 소프트웨어 RPO 성장률",
                "category": "소프트웨어",
                "freq": "분기",
                "value_text": fmt_pct(rpo_avg, signed=True),
                "date": latest_q,
                "change_text": "RPO 공개·태그 식별 가능 기업 평균",
                "state": rpo_state,
                "signal_level": rpo_sig,
                "comment": "Remaining Performance Obligation은 앞으로 매출로 인식될 계약 잔고입니다. 공개·XBRL 태그가 가능한 기업만 평균에 포함됩니다.",
                "mdd_line": "",
                "source": "SEC EDGAR companyfacts",
                "ok": True,
                "chart": None,
            }
        )
    return cards


def bigtech_capex_card() -> Dict[str, Any]:
    vals_now: List[float] = []
    vals_prev: List[float] = []
    qs = []
    errors = []
    for c in BIGTECH_CAPEX:
        try:
            facts = fetch_companyfacts(c["cik"])
            capex = latest_quarter_values(concept_entries(facts, CAPEX_CONCEPTS))
            q, val, _ = latest_yoy(capex)
            pk = prev_year_quarter(q) if q else None
            if q and val is not None and pk and pk in capex:
                vals_now.append(abs(val))
                vals_prev.append(abs(capex[pk]))
                qs.append(q)
            time.sleep(0.15)
        except Exception as e:
            errors.append(f"{c['ticker']}:{e}")
    if not vals_now or not vals_prev or not sum(vals_prev):
        return failure_card("빅테크 CAPEX", "반도체", "분기", "SEC EDGAR companyfacts", "; ".join(errors) or "CAPEX 비교값 부족")
    total_now = sum(vals_now)
    total_prev = sum(vals_prev)
    yoy = (total_now / total_prev - 1) * 100
    state, sig = signal_from_yoy(yoy, strong=20, weak=5, contraction=0)
    return {
        "name": "빅테크 CAPEX",
        "category": "반도체",
        "freq": "분기",
        "value_text": f"${total_now/1_000_000_000:,.1f}B",
        "date": max(qs),
        "change_text": f"전년 {fmt_pct(yoy)} · MSFT/GOOGL/AMZN/META/ORCL 합산",
        "state": state,
        "signal_level": sig,
        "comment": "AI 데이터센터 투자 수요를 보는 반도체 선행지표입니다. SEC companyfacts의 유형자산 취득 지출을 주요 빅테크 기준으로 합산했습니다.",
        "mdd_line": "",
        "source": "SEC EDGAR companyfacts",
        "ok": True,
        "chart": None,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Manual inputs: SIA/TrendForce/Clarkson/IR values that are not reliably free API
# ──────────────────────────────────────────────────────────────────────────────

def load_manual_cards(path: str) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    cards = []
    for item in raw.get("indicators", []):
        # Keep only enabled cards. This lets you park paid/manual indicators without deleting them.
        if item.get("enabled", True) is False:
            continue
        card = dict(item)
        card.pop("enabled", None)
        card.setdefault("mdd_line", "")
        card.setdefault("ok", True)
        card.setdefault("chart", None)
        cards.append(card)
    return cards


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def build() -> Dict[str, Any]:
    fred_key = os.getenv("FRED_API_KEY")
    dart_key = os.getenv("DART_API_KEY")
    manual_path = os.getenv("MANUAL_INPUT_PATH", "config/sector_manual_inputs.json")

    indicators: List[Dict[str, Any]] = []

    # 1) Manual/free-public-but-not-stable-API indicators first: SIA, TrendForce, Clarkson, company IR.
    indicators.extend(load_manual_cards(manual_path))

    # 2) FRED semiconductor proxies.
    for spec in FRED_SPECS:
        indicators.append(fred_card(spec, fred_key))

    # 3) Big-tech CAPEX as semiconductor AI demand proxy.
    try:
        indicators.append(bigtech_capex_card())
    except Exception as e:
        indicators.append(failure_card("빅테크 CAPEX", "반도체", "분기", "SEC EDGAR companyfacts", f"SEC CAPEX 수집 실패: {e}"))

    # 4) DART shipbuilding contracts.
    corp_map = None
    if dart_key:
        try:
            corp_map = load_dart_corp_map(dart_key)
        except Exception as e:
            corp_map = None
            indicators.append(failure_card("DART 기업코드 매핑", "조선", "주간", "OpenDART corpCode.xml", f"기업코드 매핑 실패: {e}"))
    for c in SHIPBUILDERS:
        indicators.append(dart_shipbuilder_card(c, dart_key, corp_map))

    # 5) SEC software fundamentals.
    indicators.extend(sec_software_cards())

    # Frontend-compatible payload.
    return {
        "generated_at": kst_now_str(),
        "note": "산업별 선행지표(월간·주간·분기). 색상은 과열도가 아니라 업황 방향성(모멘텀)을 의미합니다. 유료 원지표는 수동 입력 또는 공개 대체지표로 처리합니다.",
        "indicators": indicators,
    }


def main() -> int:
    out_path = os.getenv("OUTPUT_PATH", "leading.json")
    payload = build()
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"Wrote {out_path} with {len(payload['indicators'])} indicators")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
