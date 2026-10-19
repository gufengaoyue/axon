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
        # v0.4.30：旧 summary 一律进 old_part（被新摘要吸收）——它的 turn 是
        # 上次压缩时的 max+1，落在 protected 区会被当"最新消息"永久保留 →
        # 上下文叠加多份 <conversation_summary>（实测 2 次压缩后 2 份）
        old_part = [m for m in cleaned
                    if m.turn <= threshold
                    or str(m.content or "").startswith("<conversation_summary")]
        protected = [m for m in cleaned
                     if m.turn > threshold
                     and not str(m.content or "").startswith("<conversation_summary")]

        # 阶段 4a：孤立 tool 结果（无配对 call）全区丢弃——API 合法性要求
        # （与"保护边界内未配对 call 不清"不矛盾：call 不清是 resume 依赖，
        #   result 无主则是脏数据，任何区域都不可带入上下文）
        call_ids = {tc.id for m in messages if m.role == "assistant"
                    for tc in m.tool_calls}
        old_part = [m for m in old_part
                    if not (m.role == "tool" and m.tool_call_id not in call_ids)]
        protected = [m for m in protected
                     if not (m.role == "tool" and m.tool_call_id not in call_ids)]

        # v0.4.29 P1：跨边界配对一致性——call 在旧区而 result 在保护区（或反之）
        # 时，4b 会丢旧区的 call → 保护区留下无主 result → API 400。这里把
        # 跨界成对的消息移到同一区（call 跟 result 走），保证两区各自配对完整。
        old_part, protected = self._pair_across_boundary(old_part, protected)

        # 阶段 3：摘要被压区间
        summary_text = self._summarize(old_part, summarize)
        # v0.4.30：旧 summary 的内容已被上面的新摘要吸收 → 从输出中移除，
        # 否则 new_messages 里会同时存在新旧两份 <conversation_summary>
        # （实测连续两次压缩叠 2 份 → 上下文膨胀、二次 assemble 再超限）
        old_part = [m for m in old_part
                    if not str(m.content or "").startswith("<conversation_summary")]

        # 阶段 4b：老区未配对 call 清理（保护区不动——resume 断点依赖）
        old_part = self._drop_unpaired_calls(old_part)

        summary_msg = Message(
            role="user",
            content=(f'<conversation_summary turns="1-{max_turn - self.protect_turns}" '
                     f'tokens="{before}">\n{summary_text}\n</conversation_summary>'),
            # v0.4.29 P1：turn 取 max+1 且**不重排**——旧实现把全部消息重排为
            # 1..N，next_turn 回退到 N+1（远小于真实轮次）→ audit 的 _turn 与
            # messages.turn 错位、V 层回合 scope 张冠李戴（v0.4.27 的 turn_start
            # 口径依赖 turn 单调不减）
            turn=max_turn + 1,
        )
        new_messages = [summary_msg] + old_part + protected
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

    @staticmethod
    def _pair_across_boundary(old_part: list[Message],
                              protected: list[Message]) -> tuple[list[Message],
                                                                 list[Message]]:
        """跨边界配对一致性（v0.4.29 P1）：保证两区各自 call/result 配对完整。

        call 在旧区而 result 在保护区（或反之）→ 把成对消息移到同一区
        （call 跟 result 走）。迭代至稳定（一次移动可能引发新的跨界）。
        缺了它：4b 丢旧区的 call 后，保护区留下无主 result → API 400。
        """
        def _calls(msgs):
            return {tc.id for m in msgs if m.role == "assistant"
                    for tc in (m.tool_calls or [])}

        def _results(msgs):
            return {m.tool_call_id for m in msgs if m.role == "tool"}

        for _ in range(4):
            changed = False
            oc, pc = _calls(old_part), _calls(protected)
            orr, prr = _results(old_part), _results(protected)
            # call 在旧区、result 在保护区 → 该 assistant 消息移入保护区
            move_c = (oc & prr) - pc
            if move_c:
                kept = []
                moved = []
                for m in old_part:
                    if m.role == "assistant" and any(
                            tc.id in move_c for tc in (m.tool_calls or [])):
                        moved.append(m)
                        changed = True
                    else:
                        kept.append(m)
                # v0.4.30：按 turn 有序插入（旧实现逐个 insert(0) → 顺序倒置，
                # 且会插到 summary 之前 → 严格端点 400）
                for m in moved:
                    pos = 0
                    while pos < len(protected) and protected[pos].turn < m.turn:
                        pos += 1
                    protected.insert(pos, m)
                old_part = kept
            oc, pc = _calls(old_part), _calls(protected)
            orr, prr = _results(old_part), _results(protected)
            # result 在旧区、call 在保护区 → 该 tool 消息移入保护区
            move_r = (orr & pc) - oc
            if move_r:
                kept = []
                moved = []
                for m in old_part:
                    if m.role == "tool" and m.tool_call_id in move_r:
                        moved.append(m)
                        changed = True
                    else:
                        kept.append(m)
                for m in moved:
                    pos = 0
                    while pos < len(protected) and protected[pos].turn < m.turn:
                        pos += 1
                    protected.insert(pos, m)
                old_part = kept
            if not changed:
                break
        return old_part, protected

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
        from axon.agent.msgmeta import unpack_meta
        goals = [m.content for m in old_part
                 if m.role == "user" and m.content and not m.content.startswith("<")]
        # v0.4.29：扫 tool 消息 meta.path（旧实现读 m.target——Message 无该
        # 属性，files_touched 恒为"无"的死逻辑）
        paths = set()
        for m in old_part:
            if m.role == "tool" and m.content:
                _, meta = unpack_meta(m.content)
                if meta and meta.get("path"):
                    paths.add(str(meta["path"]))
        lines = [f"{f}: 无" for f in _SUMMARY_FIELDS]
        lines[0] = f"goal: {'; '.join((g or '')[:120] for g in goals[-5:]) or '无'}"
        lines[2] = f"progress: 已执行 {sum(1 for m in old_part if m.role == 'tool')} 次工具调用"
        lines[4] = f"files_touched: {', '.join(sorted(paths)[:20]) or '无'}"
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
