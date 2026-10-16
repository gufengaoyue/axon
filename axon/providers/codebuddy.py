"""codebuddy 接入路（v0.1.1 重写）：SDK 作为执行引擎的适配器。

2026-10-13 实地勘察结论（系统 Python: codebuddy_agent_sdk 0.3.274）：
- SDK 是 Claude-Agent-SDK 形态的**完整 agent 运行时**：query() 异步生成器 +
  SubprocessTransport 拉起包内置 codebuddy-headless CLI
  （--input/--output-format=stream-json 控制协议）
- 它不接收消息列表、自行管理对话与上下文 → 不适配 LLMProvider（openai 路）
  的 chat() 协议；改以 CodebuddyRunner.run_turn() 直驱，
  AgentLoop 检测到 provider 有 run_turn 即整体委派（设计 §16 偏差 #8）
- 工具经 create_sdk_mcp_server 进程内注册（@tool 装饰器）；
  **权限唯一闸门 = can_use_tool 回调** → SandboxGateway.check + TTYPrompter，
  MCP handler 只执行不自查（与 openai 路同一网关语义）
- 已知坑（本机实测）：CLI 默认在 57521 起自有控制服务器，与本机已运行的
  CodeBuddy IDE 服务冲突 → "Failed to start server. Is port 57521 in use?"。
  修法：extra_args 传 --port；默认自动挑选空闲端口
  （config: model.providers.codebuddy.cli_port，0=自动）
- 会话连续性：ResultMessage.session_id 落库，次轮 options.resume 续接
  （SDK 自管上下文/压缩；我们的 C 层压缩在该路径不适用，见设计 §16）
"""
from __future__ import annotations

import os
import socket
import uuid
from typing import Callable

from axon import __version__
from axon.agent.loop import TurnResult
from axon.agent.msgmeta import pack_tool_content
from axon.prompts.system import build_system_prompt
from axon.providers.base import Message, StreamEvent, ToolCall
from axon.sandbox.gateway import Action, action_fingerprint

MCP_SERVER_NAME = "axon"
_TOOL_PREFIX = f"mcp__{MCP_SERVER_NAME}__"


class CodebuddyUnavailable(RuntimeError):
    pass


# ---------------------------------------------------------------- 纯映射（可无 SDK 单测）

def _unwrap_event(event):
    """SDK 可能把 SSE 事件包一层：{"type":"stream_event","event":{...}} → 内层。"""
    if isinstance(event, dict) and event.get("type") == "stream_event":
        inner = event.get("event")
        if isinstance(inner, dict):
            return inner
    return event


def _partial_text(event) -> str:
    """从原始 SSE 事件字典提取文本增量（容忍多种形态）。"""
    if not isinstance(event, dict):
        return ""
    if event.get("type") == "content_block_delta":
        delta = event.get("delta") or {}
        if isinstance(delta, dict) and delta.get("type") == "text_delta":
            return delta.get("text") or ""
    return ""


def _partial_thinking(event) -> str:
    """从原始 SSE 事件字典提取**思考增量**（v0.4.11 流式化）。

    Anthropic SSE 形态：`{"type":"content_block_delta",
    "delta":{"type":"thinking_delta","thinking":"…"}}`。
    """
    if not isinstance(event, dict):
        return ""
    if event.get("type") == "content_block_delta":
        delta = event.get("delta") or {}
        if isinstance(delta, dict) and delta.get("type") == "thinking_delta":
            return delta.get("thinking") or ""
    return ""


