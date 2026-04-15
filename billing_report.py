"""지난 7일간 Clinical Agent 사용량·비용을 Slack 채널에 리포트.

각 실행 환경(GHA / GCE VM)에서 독립적으로 자신의 usage 로그를 집계·발송한다.
환경변수:
  SOURCE_LABEL       리포트 출처 라벨 (예: 'GHA 러너 (pubmed_agent, backfill)', 'GCE VM (analyze_bot)')
  USAGE_LOG_PATH     usage 로그 경로 오버라이드 (usage_logger.py와 동일 규칙)
  SLACK_BOT_TOKEN    Slack 포스팅용
  BILLING_CHANNEL_ID 리포트 전송 채널 ID
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pubmed_agent import load_dotenv, slack_post
from usage_logger import PRICING, _log_path, estimate_cost

KST = timezone(timedelta(hours=9))


def _read_log(days: int = 7) -> list[dict]:
    path = _log_path()
    if not path.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    records: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
            ts = datetime.fromisoformat(rec["ts"].replace("Z", "+00:00"))
            if ts >= cutoff:
                records.append(rec)
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
    return records


def _aggregate(records: list[dict]) -> dict:
    by_type: dict[str, dict] = {}
    total_in = total_out = total_cost = 0.0
    total_calls = 0
    for r in records:
        key = f"{r.get('script', '?')}:{r.get('request_type') or 'default'}"
        bucket = by_type.setdefault(key, {
            "calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0
        })
        in_t = r.get("input_tokens", 0)
        out_t = r.get("output_tokens", 0)
        cost = estimate_cost(in_t, out_t, r.get("model", ""))
        bucket["calls"] += 1
        bucket["input_tokens"] += in_t
        bucket["output_tokens"] += out_t
        bucket["cost_usd"] += cost
        total_calls += 1
        total_in += in_t
        total_out += out_t
        total_cost += cost
    return {
        "by_type": by_type,
        "total_calls": total_calls,
        "total_input": total_in,
        "total_output": total_out,
        "total_cost": total_cost,
    }


def _gcp_inventory_note() -> str:
    """VM 리포트에 붙는 GCP 인벤토리 요약 (정적)."""
    return (
        "*☁️ GCP 리소스 (현재 인벤토리)*\n"
        "• Compute: `e2-micro` VM 상시 구동 (us-central1-a)\n"
        "• Disk: 20GB PD standard\n"
        "• 무료 티어 내 예상 월 비용: *$0* (초과 시 ~$7/월)"
    )


def format_report_blocks(source_label: str, agg: dict,
                         start_date: str, end_date: str,
                         include_gcp: bool) -> tuple[str, list[dict]]:
    header_text = "🧾 [Clinical Agent] 주간 과금 리포트"
    lines = [
        f"*출처:* {source_label}",
        f"*기간:* {start_date} ~ {end_date} (최근 7일)",
        "",
        "*📊 Anthropic API*",
    ]
    if agg["total_calls"] == 0:
        lines.append("_사용 내역 없음_")
    else:
        for key, b in sorted(agg["by_type"].items()):
            lines.append(
                f"• `{key}`: {b['calls']}회 · "
                f"입력 {b['input_tokens']:,} / 출력 {b['output_tokens']:,} tok · "
                f"${b['cost_usd']:.4f}"
            )
        lines.append(
            f"\n*합계:* {agg['total_calls']}회 호출 · "
            f"입력 {agg['total_input']:,} tok · 출력 {agg['total_output']:,} tok · "
            f"*${agg['total_cost']:.4f}*"
        )
    if include_gcp:
        lines.append("")
        lines.append(_gcp_inventory_note())
    lines.append("")
    lines.append("_이 리포트는 PubMed Clinical Agent 프로젝트 사용량입니다._")

    blocks = [
        {"type": "header",
         "text": {"type": "plain_text", "text": header_text}},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": "\n".join(lines)}},
    ]
    return header_text, blocks


def main() -> int:
    load_dotenv()
    slack_token = os.environ.get("SLACK_BOT_TOKEN")
    channel = os.environ.get("BILLING_CHANNEL_ID")
    source_label = os.environ.get("SOURCE_LABEL", "Unknown")
    include_gcp = os.environ.get("INCLUDE_GCP", "").lower() in ("1", "true", "yes")

    if not slack_token or not channel:
        print("ERROR: SLACK_BOT_TOKEN / BILLING_CHANNEL_ID 필요", file=sys.stderr)
        return 1

    records = _read_log(days=7)
    agg = _aggregate(records)

    end = datetime.now(KST)
    start = end - timedelta(days=7)
    header_text, blocks = format_report_blocks(
        source_label, agg,
        start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"),
        include_gcp,
    )
    slack_post(slack_token, channel, header_text, blocks)
    print(f"[billing_report] 전송 완료: {source_label} · ${agg['total_cost']:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
