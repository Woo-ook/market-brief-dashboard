#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Build us_flow.json — 미국 지수선물 수급 카드 (CFTC COT / TFF).

S&P 500 · NASDAQ 100 e-mini 선물에 대해, 투자자 유형별 순포지션과 주간 추세를
계산해 대시보드의 "자금 흐름" 탭(미국판)용 us_flow.json 으로 저장한다.

한국(KRX)의 개인/기관/외국인 종목별 순매수에 대응하는 "직접" 데이터는 미국에
없다(체결 테이프에 투자자 유형 라벨이 없음). 대신 CFTC COT 의
TFF(Traders in Financial Futures) 리포트가 지수선물 포지션을 아래로 분해한다:
  - Asset Manager/Institutional  → 기관(자산운용)   ← KRX "기관"에 대응
  - Leveraged Funds              → 헤지펀드(레버리지) ← KRX "투신/사모/투기"에 대응
각 유형의 순포지션 = 롱 - 숏. 주간(화요일 종가 기준, 금 15:30 ET 발표) 데이터.

데이터 소스: CFTC 공개 리포팅 Socrata API (무료, API 키 불필요).
  TFF Futures-Only dataset id = gpe5-46if
  https://publicreporting.cftc.gov/resource/gpe5-46if.json
urllib(stdlib)만 사용 → 클라우드 루틴 egress 프록시 통과(FRED 방식과 동일 계열).

실행:
  python scripts/build_us_index_flow.py
  python scripts/build_us_index_flow.py --output us_flow.json --weeks 26
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

KST = dt.timezone(dt.timedelta(hours=9))

COT_BASE = "https://publicreporting.cftc.gov/resource/gpe5-46if.json"

# 안정적인 계약코드로 필터(시장명은 시간에 따라 바뀜).
CONTRACTS: List[Dict[str, str]] = [
    {"name": "S&P 500", "code": "13874A"},   # E-MINI S&P 500
    {"name": "NASDAQ 100", "code": "209742"},  # E-MINI NASDAQ-100 (NASDAQ MINI)
]

# 표시명 -> COT 롱/숏 컬럼
PARTICIPANTS: List[Dict[str, str]] = [
    {"name": "기관(자산운용)", "role": "기관",
     "long": "asset_mgr_positions_long", "short": "asset_mgr_positions_short"},
    {"name": "헤지펀드(레버리지)", "role": "헤지펀드",
     "long": "lev_money_positions_long", "short": "lev_money_positions_short"},
]

SELECT_COLS = (
    "report_date_as_yyyy_mm_dd,contract_market_name,open_interest_all,"
    "asset_mgr_positions_long,asset_mgr_positions_short,"
    "lev_money_positions_long,lev_money_positions_short,"
    "dealer_positions_long_all,dealer_positions_short_all"
)


def kst_now_str() -> str:
    return dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")


def fmt_ct(x: Optional[float]) -> str:
    """계약 수를 부호 포함 문자열로."""
    if x is None:
        return "—"
    sign = "+" if x >= 0 else "-"
    return f"{sign}{abs(int(round(x))):,}계약"


def to_int(v: Any) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def fetch_series(code: str, weeks: int) -> List[Dict[str, Any]]:
    """계약코드별 최근 `weeks`주 TFF 데이터를 날짜 오름차순 리스트로."""
    q = {
        "$select": SELECT_COLS,
        "$where": f"cftc_contract_market_code='{code}'",
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$limit": str(weeks),
    }
    url = COT_BASE + "?" + urllib.parse.urlencode(q)
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        rows = json.load(r)
    rows.sort(key=lambda x: x["report_date_as_yyyy_mm_dd"])  # 오름차순
    return rows


