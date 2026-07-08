# -*- coding: utf-8 -*-
"""
web_export.py — Daily Market Brief 웹 대시보드용 데이터 생성/게시기

기존 메인 스크립트의 함수를 재사용해 지표 현재값 + 국면 점수 + 차트용 시계열을
data.json 으로 만들고, GitHub Pages 저장소에 올린다.

두 가지 사용법:
  1) 로컬 단독 실행 (지표를 새로 수집해서 docs/ 에 기록 → 미리보기용)
        python web_export.py
  2) 메인 스크립트 main()에서 호출 (이미 수집한 결과 재사용 → GitHub에 PUT)
        import web_export
        web_export.publish(results, regime, brief_module=sys.modules[__name__])

게시 동작은 환경변수로 갈린다.
  - GITHUB_TOKEN 이 있으면 → GitHub Contents API로 data.json/history.json PUT
  - 없으면 → 로컬 docs/ 폴더에 파일로 기록 (Phase 1 미리보기와 동일)

이메일 발송 로직은 전혀 건드리지 않는다.
"""

import os
import sys
import json
import math
import base64
import importlib
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import pandas as pd

# 메인 스크립트(로컬 단독 실행용). 배포 환경에서 파일명이 다르면 import가 실패할 수
# 있는데, 그때는 publish(brief_module=...)로 모듈을 직접 주입받으므로 문제 없다.
try:
    B = importlib.import_module("daily_market_brief_alert_regime_v7")
except Exception:
    B = None

DOCS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs")
DATA_PATH = os.path.join(DOCS_DIR, "data.json")
HISTORY_PATH = os.path.join(DOCS_DIR, "history.json")

# 차트에 표시할 최근 거래일 수 (약 1년)
CHART_POINTS = 250

# GitHub 게시 설정 (토큰은 절대 하드코딩하지 않고 환경변수로만 읽는다)
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "Woo-ook/market-brief-dashboard")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
GITHUB_API = "https://api.github.com"


# ─────────────────────────────────────────────────────────
# 지표 분류 / 차트 스펙
# ─────────────────────────────────────────────────────────
CATEGORY = {
    "KOSPI 50일 이격도": "이격도",
    "KOSDAQ 50일 이격도": "이격도",
    "S&P500 50일 이격도": "이격도",
    "NASDAQ 50일 이격도": "이격도",
    "SOX 반도체지수 50일 이격도": "이격도",
    "삼성전자 50일 이격도": "이격도",
    "SK하이닉스 50일 이격도": "이격도",
    "원/달러 환율": "환율",
    "DXY 달러인덱스": "환율",
    "USD/CNH": "환율",
    "USD/JPY": "환율",
    "미국 2년물 금리": "금리",
    "미국 3개월물 금리": "금리",
    "Fed Funds 금리": "금리",
    "미국 10년물 금리": "금리",
    "미국 10년 실질금리": "금리",
    "미국 10년 기대인플레": "금리",
    "미국 10Y-3M 금리차": "금리",
    "미국 10Y-Fed Funds 금리차": "금리",
    "한국 10년물 금리": "금리",
    "미국 하이일드 스프레드": "위험·변동성",
    "MOVE 지수": "위험·변동성",
    "VIX 지수": "위험·변동성",
    "WTI 유가": "위험·변동성",
}

# 이격도 차트: 이름 → (fdr_symbols, yf_symbols, unit)
DISPARITY_SPECS = {
    "KOSPI 50일 이격도": (["KS11"], ["^KS11"], ""),
    "KOSDAQ 50일 이격도": (["KQ11"], ["^KQ11"], ""),
    "S&P500 50일 이격도": (["US500", "S&P500"], ["^GSPC"], ""),
    "NASDAQ 50일 이격도": (["IXIC", "NASDAQCOM"], ["^IXIC"], ""),
    "SOX 반도체지수 50일 이격도": (["^SOX"], ["^SOX"], ""),
    "삼성전자 50일 이격도": (["005930"], ["005930.KS"], "원"),
    "SK하이닉스 50일 이격도": (["000660"], ["000660.KS"], "원"),
}

# 레벨(가격) 차트: 이름 → (fdr_symbols, yf_symbols)
LEVEL_SPECS = {
    "원/달러 환율": (["USD/KRW"], ["KRW=X"]),
    "DXY 달러인덱스": (["DX-Y.NYB"], ["DX-Y.NYB", "^NYICDX"]),
    "USD/CNH": (["USD/CNY"], ["CNH=X"]),
    "USD/JPY": (["USD/JPY"], ["JPY=X"]),
    "WTI 유가": (["CL=F"], ["CL=F"]),
    "VIX 지수": (["VIX"], ["^VIX"]),
    "MOVE 지수": (["^MOVE"], ["^MOVE"]),
}

