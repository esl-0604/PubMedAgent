"""Slack DM 기반 논문 분석 봇.

DM으로 논문 제목/PMID/URL/초록 텍스트 중 하나를 보내면:
  1) DM 채널에 즉시 간단 확인 답변
  2) PubMed에서 메타/초록을 resolve (또는 입력 텍스트 그대로 사용)
  3) Claude로 EndoRobotics 관점 상세 분석
  4) 임상논문 채널(parent) + 쓰레드(상세 분석) 포스팅

구동 방식: Slack Socket Mode (상시 WebSocket 연결).
필요 환경변수:
  SLACK_BOT_TOKEN      xoxb-... (기존)
  SLACK_APP_TOKEN      xapp-... (신규, Socket Mode용)
  ANTHROPIC_API_KEY    sk-ant-... (기존)
  NCBI_API_KEY         선택
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import sys
import threading
import traceback

# Windows 콘솔 CP949에서도 한글·이모지 출력되도록 UTF-8로 재구성
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import requests
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from anthropic import Anthropic

from anthropic_logger import log_usage

from pubmed_agent import EUTILS, efetch, load_config, load_dotenv

load_dotenv()
CFG = load_config()
CHANNEL = CFG["slack_channel_id"]
MODEL = CFG["anthropic_model"]

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
NCBI_API_KEY = os.environ.get("NCBI_API_KEY")

if not (SLACK_BOT_TOKEN and SLACK_APP_TOKEN and ANTHROPIC_API_KEY):
    print("ERROR: SLACK_BOT_TOKEN / SLACK_APP_TOKEN / ANTHROPIC_API_KEY 환경변수 필요",
          file=sys.stderr)
    raise SystemExit(1)

app = App(token=SLACK_BOT_TOKEN)
claude = Anthropic(api_key=ANTHROPIC_API_KEY)

PMID_RE = re.compile(r"^\s*(\d{6,9})\s*$")
URL_RE = re.compile(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d+)")
MAX_FILE_BYTES = 20 * 1024 * 1024  # 20MB 이상 파일은 거부
INVALID_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_filename(name: str, default: str = "paper") -> str:
    """파일명으로 쓸 수 없는 문자(: / \\ | ? * 등)를 '-'로 치환."""
    name = INVALID_FILENAME_RE.sub("-", name or "")
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    if len(name) > 150:
        name = name[:150].rstrip().rstrip(".")
    return name or default


def _download_slack_file(file_info: dict) -> bytes:
    """Slack 사설 파일 URL에서 바이너리 다운로드 (Bot Token 필요)."""
    url = file_info.get("url_private_download") or file_info.get("url_private")
    if not url:
        raise ValueError("파일 URL 없음")
    r = requests.get(url, headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
                     timeout=60, stream=True)
    r.raise_for_status()
    data = b""
    for chunk in r.iter_content(chunk_size=65536):
        data += chunk
        if len(data) > MAX_FILE_BYTES:
            raise ValueError(f"파일 크기 초과 ({MAX_FILE_BYTES//1024//1024}MB 제한)")
    return data


def _extract_text_from_bytes(data: bytes, file_info: dict) -> str:
    """이미 다운로드된 바이너리에서 텍스트 추출. PDF/plaintext 지원."""
    filetype = (file_info.get("filetype") or "").lower()
    mimetype = (file_info.get("mimetype") or "").lower()
    name = file_info.get("name") or "attachment"

    if filetype == "pdf" or mimetype == "application/pdf" or name.lower().endswith(".pdf"):
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        pages = []
        for p in reader.pages:
            try:
                pages.append(p.extract_text() or "")
            except Exception:
                pass
        return "\n".join(pages).strip()

    if mimetype.startswith("text/") or filetype in ("txt", "text", "md"):
        try:
            return data.decode("utf-8", errors="replace").strip()
        except Exception:
            return ""

    raise ValueError(f"지원하지 않는 파일 타입: {filetype or mimetype or name}")


UNPAYWALL_EMAIL = os.environ.get("UNPAYWALL_EMAIL", "eunsang.lee@endorobo.com")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "esl-0604/PubMedAgent")
ANALYZED_API_PATH = "state/analyzed_pmids.json"
_REMEMBER_LOCK = threading.Lock()


def remember_analyzed_pmid(pmid: str) -> None:
    """분석 완료한 PMID를 GitHub 리포의 state/analyzed_pmids.json에 append.

    GitHub REST API `contents`를 사용해 SHA 기반 낙관적 락으로 처리.
    GITHUB_TOKEN 없거나 실패 시 경고만 찍고 스킵 (다음 DM 처리엔 영향 없음).
    """
    if not pmid:
        return
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print(f"[remember] GITHUB_TOKEN 미설정 — PMID {pmid} 기록 생략",
              file=sys.stderr)
        return

    with _REMEMBER_LOCK:
        api = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{ANALYZED_API_PATH}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        for attempt in range(3):
            # 1) 현재 파일 상태 조회
            sha = None
            existing: list[str] = []
            try:
                r = requests.get(api, headers=headers, timeout=30)
            except Exception as e:
                print(f"[remember] GET 실패: {e}", file=sys.stderr)
                return
            if r.status_code == 200:
                j = r.json()
                sha = j.get("sha")
                try:
                    raw = base64.b64decode(j.get("content") or "").decode("utf-8")
                    existing = json.loads(raw) if raw.strip() else []
                except Exception:
                    existing = []
            elif r.status_code != 404:
                print(f"[remember] GET HTTP {r.status_code}: {r.text[:200]}",
                      file=sys.stderr)
                return

            if pmid in existing:
                return  # 이미 기록됨

            try:
                merged = sorted(set(existing + [pmid]), key=int)[-5000:]
            except ValueError:
                merged = list(dict.fromkeys(existing + [pmid]))

            content = json.dumps(merged, ensure_ascii=False)
            payload = {
                "message": f"chore: analyze_bot PMID {pmid} 추가 [skip ci]",
                "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
                "branch": "main",
            }
            if sha:
                payload["sha"] = sha

            try:
                r = requests.put(api, headers=headers, json=payload, timeout=30)
            except Exception as e:
                print(f"[remember] PUT 실패: {e}", file=sys.stderr)
                return
            if r.status_code in (200, 201):
                print(f"[remember] PMID {pmid} GitHub 등록 성공", file=sys.stderr)
                return
            if r.status_code == 409:
                # SHA conflict — 다른 커밋이 먼저 들어옴. 재시도.
                print(f"[remember] SHA 충돌, 재시도 {attempt+1}/3",
                      file=sys.stderr)
                continue
            print(f"[remember] PUT HTTP {r.status_code}: {r.text[:200]}",
                  file=sys.stderr)
            return
        print(f"[remember] 3회 충돌, PMID {pmid} 기록 실패", file=sys.stderr)


def _download_pdf(url: str, headers: dict | None = None) -> bytes | None:
    """주어진 URL에서 PDF 바이너리 다운로드. PDF 헤더(%PDF) 검증."""
    try:
        r = requests.get(url, timeout=60, stream=True,
                         headers=headers or {"User-Agent": "Mozilla/5.0"},
                         allow_redirects=True)
        if r.status_code != 200:
            print(f"[pdf-dl] HTTP {r.status_code} from {url}", file=sys.stderr)
            return None
        data = b""
        for chunk in r.iter_content(chunk_size=65536):
            data += chunk
            if len(data) > MAX_FILE_BYTES:
                print(f"[pdf-dl] 크기 초과", file=sys.stderr)
                return None
        if not data.startswith(b"%PDF"):
            print(f"[pdf-dl] PDF 아님 (HTML landing page 추정): {url}",
                  file=sys.stderr)
            return None
        return data
    except Exception as e:
        print(f"[pdf-dl] 실패: {e}", file=sys.stderr)
        return None


def _pmc_pdf_bytes(pmc_id: str) -> bytes | None:
    """PMC Open-Access 서비스로 PDF URL 조회 후 다운로드."""
    if not pmc_id:
        return None
    pmc_id = pmc_id.replace("PMC", "")
    try:
        r = requests.get(
            "https://pmc.ncbi.nlm.nih.gov/utils/oa/oa.fcgi",
            params={"id": f"PMC{pmc_id}"},
            timeout=30,
        )
        if r.status_code != 200:
            print(f"[pmc] oa.fcgi HTTP {r.status_code}", file=sys.stderr)
            return None
        import xml.etree.ElementTree as ET
        root = ET.fromstring(r.text)
        if root.find(".//error") is not None:
            print(f"[pmc] PMC{pmc_id}: OA 서브셋 아님", file=sys.stderr)
            return None
        pdf_link = root.find(".//link[@format='pdf']")
        if pdf_link is None:
            print(f"[pmc] PMC{pmc_id}: PDF 링크 없음", file=sys.stderr)
            return None
        href = pdf_link.get("href")
        if not href:
            return None
        if href.startswith("ftp://"):
            href = href.replace("ftp://", "https://", 1)
        return _download_pdf(href)
    except Exception as e:
        print(f"[pmc] 다운로드 실패: {e}", file=sys.stderr)
        return None


def _unpaywall_pdf_bytes(doi: str) -> bytes | None:
    """Unpaywall API로 DOI에 대응하는 open-access PDF 찾기. PMC 밖 OA도 커버."""
    if not doi:
        return None
    try:
        r = requests.get(
            f"https://api.unpaywall.org/v2/{doi}",
            params={"email": UNPAYWALL_EMAIL},
            timeout=30,
        )
        if r.status_code != 200:
            print(f"[unpaywall] HTTP {r.status_code} for {doi}", file=sys.stderr)
            return None
        data = r.json()
        if not data.get("is_oa"):
            print(f"[unpaywall] {doi}: OA 아님", file=sys.stderr)
            return None
        # best_oa_location 우선, 없으면 oa_locations 순회
        locations = []
        if data.get("best_oa_location"):
            locations.append(data["best_oa_location"])
        locations.extend(data.get("oa_locations") or [])
        for loc in locations:
            pdf_url = loc.get("url_for_pdf")
            if not pdf_url:
                continue
            print(f"[unpaywall] {doi}: 시도 {pdf_url}", file=sys.stderr)
            pdf = _download_pdf(pdf_url)
            if pdf:
                return pdf
        print(f"[unpaywall] {doi}: 다운로드 가능한 PDF 없음", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[unpaywall] 실패: {e}", file=sys.stderr)
        return None


def _try_open_access_pdf(article: dict) -> tuple[bytes | None, str, str]:
    """PMC → Unpaywall 순서로 원문 PDF 확보 시도.

    Returns: (pdf_bytes_or_None, ext, human_readable_note).
    실패해도 note는 항상 비어있지 않은 설명 문자열.
    """
    pmc_id = article.get("pmc_id") or ""
    doi = article.get("doi") or ""
    print(f"[attach] pmc_id={pmc_id!r} doi={doi!r}", file=sys.stderr)

    if pmc_id:
        pdf = _pmc_pdf_bytes(pmc_id)
        if pdf:
            print(f"[attach] PMC에서 PDF 확보 ({len(pdf)} bytes)", file=sys.stderr)
            return pdf, ".pdf", "PMC Open-Access에서 다운로드한 원문 PDF를 이 쓰레드에 첨부했습니다."

    if doi:
        pdf = _unpaywall_pdf_bytes(doi)
        if pdf:
            print(f"[attach] Unpaywall에서 PDF 확보 ({len(pdf)} bytes)", file=sys.stderr)
            return pdf, ".pdf", "Unpaywall로 확보한 Open-Access 원문 PDF를 이 쓰레드에 첨부했습니다."

    # 실패 — 이유 정리
    print("[attach] 첨부 가능한 PDF 없음", file=sys.stderr)
    if not pmc_id and not doi:
        reason = "DOI/PMC ID를 확인할 수 없어 원문을 탐색하지 못했습니다."
    else:
        bits = []
        if pmc_id:
            bits.append("PMC Open-Access 서브셋에 포함되지 않음")
        if doi:
            bits.append("Unpaywall에서 Open-Access 전문 미확인")
        reason = "Open-Access 원문을 찾지 못했습니다 (" + ", ".join(bits) + ")."
    return None, "", reason


def _extract_doi_from_url(url: str) -> str:
    """PDF 메타 추출 등에서 나온 https://doi.org/<doi> 형태에서 DOI 추출."""
    if not url:
        return ""
    m = re.search(r"doi\.org/(.+)$", url)
    return m.group(1).strip() if m else ""