def make_card(contract: Dict[str, str], part: Dict[str, str],
              rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    labels: List[str] = []
    net_levels: List[int] = []
    for row in rows:
        labels.append(row["report_date_as_yyyy_mm_dd"][:10])
        net_levels.append(to_int(row[part["long"]]) - to_int(row[part["short"]]))

    # 주간 변화(막대) = 순포지션 레벨의 1차 차분 (첫 주는 0)
    weekly_delta: List[int] = [0] + [net_levels[i] - net_levels[i - 1]
                                     for i in range(1, len(net_levels))]

    latest_net = net_levels[-1] if net_levels else None
    latest_delta = weekly_delta[-1] if len(weekly_delta) > 1 else 0
    last_row = rows[-1] if rows else {}
    long_ct = to_int(last_row.get(part["long"]))
    short_ct = to_int(last_row.get(part["short"]))
    oi = to_int(last_row.get("open_interest_all"))
    date = (last_row.get("report_date_as_yyyy_mm_dd") or "")[:10]

    is_long = (latest_net is not None and latest_net >= 0)
    state = "순롱" if is_long else "순숏"
    signal_level = 0 if is_long else 3

    hedge_note = ""
    if part["role"] == "헤지펀드":
        hedge_note = (
            " 레버리지펀드는 S&P 베이시스(현·선물 차익)거래로 구조적 순숏인 경우가 "
            "많아, 순숏 자체보다 주간 변화 방향을 함께 봐야 한다."
        )

    comment = (
        f"{contract['name']} 선물 {part['name']} 순포지션 {fmt_ct(latest_net)} "
        f"(롱 {long_ct:,} · 숏 {short_ct:,}). 전주 대비 {fmt_ct(latest_delta)}. "
        f"막대=주간 순포지션 변화, 라인=순포지션 레벨(계약).{hedge_note} "
        "CFTC COT TFF, 화요일 종가 기준·주간."
    )

    return {
        "name": f"{contract['name']} · {part['name']}",
        "category": contract["name"],
        "value_text": f"{state} {fmt_ct(latest_net)}" if latest_net is not None else "수집 실패",
        "change_text": f"전주 대비 {fmt_ct(latest_delta)}",
        "state": state,
        "signal_level": signal_level,
        "date": date,
        "source": "CFTC COT (TFF)",
        "ok": bool(labels),
        "comment": comment,
        "mdd_line": f"롱 {long_ct:,} · 숏 {short_ct:,} · 순 {latest_net:,} · 미결제(OI) {oi:,}"
        if latest_net is not None else "",
        # COT 은 종목·섹터 단면이 없으므로 비움(상세 뷰는 차트+코멘트).
        "top_buys": [],
        "top_sells": [],
        "sector_flow": [],
        "chart": {
            "type": "flow_daily",
            "unit": "계약",
            "labels": labels,
            "series": {"value": weekly_delta, "cum": net_levels},
        },
    }


def failure_card(contract: Dict[str, str], part: Dict[str, str], reason: str) -> Dict[str, Any]:
    return {
        "name": f"{contract['name']} · {part['name']}",
        "category": contract["name"],
        "value_text": "수집 실패",
        "change_text": "",
        "state": "오류",
        "signal_level": 3,
        "date": "",
        "source": "CFTC COT (TFF)",
        "ok": False,
        "comment": str(reason)[:240],
        "mdd_line": "",
        "top_buys": [],
        "top_sells": [],
        "sector_flow": [],
        "chart": None,
    }


def build(output_path: Path, weeks: int) -> None:
    cards: List[Dict[str, Any]] = []
    latest_date = ""
    for contract in CONTRACTS:
        try:
            rows = fetch_series(contract["code"], weeks)
        except Exception as exc:  # 네트워크/스키마 문제 → 해당 계약 전체 실패 카드
            for part in PARTICIPANTS:
                cards.append(failure_card(contract, part, exc))
            print(f"[usflow] {contract['name']}: FETCH FAIL {exc}")
            continue
        if rows:
            latest_date = max(latest_date, rows[-1]["report_date_as_yyyy_mm_dd"][:10])
        for part in PARTICIPANTS:
            try:
                card = make_card(contract, part, rows)
            except Exception as exc:
                card = failure_card(contract, part, exc)
            cards.append(card)
            print(f"[usflow] {card['name']}: ok={card['ok']} {card.get('value_text')}")

    out = {
        "generated_at": kst_now_str(),
        "period": {"latest_report": latest_date, "weeks": weeks},
        "note": (
            "미국 지수선물 수급 (CFTC COT · TFF). S&P500/NASDAQ100 e-mini 선물의 "
            "기관(자산운용) vs 헤지펀드(레버리지) 순포지션과 주간 추세. "
            "화요일 종가 기준, 매주 금 15:30 ET 발표(약 3일 지연). "
            "미국은 종목별 개인/기관 순매수 데이터가 없어 지수선물 포지션으로 대체."
        ),
        "indicators": cards,
    }
    output_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    ok_n = sum(1 for c in cards if c["ok"])
    print(f"Wrote {output_path} with {len(cards)} cards ({ok_n} ok), latest {latest_date} at {kst_now_str()}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default="us_flow.json")
    ap.add_argument("--weeks", type=int, default=26, help="조회 주수 (기본 26주 ≈ 6개월)")
    args = ap.parse_args()
    build(Path(args.output), args.weeks)


if __name__ == "__main__":
    main()