# FRED 시계열 차트: 이름 → series_id
FRED_SPECS = {
    "미국 2년물 금리": "DGS2",
    "미국 3개월물 금리": "DGS3MO",
    "Fed Funds 금리": "DFF",
    "미국 10년물 금리": "DGS10",
    "미국 10년 실질금리": "DFII10",
    "미국 10년 기대인플레": "T10YIE",
}


# ─────────────────────────────────────────────────────────
# 유틸
# ─────────────────────────────────────────────────────────
def _round(x, ndigits=2):
    try:
        if x is None:
            return None
        xf = float(x)
        if math.isnan(xf) or math.isinf(xf):
            return None
        return round(xf, ndigits)
    except (TypeError, ValueError):
        return None


def _labels(index) -> list:
    return [pd.Timestamp(d).strftime("%Y-%m-%d") for d in index]


def _tail(series: pd.Series, n: int = CHART_POINTS) -> pd.Series:
    return series.tail(n)


# ─────────────────────────────────────────────────────────
# 차트 빌더
# ─────────────────────────────────────────────────────────
def build_disparity_chart(name: str) -> dict:
    fdr_symbols, yf_symbols, unit = DISPARITY_SPECS[name]
    close, _ = B.get_price_series(fdr_symbols, yf_symbols)

    df = pd.DataFrame({"Close": close})
    df["MA"] = df["Close"].rolling(B.MA_WINDOW).mean()
    df["Disparity"] = df["Close"] / df["MA"] * 100
    valid = _tail(df.dropna(subset=["Disparity"]))

    cal = B.DISPARITY_CALIB.get(name, B.DEFAULT_DISPARITY_CAL)
    return {
        "type": "disparity",
        "unit": unit,
        "labels": _labels(valid.index),
        "series": {
            "disparity": [_round(v) for v in valid["Disparity"]],
            "close": [_round(v) for v in valid["Close"]],
            "ma": [_round(v) for v in valid["MA"]],
        },
        "refs": {
            "strong": B.DISPARITY_STRONG,
            "overheat": cal["overheat"],
            "extreme": cal["extreme"],
        },
    }


def build_level_chart(name: str) -> dict:
    fdr_symbols, yf_symbols = LEVEL_SPECS[name]
    close = _tail(B.get_price_series(fdr_symbols, yf_symbols)[0])
    return {
        "type": "level",
        "labels": _labels(close.index),
        "series": {"value": [_round(v) for v in close]},
    }


def build_fred_chart(name: str) -> dict:
    s = _tail(B.get_fred_series(FRED_SPECS[name], lookback_days=540))
    return {
        "type": "level",
        "labels": _labels(s.index),
        "series": {"value": [_round(v, 3) for v in s]},
    }


def attach_chart(name: str):
    """이름에 맞는 차트를 만들어 반환. 실패하면 None."""
    try:
        if name in DISPARITY_SPECS:
            return build_disparity_chart(name)
        if name in LEVEL_SPECS:
            return build_level_chart(name)
        if name in FRED_SPECS:
            return build_fred_chart(name)
    except Exception as e:
        print(f"[chart skip] {name}: {e}")
    return None


# ─────────────────────────────────────────────────────────
# payload 조립
# ─────────────────────────────────────────────────────────
def indicator_to_dict(r) -> dict:
    return {
        "name": r.name,
        "category": CATEGORY.get(r.name, "기타"),
        "value_text": r.value_text,
        "date": r.date,
        "change_text": r.change_text,
        "state": r.state,
        "signal_level": r.signal_level,
        "comment": r.comment,
        "mdd_line": r.mdd_line,
        "source": r.source,
        "ok": r.ok,
        "chart": attach_chart(r.name) if r.ok else None,
    }


def regime_scores(regime) -> dict:
    ts = regime.timing_scores
    return {
        "risk": regime.risk_score,
        "overheating": regime.overheating_score,
        "breadth": regime.breadth_score,
        "fx_stress": regime.fx_stress_score,
        "stability": regime.credit_score,
        "entry": regime.entry_score,
        "fear_buy": ts.fear_buy_score,
        "mania_reduce": ts.mania_reduce_score,
        "final_timing": ts.final_timing,
    }


def regime_to_dict(regime) -> dict:
    return {
        "final_label": regime.final_label,
        "action_label": regime.action_label,
        "headline": regime.headline,
        "one_liner": regime.one_liner,
        "core_question": regime.core_question,
        "beginner_translation": regime.beginner_translation,
        "key_drivers": list(regime.key_drivers or []),
        "risks": list(regime.risks or []),
        "scores": regime_scores(regime),
    }