def _dedup_tail_overlap(emitted: str, new_text: str, window: int = 2000,
                        min_overlap: int = 12) -> str:
    """重放去重（v0.4.5 尾部重叠 + v0.4.16 强探测）。

    场景一（v0.4.5）：SDK 在多轮 assistant 消息之间重发"叙述前缀"，页面出现
    `…Anthropic 的文章成功返回…` 整段重复（尾窗对齐可捕获）。
    场景二（v0.4.16，client 模式实测）：**从任意位置开始的整段重放**——
    SDK 的 continuous-read API（CodeBuddySDKClient）会消费跨回合事件，
    一次重放可能远超尾窗长度（用户实测：整页正文 + 完成自检重来一遍）。
    → 增加 rfind 探测：取新文本开头 80 字符在已发文本中**最后一次**出现的位置，
    向前逐字符匹配；连续重复 ≥40 字符即整体截掉。

    两段都跑，返回截掉更多的那份。阈值（12 / 40 字符）用于避免误杀模型
    正常的短复述。
    """
    if not emitted or not new_text:
        return new_text
    best = new_text

    # ---- 尾窗对齐（场景一）----
    tail = emitted[-window:]
    max_n = min(len(tail), len(new_text))
    for n in range(max_n, min_overlap - 1, -1):
        if tail[-n:] == new_text[:n]:
            if len(new_text) - n < len(best):
                best = new_text[n:]
            break

    # ---- rfind 探测（场景二：任意位置起的整段重放）----
    probe_len = 80 if len(new_text) >= 80 else len(new_text)
    if probe_len >= min_overlap:
        probe = new_text[:probe_len]
        idx = emitted.rfind(probe)
        if idx >= 0:
            overlap = 0
            limit = min(len(new_text), len(emitted) - idx)
            while overlap < limit and emitted[idx + overlap] == new_text[overlap]:
                overlap += 1
            if overlap >= 40 and len(new_text) - overlap < len(best):
                best = new_text[overlap:]
    return best


