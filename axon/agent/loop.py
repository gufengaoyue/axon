"""E 层：AgentLoop（设计 §4.1）——静态 tool-calling 循环。

终止归 V 层（TurnVerifier 确定性规则）；防跑飞三道：max_turns + token 预算 + 节点超时；
打转检测（同指纹 + 无进展）；并行有界池（网关门控）；流式 + Ctrl+C 可中断。
"""
from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from axon.agent.context import CompactionNeeded
from axon.agent.msgmeta import pack_tool_content
from axon.providers.base import Message, StreamEvent


@dataclass
class TurnResult:
    final_text: str
    stop_reason: str            # verified | max_turns | budget_exhausted |
                                # user_interrupt | stuck_break | provider_error
                                # | verification_failed
    turns_used: int
    tokens_used: int
    side_effects: list = field(default_factory=list)
    note: str = ""              # 错误详情 / 验证建议（给人看的文本，勿塞进 side_effects）
    products: list = field(default_factory=list)   # 本轮产物（文件路径/大小/类型），v0.2.2
    acceptance: tuple = ()            # 任务级验收自检 (ok, 摘要行)，v0.4.2


class StuckDetector:
    """同指纹（tool_name + canonical args）+ 无进展（连续同 permanent 错误 / 无新 target）。"""

    def __init__(self, threshold: int = 3):
        self.threshold = threshold
        self._fingerprints: deque[str] = deque(maxlen=threshold)
        self._targets: deque[frozenset] = deque(maxlen=threshold)
        self._perm_errors: deque[bool] = deque(maxlen=threshold)

    def observe(self, tool_calls: list, results: list) -> bool:
        from axon.tools.registry import canonical_args
        fp = "|".join(f"{tc.name}:{canonical_args(tc.arguments)}" for tc in tool_calls)
        targets = frozenset(
            (tc.arguments.get("path") or tc.arguments.get("command")
             or tc.arguments.get("url") or tc.name) for tc in tool_calls)
        all_perm = bool(results) and all(
            (not r.ok) and r.error_type == "permanent" for r in results)
        self._fingerprints.append(fp)
        self._targets.append(targets)
        self._perm_errors.append(all_perm)

        if len(self._fingerprints) == self.threshold and len(set(self._fingerprints)) == 1:
            return True
        if len(self._perm_errors) == self.threshold and all(self._perm_errors):
            return True
        if len(self._targets) == self.threshold and len(set(self._targets)) == 1 \
                and len(self._fingerprints) == self.threshold:
            return True
        return False


class NullStatus:
    """无 UI 时的状态桩（库调用/测试用）。"""

    def thinking(self, note: str = "") -> None:
        pass

    def text(self) -> None:
        pass

    def tool(self, name: str, target: str = "") -> None:
        pass

    def tool_result(self, name: str, ok: bool, detail: str = "") -> None:
        pass

    def note(self, text: str) -> None:
        pass

    def idle(self) -> None:
        pass


