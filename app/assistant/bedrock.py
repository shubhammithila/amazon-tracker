"""The only caller of Amazon Bedrock: the Converse API, with tools, over plain httpx.

Auth is a Bedrock API key sent as `Authorization: Bearer` (`AWS_BEARER_TOKEN_BEDROCK`). No SigV4,
so no boto3: installing packages on the t2.micro is what once OOM-killed a deploy.

Measured against the account on 10 Oct 2026: `us.anthropic.claude-sonnet-5-5` answers a tool call
in ~2 s; a `cachePoint` after the system prompt and tools is honoured (2,803 tokens written once,
then read from cache); and `thinking: {"type": "disabled"}` is REFUSED on this model, which asks for
`{"type": "between_tools"}` instead.
"""
from __future__ import annotations

import httpx

from app.config import get_settings

TIMEOUT = 60.0


class BedrockError(RuntimeError):
    """Bedrock refused or failed. The message is Amazon's own, for the log and the screen."""


async def converse(*, system: list, messages: list, tools: list, max_tokens: int = 1200,
                   client: httpx.AsyncClient | None = None) -> dict:
    """One Converse round. Returns Bedrock's JSON (`output.message`, `stopReason`, `usage`)."""
    settings = get_settings()
    url = (f"https://bedrock-runtime.{settings.bedrock_region}.amazonaws.com"
           f"/model/{settings.assistant_model}/converse")
    body = {
        "system": system,
        "messages": messages,
        "toolConfig": {"tools": tools},
        "inferenceConfig": {"maxTokens": max_tokens},
        "additionalModelRequestFields": {"thinking": {"type": "between_tools"}},
    }
    headers = {"Authorization": f"Bearer {settings.bedrock_api_key}"}
    own = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT)
    try:
        r = await client.post(url, json=body, headers=headers)
    except httpx.HTTPError as exc:
        raise BedrockError(f"Could not reach Bedrock: {exc}") from exc
    finally:
        if own:
            await client.aclose()
    if r.status_code != 200:
        try:
            msg = r.json().get("message") or r.text
        except ValueError:
            msg = r.text
        raise BedrockError(f"Bedrock answered {r.status_code}: {msg[:300]}")
    return r.json()
