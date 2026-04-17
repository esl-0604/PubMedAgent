"""PubMed 신규 논문을 검색해 Slack 쓰레드로 전송하는 스크립트.

환경변수:
  SLACK_BOT_TOKEN      (필수) xoxb-로 시작하는 Slack Bot Token
  ANTHROPIC_API_KEY    (필수) Claude API 키
  NCBI_API_KEY         (선택) NCBI E-utilities API 키
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml
from anthropic import Anthropic

from anthropic_logger import log_usage

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.yaml"
STATE_PATH = ROOT / "state" / "sent_pmids.json"
LOGIC_HASH_PATH = ROOT / "state" / "logic_hash.json"
ENV_PATH = ROOT / ".env"

# 검색 로직을 구성하는 설정 키. 이 값들이 바뀌면 Slack에 변경 공지 전송.
LOGIC_KEYS = (
    "keywords", "domain_terms", "journals",
    "physical_ai_terms", "physical_ai_journals",
    "authors", "lookback_days", "max_results",
)
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
SLACK_API = "https://slack.com/api"


def load_dotenv(path: Path = ENV_PATH) -> None:
    """`.env` 파일을 읽어 os.environ에 주입. 기존 환경변수는 덮어쓰지 않음."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_sent_pmids() -> set[str]:
    if not STATE_PATH.exists():
        return set()
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        return set(json.load(f))


def save_sent_pmids(pmids: set[str]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    trimmed = sorted(pmids, key=int)[-5000:]
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(trimmed, f)


def compute_logic_hash(cfg: dict) -> str:
    subset = {k: cfg.get(k) for k in LOGIC_KEYS}
    payload = json.dumps(subset, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_previous_logic_hash() -> str | None:
    if not LOGIC_HASH_PATH.exists():
        return None
    try:
        return json.loads(LOGIC_HASH_PATH.read_text(encoding="utf-8")).get("hash")
    except (json.JSONDecodeError, OSError):
        return None


def save_logic_hash(hash_value: str) -> None:
    LOGIC_HASH_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOGIC_HASH_PATH.write_text(
        json.dumps({"hash": hash_value, "updated_at": datetime.now(
            timezone(timedelta(hours=9))).isoformat()}),
        encoding="utf-8",
    )


def format_logic_summary_blocks(cfg: dict) -> tuple[str, list[dict]]:
    """검색 로직 변경을 간단히 알리는 Slack 블록 (상세 키워드는 생략)."""
    lookback = cfg.get("lookback_days")
    max_n = cfg.get("max_results")
    header_text = "🔧 PubMed 검색 로직 업데이트"
    body = (
        f"쿼리 구조: `(A: GI 내시경/로봇) ∪ (B: Medical Physical AI)`\n"
        f"상위 {max_n}건 · 최근 {lookback}일 기준"
    )
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": header_text}},
        {"type": "section", "text": {"type": "mrkdwn", "text": body}},
        {"type": "divider"},
    ]
    return header_text, blocks


def build_query(keywords: list[str], authors: list[str] | None = None,
                domain_terms: list[str] | None = None,
                journals: list[str] | None = None) -> str:
    kw_parts = [f'"{kw}"[Title/Abstract]' for kw in keywords]
    query = "(" + " OR ".join(kw_parts) + ")"
    if domain_terms:
        dom_parts = [f'"{d}"[Title/Abstract]' for d in domain_terms]
        query += " AND (" + " OR ".join(dom_parts) + ")"
    if journals:
        j_parts = [f'"{j}"[Journal]' for j in journals]
        query += " AND (" + " OR ".join(j_parts) + ")"
    if authors:
        au_parts = [f'"{au}"[Author]' for au in authors]
        query += " AND (" + " OR ".join(au_parts) + ")"
    return query


def esearch(query: str, lookback_days: int, max_results: int, api_key: str | None) -> list[str]:
    params = {
        "db": "pubmed",
        "term": query,
        "retmax": max_results,
        "sort": "most+recent",
        "datetype": "mdat",
        "reldate": lookback_days,
        "retmode": "json",
    }
    if api_key:
        params["api_key"] = api_key
    r = requests.get(f"{EUTILS}/esearch.fcgi", params=params, timeout=30)
    r.raise_for_status()
    return r.json().get("esearchresult", {}).get("idlist", [])


def efetch(pmids: list[str], api_key: str | None) -> list[dict]:
    if not pmids:
        return []
    params = {"db": "pubmed", "id": ",".join(pmids), "retmode": "xml"}
    if api_key:
        params["api_key"] = api_key
    r = requests.get(f"{EUTILS}/efetch.fcgi", params=params, timeout=60)
    r.raise_for_status()
    return parse_articles(r.text)


def _text(node: ET.Element | None) -> str:
    if node is None:
        return ""
    return "".join(node.itertext()).strip()


_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}


