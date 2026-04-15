# PubMed Agent

EndoRobotics(연성 내시경 기반 수술 로봇) 관심 분야의 PubMed 논문을 자동/반자동으로 수집·요약·분석해 Slack으로 전달하는 봇 모음.

## 구성

| 스크립트 | 역할 | 실행 방식 |
|---|---|---|
| **`pubmed_agent.py`** | 매일 신규 논문 자동 수집·요약 | GitHub Actions cron (매일 10:00 KST) |
| **`analyze_bot.py`** | DM으로 받은 논문을 상세 분석해 채널에 포스팅 | GCE VM + systemd (상시 구동) |
| **`backfill.py`** | 과거 기간 일괄 백필 | 수동 1회성 |

---

## 1) 매일 아침 자동 수집 — `pubmed_agent.py`

### 동작
1. PubMed esearch로 두 개의 쿼리 실행:
   - **쿼리 A (GI 내시경/로봇):** `(내시경/로봇 키워드) AND (GI 도메인 앵커) AND (GI 저널 화이트리스트)`
   - **쿼리 B (Medical Physical AI):** `(Physical AI 키워드) AND (AI·로봇 저널 화이트리스트)` — GI 국한 없음
2. 두 결과의 PMID를 union → 상위 `max_results`건으로 자름 → `state/sent_pmids.json`으로 중복 제거
3. efetch로 메타/초록 수집, Claude로 한 줄 한국어 요약
4. Slack 채널에 부모 메시지(`📚 PubMed 신규 논문 ...`) + 쓰레드 댓글(논문 카드 1~N건) 전송
5. `config.yaml`의 검색 로직이 바뀌면 자동 공지 메시지 1회 발송 (해시 비교)

### 논문 카드 포맷
```
[i/N] 논문 제목 - 저널 - 발행일자
     제1저자 et al. · 주요 기관

[핵심 요약]
한 문장 한국어 요약
```

### 스케줄
`.github/workflows/daily.yml` — `cron: "0 1 * * *"` UTC = **10:00 KST 매일**.
실행 후 갱신된 `state/sent_pmids.json`을 GitHub Actions bot이 자동 커밋.

### 검색 로직 수정
`config.yaml`의 아래 키를 편집하면 다음 실행에서 자동 반영:
- `keywords`, `domain_terms`, `journals` (쿼리 A)
- `physical_ai_terms`, `physical_ai_journals` (쿼리 B)
- `lookback_days`, `max_results`

---

## 2) DM 기반 논문 분석 봇 — `analyze_bot.py`

Slack에서 봇에게 **DM**으로 논문 정보를 보내면, 봇이 EndoRobotics 관점에서 분석해 **임상논문 채널에 업로드**합니다.

### 입력 형식 (자동 판별)
- **PMID** (예: `41970692`)
- **PubMed URL** (예: `https://pubmed.ncbi.nlm.nih.gov/41970692/`)
- **논문 제목** (한 줄 텍스트) — PubMed에서 자동 검색
- **초록/본문 원문** (긴 텍스트, 500자+) — 그대로 분석
- **PDF 파일 첨부** — 텍스트 추출 + Claude가 메타데이터(제목·저자·저널·발행일) 자동 파싱

### 흐름
1. DM 수신 → 즉시 답장 *"알겠습니다. 분석 후 임상논문 채널에 업로드하겠습니다."*
2. 입력 판별 · 메타 resolve
3. Claude로 상세 분석 (핵심 요약 / 방법·결과 / EndoRobotics 관점 시사점 / 한계·후속 과제)
4. 채널에 `🔍 논문 분석 요청` 부모 메시지 + 쓰레드에 상세 분석

### 구동 방식
**Slack Socket Mode** — 공개 URL 불필요, outbound WebSocket 하나로 이벤트 수신.

### 배포 위치
GCE `pubmed-bot` VM (`us-central1-a`, e2-micro) · systemd 서비스 `pubmed-bot.service`.

운영 명령:
```bash
# 로그
gcloud compute ssh pubmed-bot --zone=us-central1-a --command='sudo journalctl -u pubmed-bot -f'

# 재시작
gcloud compute ssh pubmed-bot --zone=us-central1-a --command='sudo systemctl restart pubmed-bot'

# 코드 업데이트
gcloud compute scp analyze_bot.py pubmed-bot:/home/eslee/PubMedAgent/ --zone=us-central1-a
gcloud compute ssh pubmed-bot --zone=us-central1-a --command='sudo systemctl restart pubmed-bot'
```

---

## 3) 과거 논문 백필 — `backfill.py`

지정 기간(기본 `2026-01-01 ~ 오늘`) 전체 PubMed 결과를 10건씩 끊어 순차 포스팅. `sent_pmids.json` 존중하므로 이미 보낸 논문은 스킵.

```bash
python backfill.py
```

2026-01-01 ~ 2026-04-15 구간 177건은 최초 배포 시 일괄 전송 완료.

---

## 환경변수

| 이름 | 필요 | 용도 |
|---|---|---|
| `SLACK_BOT_TOKEN` | 필수 | 채널 포스팅 (`xoxb-...`) |
| `SLACK_APP_TOKEN` | `analyze_bot`만 | Socket Mode 연결 (`xapp-...`) |
| `ANTHROPIC_API_KEY` | 필수 | Claude 요약/분석 |
| `NCBI_API_KEY` | 선택 | PubMed rate limit 완화 |

### 주입 방식
- **로컬 개발:** 프로젝트 루트 `.env` (git 제외) → `pubmed_agent.py`/`analyze_bot.py`가 자동 로드
- **GHA 스케줄 (pubmed_agent):** Settings → Secrets and variables → Actions
- **GCE VM (analyze_bot):** `/etc/pubmed-bot.env` (`chmod 600`) → systemd `EnvironmentFile`

---

## 로컬 개발/테스트

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt   # Windows
# .venv/bin/pip install -r requirements.txt      # Linux/Mac

# .env 파일에 토큰 작성 후
python pubmed_agent.py       # 자동 수집 1회
python analyze_bot.py        # DM 분석 봇 상시 구동 (Ctrl+C로 종료)
python backfill.py           # 백필
```

---

## 디렉토리 구조

```
.
├── .github/workflows/daily.yml   # GHA cron (매일 10:00 KST)
├── analyze_bot.py                # DM 분석 봇 (GCE)
├── backfill.py                   # 과거 논문 일괄 전송
├── config.yaml                   # 쿼리/저널/모델 설정
├── pubmed_agent.py               # 일일 자동 수집
├── requirements.txt
└── state/
    ├── sent_pmids.json           # 전송 이력 (중복 방지)
    └── logic_hash.json           # 로직 변경 공지 트리거
```
