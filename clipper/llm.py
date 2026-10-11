"""Provider-agnostic chat client: 'anthropic' or 'openai' (OpenAI-compatible)."""
import json

import requests

from .config import llm_settings

TIMEOUT = 300  # reasoning models are slow; 12000 tokens leaves room to think



def chat(system: str, user: str, max_tokens: int = 12000) -> str:
    s = llm_settings()
    if s["provider"] == "anthropic":
        r = requests.post(
            f"{s['base_url']}/v1/messages", timeout=TIMEOUT,
            headers={"x-api-key": s["api_key"], "anthropic-version": "2023-06-01"},
            json={"model": s["model"], "max_tokens": max_tokens, "system": system,
                  "messages": [{"role": "user", "content": user}]})
        _check(r)
        return "".join(b.get("text", "") for b in r.json()["content"])
    if s["provider"] in ("openai", "openai-compatible"):
        # Stream: reasoning models can think >100s, which gateways cut off (HTTP 524).
        r = requests.post(
            f"{s['base_url']}/chat/completions", timeout=TIMEOUT, stream=True,
            headers={"Authorization": f"Bearer {s['api_key']}"},
            json={"model": s["model"], "max_tokens": max_tokens, "temperature": 0.3,
                  "stream": True, **s["extra_body"],
                  "messages": [{"role": "system", "content": system},
                               {"role": "user", "content": user}]})
        _check(r)
        r.encoding = "utf-8"  # SSE has no charset header; requests would guess Latin-1
        text, finish = [], None
        for line in r.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            choices = json.loads(payload).get("choices") or []
            if choices:
                text.append((choices[0].get("delta") or {}).get("content") or "")
                finish = choices[0].get("finish_reason") or finish
        out = "".join(text)
        if not out and finish == "length":
            raise RuntimeError("LLM hit max_tokens before answering (reasoning model); "
                               "raise max_tokens in llm.chat")
        return out
    raise RuntimeError(f"Unknown LLM_PROVIDER: {s['provider']!r}")


def _check(r: requests.Response) -> None:
    if not r.ok:  # body may echo request details; keep it short, never include headers
        raise RuntimeError(f"LLM HTTP {r.status_code}: {r.text[:300]}")