def build_payload(results, regime, session=None) -> dict:
    if session is None:
        session = B.current_session()
    return {
        "generated_at": B.now_kst().strftime("%Y-%m-%d %H:%M KST"),
        "session": getattr(session, "label", ""),
        "regime": regime_to_dict(regime),
        "indicators": [indicator_to_dict(r) for r in results],
    }


def _append_history(history, regime) -> list:
    run_date = B.now_kst().strftime("%Y-%m-%d %H:%M")
    entry = {"run_date": run_date, "final_label": regime.final_label}
    entry.update(regime_scores(regime))
    history = [h for h in history if h.get("run_date") != run_date]
    history.append(entry)
    return history[-400:]  # 너무 커지지 않도록 제한


# ─────────────────────────────────────────────────────────
# GitHub Contents API (urllib, 무SDK)
# ─────────────────────────────────────────────────────────
def _gh_request(method: str, path_with_query: str, body=None):
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{path_with_query}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "market-brief-dashboard",
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(url, data=data, headers=headers, method=method)
    with urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}


def _gh_get_file(path: str):
    """(sha, 디코드된 텍스트) 반환. 없으면 (None, None)."""
    try:
        info = _gh_request("GET", f"{path}?ref={GITHUB_BRANCH}")
    except HTTPError as e:
        if e.code == 404:
            return None, None
        raise
    sha = info.get("sha")
    content_b64 = (info.get("content") or "").replace("\n", "")
    text = None
    if content_b64:
        try:
            text = base64.b64decode(content_b64).decode("utf-8")
        except Exception:
            text = None
    return sha, text


def github_put_file(path: str, content_str: str, message: str) -> None:
    sha, _ = _gh_get_file(path)
    body = {
        "message": message,
        "content": base64.b64encode(content_str.encode("utf-8")).decode("ascii"),
        "branch": GITHUB_BRANCH,
    }
    if sha:
        body["sha"] = sha
    _gh_request("PUT", path, body)


# ─────────────────────────────────────────────────────────
# 게시 진입점
# ─────────────────────────────────────────────────────────
def publish(results, regime, session=None, brief_module=None) -> str:
    """메인 스크립트 main()에서 호출. 수집한 결과를 재사용해 대시보드 데이터를 게시한다.

    GITHUB_TOKEN 이 있으면 GitHub에 PUT, 없으면 로컬 docs/ 에 기록한다.
    반환값은 'github' 또는 'local'.
    """
    global B
    if brief_module is not None:
        B = brief_module
    if B is None:
        raise RuntimeError("메인 스크립트 모듈을 찾지 못했습니다(brief_module 미지정).")

    payload = build_payload(results, regime, session)
    data_str = json.dumps(payload, ensure_ascii=False, indent=2)
    stamp = payload["generated_at"]

    if GITHUB_TOKEN:
        github_put_file("data.json", data_str, f"chore: data update {stamp}")
        _, hist_text = _gh_get_file("history.json")
        try:
            history = json.loads(hist_text) if hist_text else []
        except Exception:
            history = []
        history = _append_history(history, regime)
        github_put_file(
            "history.json",
            json.dumps(history, ensure_ascii=False, indent=2),
            f"chore: history update {stamp}",
        )
        return "github"

    # 로컬 폴백 (토큰 없을 때 — 미리보기용)
    os.makedirs(DOCS_DIR, exist_ok=True)
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        f.write(data_str)
    history = []
    if os.path.exists(HISTORY_PATH):
        try:
            with open(HISTORY_PATH, "r", encoding="utf-8") as f:
                history = json.load(f)
        except Exception:
            history = []
    history = _append_history(history, regime)
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
    return "local"


# ─────────────────────────────────────────────────────────
# 로컬 단독 실행 (미리보기용: 지표를 새로 수집)
# ─────────────────────────────────────────────────────────
def main() -> None:
    if B is None:
        print("메인 스크립트를 import하지 못했습니다.", file=sys.stderr)
        sys.exit(1)

    print("지표 수집 중...")
    results = B.collect_indicators()
    try:
        _, leadership_state = B.build_leading_stock_report()
    except Exception as e:
        print(f"[leadership skip] {e}")
        leadership_state = None
    regime = B.classify_market_regime(results, leadership_state)

    where = publish(results, regime)
    print(f"게시 완료 → {where}")
    if where == "local":
        print(f"  {DATA_PATH}")
        print(f"  {HISTORY_PATH}")


if __name__ == "__main__":
    main()