def _esearch_by_title(title: str) -> str | None:
    """제목(근사) 매칭으로 PMID 1개 찾기."""
    params = {
        "db": "pubmed",
        "term": f'"{title}"[Title]',
        "retmax": 1,
        "retmode": "json",
    }
    if NCBI_API_KEY:
        params["api_key"] = NCBI_API_KEY
    r = requests.get(f"{EUTILS}/esearch.fcgi", params=params, timeout=30)
    r.raise_for_status()
    ids = r.json().get("esearchresult", {}).get("idlist", [])
    if ids:
        return ids[0]
    # relaxed: 일반 텀 검색
    params["term"] = title
    r = requests.get(f"{EUTILS}/esearch.fcgi", params=params, timeout=30)
    r.raise_for_status()
    ids = r.json().get("esearchresult", {}).get("idlist", [])
    return ids[0] if ids else None


def resolve_input(text: str) -> dict | None:
    """입력 텍스트를 분석 대상 article dict로 해소.

    우선순위: URL → PMID(숫자) → 긴 텍스트(500자+, 초록으로 간주) → 짧은 문구(제목으로 검색)
    """
    text = text.strip()
    if not text:
        return None

    url_match = URL_RE.search(text)
    if url_match:
        articles = efetch([url_match.group(1)], NCBI_API_KEY)
        return articles[0] if articles else None

    pmid_match = PMID_RE.match(text)
    if pmid_match:
        articles = efetch([pmid_match.group(1)], NCBI_API_KEY)
        return articles[0] if articles else None

    if len(text) >= 500 or text.count("\n") >= 2:
        # 초록/본문 텍스트로 간주
        return {
            "pmid": "",
            "pmc_id": "",
            "doi": "",
            "title": "(DM 입력 텍스트 분석)",
            "abstract": text,
            "authors": [],
            "affiliation": "",
            "journal": "",
            "pubdate": "",
            "url": "",
        }

    # 짧은 제목으로 간주 → 검색
    pmid = _esearch_by_title(text)
    if pmid:
        articles = efetch([pmid], NCBI_API_KEY)
        return articles[0] if articles else None
    return None


