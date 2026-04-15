"""Anthropic API 호출 토큰 사용량을 JSONL로 기록.

각 스크립트가 Claude 호출 직후 log_usage()를 호출하면
한 줄 JSON으로 append된다. billing_report.py가 이 파일을 집계.

기본 저장 위치: <repo>/state/usage_log.jsonl
환경변수 USAGE_LOG_PATH로 오버라이드 가능 (VM에서 /home/eslee/usage_log.jsonl 등).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

# 1M 토큰당 USD 단가 (2026-04 기준, 변경 시 업데이트)
PRICING = {
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "claude-opus-4-6": {"input": 15.0, "output": 75.0},
}


def _log_path() -> Path:
    override = os.environ.get("USAGE_LOG_PATH")
    if override:
        return Path(override)
    return Path(__file__).parent / "state" / "usage_log.jsonl"


def log_usage(script: str, model: str, usage, request_type: str = "") -> None:
    """Anthropic 응답의 usage 필드(또는 dict)를 받아 JSONL에 append."""
    path = _log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    input_tokens = getattr(usage, "input_tokens", None)
    output_tokens = getattr(usage, "output_tokens", None)
    if input_tokens is None and isinstance(usage, dict):
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "script": script,
        "model": model,
        "request_type": request_type,
        "input_tokens": int(input_tokens or 0),
        "output_tokens": int(output_tokens or 0),
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def estimate_cost(input_tokens: int, output_tokens: int, model: str) -> float:
    p = PRICING.get(model, PRICING["claude-haiku-4-5"])
    return (input_tokens / 1_000_000) * p["input"] + (output_tokens / 1_000_000) * p["output"]
