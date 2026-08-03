"""AtessaAPI: async client for the atessa.top proxy. FROZEN CONTRACT — see PLAN.md.

Proxy quirks (learned from the shell CLIs):
- /images/generations is broken; image gen goes through /responses image_generation tool.
- The proxy cannot fetch remote URLs: vision inputs must be base64 data URLs.
- No /embeddings, /audio, /moderations (405).
- Image tool ignores explicit sizes; aspect is steered via the prompt.
"""
from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
from pathlib import Path
from typing import AsyncIterator
import httpx

from .config import Config


class ApiError(Exception):
    pass


def _err_text(data: dict) -> str | None:
    err = data.get("error")
    if not err:
        return None
    if isinstance(err, dict):
        return err.get("message") or json.dumps(err)
    return str(err)


MAX_API_BYTES = 10 * 1024 * 1024
MAX_ERROR_BYTES = 64 * 1024


async def _read_limited(response: httpx.Response, limit: int) -> bytes:
    declared = response.headers.get("content-length")
    if declared:
        try:
            if int(declared) > limit:
                raise ApiError(f"response body too large ({declared} bytes; limit {limit})")
        except ValueError:
            pass
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) > limit:
            raise ApiError(f"response body exceeded {limit} bytes")
    return bytes(body)


class AtessaAPI:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.model_context: dict[str, int | None] = {}
        self._client = httpx.AsyncClient(
            base_url=cfg.base_url,
            headers={"Authorization": f"Bearer {cfg.api_key}"},
            timeout=httpx.Timeout(180.0, connect=15.0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _post(self, path: str, body: dict) -> dict:
        try:
            async with self._client.stream("POST", path, json=body) as response:
                status_code = response.status_code
                content = await _read_limited(response, MAX_API_BYTES)
        except httpx.HTTPError as error:
            raise ApiError(f"network error: {error}") from error
        try:
            data = json.loads(content.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as error:
            preview = content.decode("utf-8", "replace")[:300]
            raise ApiError(f"HTTP {status_code}: {preview}") from error
        if not isinstance(data, dict):
            raise ApiError(f"HTTP {status_code}: unexpected response envelope")
        if message := _err_text(data):
            raise ApiError(message)
        if status_code >= 400:
            raise ApiError(f"HTTP {status_code}: {json.dumps(data)[:300]}")
        return data
    async def models(self) -> list[str]:
        try:
            async with self._client.stream("GET", "/models") as response:
                status_code = response.status_code
                content = await _read_limited(response, MAX_API_BYTES)
        except httpx.HTTPError as error:
            raise ApiError(f"models: network error: {error}") from error
        try:
            data = json.loads(content.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as error:
            preview = content.decode("utf-8", "replace")[:300]
            raise ApiError(f"models: HTTP {status_code}: {preview}") from error
        if not isinstance(data, dict):
            raise ApiError(f"models: HTTP {status_code}: unexpected response envelope")
        if message := _err_text(data):
            raise ApiError(f"models: {message}")
        if status_code >= 400:
            raise ApiError(f"models: HTTP {status_code}: {json.dumps(data)[:300]}")
        rows = data.get("data")
        if not isinstance(rows, list):
            raise ApiError(f"models: unexpected response: {json.dumps(data)[:300]}")
        self.model_context = {
            row["id"]: row.get("context_length")
            for row in rows
            if isinstance(row, dict) and row.get("id")
        }
        return sorted(row["id"] for row in rows if isinstance(row, dict) and row.get("id"))

    async def chat(self, messages: list[dict], model: str, max_tokens: int = 2048) -> str:
        data = await self._post(
            "/chat/completions",
            {"model": model, "max_tokens": max_tokens, "messages": messages},
        )
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise ApiError(f"unexpected response: {json.dumps(data)[:300]}") from error
        if isinstance(content, list):
            content = "".join(
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and isinstance(block.get("text", ""), str)
            )
        if not isinstance(content, str) or not content.strip():
            raise ApiError(f"empty response: {json.dumps(data)[:300]}")
        return content

    async def chat_stream(
        self, messages: list[dict], model: str, max_tokens: int = 2048
    ) -> AsyncIterator[str]:
        body = {"model": model, "max_tokens": max_tokens, "stream": True, "messages": messages}
        try:
            async with self._client.stream("POST", "/chat/completions", json=body) as response:
                if response.status_code >= 400:
                    content = await _read_limited(response, MAX_ERROR_BYTES)
                    text = content.decode("utf-8", "replace")[:300]
                    raise ApiError(f"HTTP {response.status_code}: {text}")
                buffer = bytearray()
                total = 0
                complete = False
                async for chunk_bytes in response.aiter_bytes():
                    total += len(chunk_bytes)
                    if total > MAX_API_BYTES:
                        raise ApiError(f"stream exceeded {MAX_API_BYTES} bytes")
                    buffer.extend(chunk_bytes)
                    while b"\n" in buffer:
                        raw_line, _, remainder = buffer.partition(b"\n")
                        buffer = bytearray(remainder)
                        line = raw_line.rstrip(b"\r").decode("utf-8", "replace")
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            complete = True
                            break
                        try:
                            event = json.loads(payload)
                        except ValueError as error:
                            raise ApiError(f"malformed stream event: {payload[:200]}") from error
                        if not isinstance(event, dict):
                            raise ApiError("malformed stream event envelope")
                        if message := _err_text(event):
                            raise ApiError(message)
                        choices = event.get("choices")
                        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                            delta = choices[0].get("delta")
                            if isinstance(delta, dict) and isinstance(delta.get("content"), str):
                                if delta["content"]:
                                    yield delta["content"]
                    if complete:
                        break
                if not complete:
                    raise ApiError("incomplete streaming response (missing [DONE])")
        except httpx.HTTPError as error:
            raise ApiError(f"network error: {error}") from error

    async def web_search(self, query: str, max_results: int = 5) -> str:
        data = await self._post(
            "/tools/web-search", {"query": query, "max_results": max_results}
        )
        output = [
            content.get("text", "")
            for item in data.get("output", [])
            if isinstance(item, dict) and item.get("type") == "message"
            for content in item.get("content", [])
            if isinstance(content, dict) and content.get("type") == "output_text"
        ]
        answer = "\n".join(output).strip()
        if not answer:
            raise ApiError(f"web search returned no answer: {json.dumps(data)[:300]}")
        return answer

    async def image_gen(self, prompt: str, quality: str = "high", model: str | None = None) -> bytes:
        data = await self._post(
            "/responses",
            {
                "model": model or self.cfg.model_for("image"),
                "input": prompt,
                "tools": [{"type": "image_generation", "quality": quality}],
            },
        )
        for it in data.get("output", []):
            if isinstance(it, dict) and it.get("type") == "image_generation_call" and it.get("result"):
                return base64.b64decode(it["result"])
        raise ApiError(f"no image in response: {json.dumps(data)[:200]}")

    async def vision(
        self, image_path: str, prompt: str, model: str, max_tokens: int = 2048
    ) -> str:
        def _read_and_encode():
            path = Path(image_path)
            if not path.is_file():
                raise ApiError(f"image file not found: {image_path}")
            size = path.stat().st_size
            if size > 20 * 1024 * 1024:
                raise ApiError(f"image file too large ({size / (1024*1024):.1f}MB, max 20MB)")
            mt = mimetypes.guess_type(image_path)[0] or "image/png"
            with open(image_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
            return mt, b64

        mt, b64 = await asyncio.to_thread(_read_and_encode)
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{mt};base64,{b64}"}},
                ],
            }
        ]
        return await self.chat(messages, model=model, max_tokens=max_tokens)
    async def ping_model(self, model: str, timeout: float = 3.5) -> dict:
        messages = [{"role": "user", "content": "hi"}]
        start = asyncio.get_event_loop().time()
        try:
            url = f"{self.cfg.base_url.rstrip('/')}/v1/chat/completions"
            headers = {"Authorization": f"Bearer {self.cfg.api_key}", "Content-Type": "application/json"}
            payload = {"model": model, "messages": messages, "max_tokens": 1}
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
                res = await client.post(url, headers=headers, json=payload)
                elapsed = round((asyncio.get_event_loop().time() - start) * 1000)
                if res.status_code == 200:
                    return {"model": model, "status": "ONLINE", "latency_ms": elapsed, "error": None}
                return {"model": model, "status": "UNAVAILABLE", "latency_ms": elapsed, "error": f"HTTP {res.status_code}"}
        except httpx.TimeoutException:
            elapsed = round((asyncio.get_event_loop().time() - start) * 1000)
            return {"model": model, "status": "TIMEOUT", "latency_ms": elapsed, "error": f"> {timeout}s"}
        except Exception as e:
            elapsed = round((asyncio.get_event_loop().time() - start) * 1000)
            return {"model": model, "status": "UNAVAILABLE", "latency_ms": elapsed, "error": str(e)[:60]}

    async def ping_all_models(self, models: list[str], max_concurrency: int = 10) -> list[dict]:
        sem = asyncio.Semaphore(max_concurrency)

        async def _probe(m: str) -> dict:
            async with sem:
                return await self.ping_model(m)

        return await asyncio.gather(*[_probe(m) for m in models])
