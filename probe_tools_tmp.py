"""One-shot diagnostic: what does Muse Spark return for a tools request?"""
import json
import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()
api_key = os.getenv("MODEL_API_KEY", "").strip()
if not api_key:
    print("NO_API_KEY")
    sys.exit(2)

auth = api_key if api_key.lower().startswith("bearer ") else "Bearer " + api_key
payload = {
    "model": os.getenv("MUSE_CHAT_MODEL", "muse-spark-1.3").strip() or "muse-spark-1.3",
    "messages": [
        {"role": "user", "content": "I want to send an email."},
    ],
    "stream": False,
    "max_completion_tokens": 256,
    "tools": [
        {
            "type": "function",
            "function": {
                "name": "send_email",
                "description": "Send a plain-text email.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "to": {"type": "array", "items": {"type": "string"}},
                        "subject": {"type": "string"},
                        "body": {"type": "string"},
                    },
                },
            },
        }
    ],
    "tool_choice": "auto",
}
try:
    response = httpx.post(
        "https://api.meta.ai/v1/chat/completions",
        headers={"Authorization": auth, "Content-Type": "application/json"},
        json=payload,
        timeout=60,
    )
except Exception as exc:
    print(f"TRANSPORT_ERROR: {type(exc).__name__}: {exc}")
    sys.exit(3)
print(f"STATUS: {response.status_code}")
try:
    body = response.json()
except ValueError:
    print("NON_JSON_BODY:")
    print(response.text[:1000])
    sys.exit(0)
text = json.dumps(body)
print(f"BODY_LEN: {len(text)}")
print(text[:2000])