def _ymd_from_node(node: ET.Element | None) -> tuple[str, str, str] | None:
    """Year/Month/Day 자식을 가진 노드에서 (yy, mm, dd)를 뽑음. Year 없으면 None."""
    if node is None:
        return None
    year = _text(node.find("Year"))
    if not year:
        return None
    month_raw = _text(node.find("Month"))
    day = _text(node.find("Day"))
    yy = year[-2:]
    if not month_raw:
        return (yy, "", "")
    mm = _MONTHS.get(month_raw[:3].capitalize())
    mm_str = f"{mm:02d}" if mm else month_raw.zfill(2)
    dd_str = day.zfill(2) if day else ""
    return (yy, mm_str, dd_str)


def _format_pubdate(art: ET.Element) -> str:
    """논문 발행일을 'YY.MM.DD'로. 여러 소스를 시도해 가장 완전한 날짜를 선택."""
    # 우선순위: ArticleDate(electronic pub) → PubMedPubDate(pubmed 등재일) → PubDate(저널 표기)
    candidates: list[tuple[str, str, str]] = []
    for node in art.findall(".//ArticleDate"):
        ymd = _ymd_from_node(node)
        if ymd:
            candidates.append(ymd)
    for node in art.findall(".//PubMedPubDate"):
        if node.get("PubStatus") in ("pubmed", "entrez"):
            ymd = _ymd_from_node(node)
            if ymd:
                candidates.append(ymd)
    ymd = _ymd_from_node(art.find(".//PubDate"))
    if ymd:
        candidates.append(ymd)

    if not candidates:
        return ""
    # 가장 필드가 많이 채워진(= 일 단위까지 있는) 것을 선호
    best = max(candidates, key=lambda t: (bool(t[2]), bool(t[1])))
    yy, mm, dd = best
    if dd:
        return f"{yy}.{mm}.{dd}"
    if mm:
        return f"{yy}.{mm}"
    return yy


def _shorten_affiliation(aff: str) -> str:
    """PubMed affiliation 문자열에서 기관 이름 위주로 짧게 정리.

    원문 예: 'Department of Gastroenterology, Seoul National University Hospital,
    Seoul, Korea, 03080. email@...' → 'Seoul National University Hospital'.
    """
    # 이메일/끝의 우편번호 등 제거
    aff = aff.split(". Electronic address:")[0]
    aff = aff.split(" Electronic address:")[0]
    parts = [p.strip().rstrip(".") for p in aff.split(",") if p.strip()]
    # "Department of ...", "Division of ..." 같은 세부 부서 제거 → 기관명이 남도록
    junk_prefixes = ("department of", "division of", "section of", "school of",
                     "faculty of", "institute of", "laboratory of", "center for",
                     "centre for", "unit of", "clinic of")
    core = [p for p in parts if not p.lower().startswith(junk_prefixes)]
    if not core:
        core = parts
    # 남은 것 중 첫 항목(보통 대학/병원명)을 기관으로 채택
    return core[0] if core else aff.strip()