def map_sdk_message(msg, state: dict) -> list[StreamEvent]:
    """SDK Message → 内部 StreamEvent（纯函数；state 跨消息累积部分文本）。

    state: {"partial": str, "seen_partial": bool, "emitted": str, "last_full": str}

    **防重复（v0.2.3，用户实测"这些话总是重复"）**——两条独立防线：
    1. 累积型 partial 事件（每个事件的 text 是"到目前为全量"而非增量）：
       若 text 以已累积的 partial 开头 → 只取增量部分发出。
    2. 完整 AssistantMessage 与已发内容重合（重复到达 / partial 已覆盖全文）：
       只发尚未发过的部分；完全重合则不发。
    3. 流被整体重放（同一段文本再走一遍 partial 序列，用户实测场景）：
       较长 chunk 若已出现在最近已发窗口（400 字符）内 → 抑制。
    """
    t = type(msg).__name__
    out: list[StreamEvent] = []
    if t == "StreamEvent":
        # 思考增量流（v0.4.11：与文本增量同一条 SSE 通道，逐字播出）
        think_now = _partial_thinking(_unwrap_event(getattr(msg, "event", None)))
        if think_now:
            prev_t = state.get("thinking_stream", "")
            inc = think_now[len(prev_t):] if think_now.startswith(prev_t) else think_now
            state["thinking_stream"] = think_now
            if inc:
                # v0.4.21：**全局去重**——SES 在回合/中断边界会**重放同一段思考流**
                # （用户实测：两个完全相同的深度思考框）。局部 thinking_stream 在
                # 消息边界被重置抓不到，故与已发全文（thinking_emitted）做
                # 尾部重叠 + rfind 强探测（复用正文的 _dedup_tail_overlap）。
                inc = _dedup_tail_overlap(state.get("thinking_emitted", ""), inc)
            if inc:
                state["thinking_emitted"] = state.get("thinking_emitted", "") + inc
                out.append(StreamEvent(kind="thinking", text=inc))
        text = _partial_text(_unwrap_event(getattr(msg, "event", None)))
        if text:
            prev = state.get("partial", "")
            if prev and text.startswith(prev):
                new = text[len(prev):]          # 累积型 → 仅增量
            elif prev and text in prev:
                new = ""                        # 重复片段
            else:
                new = text
            state["partial"] = prev + new
            acc = state["partial"]
            # 防线 3：流被重放（消息边界后 partial 归零，重放序列逐字重现上一条
            # 消息的文本）——累积片仍是"上一条消息文本"的前缀 ⇒ 抑制；
            # 一旦分叉说明是本条消息的正当内容 → 整体补发（含已抑制部分）。
            replay_src = state.get("replay_src", "")
            if not prev and state.get("prev_stream"):
                replay_src = state["prev_stream"]
                state["replay_src"] = replay_src
            if replay_src:
                if replay_src.startswith(acc):
                    new = ""                     # 仍在重放 → 不发
                else:
                    state["replay_src"] = ""     # 分叉 → 结束重放
                    new = acc                    # 补发整段（此前被抑制的内容在内）
            if new:
                emitted_before = state.get("emitted", "")
                new = _dedup_tail_overlap(emitted_before, new)
                if new:
                    state["seen_partial"] = True
                    state["emitted"] = emitted_before + new
                    out.append(StreamEvent(kind="text", text=new))
                else:
                    state["partial"] = ""          # 整段与已发重叠 → 抑制
    elif t == "AssistantMessage":
        # 思考块（v0.4.11：让用户看到决策过程）——SDK 的 ThinkingBlock
        think_raw = "\n".join(
            (getattr(b, "thinking", None) or getattr(b, "text", "") or "")
            for b in (getattr(msg, "content", None) or [])
            if type(b).__name__ == "ThinkingBlock").strip()
        if think_raw:
            prev = state.get("thinking_emitted", "")
            # 思考去重（v0.4.11）：整块重复到达（partial/完整消息边界）→ 抑制；
            # 不用 _dedup_tail_overlap 的 12 字符阈值——短思考会被误判为新内容
            if not prev.endswith(think_raw):
                state["thinking_emitted"] = prev + think_raw
                out.append(StreamEvent(kind="thinking", text=think_raw))
        full = "".join(
            getattr(b, "text", "") for b in (getattr(msg, "content", None) or [])
            if type(b).__name__ == "TextBlock")
        if full:
            partial = state.get("partial", "")
            emitted = state.get("emitted", "")
            if full == state.get("last_full", ""):
                rest = ""                       # 同一完整消息重复到达
            elif partial and full.startswith(partial):
                rest = full[len(partial):]      # partial 已展示前缀 → 只发余量
            elif partial and full.strip() and full.strip() in partial:
                rest = ""                       # partial 已覆盖全文
            elif full and full not in emitted:
                rest = full                     # 无法对齐 → 全发（但整体未发过）
            else:
                rest = ""
            if rest:
                rest = _dedup_tail_overlap(emitted, rest)
            if rest:
                out.append(StreamEvent(kind="text", text=rest))
                state["emitted"] = emitted + rest
        state["last_full"] = full
        state["partial"] = ""
        state["seen_partial"] = False
        state["replay_src"] = ""        # 新消息边界：重置重放检测
        state["thinking_stream"] = ""   # 思考累积也按消息边界重置
        if full:
            state["prev_stream"] = full  # 供下一条消息的重放检测比对
        for b in (getattr(msg, "content", None) or []):
            if type(b).__name__ == "ToolUseBlock":
                out.append(StreamEvent(kind="tool_call", tool_call=ToolCall(
                    id=getattr(b, "id", ""), name=getattr(b, "name", ""),
                    arguments=getattr(b, "input", None) or {})))
    elif t == "ResultMessage":
        out.append(StreamEvent(kind="usage", usage=_map_usage(getattr(msg, "usage", None))))
        out.append(StreamEvent(kind="done"))
    return out


def _map_usage(usage) -> dict:
    if not isinstance(usage, dict):
        return {}
    return {
        "prompt_tokens": usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0,
        "completion_tokens": usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0,
        "prompt_cache_hit_tokens": usage.get(
            "cache_read_input_tokens", usage.get("prompt_cache_hit_tokens")),
    }


# ---------------------------------------------------------------- Runner

