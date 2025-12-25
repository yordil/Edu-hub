import json

import boto3

from app.scripts.check_bedrock import load_env_file

load_env_file()
client = boto3.client("bedrock-runtime", region_name="us-east-1")
try:
    r = client.invoke_model(modelId="amazon.titan-embed-text-v2:0", body=json.dumps({"inputText": "hello", "dimensions": 512}))
    v = json.loads(r["body"].read())["embedding"]
    print(f"TITAN OK: {len(v)} dims, first values {v[:3]}")
except Exception as e:
    print(f"TITAN FAIL: {type(e).__name__}: {e}")
