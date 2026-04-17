"""paper_interests.jsonl 집계 → 사용자 관심 프로파일 요약 출력.

로컬 수동 실행 전용. analyze_bot이 누적한 관심 논문 기록을 읽어
저널/저자/기관/토픽/기술 분포를 집계하고, 선택적으로 Claude에 요약 요청.

사용법:
  python interest_profile.py         # 통계만
  python interest_profile.py --llm   # Claude 요약 포함
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter
from pathlib import Path

from pubmed_agent import load_dotenv

ROOT = Path(__file__).parent
INTERESTS_PATH = ROOT / "state" / "paper_interests.jsonl"


def load_records() -> list[dict]:
    if not INTERESTS_PATH.exists():
        return []
    records = []
    for line in INTERESTS_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def aggregate(records: list[dict]) -> dict:
    journals = Counter()
    focus = Counter()
    domains = Counter()
    study_types = Counter()
    topics = Counter()
    techniques = Counter()
    signals = Counter()
    authors = Counter()
    affiliations = Counter()

    for r in records:
        if r.get("journal"):
            journals[r["journal"]] += 1
        for a in r.get("authors") or []:
            authors[a] += 1
        for a in r.get("affiliations") or []:
            affiliations[a] += 1
        f = r.get("features") or {}
        if f.get("focus"):
            focus[f["focus"]] += 1
        if f.get("domain"):
            domains[f["domain"]] += 1
        if f.get("study_type"):
            study_types[f["study_type"]] += 1
        for t in f.get("topics") or []:
            topics[t] += 1
        for t in f.get("techniques") or []:
            techniques[t] += 1
        for s in f.get("signals") or []:
            signals[s] += 1

    return {
        "journals": journals, "focus": focus, "domains": domains,
        "study_types": study_types, "topics": topics, "techniques": techniques,
        "signals": signals, "authors": authors, "affiliations": affiliations,
    }


def print_top(title: str, counter: Counter, n: int = 15) -> None:
    print(f"\n[{title}]")
    if not counter:
        print("  (데이터 없음)")
        return
    for item, cnt in counter.most_common(n):
        print(f"  {cnt:>3}  {item}")


def llm_summary(records: list[dict]) -> str:
    from anthropic import Anthropic
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return "(ANTHROPIC_API_KEY 없음)"
    client = Anthropic(api_key=api_key)

    # 토큰 절약을 위해 features만 뽑아 전달
    condensed = []
    for r in records[-200:]:  # 최근 200건까지
        condensed.append({
            "title": r.get("title"),
            "journal": r.get("journal"),
            "authors": (r.get("authors") or [])[:3],
            "affiliations": (r.get("affiliations") or [])[:2],
            "features": r.get("features") or {},
        })
    system = (
        "사용자가 주로 분석 요청하는 임상·기술 논문들의 패턴을 한국어로 요약. "
        "EndoRobotics(연성 내시경 수술 로봇/기구) 관점에서 해석. "
        "섹션: *[선호 주제]*, *[선호 저널]*, *[선호 저자·기관]*, *[선호 기술·기구]*, *[연구 유형 경향]*, *[종합 해석]*. "
        "각 섹션 3~5줄 이내, 구체적 예시 인용. Slack mrkdwn 가능."
    )
    resp = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=1500,
        system=system,
        messages=[{"role": "user",
                   "content": f"총 {len(records)}건 기록. 아래는 최근 {len(condensed)}건:\n\n{json.dumps(condensed, ensure_ascii=False, indent=1)}"}],
    )
    return resp.content[0].text.strip()


def main() -> int:
    load_dotenv()
    records = load_records()
    if not records:
        print("아직 기록된 관심 논문이 없습니다.")
        print(f"(경로: {INTERESTS_PATH})")
        return 0

    print(f"총 {len(records)} 건 기록")
    agg = aggregate(records)
    print_top("저널 TOP", agg["journals"])
    print_top("토픽 TOP", agg["topics"])
    print_top("기술·기구 TOP", agg["techniques"])
    print_top("저자 TOP", agg["authors"])
    print_top("기관 TOP", agg["affiliations"])
    print_top("도메인 분포", agg["domains"])
    print_top("연구 유형", agg["study_types"])
    print_top("Focus 분포", agg["focus"])
    print_top("EndoRobotics signals TOP", agg["signals"])

    if "--llm" in sys.argv:
        print("\n" + "=" * 60)
        print("Claude 요약:")
        print("=" * 60)
        print(llm_summary(records))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