class AgentLoop:
    def __init__(self, provider, gateway, registry, ctx_builder, compactor,
                 verifier, store, config, status=None, journal=None):
        self.provider = provider
        self.gateway = gateway
        self.registry = registry
        self.ctx_builder = ctx_builder
        self.compactor = compactor
        self.verifier = verifier
        self.store = store
        self.config = config
        self.status = status or NullStatus()
        self.journal = journal

    # ---- 主入口（回合结束自动沉淀进度日志，v0.2.0） ----
    def run(self, session, user_input: str,
            on_text: Callable[[str], None] | None = None,
            on_stuck: Callable[[], bool] | None = None,
            images: list[str] | None = None,
            on_thinking: Callable[[str], None] | None = None,
            interject=None) -> TurnResult:
        try:
            result = self._run(session, user_input, on_text=on_text,
                               on_stuck=on_stuck, images=images,
                               on_thinking=on_thinking, interject=interject)
        except KeyboardInterrupt:
            # Ctrl+C 可能落在 provider/SDK 内部（openai 路已在 _run 内捕获）——
            # 这里兜住剩下的情形，保证"未完成任务"仍被持久化（v0.2.5）
            result = TurnResult("", "user_interrupt", 0, 0,
                                note="用户中断（Ctrl+C）")
        self._post_turn(session, result, user_input)
        return result


    def _post_turn(self, session, result: TurnResult, user_input: str = "") -> None:
        """回合收尾：① 持久化未完成任务（v0.2.5）② 清理临时文件 ③ 产物 ④ 进度日志。"""
        try:
            self._persist_unfinished(session, result, user_input)
        except Exception:
            pass
        cleaned: list[str] = []
        try:
            if self.config.tools.cleanup_temp_each_turn:
                from axon.env_cleanup import cleanup_temp
                cleaned = cleanup_temp(self.config)
                if cleaned:
                    from axon.sandbox.audit import AuditLog
                    from axon.sandbox.gateway import Action as _A
                    AuditLog(self.store.conn).record(
                        _A(kind="env_cleanup", target=f"{len(cleaned)} 个临时文件",
                           session_id=session.id, turn=result.turns_used),
                        "allow", "cleanup:temp", "ok")
        except Exception:
            cleaned = []
        try:
            result.products = self._collect_products(session)
        except Exception:
            result.products = []
        archived: list[tuple[str, str]] = []
        try:
            archived = self._archive_products(result)
        except Exception:
            archived = []
        try:                                   # 任务级验收自检（v0.4.2）
            from axon.agent.verify import acceptance_check
            result.acceptance = acceptance_check(user_input or "", result, self.config)
        except Exception:
            result.acceptance = ()
        self._record_journal(session, result, cleaned, archived=archived)

    UNFINISHED_REASONS = ("user_interrupt", "provider_error", "stuck_break",
                          "max_turns", "budget_exhausted")

    def _persist_unfinished(self, session, result: TurnResult,
                            user_input: str) -> None:
        """中断/失败时把"未完成任务"写进会话与项目记忆（用户实测：中断后 resume 继续不了）。

        没有这一步，resume 上下文只有副作用清单，模型不知道要做什么 →
        "请继续上次未完成的任务"无从下手。
        """
        if result.stop_reason not in self.UNFINISHED_REASONS or not user_input:
            return
        from axon.providers.base import Message as _M
        reason_cn = {"user_interrupt": "用户中断（Ctrl+C）",
                     "provider_error": "模型调用失败",
                     "stuck_break": "检测到打转被中断",
                     "max_turns": "达到轮次上限",
                     "budget_exhausted": "token 预算耗尽"}.get(result.stop_reason,
                                                              result.stop_reason)
        self.store.append_message(session.id, _M(role="user", content=(
            "<interrupted_task>\n"
            f"status: 未完成（{reason_cn}）\n"
            f"original_request: {' '.join(user_input.split())[:400]}\n"
            "提示：用户若要求「继续」，请先核对已完成部分（见 <resume_context> 与审计），"
            "从断点续做——不要重头再来、不要重复已完成的副作用。\n"
            "</interrupted_task>")))
        if self.journal is not None:
            try:
                self.journal.memory.append_project_note(
                    "next", f"- [未完成] {' '.join(user_input.split())[:160]}"
                            f"（{reason_cn}）")
            except Exception:
                pass

    def _archive_products(self, result: TurnResult) -> list[tuple[str, str]]:
        """把工作空间里的产物搬到**项目目录**（v0.3.0 用户要求）。

        定位：工作空间（`<project>/.workspace`）是草稿/执行区，项目目录才是
        用户可见的正式产出区。规则：
        - 只搬 **本回合产物**（result.products）中位于工作空间内的文件
        - 跳过工作区的素材/内部区（`downloads/`、`tmp/`、隐藏目录）——它们是
          来源素材与临时物，留在工作区（避免路径漂移）
        - 目标已存在且内容相同 → 视为已归档，删掉工作区副本
        - 目标已存在但内容不同 → 加 `-2` 后缀，绝不覆盖用户文件
        - 移动（非复制），并把 products 里的路径更新为项目内新路径
        开关：`tools.move_products_to_project`（默认 true）。
        """
        import shutil
        from pathlib import Path
        if not getattr(self.config.tools, "move_products_to_project", True):
            return []
        ws = Path(self.config.workspace).resolve()
        proj = Path(self.config.project).resolve()
        if str(ws) == str(proj):
            return []
        moved: list[tuple[str, str]] = []
        for item in getattr(result, "products", []) or []:
            raw = item.get("path")
            if not raw:
                continue
            src = Path(raw)
            try:
                if not src.is_file():
                    continue
                rel = src.resolve().relative_to(ws)
            except (OSError, ValueError):
                continue                      # 不在工作空间 → 不动
            if not rel.parts:
                continue
            top = rel.parts[0]
            if top in ("downloads", "tmp") or top.startswith("."):
                continue                      # 素材/临时/内部区不搬
            dst = proj / rel
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                if dst.exists():
                    if dst.read_bytes() == src.read_bytes():
                        src.unlink()           # 已归档过 → 清理工作区副本
                        item["path"] = str(dst)
                        item["archived"] = "same"
                        moved.append((str(src), str(dst)))
                        continue
                    i = 2
                    while dst.exists():
                        dst = dst.with_name(f"{dst.stem}-{i}{dst.suffix}")
                        i += 1
                shutil.move(str(src), str(dst))
            except (OSError, ValueError):
                continue
            item["path"] = str(dst)
            item["archived"] = "moved"
            moved.append((str(src), str(dst)))
        return moved

    def _collect_products(self, session) -> list[dict]:
        """本回合产物：工具返回 meta 含 path 的写入/下载物（带大小）。"""
        from axon.agent.msgmeta import unpack_meta
        import os as _os
        messages = self.store.load_messages(session.id)
        start = 0
        for i, m in enumerate(messages):
            if m.role == "user":
                start = i
        out: list[dict] = []
        seen: set[str] = set()
        for m in messages[start:]:
            if m.role != "tool" or not m.content:
                continue
            _, meta = unpack_meta(m.content)
            if not meta or not meta.get("path"):
                continue
            path = str(meta["path"])
            if path in seen:
                continue
            seen.add(path)
            size = meta.get("size_bytes")
            if size is None:
                try:
                    size = _os.path.getsize(path)
                except OSError:
                    size = None
            out.append({"path": path, "size": size,
                        "kind": "download" if meta.get("downloaded") else "file",
                        "sha256": meta.get("sha256")})
        return out

    def _record_journal(self, session, result: TurnResult,
                        cleaned: list[str] | None = None,
                        archived: list | None = None) -> None:
        if self.journal is None or not self.config.memory.auto_journal:
            return
        try:
            from axon.sandbox.audit import AuditLog
            audit = AuditLog(self.store.conn)
            messages = self.store.load_messages(session.id)
            self.journal.record_turn(session, result, messages,
                                     audit.rows(session.id),
                                     cleaned=cleaned or [],
                                     archived=archived or [])
        except Exception:
            pass  # 进度日志失败不影响主流程

    def _run(self, session, user_input: str,
             on_text: Callable[[str], None] | None = None,
             on_stuck: Callable[[], bool] | None = None,
             images: list[str] | None = None,
             on_thinking: Callable[[str], None] | None = None,
             interject=None) -> TurnResult:
        images = list(images or [])
        # codebuddy 路径：SDK 自驱执行引擎（设计 §16 偏差 #8），整体委派
        if hasattr(self.provider, "run_turn"):
            return self.provider.run_turn(session, user_input, on_text=on_text,
                                          on_stuck=on_stuck, status=self.status,
                                          images=images, on_thinking=on_thinking,
                                          interject=interject)

        self._session_model = session.model
        # 多模态：图片随用户消息注入（openai 路 → content blocks，v0.4.6）
        self.store.append_message(session.id, Message(role="user", content=user_input,
                                                      images=images))
        self.store.touch_session(session.id)
        tokens_used = 0
        turns = 0
        stuck = StuckDetector(self.config.agent.stuck_threshold)
        stuck_fired = False
        vf_count = 0                       # v0.4.27：连续 verification_failed 熔断计数
        # v0.4.27：本回合起始 turn——V 层验证按回合 scope 扫描，历史遗留失败
        # （往回合的 transient/resource 错误）不再污染新回合（旧实现扫全量 → 死循环）
        turn_start = self.store.next_turn(session.id) - 1

        try:
            while turns < self.config.agent.max_turns:
                turns += 1
                # ---- 组装（超 90% → 强制压缩后重组装） ----
                history = self.store.load_messages(session.id)
                try:
                    ctx = self.ctx_builder.assemble(session, history)
                except CompactionNeeded:
                    self.compactor.compact(session.id, history, self._summarize, self.store)
                    history = self.store.load_messages(session.id)
                    ctx = self.ctx_builder.assemble(session, history)
                # 80% 水位清理（记 warn 失效账）
                if ctx.tokens_est >= self.config.context.compact_warn_ratio * \
                        self.config.model.context_window:
                    cleaned, n_elided = self.ctx_builder.cleanup(history)
                    if n_elided:
                        from axon.sandbox.hooks import CompactionEvent
                        self.store.record_compaction(CompactionEvent(
                            session_id=session.id, at_turn=turns, trigger="warn",
                            before_tokens=ctx.tokens_est,
                            after_tokens=ctx.tokens_est,  # 清理收益在下一轮组装体现
                            summary=f"elided {n_elided} old tool outputs"))
                        ctx = self.ctx_builder.assemble(session, cleaned)

                # ---- 推理（流式：逐事件回调，首 token 前转圈） ----
                self.status.thinking()
                text_parts: list[str] = []
                tool_calls = []
                usage: dict = {}
                first_text = [True]

                def _ot(t: str) -> None:
                    if first_text[0]:
                        first_text[0] = False
                        self.status.text()
                    if on_text:
                        on_text(t)

                def _oth(t: str) -> None:
                    if on_thinking:
                        on_thinking(t)

                # v0.4.26（用户实测 openai 路裸 traceback）：连接/鉴权/模型不存在等
                # SDK 异常统一转 provider_error + 排查话术——不再把 Python 栈泼到终端。
                # KeyboardInterrupt 是 BaseException，不被 except Exception 捕获，
                # 仍走外层 except → user_interrupt 语义不变。
                try:
                    for ev in self.provider.chat(
                            ctx.messages, ctx.tools, model=session.model,
                            timeout=self.config.agent.model_timeout_sec):
                        if ev.kind == "text":
                            text_parts.append(ev.text)
                            _ot(ev.text)
                        elif ev.kind == "thinking":
                            _oth(ev.text)
                        elif ev.kind == "tool_call" and ev.tool_call is not None:
                            tool_calls.append(ev.tool_call)
                        elif ev.kind == "usage":
                            usage = ev.usage
                except Exception as e:  # noqa: BLE001 - SDK/传输异常 → 可操作的失败结论
                    return TurnResult("".join(text_parts), "provider_error", turns,
                                      tokens_used, self._side_effects(session),
                                      note=_friendly_provider_error(
                                          e, self.config, session.model))
                text = "".join(text_parts)
                tokens_used += int(usage.get("prompt_tokens", 0)) + \
                    int(usage.get("completion_tokens", 0))

                self.store.append_message(session.id, Message(
                    role="assistant", content=text or None, tool_calls=tool_calls))
                asst = self.store.load_messages(session.id)[-1]
                self.gateway.bind(session.id, asst.turn)

                # ---- 无工具调用 → V 层验证 ----
                if not tool_calls:
                    vr = self.verifier.verify(
                        session, text, stuck_fired=stuck_fired,
                        scope_turns={m.turn for m in
                                     self.store.load_messages(session.id)
                                     if m.turn >= turn_start})
                    if vr.passed:
                        return TurnResult(text, "verified", turns, tokens_used,
                                          self._side_effects(session))
                    vf_count += 1
                    if vf_count >= 2:
                        # v0.4.27：连续 2 次验证失败 → 熔断。旧实现无限追加
                        # <verification_failed> 观察消息 → 模型反复自证重写
                        # （用户实测"总结时陷入自证死循环"，跑满 max_turns 才停）
                        return TurnResult(text, "verification_failed", turns,
                                          tokens_used, self._side_effects(session),
                                          note=(f"V 层验证连续 {vf_count} 次未通过"
                                                f"（{', '.join(vr.failed_rules)}），已停止"
                                                f"自动重试。{vr.advice}"))
                    self.store.append_message(session.id, Message(
                        role="user",
                        content=f"<verification_failed>\n未通过规则: "
                                f"{', '.join(vr.failed_rules)}\n{vr.advice}\n"
                                "（说明：这是**系统自动规则校验**，不是用户报错。若你判断"
                                "任务实际已完成、上述规则为误报，请在终答中说明理由即可"
                                "结题——不要反复重写已经正确的答案。）\n"
                                "</verification_failed>"))
                    continue

                # ---- 工具执行（全部过沙箱网关） ----
                batch = []
                for tc in tool_calls:
                    spec = self.registry.get(tc.name)
                    if spec is None:
                        self.store.append_message(session.id, Message(
                            role="tool", tool_call_id=tc.id, name=tc.name,
                            content=f"工具不存在: {tc.name}。可用工具: "
                                    f"{', '.join(self.registry.names())}"))
                        continue
                    batch.append((tc, spec, tc.arguments))
                for tc, spec, args in batch:
                    self.status.tool(tc.name, _target_of(args))
                results = self.gateway.execute_batch(batch)
                for r in results:
                    self.status.tool_result(r.name, r.ok,
                                            (r.recovery_hint or "")[:80] if not r.ok else "")
                    self.store.append_message(session.id, Message(
                        role="tool", tool_call_id=r.call_id, name=r.name,
                        content=pack_tool_content(r.output, r.meta, r.error_type)))
                self.status.thinking()

                # ---- 打转检测 ----
                if stuck.observe(tool_calls, results):
                    stuck_fired = True
                    cont = on_stuck() if on_stuck else False
                    if not cont:
                        return TurnResult(text or "", "stuck_break", turns, tokens_used,
                                          self._side_effects(session))
                    stuck = StuckDetector(self.config.agent.stuck_threshold)
                    # v0.4.27：用户选择"继续尝试" = 重新来过——**必须解除粘滞**。
                    # 旧实现保留 stuck_fired=True，导致本回合后续所有终答都被规则 5
                    # 判负 → 无限追加 <verification_failed> → 模型自证死循环
                    # （用户实测：换方法成功后仍无法结题，只能重启会话）
                    stuck_fired = False

                # ---- 预算闸 ----
                if tokens_used > self.config.model.session_token_budget:
                    return TurnResult(text or "", "budget_exhausted", turns, tokens_used,
                                      self._side_effects(session))
        except KeyboardInterrupt:
            return TurnResult("", "user_interrupt", turns, tokens_used,
                              self._side_effects(session))

        return TurnResult("", "max_turns", turns, tokens_used, self._side_effects(session))

    # ---- 辅助 ----
    def _summarize(self, prompt: str) -> str:
        events = list(self.provider.chat(
            [Message(role="user", content=prompt)], None,
            model=self._model_of_last_session(), timeout=self.config.agent.model_timeout_sec))
        text, _, _ = self._collect(events)
        return text

    def _model_of_last_session(self) -> str:
        # 压缩摘要沿用主模型（v1 单模型；ModelRouter 分档为 v2）
        return getattr(self, "_session_model", "") or self.config.active_model()

    @staticmethod
    def _collect(events: list[StreamEvent]) -> tuple[str, list, dict]:
        text_parts: list[str] = []
        tool_calls = []
        usage: dict = {}
        for ev in events:
            if ev.kind == "text":
                text_parts.append(ev.text)
            elif ev.kind == "tool_call" and ev.tool_call is not None:
                tool_calls.append(ev.tool_call)
            elif ev.kind == "usage":
                usage = ev.usage
        return "".join(text_parts), tool_calls, usage

    def _side_effects(self, session) -> list:
        from axon.sandbox.audit import AuditLog
        audit = AuditLog(self.store.conn)
        return audit.turn_side_effects(session.id)


