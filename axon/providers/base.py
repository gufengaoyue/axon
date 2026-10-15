"""Provider 抽象：Message / ToolCall / StreamEvent 归一化 + LLMProvider 协议（设计 §8.1）。

两条接入路归一到同一消息格式（OpenAI Chat Completions 形态）：
  - openai_compat：openai 包，任意兼容端点
  - codebuddy：codebuddy_agent_sdk（懒加载适配；导出面以实现时 dir() 核对为准）
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Iterator, Literal, Protocol


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict          # 已解析 JSON


@dataclass
class Message:
    role: Literal["system", "user", "assistant", "tool"]
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)   # assistant 消息
    tool_call_id: str | None = None                            # tool 消息
    name: str | None = None
    turn: int = 0
    tokens_est: int = 0
    images: list[str] = field(default_factory=list)   # 图片附件路径（v0.4.6 多模态）

    # ---- OpenAI 格式转换 ----
    def to_openai(self) -> dict:
        msg: dict = {"role": self.role}
        if self.images and self.role == "user":
            # 多模态：文本 + image_url（data URI）——模型直接"看见"图片
            from axon import vision as _vision
            blocks: list[dict] = [{"type": "text", "text": self.content or ""}]
            for path in self.images:
                url = _vision.to_data_url(path)
                if url:
                    blocks.append({"type": "image_url",
                                   "image_url": {"url": url}})
            if len(blocks) > 1:
                msg["content"] = blocks
            elif self.content is not None:
                msg["content"] = self.content
        elif self.content is not None:
            msg["content"] = self.content
        if self.tool_calls:
            msg["tool_calls"] = [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.name,
                              "arguments": json.dumps(tc.arguments, ensure_ascii=False)}}
                for tc in self.tool_calls
            ]
        if self.tool_call_id is not None:
            msg["tool_call_id"] = self.tool_call_id
        if self.name is not None:
            msg["name"] = self.name
        return msg

    @staticmethod
    def from_openai(d: dict) -> "Message":
        tcs = []
        for tc in d.get("tool_calls") or []:
            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
            except (json.JSONDecodeError, KeyError):
                args = {}
            tcs.append(ToolCall(id=tc.get("id", ""), name=tc["function"]["name"], arguments=args))
        return Message(role=d["role"], content=d.get("content"), tool_calls=tcs,
                       tool_call_id=d.get("tool_call_id"), name=d.get("name"))


@dataclass
class StreamEvent:
    kind: Literal["text", "thinking", "tool_call", "usage", "done"]
    text: str = ""                       # kind=text/thinking 的增量
    tool_call: ToolCall | None = None    # kind=tool_call 的完整调用
    usage: dict = field(default_factory=dict)


class LLMProvider(Protocol):
    name: str

    def chat(self, messages: list[Message], tools: list[dict] | None, *,
             model: str, timeout: float = 180.0) -> Iterator[StreamEvent]:
        """流式对话；产出 text 增量 + 完整 tool_call + usage + done。"""
        ...
