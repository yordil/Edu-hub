"""Check the Bedrock OpenAI-compatible endpoint: `python -m app.scripts.check_llm [model]`.

Streams a short reply, then verifies tool calling (the tutor agent depends on it). Secrets are read
from the environment or ./.env and never printed.
"""

import json
import os
import sys
import time

from openai import APIError, OpenAI

from app.scripts.check_bedrock import load_env_file

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weak_topics",
            "description": "Return the student's weakest topics from their mastery data.",
            "parameters": {
                "type": "object",
                "properties": {"limit": {"type": "integer", "description": "How many topics to return"}},
                "required": ["limit"],
            },
        },
    }
]


def describe_key(name: str) -> None:
    """Print the key's shape (prefix, length, stray characters) without revealing it."""
    value = os.environ.get(name)
    if value is None:
        return
    print(
        f"{name}: prefix={value[:4]!r} length={len(value)} "
        f"whitespace={any(c.isspace() for c in value)} quotes={any(c in value for c in '\"\'')}"
    )


def make_client() -> OpenAI:
    load_env_file()
    for name in ("OPENAI_API_KEY", "AWS_BEARER_TOKEN_BEDROCK"):
        describe_key(name)
    # A Bedrock API key works as the bearer token for the OpenAI-compatible endpoint.
    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
    if not api_key:
        raise SystemExit("Set OPENAI_API_KEY or AWS_BEARER_TOKEN_BEDROCK in .env")
    base_url = os.environ.get("OPENAI_BASE_URL")
    if not base_url:
        raise SystemExit("Set OPENAI_BASE_URL in .env")
    return OpenAI(api_key=api_key, base_url=base_url, timeout=60, max_retries=1)


def stream_check(client: OpenAI, model: str) -> None:
    print(f"\n[stream] {model}")
    t0 = time.perf_counter()
    first = None
    stream = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "You are a friendly tutor for high-school students. Be concise."},
            {"role": "user", "content": "In two sentences, explain what a mole is in chemistry."},
        ],
        stream=True,
        max_tokens=400,
    )
    for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta.content or ""
        if delta and first is None:
            first = time.perf_counter() - t0
        print(delta, end="", flush=True)
    total = time.perf_counter() - t0
    print(f"\n  -> first token {first * 1000:.0f} ms, total {total * 1000:.0f} ms" if first else "\n  -> no text received")


def tool_check(client: OpenAI, model: str) -> None:
    print(f"\n[tools] {model}")
    r = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "What are my 3 weakest topics? Use the tool."}],
        tools=TOOLS,
        max_tokens=400,
    )
    msg = r.choices[0].message
    if msg.tool_calls:
        call = msg.tool_calls[0]
        print(f"  OK  model called {call.function.name}({json.loads(call.function.arguments or '{}')})")
    else:
        print(f"  NO  model answered without calling the tool: {(msg.content or '')[:120]!r}")
    if r.usage:
        print(f"  tokens in/out {r.usage.prompt_tokens}/{r.usage.completion_tokens}")


def main() -> None:
    client = make_client()
    model = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("LLM_CHAT_MODEL", "openai.gpt-oss-120b")
    print(f"Endpoint: {client.base_url} | model: {model}")
    try:
        stream_check(client, model)
        tool_check(client, model)
    except APIError as e:
        raise SystemExit(f"\nFAILED {type(e).__name__} (status {getattr(e, 'status_code', '?')}): {str(e)[:300]}")


if __name__ == "__main__":
    main()
