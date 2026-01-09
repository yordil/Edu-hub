"""Probe which embedding models Bedrock's OpenAI-compatible endpoint serves: `python -m app.scripts.check_embeddings`."""

import os

from openai import APIError, OpenAI

from app.scripts.check_bedrock import load_env_file

CANDIDATES = ["amazon.titan-embed-text-v2:0", "amazon.titan-embed-text-v2", "cohere.embed-multilingual-v3", "cohere.embed-english-v3", "amazon.nova-2-multimodal-embeddings-v1:0"]


def main() -> None:
    load_env_file()
    key = os.environ.get("OPENAI_API_KEY") or os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
    client = OpenAI(api_key=key, base_url=os.environ.get("OPENAI_BASE_URL"), timeout=30, max_retries=0)
    try:
        ids = sorted(m.id for m in client.models.list().data if "embed" in m.id.lower())
        print("embedding models listed:", ", ".join(ids) or "none")
        candidates = ids or CANDIDATES
    except APIError as e:
        print(f"model listing failed: {type(e).__name__} {getattr(e, 'status_code', '')}")
        candidates = CANDIDATES
    for model in candidates:
        try:
            r = client.embeddings.create(model=model, input=["balancing chemical equations"])
            print(f"OK   {model}: {len(r.data[0].embedding)} dims")
        except APIError as e:
            print(f"FAIL {model}: {getattr(e, 'status_code', '')} {str(e)[:120]}")


if __name__ == "__main__":
    main()
