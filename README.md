# PubMed Agent

매일 아침 PubMed에서 신규 논문을 검색해 Slack 채널로 전송합니다.

## 로컬 테스트

```bash
pip install -r requirements.txt
export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..."
# (선택) export NCBI_API_KEY="..."
python pubmed_agent.py
```

Windows PowerShell에서는 `export` 대신 `$env:SLACK_WEBHOOK_URL="..."`.

## 검색 기준 수정

`config.yaml` 파일의 `keywords`, `authors` 목록을 편집하세요.

## 스케줄 배포

`.github/workflows/daily.yml`에 의해 매일 KST 08:50 (UTC 23:50) 자동 실행.
GitHub repo → Settings → Secrets and variables → Actions에서 다음 시크릿 등록:
- `SLACK_WEBHOOK_URL` (필수)
- `NCBI_API_KEY` (선택)
