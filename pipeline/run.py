#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pipeline/run.py — 대시보드 데이터 일괄 생성 오케스트레이터 (이메일 없음)

원래 Google Cloud Run 이미지가 하던 일을 '이메일 발송만' 빼고 재현하고,
추가로 원자재/산업별 주가/산업별 선행지표까지 한 번에 갱신한다.

생성/갱신 파일 (모두 저장소 루트):
  - data.json      : 거시 지표(이격도/환율/금리/위험·변동성) + 국면 + 원자재
  - history.json   : 국면 점수 이력
  - sector.json    : 산업별 이동평균 카드
  - leading.json   : 산업별 선행지표 카드
  - daily_market_regime_log.csv : 전일 대비 비교용 국면 로그
        (저장소에 커밋되어 다음 실행에서 읽힘 — 원래 GCS가 하던 역할을 대체)

이메일이 빠지는 원리:
  daily_market_brief_alert.py 의 send_email() 은 오직 main() 에서만 호출된다.
  이 스크립트는 그 모듈을 import 만 하고 main() 을 호출하지 않으므로,
  이메일 코드 경로는 실행되지 않는다(원본 파일은 수정하지 않는다).

실행:  python pipeline/run.py

게시(publish):
  이 스크립트는 파일을 로컬 작업트리에 '생성/갱신만' 한다. 게시는 루틴(에이전트)이
  `git add/commit/push` 로 수행한다.
  이유: Claude Code 루틴의 egress 프록시가 api.github.com 직접 호출(PAT)을 차단하고,
  GitHub 접근은 통합 GitHub App 경로(git push / GitHub MCP 도구)로만 허용된다.
  따라서 PAT 기반 직접 게시는 이 환경에서 불가능하며, 게시는 App 인증 git push 로 한다.
"""
from __future__ import annotations

import os
import sys
import json
import subprocess
from pathlib import Path

# 무거운 전종목 breadth 계산은 끈다(원본 Cloud Run 잡과 동일: ENABLE_KRX_TECH_BREADTH=0).
# import 시점의 모듈 전역이 이 값을 읽으므로, 모듈 import 전에 반드시 설정한다.
os.environ.setdefault("ENABLE_KRX_TECH_BREADTH", "0")

ROOT = Path(__file__).resolve().parents[1]
PIPELINE_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = ROOT / "scripts"

# 상대경로(data.json 등)가 저장소 루트에 쓰이도록 보장.
os.chdir(ROOT)

# 파이프라인 모듈 import (email 코드는 main()에서만 호출되므로 여기선 실행되지 않음).
sys.path.insert(0, str(PIPELINE_DIR))
import daily_market_brief_alert as B  # noqa: E402
import web_export as W  # noqa: E402

# web_export 가 메인 모듈 함수를 참조하도록 주입(원래 publish(brief_module=...)가 하던 일).
W.B = B


def build_macro() -> None:
    """거시 지표 수집 → data.json + history.json(루트) 작성. 이메일은 보내지 않는다."""
    results = B.collect_indicators()

    # 주도주(leadership)는 실패해도 전체가 죽지 않도록 격리 — 원본 main()과 동일한 방침.
    try:
        _, leadership = B.build_leading_stock_report()
    except Exception as e:  # noqa: BLE001
        leadership = None
        print(f"[leading skip] {type(e).__name__}: {e}", file=sys.stderr)

    regime = B.classify_market_regime(results, leadership)

    # 전일 대비 국면 로그 갱신(GCS 미설정 → 로컬 CSV; 저장소에 커밋되어 다음 실행에서 읽힘).
    try:
        B.append_regime_log(regime, leadership)
    except Exception as e:  # noqa: BLE001
        print(f"[regime log skip] {type(e).__name__}: {e}", file=sys.stderr)

    payload = W.build_payload(results, regime)
    (ROOT / "data.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    hist_path = ROOT / "history.json"
    try:
        history = json.loads(hist_path.read_text(encoding="utf-8")) if hist_path.exists() else []
    except Exception:  # noqa: BLE001
        history = []
    history = W._append_history(history, regime)
    hist_path.write_text(
        json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[macro] data.json / history.json 작성 완료 ({payload['generated_at']})")


def run_script(argv, env_extra=None) -> None:
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    print(f"[run] {' '.join(str(a) for a in argv)}")
    subprocess.run([sys.executable, *[str(a) for a in argv]], check=True, cwd=str(ROOT), env=env)


# 게시 대상 파일 (저장소 루트 기준 경로) — 루틴이 이 목록을 git add/commit/push 한다.
PUBLISH_FILES = [
    "data.json",
    "history.json",
    "sector.json",
    "leading.json",
    "daily_market_regime_log.csv",
]


def main() -> int:
    build_macro()

    # 원자재(금·은·구리) 카드 병합 — publish()가 data.json을 통째로 덮어쓰며 원자재를
    # 날려버리던 문제를 여기서 해결한다(거시 data.json 작성 직후에 병합).
    run_script([SCRIPTS_DIR / "build_macro_commodities.py",
                "--input", "data.json", "--output", "data.json"])

    # 산업별 이동평균 카드 → sector.json
    run_script([SCRIPTS_DIR / "build_sector_ma.py",
                "--config", "config/sector_price_universe.json",
                "--output", "sector.json"])

    # 산업별 선행지표 → leading.json (FRED_API_KEY / DART_API_KEY 사용)
    run_script([SCRIPTS_DIR / "build_sector_leading.py"],
               env_extra={"OUTPUT_PATH": "leading.json"})

    # 게시는 하지 않는다 — 루틴(에이전트)이 아래 파일을 git add/commit/push 한다.
    print("[files] 게시 대상: " + ", ".join(PUBLISH_FILES))
    print("[done] 모든 대시보드 데이터 갱신 완료 (게시는 루틴이 git push 로 수행)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
