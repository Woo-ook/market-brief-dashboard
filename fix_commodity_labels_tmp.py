from pathlib import Path
import json
import re

TARGET_FILES = [
    Path("data.json"),
    Path("docs/data.json"),
]

def infer_label(ind):
    name = str(ind.get("name", ""))
    source = str(ind.get("source", ""))

    if "GC=F" in source or name.startswith("금"):
        return "금"
    if "SI=F" in source or name.startswith("은"):
        return "은"
    if "HG=F" in source or name.startswith("구리"):
        return "구리"
    return None

def fix_indicator(ind):
    label = infer_label(ind)
    if not label:
        return False

    old = dict(ind)

    # 제목: 가격 카드로 고정
    ind["name"] = f"{label} 가격"

    # 메인 값: 가격만 남김
    value_text = str(ind.get("value_text", ""))
    value_text = re.sub(r"\s*·\s*이격도\s*[-+]?\d+(?:\.\d+)?", "", value_text)
    value_text = re.sub(r"\s*·\s*50일선 대비\s*[-+]?\d+(?:\.\d+)?", "", value_text)
    ind["value_text"] = value_text.strip()

    # change_text: 이격도는 보조 설명으로 이동
    change_text = str(ind.get("change_text", ""))
    change_text = change_text.replace("· 이격도 ", "· 50일선 대비 ")
    change_text = change_text.replace("이격도 ", "50일선 대비 ")
    ind["change_text"] = change_text

    # comment: 오해 없게 강제 재작성
    ind["comment"] = (
        f"{label}의 현재 가격을 보여주는 카드입니다. "
        "상태 판단은 현재 가격의 50일 이동평균 대비 이격도와 "
        "최근 5년 이격도 분포 기준으로 계산합니다. "
        "따라서 메인 값은 가격이고, 이격도는 과열·침체 판단을 위한 보조지표입니다."
    )

    return old != ind

changed = False

for path in TARGET_FILES:
    data = json.loads(path.read_text(encoding="utf-8"))

    for ind in data.get("indicators", []):
        if ind.get("category") == "원자재":
            if fix_indicator(ind):
                changed = True

    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

# 재사용 가능한 보정 스크립트도 저장
out = Path("scripts/fix_commodity_labels.py")
out.write_text(Path(__file__).read_text(encoding="utf-8") if "__file__" in globals() else "", encoding="utf-8")

print("changed =", changed)
print("commodity labels normalized")
