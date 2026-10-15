"""OpenAI 兼容 Provider（openai 包，Chat Completions API）。

真实 API：client.chat.completions.create(model=..., messages=..., tools=...,
             stream=True, stream_options={"include_usage": True})
增量 → 累积成完整 ToolCall 后一次性产出（简化循环侧处理）。
"""
from __future__ import annotations

import json
import os
from typing import Iterator

from axon.providers.base import Message, StreamEvent, ToolCall


class OpenAICompatProvider:
    name = "openai"

    def __init__(self, api_key: str, base_url: str = "", timeout: float = 180.0,
                 default_headers: dict | None = None):
        try:
            from openai import OpenAI
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("需要 openai 包：pip install openai") from e
        self._client = OpenAI(
            api_key=api_key,
            base_url=base_url or None,
            timeout=timeout,
            default_headers=default_headers or None,
        )

    @classmethod
    def from_config(cls, cfg) -> "OpenAICompatProvider":
        p = cfg.active_provider_cfg()
        api_key = os.environ.get(p.env_api_key, "")
        if not api_key:
            raise RuntimeError(
                f"环境变量 {p.env_api_key} 未设置（{cfg.model.active_provider} 路需要 API key）")
        return cls(api_key=api_key, base_url=p.base_url, timeout=p.timeout_sec)

    def chat(self, messages: list[Message], tools: list[dict] | None, *,
             model: str, timeout: float = 180.0) -> Iterator[StreamEvent]:
        kwargs: dict = {
            "model": model,
            "messages": [m.to_openai() for m in messages],
            "stream": True,
            "stream_options": {"include_usage": True},
            "timeout": timeout,
        }
        if tools:
            kwargs["tools"] = tools
        stream = self._client.chat.completions.create(**kwargs)

        # tool_call 增量累积
        acc: dict[int, dict] = {}
        order: list[int] = []
        for chunk in stream:
            if not chunk.choices:
                if getattr(chunk, "usage", None):
                    yield StreamEvent(kind="usage", usage={
                        "prompt_tokens": chunk.usage.prompt_tokens or 0,
                        "completion_tokens": chunk.usage.completion_tokens or 0,
                        "prompt_cache_hit_tokens": getattr(
                            chunk.usage, "prompt_cache_hit_tokens", None),
                    })
                continue
            delta = chunk.choices[0].delta
            if delta is None:
                continue
            if getattr(delta, "content", None):
                yield StreamEvent(kind="text", text=delta.content)
            # 推理模型的思考链（DeepSeek-R1 / reasoning 模型，v0.4.11）
            reasoning = (getattr(delta, "reasoning_content", None)
                         or getattr(delta, "reasoning", None))
            if reasoning:
                yield StreamEvent(kind="thinking", text=reasoning)
            for tc in getattr(delta, "tool_calls", None) or []:
                idx = tc.index
                if idx not in acc:
                    acc[idx] = {"id": "", "name": "", "args": ""}
                    order.append(idx)
                if tc.id:
                    acc[idx]["id"] = tc.id
                if tc.function:
                    if tc.function.name:
                        acc[idx]["name"] += tc.function.name
                    if tc.function.arguments:
                        acc[idx]["args"] += tc.function.arguments
        for idx in order:
            a = acc[idx]
            try:
                args = json.loads(a["args"]) if a["args"] else {}
            except json.JSONDecodeError:
                args = {"_raw": a["args"]}
            yield StreamEvent(kind="tool_call", tool_call=ToolCall(
                id=a["id"] or f"call_{idx}", name=a["name"], arguments=args))
        yield StreamEvent(kind="done")
