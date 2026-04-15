"""과거 논문 백필 스크립트.

지정 기간(기본: 2026-01-01 ~ 오늘)의 쿼리 A+B 결과를 모아
발행일 오름차순으로 10건씩 Slack에 포스팅한다.
`sent_pmids.json`을 존중해 중복 전송을 막는다.
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

from pubmed_agent import (
    EUTILS,
    build_query,
    efetch,
    format_article_blocks,
    load_config,
    load_dotenv,
    load_sent_pmids,
    save_sent_pmids,
    slack_post,
    summarize_korean,
)
from anthropic import Anthropic

CHUNK_SIZE = 10
BACKFILL_START = "2026/01/01"


def esearch_range(query: str, mindate: str, maxdate: str,
                  api_key: str | None, retmax: int = 2000) -> list[str]:
    """발행일 범위로 esearch. retmax까지 한 번에 회수."""
    params = {
        "db": "pubmed",
        "term": query,
        "datetype": "pdat",
        "mindate": mindate,
        "maxdate": maxdate,
        "retmode": "json",
        "retmax": retmax,
        "sort": "pub+date",  # 오래된 순
    }
    if api_key:
        params["api_key"] = api_key
    r = requests.get(f"{EUTILS}/esearch.fcgi", params=params, timeout=60)
    r.raise_for_status()
    return r.json().get("esearchresult", {}).get("idlist", [])


def main() -> int:
    load_dotenv()
    cfg = load_config()
    slack_token = os.environ.get("SLACK_BOT_TOKEN")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    ncbi_key = os.environ.get("NCBI_API_KEY")
    if not slack_token or not anthropic_key:
        print("ERROR: SLACK_BOT_TOKEN / ANTHROPIC_API_KEY 환경변수 필요", file=sys.stderr)
        return 1

    today_kst = datetime.now(timezone(timedelta(hours=9)))
    maxdate = today_kst.strftime("%Y/%m/%d")
    channel = cfg["slack_channel_id"]

    # 쿼리 A, B 실행
    qa = build_query(cfg["keywords"], [], cfg.get("domain_terms") or [],
                     cfg.get("journals") or [])
    qb = build_query(cfg["physical_ai_terms"], [], [],
                     cfg.get("physical_ai_journals") or [])
    pmids_a = esearch_range(qa, BACKFILL_START, maxdate, ncbi_key)
    print(f"[backfill] A: {len(pmids_a)} PMIDs")
    time.sleep(0.4)
    pmids_b = esearch_range(qb, BACKFILL_START, maxdate, ncbi_key)
    print(f"[backfill] B: {len(pmids_b)} PMIDs")

    # Union (A 먼저, 중복 제거). esearch는 최신→과거 정렬로 올 수 있으므로
    # 아래에서 발행일 기준 오름차순으로 재정렬.
    seen: set[str] = set()
    merged: list[str] = []
    for p in pmids_a + pmids_b:
        if p not in seen:
            seen.add(p)
            merged.append(p)

    sent = load_sent_pmids()
    new_pmids = [p for p in merged if p not in sent]
    print(f"[backfill] {len(new_pmids)} new (after dedup with sent_pmids)")
    if not new_pmids:
        print("백필할 신규 논문 없음.")
        return 0

    # efetch는 한 번에 많이 가능하나, 안정성을 위해 100개씩 나눠서 호출
    articles: list[dict] = []
    for i in range(0, len(new_pmids), 100):
        batch = new_pmids[i:i + 100]
        articles.extend(efetch(batch, ncbi_key))
        time.sleep(0.4)

    # 발행일(YY.MM.DD 문자열) 오름차순 정렬. 빈 문자열은 뒤로.
    def _sort_key(a: dict) -> tuple[int, str]:
        pd = a.get("pubdate") or ""
        return (0 if pd else 1, pd)
    articles.sort(key=_sort_key)

    total = len(articles)
    total_batches = (total + CHUNK_SIZE - 1) // CHUNK_SIZE
    print(f"[backfill] {total}건을 {total_batches}개 배치로 전송 시작")

    client = Anthropic(api_key=anthropic_key)

    # 백필 전체 안내 메시지 1회
    intro_text = f"📦 PubMed 과거 논문 백필 ({BACKFILL_START.replace('/', '-')} ~ {maxdate.replace('/', '-')})"
    intro_blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": intro_text}},
        {"type": "section", "text": {"type": "mrkdwn",
                                     "text": f"총 {total}건 · {total_batches}개 배치로 순차 전송합니다 (발행일 오름차순)."}},
    ]
    slack_post(slack_token, channel, intro_text, intro_blocks)

    # 10건씩 배치로 전송
    for b_idx in range(total_batches):
        batch_articles = articles[b_idx * CHUNK_SIZE:(b_idx + 1) * CHUNK_SIZE]
        batch_num = b_idx + 1
        parent_text = f"📚 백필 [{batch_num}/{total_batches}] — {len(batch_articles)}건"
        parent_blocks = [
            {"type": "header", "text": {"type": "plain_text", "text": parent_text}},
        ]
        parent = slack_post(slack_token, channel, parent_text, parent_blocks)
        thread_ts = parent["ts"]
        print(f"[slack] 배치 {batch_num}/{total_batches} 부모 전송")

        for i, art in enumerate(batch_articles, 1):
            try:
                summary = summarize_korean(client, cfg["anthropic_model"], art)
            except Exception as e:
                summary = f"_(요약 실패: {e})_"
            blocks = format_article_blocks(i, len(batch_articles), art, summary)
            fallback = f"[{i}/{len(batch_articles)}] {art['title']}"
            slack_post(slack_token, channel, fallback, blocks, thread_ts=thread_ts)
            print(f"[slack]  └ ({i}/{len(batch_articles)}) {art['pmid']}")
            time.sleep(0.3)

        # 배치 간에 잠깐 쉬어 rate limit 여유
        time.sleep(1.0)

    # 전송된 PMID 모두 상태에 반영
    sent.update(a["pmid"] for a in articles)
    save_sent_pmids(sent)
    print(f"[backfill] 완료. sent_pmids에 {total}건 추가됨.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