class CodebuddyRunner:
    """驱动 codebuddy_agent_sdk 的 query()；工具/沙箱/审计全走 AXON 网关。"""

    name = "codebuddy"

    def __init__(self, config, gateway, registry, prompter, store, memory_store):
        self.config = config
        self.gateway = gateway
        self.registry = registry
        self.prompter = prompter
        self.store = store
        self.memory_store = memory_store
        self._tool_names: dict[str, str] = {}   # tool_use_id -> 短工具名
        self._stderr_lines: list[str] = []      # CLI stderr 收集（诊断用）

    # ---- 构造 ----
    @classmethod
    def from_config(cls, cfg, gateway, registry, prompter, store, memory_store):
        p = cfg.active_provider_cfg()
        from axon.config import resolve_api_key
        missing = [v for v in (p.env_api_key, p.env_environment)
                   if v and not resolve_api_key(v)]
        if missing:
            raise CodebuddyUnavailable(
                f"环境变量未设置: {', '.join(missing)}（codebuddy 路需要）\n"
                "  设置方法（任选一）：\n"
                f"  1) 命令行设置：set {' '.join(missing)}=<值>\n"
                f"  2) 写进配置文件：{os.environ.get('AXON_HOME') or '数据目录'}"
                "/config.yml 的 model.providers.codebuddy.env_api_key\n"
                "  3) 或改用 openai 路：axon --provider openai（需 OPENAI_API_KEY）")
        try:
            import codebuddy_agent_sdk  # noqa: F401
        except ImportError as e:
            raise CodebuddyUnavailable(
                "未安装 codebuddy_agent_sdk（pip install codebuddy-agent-sdk）；"
                "或改用 openai 路：config.yml 中 active_provider: openai") from e
        return cls(cfg, gateway, registry, prompter, store, memory_store)

    # ---- 主入口 ----
    def run_turn(self, session, user_input: str,
                 on_text: Callable[[str], None] | None = None,
                 on_stuck: Callable[[], bool] | None = None,
                 status=None, images: list[str] | None = None,
                 on_thinking: Callable[[str], None] | None = None,
                 interject=None) -> TurnResult:
        import asyncio

        from axon.agent.loop import NullStatus
        status = status or NullStatus()
        from axon.config import ensure_dirs
        ensure_dirs(self.config)   # 防御：workspace 不存在 → CLI 子进程 cwd 无效（WinError 267）
        sdk = self._import_sdk()
        self._stderr_lines = []
        options = self._build_options(sdk, session)
        state: dict = {"partial": "", "seen_partial": False, "emitted": "",
                       "last_full": "", "replay_src": "", "prev_stream": ""}
        # 用户输入落库（v0.2.3）：SDK 路自管会话，若不落库则进度日志"请求"恒为空
        self.store.append_message(session.id, Message(role="user",
                                                     content=user_input))
        texts: list[str] = []
        self._pending_text = ""
        tokens_used = 0
        scope_turns: set[int] = set()
        sdk_session_id = None
        error_msg = ""
        first_text = [True]
        status.thinking()

        # ---- 多模态注入（v0.4.6）：原生 image block；失败则进程内降级 ----（见下）
        prompt_input = user_input
        image_paths = list(images or [])
        if image_paths and getattr(self, "_native_image_blocked", False):
            image_paths = []                 # 曾失败 → 本进程内不再尝试原生
        use_image_stream = bool(image_paths) and _vision.vision_available(
            self.config, session.model)
        stream_interject = bool(getattr(self.config.agent, "stream_interject", True))
        # v0.4.16：插嘴不再走 prompt 流（CLI 会把执行中的输入**排队到本回合结束**，
        # 用户实测"看到但不受影响"）。改为 client 模式：插嘴到达 → interrupt()
        # 中断当前回合 → 同 session 用 query(插嘴) 立即重发（下一个决策点生效）。
        # prompt 仍可走流式（仅用于图片多模态注入）。
        if use_image_stream:
            from axon import vision as _vision
            if _vision.vision_available(self.config, session.model):
                async def _prompt_stream():
                    content: list[dict] = [{"type": "text", "text": user_input}]
                    for path in image_paths:
                        block = _vision.to_claude_block(path)
                        if block:
                            content.append(block)
                    yield {"type": "user",
                           "message": {"role": "user", "content": content}}
                prompt_input = _prompt_stream()
            else:
                from axon import vision as _vision2
                note = _vision2.build_vision_note(image_paths, self.config,
                                                  session.model)
                prompt_input = user_input + (note or "")

        async def _drive() -> None:
            nonlocal tokens_used, sdk_session_id, error_msg
            # v0.4.16：改用 CodeBuddySDKClient（有状态客户端）——它暴露 interrupt()，
            # 支撑"插嘴到达 → 中断当前回合 → 同 session 立即重发"（下一个动作前生效）。
            # v0.4.18 修复（用户实测"159 秒无输出卡死"）：**不可**在 async with 之后再
            # connect(prompt) —— __aenter__ 已 connect()（无 prompt），再次 connect 会
            # 创建**第二个 CLI 子进程**并遗弃第一个 → 两进程互抢 deadlock。
            # 正确用法：async with 建立连接（延迟 initialize）→ query() 首次动作
            # （内部 initialize(has_prompt=True)，CLI 不回放历史）→ receive_messages()。
            from axon.interject import wrap_interjection as _wrap_ij
            client_cls = getattr(sdk, "CodeBuddySDKClient", None)
            if client_cls is None:               # 走 _import_sdk() 注入点（可测/可诊断）
                raise RuntimeError(
                    "codebuddy_agent_sdk 缺少 CodeBuddySDKClient"
                    "（需要支持有状态客户端与 interrupt 的版本，如 >=0.3.274）")

            pending_ij: list[str] = []
            interrupted = False
            async with client_cls(options=options) as client:
                await client.query(prompt_input)      # 首个动作 = query → has_prompt=True
                # 显式迭代 + 首条超时护栏（v0.4.18）：连接层卡死时不再无声转圈
                stream = client.receive_messages().__aiter__()
                first_msg = True
                while True:
                    try:
                        timeout = (self.config.agent.model_timeout_sec if first_msg
                                   else max(600.0, self.config.agent.model_timeout_sec))
                        msg = await asyncio.wait_for(stream.__anext__(), timeout)
                    except StopAsyncIteration:
                        break
                    except asyncio.TimeoutError:
                        error_msg = (f"等待模型响应超时（{timeout:.0f}s 内无任何事件）——"
                                     "可能是 CLI 连接异常或模型服务不可用")
                        break
                    first_msg = False
                    t = type(msg).__name__

                    # ---- 插嘴检查（每个消息边界，非阻塞）----
                    if stream_interject and interject is not None:
                        while True:
                            ij = await interject.get(0.0)
                            if not ij:
                                break
                            pending_ij.append(ij)
                        if pending_ij and not interrupted:
                            interrupted = True
                            if status is not None:
                                status.ensure_newline()
                                status.note("⚡ 收到插嘴，正在中断当前步骤并重新规划…")
                            try:
                                await client.interrupt()
                            except Exception as exc:      # noqa: BLE001
                                # 中断不受支持 → 退化为队列语义（回合结束后处理）
                                self._stderr_lines.append(
                                    f"[interject] interrupt 失败，已降级：{exc}")

                    if t == "ErrorMessage":
                        # 记录但不提前 break：SDK 生成器提前退出会触发 anyio
                        # cancel scope 跨任务退出崩溃（Windows 实测）
                        error_msg = str(getattr(msg, "error", msg))
                    for ev in map_sdk_message(msg, state):
                        if ev.kind == "text":
                            texts.append(ev.text)
                            self._pending_text += ev.text
                            if first_text[0]:
                                first_text[0] = False
                                status.text()
                            if on_text:
                                on_text(ev.text)
                        elif ev.kind == "thinking":
                            self._pending_text += ev.text
                            if on_thinking:
                                on_thinking(ev.text)
                        elif ev.kind == "tool_call" and ev.tool_call is not None:
                            self._mirror_tool_use(session, ev.tool_call, scope_turns,
                                                  status=status)
                        elif ev.kind == "usage":
                            tokens_used += int(ev.usage.get("prompt_tokens", 0)) + \
                                int(ev.usage.get("completion_tokens", 0))
                    # UserMessage 携带工具结果 → 镜像落库
                    if t == "UserMessage":
                        self._mirror_tool_results(session, msg, status=status,
                                                  scope_turns=scope_turns)
                    if t == "ResultMessage":
                        sdk_session_id = getattr(msg, "session_id", None) or sdk_session_id
                        if getattr(msg, "is_error", False) and not pending_ij:
                            error_msg = "; ".join(getattr(msg, "errors", None) or []) or \
                                f"ResultMessage is_error ({getattr(msg, 'subtype', '')})"
                            break
                        if pending_ij:
                            # 中断已生效（或 CLI 回合刚结束）→ 插嘴立即作为新输入
                            text_ij = "\n".join(pending_ij)
                            pending_ij.clear()
                            interrupted = False
                            if status is not None:
                                status.note("💬 插嘴已送达，智能体重新规划中…")
                            await client.query(_wrap_ij(text_ij))
                            continue          # 继续收新回合的消息
                        break                 # 正常回合结束

        try:
            asyncio.run(_drive())
        except Exception as e:  # noqa: BLE001 - SDK/传输异常如实转观察
            # 已捕获到 ErrorMessage 时优先报真实错误（忽略 anyio 收尾噪音）
            if not error_msg:
                error_msg = f"{type(e).__name__}: {e}"

        final_text = "".join(texts)
        if sdk_session_id:
            self.store.set_sdk_session_id(session.id, sdk_session_id)
        self._flush_pending_text(session)   # 尾部叙述（工具调用之后的收尾说明）
        self._flush_pending_text(session)

        if error_msg:
            note = error_msg
            tail = [ln for ln in self._stderr_lines if ln.strip()][-8:]
            if tail:
                note += "\n[CLI stderr 尾部]\n" + "\n".join(tail)
            self._hint_tool_names(session, error_msg)   # v0.4.1：短名自愈
            low = (error_msg or "").lower()
            if image_paths and any(k in low for k in ("image", "content block",
                                                     "media_type", "unsupported")):
                self._native_image_blocked = True       # 底层不支持图片 → 降级
                note += ("\\n[提示] 本次原生图片注入被拒绝，已自动降级："
                         "下回合将改用视觉副模型描述（可配置 model.vision_helper）。")
            return TurnResult(final_text, "provider_error",
                              len(scope_turns) or 1, tokens_used,
                              self._side_effects(session), note=note)

        # V 层验证（审计口径；SDK 路径无回读/退出码 meta → 跳过规则 3/4）
        from axon.agent.verify import TurnVerifier
        verifier = TurnVerifier(self.store, self.gateway.audit, self.config)
        vr = verifier.verify(session, final_text, scope_turns=scope_turns,
                             check_effects=False)
        if vr.passed:
            return TurnResult(final_text, "verified", len(scope_turns) or 1,
                              tokens_used, self._side_effects(session))
        return TurnResult(final_text, "verification_failed", len(scope_turns) or 1,
                          tokens_used, self._side_effects(session), note=vr.advice)

    def _side_effects(self, session) -> list:
        from axon.sandbox.audit import AuditLog
        return AuditLog(self.store.conn).turn_side_effects(session.id)

    def _collect_stderr(self, line: str) -> None:
        self._stderr_lines.append(line)

    # ---- 选项构建 ----
    def _build_options(self, sdk, session):
        from codebuddy_agent_sdk.types import CodeBuddyAgentOptions

        system = build_system_prompt(self.config)
        # v0.4.21：**长期记忆（跨项目）先于项目记忆注入**（用户要求的载入顺序）
        from axon.memory.global_store import GlobalMemoryStore
        try:
            glob_mem = GlobalMemoryStore(self.config.memory.long_term_path()).render_block()
        except Exception:                       # noqa: BLE001 - 记忆缺失不影响主流程
            glob_mem = ""
        mem = self.memory_store.render_block()
        if glob_mem:
            system += "\n\n<global_memory>\n" + glob_mem + "\n</global_memory>"
        if mem:
            system += "\n\n" + mem

        sdk_tools = [self._make_sdk_tool(spec) for spec in self.registry.disclosed()]
        server = sdk.create_sdk_mcp_server(name=MCP_SERVER_NAME,
                                           version=__version__, tools=sdk_tools)

        opts = CodeBuddyAgentOptions(
            model=session.model or self.config.active_model(),
            system_prompt=system,
            mcp_servers={MCP_SERVER_NAME: server},
            tools=[""],                 # 禁用全部内置工具，只暴露我们的 MCP 工具
            disallowed_tools=["Bash", "Edit", "Write", "Read", "Glob", "Grep",
                              "WebFetch", "Task", "NotebookEdit", "TodoWrite"],
            allowed_tools=[],            # 不预授权 → can_use_tool 每次都是唯一闸门
            setting_sources=[],          # 不加载文件系统设置（干净隔离）
            cwd=str(self.config.workspace),
            max_turns=self.config.agent.max_turns,
            include_partial_messages=True,
            request_timeout_ms=int(self.config.agent.model_timeout_sec * 1000),
            can_use_tool=self._can_use_tool,
            extra_args={"port": str(self._pick_port())},
            env={},
            stderr=self._collect_stderr,
        )
        sdk_sid = self.store.get_sdk_session_id(session.id)
        if sdk_sid:
            opts.resume = sdk_sid
        return opts

    # ---- 权限闸门（can_use_tool） ----
    async def _can_use_tool(self, tool_name: str, tool_input: dict, opts):
        from codebuddy_agent_sdk import PermissionResultAllow, PermissionResultDeny

        short = tool_name[len(_TOOL_PREFIX):] if tool_name.startswith(_TOOL_PREFIX) \
            else tool_name
        spec = self.registry.get(short)
        if spec is None:
            # 非我方工具（内置工具残留等）→ 一律拒绝，引导回 MCP 工具
            action = Action(kind="mcp", target=tool_name, detail=tool_input,
                            session_id=self.gateway.session_id, turn=self.gateway.turn)
            self.gateway.audit.record(action, "deny", "mcp:unknown_tool", "blocked")
            return PermissionResultDeny(
                behavior="deny",
                message=f"AXON 沙箱：未知工具 {tool_name}；请使用 mcp__{MCP_SERVER_NAME}__* 系列工具")

        action = spec.to_action(tool_input)
        action = Action(**{**action.__dict__,
                           "session_id": self.gateway.session_id,
                           "turn": self.gateway.turn})
        decision = self.gateway.check(action)

        if decision.verdict == "deny":
            self.gateway.audit.record(action, "deny", decision.rule, "blocked")
            return PermissionResultDeny(
                behavior="deny",
                message=f"沙箱拒绝（{decision.rule}）：{decision.reason}")

        if decision.verdict == "ask":
            approval = self.prompter.confirm(action, decision)
            self.gateway.audit.record(
                action, "ask_approved" if approval.approved else "ask_denied",
                decision.rule, "ok" if approval.approved else "blocked")
            if not approval.approved:
                return PermissionResultDeny(behavior="deny", message="用户拒绝执行该操作")
            if approval.remember_session:
                key = action_fingerprint(action,
                                         self.config.sandbox.approval_granularity)
                self.gateway._auth_cache[key] = None
            if approval.edited_detail:
                tool_input = approval.edited_detail

        self.gateway.audit.record(action, "allow", decision.rule, "pending")
        return PermissionResultAllow(behavior="allow", updated_input=tool_input)

    # ---- MCP 工具包装（只执行，不自查） ----
    def _make_sdk_tool(self, spec):
        from codebuddy_agent_sdk import tool

        @tool(spec.name, spec.description, spec.parameters)
        def handler(args: dict) -> dict:
            call = ToolCall(id=f"cb-{uuid.uuid4().hex[:8]}", name=spec.name,
                            arguments=args)
            handle = _RunnerHandle(self.gateway)
            try:
                result = spec.handler(call, args, handle)
            except Exception as e:  # noqa: BLE001 - 工具异常 → isError 观察
                return {"content": [{"type": "text", "text":
                                     f"{type(e).__name__}: {e}"}], "isError": True}
            action = spec.to_action(args)
            action = Action(**{**action.__dict__,
                               "session_id": self.gateway.session_id,
                               "turn": self.gateway.turn})
            self.gateway.audit.record(
                action, "allow", "executed", "ok" if result.ok else "error")
            return {"content": [{"type": "text", "text": result.output}],
                    "isError": not result.ok}

        return handler

    # ---- 会话镜像（SDK 对话 → 我们的 messages 表） ----
    def _mirror_tool_use(self, session, tc: ToolCall, scope_turns: set[int],
                         status=None) -> None:
        from axon.agent.loop import NullStatus
        status = status or NullStatus()
        short = tc.name[len(_TOOL_PREFIX):] if tc.name.startswith(_TOOL_PREFIX) else tc.name
        self._tool_names[tc.id] = short
        status.tool(short, str(tc.arguments.get("path") or tc.arguments.get("command")
                               or tc.arguments.get("url") or "")[:80])
        # assistant 文本（v0.2.5）：随工具调用一起落库，保持"叙述→调用"的真实顺序；
        # 原实现只镜像 tool_calls，导致会话历史里没有 agent 的说明文字
        #（resume 后看不到回答、进度日志缺上下文、中断后无从继续）。
        prose = (self._pending_text or "").strip()
        self._pending_text = ""
        msg = self.store.append_message(session.id, Message(
            role="assistant", content=prose or None, tool_calls=[tc]))
        self.gateway.bind(session.id, msg.turn)
        scope_turns.add(msg.turn)

    def _hint_tool_names(self, session, error_msg: str) -> None:
        """工具短名错误自愈（v0.4.1，用户实测）。

        现象：模型调用 `execute_shell`（短名），而本环境注册的是
        `mcp__axon__execute_shell` → SDK 报 "Tool 'X' not found in the current tool
        set" 并**直接中止回合**（token 0）。这里把纠正提示落库成一条观察消息，
        用户说「继续」时模型即可看到正确全名并重试。
        """
        import re as _re
        m = _re.search(r"Tool '([^']+)' not found in the current tool set",
                       error_msg or "")
        if not m:
            return
        short = m.group(1)
        full = short if short.startswith("mcp__") else f"mcp__{MCP_SERVER_NAME}__{short}"
        tools = "、".join(f"`mcp__{MCP_SERVER_NAME}__{n}`" for n in
                         ("read_file", "write_file", "edit_file", "search_files",
                          "web_fetch", "execute_shell", "memory_read",
                          "memory_write", "project_note", "image_generate"))
        self.store.append_message(session.id, Message(role="user", content=(
            "<tool_name_hint>\n"
            f"你刚才调用了短名 `{short}`，本环境要求使用 **MCP 全名**：`{full}`。\n"
            f"可用工具全名：{tools}。\n"
            "请用全名重新发起该调用（其余已完成的工作不必重做）。\n"
            "</tool_name_hint>")))

    def _flush_pending_text(self, session) -> None:
        """把尚未挂到工具调用上的叙述文本落库（回合收尾）。"""
        prose = (self._pending_text or "").strip()
        self._pending_text = ""
        if prose:
            self.store.append_message(session.id, Message(role="assistant",
                                                          content=prose))

    def _mirror_tool_results(self, session, user_msg, status=None,
                             scope_turns: set[int] | None = None) -> None:
        from axon.agent.loop import NullStatus
        status = status or NullStatus()
        for b in (getattr(user_msg, "content", None) or []):
            if type(b).__name__ != "ToolResultBlock":
                continue
            tid = getattr(b, "tool_use_id", "")
            name = self._tool_names.get(tid, "")
            text = getattr(b, "content", "")
            if isinstance(text, list):
                text = "\n".join(getattr(c, "text", str(c)) for c in text)
            is_err = bool(getattr(b, "is_error", False))
            status.tool_result(name, not is_err)
            m = self.store.append_message(session.id, Message(
                role="tool", tool_call_id=tid, name=name,
                content=pack_tool_content(str(text), {},
                                          error_type="model" if is_err else None)))
            if scope_turns is not None:
                scope_turns.add(m.turn)    # v0.4.27：tool 消息纳入回合 scope

    # ---- 端口 ----
    def _pick_port(self) -> int:
        configured = getattr(self.config.active_provider_cfg(), "cli_port", 0) or 0
        if configured:
            return int(configured)
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", 0))
            return int(s.getsockname()[1])
        finally:
            s.close()

    def _import_sdk(self):
        try:
            import codebuddy_agent_sdk
            return codebuddy_agent_sdk
        except ImportError as e:
            raise CodebuddyUnavailable(
                "未安装 codebuddy_agent_sdk（pip install codebuddy-agent-sdk）") from e


class _RunnerHandle:
    """runner 路径的 GatewayHandle（子动作上报 + 工具内确认用）。"""

    def __init__(self, gateway):
        self._gateway = gateway

    @property
    def session_id(self) -> str:
        return self._gateway.session_id

    @property
    def turn(self) -> int:
        return self._gateway.turn

    @property
    def workspace(self) -> str:
        return str(self._gateway.config.workspace)

    def note_subaction(self, action: Action) -> None:
        self._gateway._note_subaction(action)

    def confirm(self, action: Action, reason: str) -> bool:
        return self._gateway._confirm_from_tool(action, reason)
