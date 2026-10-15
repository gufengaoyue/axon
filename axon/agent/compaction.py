"""C 层：90% 强制压缩四阶段（设计 §4.3）。

阶段：
  1. 清旧工具输出（最旧一半 → 占位）
  2. 定保护边界（最近 K 回合原文不动）
  3. 结构化摘要（主模型一次调用，9 字段；不可用时退确定性摘要）
  4. 清孤立工具对（保护边界内的未配对 call 不清——resume 断点依赖，见 session/resume.py）

压缩 = 前缀重写事件：before/after tokens 记 compaction_events（失效账本）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from axon.agent.tokenizer import estimate_messages_tokens, estimate_tokens
from axon.providers.base import Message
from axon.sandbox.hooks import CompactionEvent

_SUMMARY_FIELDS = ["goal", "constraints", "progress", "decisions", "files_touched",
                   "errors_and_fixes", "open_questions", "user_preferences", "next_steps"]

_SUMMARY_PROMPT = """把以下对话历史压缩成结构化摘要。严格要求：只输出下面 9 个字段，每行一个，格式为 "字段名: 内容"；没有内容的字段写 "无"。不要输出其他任何文字。

字段名依次为：{fields}

对话历史：
{transcript}
"""


@dataclass
class CompactResult:
    messages: list[Message]
    before_tokens: int
    after_tokens: int
    summary: str
    trigger: str = "force"
    elided: int = 0


class Compactor:
    def __init__(self, config):
        self.config = config
        self.protect_turns = 6

    # ---- 对外 ----
    def compact(self, session_id: str, messages: list[Message], summarize=None,
                store=None) -> CompactResult:
        before = estimate_messages_tokens(messages)
        max_turn = max((m.turn for m in messages), default=0)

        # 阶段 1+2：清理最旧工具输出，保护最近 K 回合
        cleaned, elided = self._elide_old_tool_outputs(messages, max_turn)
        threshold = max_turn - self.protect_turns
        old_part = [m for m in cleaned if m.turn <= threshold]
        protected = [m for m in cleaned if m.turn > threshold]

        # 阶段 4a：孤立 tool 结果（无配对 call）全区丢弃——API 合法性要求
        # （与"保护边界内未配对 call 不清"不矛盾：call 不清是 resume 依赖，
        #   result 无主则是脏数据，任何区域都不可带入上下文）
        call_ids = {tc.id for m in messages if m.role == "assistant"
                    for tc in m.tool_calls}
        old_part = [m for m in old_part
                    if not (m.role == "tool" and m.tool_call_id not in call_ids)]
        protected = [m for m in protected
                     if not (m.role == "tool" and m.tool_call_id not in call_ids)]

        # 阶段 3：摘要被压区间
        summary_text = self._summarize(old_part, summarize)

        # 阶段 4b：老区未配对 call 清理（保护区不动——resume 断点依赖）
        old_part = self._drop_unpaired_calls(old_part)

        summary_msg = Message(
            role="user",
            content=(f'<conversation_summary turns="1-{max_turn - self.protect_turns}" '
                     f'tokens="{before}">\n{summary_text}\n</conversation_summary>'),
            turn=max_turn - self.protect_turns,
        )
        new_messages = [summary_msg] + old_part + protected
        # turn 重排（保持单调）
        for i, m in enumerate(new_messages):
            m.turn = i + 1
        after = estimate_messages_tokens(new_messages)

        if store is not None:
            store.compact_history(session_id, new_messages, CompactionEvent(
                session_id=session_id, at_turn=max_turn, trigger="force",
                before_tokens=before, after_tokens=after, summary=summary_text))

        return CompactResult(messages=new_messages, before_tokens=before,
                             after_tokens=after, summary=summary_text, elided=elided)

    # ---- 阶段实现 ----
    def _elide_old_tool_outputs(self, messages: list[Message],
                                max_turn: int) -> tuple[list[Message], int]:
        threshold = max_turn - self.protect_turns
        out: list[Message] = []
        n = 0
        for m in messages:
            if m.turn <= threshold and m.role == "tool" and m.content \
                    and not m.content.startswith("[tool output elided"):
                out.append(Message(role="tool",
                                   content=f"[tool output elided, turn {m.turn}]",
                                   tool_call_id=m.tool_call_id, name=m.name, turn=m.turn))
                n += 1
            else:
                out.append(m)
        return out, n

    def _drop_unpaired_calls(self, messages: list[Message]) -> list[Message]:
        """老区清理：assistant 的 tool_call 无配对 result → 丢掉该 call；
        整条消息只剩未配对 call 且无文本 → 整条丢弃。"""
        answered = {m.tool_call_id for m in messages if m.role == "tool"}
        out: list[Message] = []
        for m in messages:
            if m.role == "assistant" and m.tool_calls:
                kept = [tc for tc in m.tool_calls if tc.id in answered]
                if not kept and not (m.content or "").strip():
                    continue
                m = Message(role="assistant", content=m.content, tool_calls=kept,
                            turn=m.turn)
            out.append(m)
        return out

    def _summarize(self, old_part: list[Message], summarize) -> str:
        transcript = self._render(old_part)
        if summarize is not None:
            try:
                prompt = _SUMMARY_PROMPT.format(fields=" / ".join(_SUMMARY_FIELDS),
                                                transcript=transcript[:60000])
                text = summarize(prompt).strip()
                if text:
                    return text[:8000]
            except Exception:
                pass  # 摘要失败 → 退确定性摘要（不阻塞循环）
        return self._fallback_summary(old_part)

    def _fallback_summary(self, old_part: list[Message]) -> str:
        goals = [m.content for m in old_part
                 if m.role == "user" and m.content and not m.content.startswith("<")]
        paths = sorted({m.target for m in old_part if getattr(m, "target", None)})
        lines = [f"{f}: 无" for f in _SUMMARY_FIELDS]
        lines[0] = f"goal: {'; '.join((g or '')[:120] for g in goals[-5:]) or '无'}"
        lines[2] = f"progress: 已执行 {sum(1 for m in old_part if m.role == 'tool')} 次工具调用"
        lines[4] = f"files_touched: {', '.join(paths[:20]) or '无'}"
        lines[8] = "next_steps: （确定性回退摘要，细节以近期对话为准）"
        return "\n".join(lines)

    @staticmethod
    def _render(messages: list[Message]) -> str:
        lines = []
        for m in messages:
            if m.role == "tool":
                lines.append(f"[tool {m.name}] {(m.content or '')[:400]}")
            elif m.role == "assistant":
                if m.tool_calls:
                    lines.append("[assistant 调用] " +
                                 ", ".join(f"{tc.name}({tc.arguments})" for tc in m.tool_calls))
                if m.content:
                    lines.append(f"[assistant] {m.content[:400]}")
            else:
                lines.append(f"[user] {(m.content or '')[:400]}")
        return "\n".join(lines)
