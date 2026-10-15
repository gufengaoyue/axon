"""C 层：上下文组装（设计 §4.3）。

每轮从 DB 重放重组装：
  [system(固定模板, 无时间戳)] + [memory_block(≤预算)] + [history(只追加)]
  工具定义经 registry 固定序序列化（separate 参数，不进消息体）
双水位：≥force_ratio 抛 CompactionNeeded；≥warn_ratio 由 loop 调 cleanup() 清理旧工具输出。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from axon.agent.tokenizer import estimate_messages_tokens, estimate_tokens
from axon.prompts.system import build_system_prompt
from axon.providers.base import Message

_PROTECT_RECENT_MESSAGES = 12   # 约最近 6 回合原文不动


class CompactionNeeded(Exception):
    def __init__(self, tokens_est: int, window: int):
        self.tokens_est = tokens_est
        self.window = window
        super().__init__(f"上下文占用 {tokens_est}/{window}（≥90%），需要强制压缩")


@dataclass
class AssembledContext:
    messages: list[Message]
    tools: list[dict]
    tokens_est: int
    prefix_tokens: int          # 稳定前缀大小（观测命中率用）


class ContextBuilder:
    def __init__(self, config, memory_store, registry):
        self.config = config
        self.memory_store = memory_store
        self.registry = registry
        self._system_prompt = build_system_prompt(config)

    def assemble(self, session, history: list[Message]) -> AssembledContext:
        from axon.agent.msgmeta import unpack_meta
        messages: list[Message] = [Message(role="system", content=self._system_prompt)]
        # v0.4.21：**长期记忆（跨项目）先于项目记忆注入**（用户要求的载入顺序）
        from axon.memory.global_store import GlobalMemoryStore
        try:
            glob_mem = GlobalMemoryStore(self.config.memory.long_term_path()).render_block()
        except Exception:                       # noqa: BLE001 - 记忆缺失不影响主流程
            glob_mem = ""
        mem = self.memory_store.render_block()
        if mem:
            messages.append(Message(role="system", content=mem))
        for m in history:
            if m.role == "tool":
                clean, _ = unpack_meta(m.content)
                messages.append(Message(role="tool", content=clean,
                                        tool_call_id=m.tool_call_id, name=m.name,
                                        turn=m.turn))
            else:
                messages.append(m)

        tokens_est = estimate_messages_tokens(messages)
        window = self.config.model.context_window
        if tokens_est >= self.config.context.compact_force_ratio * window:
            raise CompactionNeeded(tokens_est, window)

        tools = self.registry.openai_tools()
        return AssembledContext(
            messages=messages,
            tools=tools,
            tokens_est=tokens_est,
            prefix_tokens=estimate_tokens(self._system_prompt) + estimate_tokens(mem),
        )

    def cleanup(self, history: list[Message]) -> tuple[list[Message], int]:
        """80% 水位清理：丢弃最旧工具输出原文（留占位），保护最近回合。"""
        if len(history) <= _PROTECT_RECENT_MESSAGES:
            return history, 0
        cut = len(history) - _PROTECT_RECENT_MESSAGES
        out: list[Message] = []
        n_elided = 0
        for i, m in enumerate(history):
            if i < cut and m.role == "tool" and m.content and not m.content.startswith(
                    "[tool output elided"):
                out.append(Message(role="tool", content=f"[tool output elided, turn {m.turn}]",
                                   tool_call_id=m.tool_call_id, name=m.name, turn=m.turn))
                n_elided += 1
            else:
                out.append(m)
        return out, n_elided