def parse_articles(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    out = []
    for art in root.findall(".//PubmedArticle"):
        pmid = _text(art.find(".//PMID"))
        title = _text(art.find(".//ArticleTitle"))
        journal = _text(art.find(".//Journal/Title"))
        abstract_nodes = art.findall(".//Abstract/AbstractText")
        abstract = " ".join(_text(n) for n in abstract_nodes)
        authors = []
        first_affiliation = ""
        for au in art.findall(".//AuthorList/Author"):
            last = _text(au.find("LastName"))
            initials = _text(au.find("Initials"))
            if last:
                authors.append(f"{last} {initials}".strip())
            if not first_affiliation:
                aff = _text(au.find("AffiliationInfo/Affiliation"))
                if aff:
                    first_affiliation = _shorten_affiliation(aff)
        pubdate = _format_pubdate(art)
        pmc_id = ""
        doi = ""
        for aid in art.findall(".//ArticleIdList/ArticleId"):
            id_type = aid.get("IdType")
            if id_type == "pmc" and not pmc_id:
                pmc_id = _text(aid).replace("PMC", "")
            elif id_type == "doi" and not doi:
                doi = _text(aid)
        out.append({
            "pmid": pmid,
            "pmc_id": pmc_id,
            "doi": doi,
            "title": title,
            "abstract": abstract,
            "authors": authors,
            "affiliation": first_affiliation,
            "journal": journal,
            "pubdate": pubdate,
            "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        })
    return out


def summarize_korean(client: Anthropic, model: str, article: dict) -> str:
    """논문 초록을 고정 템플릿 국문 요약으로 변환."""
    if not article["abstract"]:
        return "_(초록 없음)_"
    system = (
        "당신은 의학 논문을 단답형으로 요약하는 어시스턴트입니다. "
        "한국어 **한 문장, 80자 이내**로 이 논문이 무엇을 했고 무엇을 발견했는지만 쓰세요. "
        "수식어·배경설명·서론 없이 핵심만. 불릿/줄바꿈 금지. 전문 용어는 원어 허용."
    )
    user = f"제목: {article['title']}\n\n초록: {article['abstract']}"
    resp = client.messages.create(
        model=model,
        max_tokens=200,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    log_usage(script="pubmed_agent", model=model,
              input_tokens=resp.usage.input_tokens,
              output_tokens=resp.usage.output_tokens,
              request_type="summary")
    return resp.content[0].text.strip()


def slack_post(token: str, channel: str, text: str, blocks: list | None = None,
               thread_ts: str | None = None) -> dict:
    payload = {
        "channel": channel,
        "text": text,
        "unfurl_links": False,
        "unfurl_media": False,
    }
    if blocks:
        payload["blocks"] = blocks
    if thread_ts:
        payload["thread_ts"] = thread_ts
    r = requests.post(
        f"{SLACK_API}/chat.postMessage",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json; charset=utf-8"},
        data=json.dumps(payload),
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack API error: {data}")
    return data


def format_article_blocks(index: int, total: int, article: dict, summary_kr: str) -> list[dict]:
    first_author = article["authors"][0] if article["authors"] else ""
    if len(article["authors"]) > 1:
        first_author += " et al."
    affiliation = article.get("affiliation") or "기관 정보 없음"
    byline = f"{first_author or '저자 정보 없음'} · {affiliation}"
    header_bits = [f"<{article['url']}|{article['title']}>"]
    if article.get("journal"):
        header_bits.append(article["journal"])
    if article.get("pubdate"):
        header_bits.append(article["pubdate"])
    header = " - ".join(header_bits)
    body = (
        f"*[{index}/{total}] {header}*\n"
        f"_{byline}_\n\n"
        f"*[핵심 요약]*\n{summary_kr}"
    )
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": body}},
        {"type": "divider"},
    ]


def main() -> int:
    load_dotenv()
    cfg = load_config()
    slack_token = os.environ.get("SLACK_BOT_TOKEN")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    ncbi_key = os.environ.get("NCBI_API_KEY")

    if not slack_token:
        print("ERROR: SLACK_BOT_TOKEN 환경변수가 없습니다.", file=sys.stderr)
        return 1
    if not anthropic_key:
        print("ERROR: ANTHROPIC_API_KEY 환경변수가 없습니다.", file=sys.stderr)
        return 1

    channel = cfg["slack_channel_id"]
    max_results = cfg["max_results"]
    lookback = cfg["lookback_days"]

    # 검색 로직이 바뀌었으면 Slack에 변경 공지 1회 전송.
    current_hash = compute_logic_hash(cfg)
    previous_hash = load_previous_logic_hash()
    if previous_hash != current_hash:
        header_text, blocks = format_logic_summary_blocks(cfg)
        try:
            slack_post(slack_token, channel, header_text, blocks)
            print("[slack] 로직 변경 공지 전송")
        except Exception as e:
            print(f"[slack] 로직 변경 공지 실패: {e}", file=sys.stderr)
        save_logic_hash(current_hash)

    query_a = build_query(
        cfg["keywords"],
        cfg.get("authors") or [],
        cfg.get("domain_terms") or [],
        cfg.get("journals") or [],
    )
    print(f"[query A] {query_a}")
    pmids_a = esearch(query_a, lookback, max_results, ncbi_key)
    print(f"[esearch A] {len(pmids_a)} PMIDs")

    pmids_b: list[str] = []
    if cfg.get("physical_ai_terms"):
        time.sleep(0.4)
        query_b = build_query(
            cfg["physical_ai_terms"],
            [],
            [],
            cfg.get("physical_ai_journals") or [],
        )
        print(f"[query B] {query_b}")
        pmids_b = esearch(query_b, lookback, max_results, ncbi_key)
        print(f"[esearch B] {len(pmids_b)} PMIDs")

    # 두 쿼리 결과 union (순서 보존, A 먼저), 상위 max_results로 자름.
    seen: set[str] = set()
    merged: list[str] = []
    for pmid in pmids_a + pmids_b:
        if pmid not in seen:
            seen.add(pmid)
            merged.append(pmid)
    pmids = merged[:max_results]
    print(f"[merge] {len(pmids)} PMIDs after union/cap")

    sent = load_sent_pmids()
    new_pmids = [p for p in pmids if p not in sent]
    print(f"[filter] {len(new_pmids)} new (after dedup)")

    if not new_pmids:
        print("신규 논문 없음. Slack 전송 생략.")
        return 0

    time.sleep(0.4)
    articles = efetch(new_pmids, ncbi_key)
    client = Anthropic(api_key=anthropic_key)

    # 1) 부모 메시지 (날짜 요약)
    today = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d")
    parent_text = f"📚 PubMed 신규 논문 ({today}) — 총 {len(articles)}건"
    parent_blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": parent_text}},
        {"type": "context",
         "elements": [{"type": "mrkdwn", "text": "쓰레드에서 상세 확인"}]},
        {"type": "divider"},
    ]
    parent = slack_post(slack_token, channel, parent_text, parent_blocks)
    thread_ts = parent["ts"]
    print(f"[slack] 부모 메시지 전송 (ts={thread_ts})")

    # 2) 각 논문을 쓰레드 댓글로
    for i, art in enumerate(articles, 1):
        try:
            summary = summarize_korean(client, cfg["anthropic_model"], art)
        except Exception as e:
            summary = f"_(요약 실패: {e})_"
        blocks = format_article_blocks(i, len(articles), art, summary)
        fallback = f"[{i}/{len(articles)}] {art['title']}"
        slack_post(slack_token, channel, fallback, blocks, thread_ts=thread_ts)
        print(f"[slack] ({i}/{len(articles)}) {art['pmid']} 전송")
        time.sleep(0.3)  # Slack rate limit 여유

    sent.update(a["pmid"] for a in articles)
    save_sent_pmids(sent)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