def extract_metadata_from_text(text: str) -> dict:
    """PDF/긴 텍스트 앞부분에서 제목·저자·저널·발행일을 Claude로 추출."""
    snippet = text[:4000]
    system = (
        "너는 학술 논문의 앞부분 텍스트에서 서지 메타데이터를 추출하는 도우미다. "
        "반드시 **JSON만** 출력하며, 다음 키를 포함: "
        '{"title": str, "authors": [str, ...], "affiliation": str, "journal": str, "pubdate": str, "url": str}. '
        "authors는 최대 6명까지 'LastName FM' 형식. "
        "affiliation은 제1저자 소속(대학/병원명 하나만). "
        "pubdate는 'YY.MM.DD' 또는 'YY.MM' 또는 'YY' 형식. "
        "url은 DOI 링크가 있으면 https://doi.org/<doi> 형태로 채움. "
        "확신이 없는 필드는 빈 문자열 혹은 빈 배열. 다른 설명·코드블록 없이 JSON만."
    )
    resp = claude.messages.create(
        model=MODEL,
        max_tokens=600,
        system=system,
        messages=[{"role": "user", "content": snippet}],
    )
    log_usage(script="analyze_bot", model=MODEL,
              input_tokens=resp.usage.input_tokens,
              output_tokens=resp.usage.output_tokens,
              request_type="extract_meta")
    raw = resp.content[0].text.strip()
    # 혹시 ```json ... ``` 포장되어 오면 벗기기
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.DOTALL)
    try:
        meta = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    # 안전한 기본값
    return {
        "title": meta.get("title") or "",
        "authors": [a for a in (meta.get("authors") or []) if isinstance(a, str)][:6],
        "affiliation": meta.get("affiliation") or "",
        "journal": meta.get("journal") or "",
        "pubdate": meta.get("pubdate") or "",
        "url": meta.get("url") or "",
    }