def _target_of(args: dict) -> str:
    return str(args.get("path") or args.get("command") or args.get("url") or "")[:80]


# 非 OpenAI 官模的模型名特征（"端点 vs 模型名"错配检测用，v0.4.26）
_NON_OPENAI_MODEL_HINTS = ("qwen", "deepseek", "glm", "moonshot", "kimi", "ernie",
                           "doubao", "hunyuan", "minimax", "yi-", "baichuan",
                           "spark", "sensechat", "command-r", "llama", "mistral")


def _friendly_provider_error(exc: BaseException, cfg, model: str = "") -> str:
    """openai SDK（及兼容层）异常 → 可操作的中文说明（v0.4.26）。

    用户实测：`axon --provider openai --model qwen3.7-flash` 时
    `openai.APIConnectionError: Connection error.` 以裸 traceback 泼屏——
    根因是 openai 路的异常在 AgentLoop 无人捕获（只捕 KeyboardInterrupt）。
    调用方（_run）已把异常转成 provider_error；这里按异常类型给出排查路径，
    连接错误额外指出「端点 vs 模型名」错配（qwen 等模型打到 api.openai.com
    必然连不上）与代理环境变量陷阱（httpx 默认读 HTTP(S)_PROXY）。
    """
    import os
    name = type(exc).__name__
    head = f"{name}: {exc}"
    prov = cfg.active_provider_cfg() if cfg is not None else None
    prov_name = str(getattr(cfg.model, "active_provider", "")) if cfg else ""
    base_url = str(getattr(prov, "base_url", "") or "")
    tail: list[str] = []
    if name in ("APIConnectionError", "ConnectError", "ConnectionError",
                "APITimeoutError", "TimeoutException"):
        tail.append("无法连接模型端点。请依次排查：")
        tail.append(f"① 端点：当前 base_url = {base_url or '(未配置)'}"
                    f"（config.yml → model.providers.{prov_name}.base_url，"
                    "或用 --base-url 临时指定）")
        if model:
            tail.append(f"② 模型：{model}——请确认它属于该端点所属的服务商")
        if model and base_url and "api.openai.com" in base_url and \
                any(h in model.lower() for h in _NON_OPENAI_MODEL_HINTS):
            tail.append(f"③ ⚠ 端点/模型不匹配：{model} 不是 OpenAI 官方模型，"
                        "打到 api.openai.com 既连不上也找不到模型。qwen 系应改用 "
                        "DashScope 兼容端点 https://dashscope.aliyuncs.com/compatible-mode/v1"
                        "（key 用 DashScope 颁发的 sk-...，设为 OPENAI_API_KEY）")
        proxies = {k: os.environ.get(k) for k in
                   ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")
                   if os.environ.get(k)}
        if proxies:
            k0 = next(iter(proxies))
            tail.append(f"④ ⚠ 检测到代理环境变量 {sorted(proxies)}（如 {k0}={proxies[k0]}）——"
                        "httpx 会走该代理；若是已失效的本地代理，清空后重试"
                        "（set HTTP_PROXY= & set HTTPS_PROXY=）")
        else:
            tail.append("③ 网络：确认本机能访问该端点（公司网/代理需自行配置）")
    elif name == "AuthenticationError":
        tail.append("API key 无效或未授权（401）。请检查该服务商颁发的密钥是否已设为 "
                    f"{getattr(prov, 'env_api_key', 'OPENAI_API_KEY')} 环境变量")
    elif name == "NotFoundError":
        tail.append(f"端点或模型不存在（404）。请核对 base_url（{base_url or '未配置'}）"
                    f"与模型名（{model or '?'}）是否匹配——模型名是否属于该服务商、"
                    "是否拼错")
    elif name == "RateLimitError":
        tail.append("触发限额（429）。稍后重试，或到服务商控制台检查额度")
    elif name == "BadRequestError":
        tail.append("请求被拒绝（400）。常见于模型名不被该端点支持、参数越界"
                    "（max_tokens 超上限）、或多模态格式不被支持")
    else:
        tail.append("模型调用异常。请核对端点/密钥/网络后重试；持续失败可带此信息反馈")
    return head + "\n" + "\n".join(tail)
