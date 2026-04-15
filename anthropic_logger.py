"""Drop-in helper to log Anthropic API usage to a shared GCS bucket.

**Copy this file into each project that calls Anthropic API.**
After every API call, invoke `log_usage(...)` with the token counts from the
response `.usage` field.

The log is uploaded as a single-record JSONL object to:
  gs://<bucket>/logs/<script>/<YYYY-MM-DD>/<ts>-<uuid>.jsonl

ResourceAgent's weekly_report scans this bucket and aggregates the usage.

Auth: uses Application Default Credentials (ADC). On GCE VMs it's automatic
via the VM's attached service account; locally run `gcloud auth
application-default login`. The authenticating principal needs
`roles/storage.objectCreator` on the bucket.

Env:
  ANTHROPIC_LOG_BUCKET   GCS bucket name (default: resourceagent-usage)
  ANTHROPIC_LOG_DISABLE  set to "1" to silently no-op (useful in tests)
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from urllib.request import Request, urlopen

try:
    import google.auth
    import google.auth.transport.requests
except ImportError as e:
    raise ImportError(
        "anthropic_logger requires google-auth. Install with: "
        "pip install google-auth") from e


_BUCKET = os.environ.get("ANTHROPIC_LOG_BUCKET", "resourceagent-usage")
_creds = None


def _token() -> str:
    global _creds
    if _creds is None:
        _creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"])
    if not _creds.valid:
        _creds.refresh(google.auth.transport.requests.Request())
    return _creds.token


def log_usage(
    script: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    request_type: str | None = None,
    extra: dict | None = None,
    bucket: str | None = None,
    raise_on_error: bool = False,
) -> bool:
    """Append one usage record to the shared GCS log. Returns True on success.

    Args:
        script: identifier for this project/component (e.g. "pubmed_agent").
        model: Anthropic model ID (e.g. "claude-haiku-4-5").
        input_tokens, output_tokens: from response.usage.
        request_type: optional label (e.g. "summary", "triage").
        extra: optional dict of additional fields to include.
        bucket: override ANTHROPIC_LOG_BUCKET env var.
        raise_on_error: if True, re-raise network/auth errors. Default is
            to swallow so a logging failure never breaks the caller.
    """
    if os.environ.get("ANTHROPIC_LOG_DISABLE") in ("1", "true", "yes"):
        return False

    now = datetime.now(timezone.utc)
    ts = now.isoformat()
    record = {
        "ts": ts,
        "script": script,
        "model": model,
        "request_type": request_type,
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
    }
    if extra:
        record.update(extra)

    body = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    bkt = bucket or _BUCKET
    date = now.strftime("%Y-%m-%d")
    obj_name = (f"logs/{script}/{date}/"
                f"{now.strftime('%H%M%S')}-{uuid.uuid4().hex[:8]}.jsonl")
    # URL-escape the object name for the path portion
    from urllib.parse import quote
    url = (f"https://storage.googleapis.com/upload/storage/v1/b/{bkt}/o"
           f"?uploadType=media&name={quote(obj_name, safe='')}")
    req = Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {_token()}",
        "Content-Type": "application/x-ndjson",
    })
    try:
        with urlopen(req, timeout=10) as r:
            return 200 <= r.status < 300
    except Exception:
        if raise_on_error:
            raise
        return False
