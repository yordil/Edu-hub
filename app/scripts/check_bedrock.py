"""Bedrock connectivity check: `python -m app.scripts.check_bedrock [model_id ...]`.

Reads credentials from the environment or ./.env and never prints them. For each model it reports
whether a plain chat call works and whether the model supports tool calling (needed by the tutor agent).
"""

import os
import sys
import time
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

CANDIDATES = [
    "us.deepseek.v3-v1:0",
    "deepseek.v3-v1:0",
    "us.deepseek.r1-v1:0",
    "us.amazon.nova-lite-v1:0",
    "us.amazon.nova-micro-v1:0",
]

PROBE_TOOL = {
    "tools": [
        {
            "toolSpec": {
                "name": "get_weak_topics",
                "description": "Return the student's weakest topics.",
                "inputSchema": {"json": {"type": "object", "properties": {"limit": {"type": "integer"}}}},
            }
        }
    ]
}


def load_env_file(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def configure_credentials() -> str:
    # Bedrock API keys (prefix ABSK) are bearer tokens that boto3 reads from this variable.
    if "AWS_BEARER_TOKEN_BEDROCK" not in os.environ:
        for name in ("BEDROCK_API_KEY", "API_KEY"):
            if os.environ.get(name, "").startswith("ABSK"):
                os.environ["AWS_BEARER_TOKEN_BEDROCK"] = os.environ[name]
                return f"Bedrock API key from {name}"
    if "AWS_BEARER_TOKEN_BEDROCK" in os.environ:
        return "Bedrock API key from AWS_BEARER_TOKEN_BEDROCK"
    if os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("AWS_PROFILE"):
        return "IAM credentials"
    raise SystemExit("No Bedrock credentials found (set AWS_BEARER_TOKEN_BEDROCK in .env)")


def list_models(region: str) -> list[str]:
    try:
        client = boto3.client("bedrock", region_name=region)
        summaries = client.list_foundation_models(byOutputModality="TEXT")["modelSummaries"]
        return sorted(m["modelId"] for m in summaries if any(k in m["modelId"] for k in ("deepseek", "nova")))
    except (ClientError, BotoCoreError) as e:
        return [f"(listing not permitted: {type(e).__name__})"]


def probe(runtime, model_id: str) -> None:
    messages = [{"role": "user", "content": [{"text": "Reply with exactly the word OK."}]}]
    try:
        t0 = time.perf_counter()
        r = runtime.converse(modelId=model_id, messages=messages, inferenceConfig={"maxTokens": 300, "temperature": 0})
        ms = (time.perf_counter() - t0) * 1000
        text = next((c["text"] for c in r["output"]["message"]["content"] if "text" in c), "").strip()
        print(f"  chat   OK  {ms:6.0f} ms  tokens in/out {r['usage']['inputTokens']}/{r['usage']['outputTokens']}  reply={text[:60]!r}")
    except ClientError as e:
        print(f"  chat   FAIL {e.response['Error']['Code']}: {e.response['Error']['Message'][:140]}")
        return

    try:
        r = runtime.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": "What are my weakest topics? Use the tool."}]}],
            toolConfig=PROBE_TOOL,
            inferenceConfig={"maxTokens": 300, "temperature": 0},
        )
        used = any("toolUse" in c for c in r["output"]["message"]["content"])
        print(f"  tools  {'OK  model called the tool' if used else 'NO  answered without calling the tool'}")
    except ClientError as e:
        print(f"  tools  UNSUPPORTED {e.response['Error']['Code']}: {e.response['Error']['Message'][:120]}")


def main() -> None:
    load_env_file()
    source = configure_credentials()
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
    print(f"Credentials: {source} | region: {region}")
    print(f"Available deepseek/nova models: {', '.join(list_models(region)) or 'none'}")

    runtime = boto3.client("bedrock-runtime", region_name=region, config=Config(retries={"max_attempts": 2}, read_timeout=60))
    for model_id in sys.argv[1:] or ([os.environ["BEDROCK_CHAT_MODEL_ID"]] if os.environ.get("BEDROCK_CHAT_MODEL_ID") else CANDIDATES):
        print(f"\n{model_id}")
        probe(runtime, model_id)


if __name__ == "__main__":
    main()