def analyze_article(article: dict) -> str:
    """Claude로 EndoRobotics 관점 상세 분석 (Slack mrkdwn).

    아래 4개 섹션만 순서대로 작성. `[원문 첨부]` 섹션은 코드에서 별도로 앞에 붙인다.
    """
    system = (
        "당신은 한국의 의료기기 회사 EndoRobotics의 R&D 엔지니어 관점에서 임상 논문을 분석합니다. "
        "EndoRobotics는 연성 내시경 기반 수술 로봇·기구를 개발하며, "
        "ESD·POEM·endoscopic suturing·NOTES 등 GI 시술이 주력 분야입니다. "
        "Slack mrkdwn 형식으로 아래 4개 섹션을 **정확히 이 순서대로**, **제목도 문자 그대로** 한국어로 작성하세요. "
        "각 섹션 헤더는 `*[제목]*` 형식(별표 포함), 불릿은 • 사용. 다른 섹션은 추가 금지.\n\n"
        "*[핵심 요약]* — 3~4줄\n"
        "*[방법 / 결과]* — 3~5줄\n"
        "*[한계 / 후속 과제]* — 2~3줄\n"
        "*[EndoRobotics 관점 시사점]* — 회사 제품·파이프라인과의 연결점, 3~5줄"
    )
    user = (
        f"제목: {article.get('title')}\n"
        f"저자: {', '.join(article.get('authors') or [])}\n"
        f"소속: {article.get('affiliation')}\n"
        f"저널: {article.get('journal')} ({article.get('pubdate')})\n"
        f"PMID: {article.get('pmid')}\n\n"
        f"초록:\n{article.get('abstract') or '(초록 없음)'}"
    )
    resp = claude.messages.create(
        model=MODEL,
        max_tokens=1500,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    log_usage(script="analyze_bot", model=MODEL,
              input_tokens=resp.usage.input_tokens,
              output_tokens=resp.usage.output_tokens,
              request_type="analyze")
    return resp.content[0].text.strip()


def post_to_channel(article: dict, analysis: str, requester_id: str,
                    web_client,
                    attachment_bytes: bytes | None = None,
                    attachment_ext: str = "") -> None:
    title = article.get("title") or "제목 미상"
    url = article.get("url")
    title_link = f"<{url}|{title}>" if url else title
    header_bits = [title_link]
    if article.get("journal"):
        header_bits.append(article["journal"])
    if article.get("pubdate"):
        header_bits.append(article["pubdate"])
    header = " - ".join(header_bits)

    byline_bits = []
    if article.get("authors"):
        first = article["authors"][0]
        if len(article["authors"]) > 1:
            first += " et al."
        byline_bits.append(first)
    if article.get("affiliation"):
        byline_bits.append(article["affiliation"])
    byline = " · ".join(byline_bits) if byline_bits else "출처 정보 없음"

    parent_text = f"🔍 논문 분석 요청 — {title}"
    parent_blocks = [
        {"type": "header",
         "text": {"type": "plain_text", "text": "🔍 논문 분석 요청"}},
        {"type": "section",
         "text": {"type": "mrkdwn",
                  "text": f"*{header}*\n_{byline}_\n요청자: <@{requester_id}>"}},
        {"type": "divider"},
    ]
    parent = web_client.chat_postMessage(
        channel=CHANNEL, text=parent_text, blocks=parent_blocks,
        unfurl_links=False, unfurl_media=False,
    )
    thread_ts = parent["ts"]

    # Slack section 블록 텍스트 한계(3000자) 방어: 필요시 분할.
    # 마지막 청크에만 divider 붙여서 다음 메시지와 시각 분리.
    chunks = _chunk(analysis, 2800)
    for i, chunk in enumerate(chunks):
        blocks = [{"type": "section",
                   "text": {"type": "mrkdwn", "text": chunk}}]
        if i == len(chunks) - 1:
            blocks.append({"type": "divider"})
        web_client.chat_postMessage(
            channel=CHANNEL,
            thread_ts=thread_ts,
            text="상세 분석",
            blocks=blocks,
            unfurl_links=False, unfurl_media=False,
        )

    # 원문 첨부 (있을 때만). 파일명은 논문 제목 기준으로 정리.
    if attachment_bytes:
        filename = _safe_filename(title) + (attachment_ext or ".bin")
        try:
            web_client.files_upload_v2(
                channel=CHANNEL,
                thread_ts=thread_ts,
                file=attachment_bytes,
                filename=filename,
                title=filename,
            )
        except Exception as e:
            print(f"[upload] 첨부 업로드 실패: {e}", file=sys.stderr)
            traceback.print_exc()


def _chunk(text: str, size: int) -> list[str]:
    if len(text) <= size:
        return [text]
    parts: list[str] = []
    remaining = text
    while len(remaining) > size:
        cut = remaining.rfind("\n", 0, size)
        if cut < size // 2:
            cut = size
        parts.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        parts.append(remaining)
    return parts


@app.event("message")
def handle_message(event, say, client):
    # DM(im)만 처리. 봇 자신의 메시지·수정/삭제는 무시. file_share subtype은 허용.
    if event.get("channel_type") != "im":
        return
    if event.get("bot_id"):
        return
    subtype = event.get("subtype")
    if subtype and subtype != "file_share":
        return
    user_id = event.get("user")
    text = (event.get("text") or "").strip()
    files = event.get("files") or []
    if not user_id:
        return
    if not text and not files:
        return

    # 1) 즉시 DM 응답
    say("알겠습니다. 분석 후 임상논문 채널에 업로드하겠습니다.")

    try:
        article = None
        attachment_bytes: bytes | None = None
        attachment_ext: str = ""

        # 파일이 있으면 우선 파일에서 텍스트 추출 → 초록으로 사용
        if files:
            extracted_chunks: list[str] = []
            for f in files:
                try:
                    data = _download_slack_file(f)
                    text_part = _extract_text_from_bytes(data, f)
                    if text_part:
                        extracted_chunks.append(text_part)
                    # 첫 번째 파일을 원문 첨부용으로 보관
                    if attachment_bytes is None:
                        attachment_bytes = data
                        fname = f.get("name") or ""
                        if "." in fname:
                            attachment_ext = "." + fname.rsplit(".", 1)[1].lower()
                        else:
                            ft = (f.get("filetype") or "pdf").lower()
                            attachment_ext = f".{ft}"
                except Exception as fe:
                    say(f"⚠️ 파일 `{f.get('name')}` 처리 실패: {fe}")
            extracted = "\n\n".join(c for c in extracted_chunks if c).strip()
            if extracted:
                # 메타데이터를 Claude로 추출해 채우기
                try:
                    meta = extract_metadata_from_text(extracted)
                except Exception as me:
                    print(f"[analyze_bot] 메타 추출 실패: {me}", file=sys.stderr)
                    meta = {}
                title_hint = (meta.get("title")
                              or (text if text else "(첨부 파일)"))[:300]
                meta_url = meta.get("url") or ""
                article = {
                    "pmid": "",
                    "pmc_id": "",
                    "doi": _extract_doi_from_url(meta_url),
                    "title": title_hint,
                    "abstract": extracted,
                    "authors": meta.get("authors") or [],
                    "affiliation": meta.get("affiliation") or "",
                    "journal": meta.get("journal") or "",
                    "pubdate": meta.get("pubdate") or "",
                    "url": meta_url,
                }
        # 파일이 없거나 추출 실패면 텍스트 경로
        if article is None and text:
            article = resolve_input(text)

        if not article:
            say("⚠️ 입력에서 논문을 식별하지 못했습니다. 제목 / PMID / PubMed URL / 초록 원문 / PDF 파일 중 하나를 보내주세요.")
            return

        # 첨부 결정 및 상태 문구 생성.
        if attachment_bytes is not None:
            attach_note = "사용자가 업로드한 원문 PDF를 이 쓰레드에 첨부했습니다."
        else:
            attachment_bytes, attachment_ext, attach_note = _try_open_access_pdf(article)

        analysis_body = analyze_article(article)
        full_body = f"*[원문 첨부]*\n{attach_note}\n\n{analysis_body}"
        post_to_channel(article, full_body, user_id, client,
                        attachment_bytes=attachment_bytes,
                        attachment_ext=attachment_ext)
        # 분석한 PMID를 GitHub에 기록해 pubmed_agent 일일 수집과 중복 방지
        pmid = article.get("pmid")
        if pmid:
            remember_analyzed_pmid(pmid)
    except Exception as e:
        traceback.print_exc()
        say(f"❌ 분석 중 오류: `{e}`")


if __name__ == "__main__":
    print("[analyze_bot] Socket Mode 시작 — DM 대기 중...")
    SocketModeHandler(app, SLACK_APP_TOKEN).start()
