"""V 层：TurnVerifier 确定性规则集（设计 §4.6）。

验证强度阶梯第一档（确定性规则）：
  1. no_unresolved_errors  末态无 transient/resource 悬挂结果；
     permanent 错误若已向用户明示替代路径并继续，记审计但不判负
  2. red_actions_approved 本会话红档动作无 ask_denied/deny 记录
  3. writes_readback_ok   写文件回读 hash 与 meta.sha256 一致
  4. shell_exitcodes_ok   shell 动作 meta.exit_code 均为 0
  5. no_stuck             打转检测未触发（由 loop 传入）
  6. final_nonempty       终答非空

评估走独立读路径（CQRS）：只读 messages/audit_log，不经过模型上下文。
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from axon.sandbox.hooks import VerifyResult


# ---------------------------------------------------------------- 任务级验收（v0.4.2）

# 可分立的交付物量词（用于与产物数量严格比对）
_PIECE_UNITS = ("篇", "个", "份", "条", "部", "套", "种", "款", "张", "本", "页")
# 属于"单一文件内部"的量词 → 不参与严格比对（如"5 页 PPT"只产出 1 个文件）
_INTERNAL_UNITS = ("页", "人", "次", "轮", "行", "字", "秒", "分钟", "章")
_COUNT_RE = re.compile(r"(\d+)\s*(" + "|".join(_PIECE_UNITS + _INTERNAL_UNITS) + ")")
_OUT_PATH_RE = re.compile(
    r"[A-Za-z]:[\\/][^\s\"'|<>]+\.(?:pptx?|docx?|xlsx?|pdf|md|txt|csv|json|png|jpe?g)")


def acceptance_check(user_input: str, result, cfg=None) -> tuple[bool, list[str]]:
    """任务级验收自检（v0.4.2，用户要求"检查任务有没有完成、跟任务有什么出入"）。

    与 V 层（TurnVerifier，技术性：错误悬挂/回读/退出码）互补——这里**对照用户原始请求**
    核对交付情况，规则式、零 token：
    1. 量化要求 vs 产物数（仅当只有一个"可分立"数量词时严格比对；
       多个数量词或"页/张"等文件内部量词只给提示，避免误报，如"5 页 PPT"= 1 个文件）
    2. 产物存在性（列出后又被删/移走的会报出来）
    3. 未成功的操作（side_effects 里 result != ok）
    4. 产物是否仍在工作空间（未归档提示）
    5. 请求里显式给出的输出路径是否存在

    返回 (是否通过, 摘要行列表)。
    """
    lines: list[str] = []
    ok = True
    text = user_input or ""
    products = list(getattr(result, "products", []) or [])

    counts = [(int(n), u) for n, u in _COUNT_RE.findall(text)]
    external = [(n, u) for n, u in counts if u not in _INTERNAL_UNITS]
    if len(external) == 1 and products:
        need, unit = external[0]
        if len(products) < need:
            ok = False
            lines.append(f"⚠ 请求要求 {need}{unit}，本回合产物 {len(products)} 项"
                         f"（可能未完成，或被合并进单个文件）")
        else:
            lines.append(f"✓ 产物 {len(products)} 项（请求 {need}{unit}）")
    elif external and not products:
        ok = False
        lines.append("⚠ 请求含量化要求 " +
                     "、".join(f"{n}{u}" for n, u in external) + "，但本回合没有产物")
    elif counts and products:
        lines.append(f"ℹ 请求含数量描述（{'、'.join(f'{n}{u}' for n, u in counts)}），"
                     f"本回合产物 {len(products)} 项")

    missing = [str(p.get("path")) for p in products
               if p.get("path") and not Path(str(p["path"])).is_file()]
    if missing:
        ok = False
        lines.append("⚠ 产物不存在: " + "、".join(missing[:3]))

    failed = [e for e in (getattr(result, "side_effects", []) or [])
              if str(e.get("result")) not in ("ok", "None")]
    if failed:
        ok = False
        lines.append(f"⚠ 有 {len(failed)} 项操作未成功（详见 /audit）")

    if cfg is not None and products:
        try:
            ws = Path(cfg.workspace).resolve()
            stayed = [str(p["path"]) for p in products
                      if Path(str(p["path"])).resolve().parent == ws
                      or ws in Path(str(p["path"])).resolve().parents]
            if stayed:
                lines.append(f"ℹ {len(stayed)} 个产物仍在工作空间（未归档到项目目录）")
        except (OSError, ValueError):
            pass

    for m in _OUT_PATH_RE.finditer(text):
        p = Path(m.group(0))
        if not p.exists():
            ok = False
            lines.append(f"⚠ 请求中提到的输出文件不存在: {p}")

    if not lines:
        lines.append("✓ 无可核对的量化要求（本回合无产物）")
    return ok, lines


class TurnVerifier:
    def __init__(self, store, audit, config=None):
        self.store = store
        self.audit = audit
        self.config = config

    def verify(self, session, final_text: str, stuck_fired: bool = False,
               scope_turns: set[int] | None = None,
               check_effects: bool = True) -> VerifyResult:
        """scope_turns: **本回合全部消息的 turn 集合**（v0.4.27 起规则 1/3/4 按它
        过滤——历史遗留失败不再污染新回合；None = 全量扫描的旧行为）。
        codebuddy runner 路径由 _mirror_* 逐条纳入；openai loop 路径按
        turn_start 过滤。check_effects=False 时跳过规则 3/4（runner 路径
        无回读/退出码 meta）。
        """
        failed: list[str] = []
        advice_parts: list[str] = []
        messages = self.store.load_messages(session.id)
        audit_rows = self.audit.rows(session.id)

        # 规则 1：无未解决错误（v0.4.27：按回合 scope 扫描——旧实现扫全量历史，
        # 往回合遗留的 transient/resource 失败会永久污染后续所有回合的验证）
        for m in messages:
            if m.role == "tool" and m.content and self._in_scope(m, scope_turns):
                et = self._error_type_of(m)
                if et in ("transient", "resource"):
                    failed.append("no_unresolved_errors")
                    advice_parts.append(
                        f"工具 {m.name} 仍有未解决的 {et} 类错误，请重试或换路径后继续")
                    break

        # 规则 2：红档动作审批核对（v0.4.27：只看**最后一个轮次**的拒绝记录）
        if scope_turns is None:
            last_asst_turn = max((m.turn for m in messages if m.role == "assistant"),
                                 default=0)
            scope = {last_asst_turn}
        elif scope_turns:
            # v0.4.30：规则 2 是**统计口径**（本回合被拒次数），用全集；
            # 不再取"最后一个 tool 轮"——那会跳过早期的被拒记录，
            # "反复重试被拒动作"反而查不出来
            scope = set(scope_turns)
        else:
            # v0.4.29 P0：空 scope（如 codebuddy 路无工具的纯问答回合——
            # scope_turns 只由工具镜像填充）→ 无审批记录可核对，直接放行。
            # 旧实现 max(空集) 抛 ValueError 穿透 CLI（用户每问一句"你好"即崩）
            scope = set()

        def _row_turn(row) -> int | None:
            import json as _json
            try:
                return _json.loads(row["detail"] or "{}").get("_turn")
            except ValueError:
                return None

        # 本回合内按 kind 归集被拒记录：同类被拒 ≥2 次 = 反复重试被拒动作
        denied_by_kind: dict[str, list] = {}
        for r in audit_rows:
            if r["verdict"] in ("ask_denied", "deny") and _row_turn(r) in scope:
                denied_by_kind.setdefault(r["kind"], []).append(r)
        for kind, rows in denied_by_kind.items():
            if len(rows) >= 2:
                last = rows[-1]
                failed.append("red_actions_approved")
                advice_parts.append(
                    f"动作 {last['kind']}:{last['target'][:60]} 被拒绝/拦截后"
                    f"本回合内又重试 {len(rows) - 1} 次（{last['rule']}）——"
                    "请改用合规方式或征求用户同意，不要反复重试")
                break

        # 规则 3：写文件回读校验（v0.4.27：同样按回合 scope）
        if check_effects:
            for m in messages:
                if m.role != "tool" or not m.content or not self._in_scope(m, scope_turns):
                    continue
                meta = self._meta_of(m)
                if meta and "sha256" in meta and "path" in meta:
                    try:
                        actual = hashlib.sha256(
                            Path(meta["path"]).read_bytes()).hexdigest()[:16]
                        if actual != meta["sha256"]:
                            failed.append("writes_readback_ok")
                            advice_parts.append(
                                f"文件 {meta['path']} 回读校验不一致，请重新读取并修正")
                            break
                    except OSError:
                        failed.append("writes_readback_ok")
                        advice_parts.append(f"文件 {meta['path']} 回读失败（不存在或不可读）")
                        break

        # 规则 4：shell 退出码
        if check_effects:
            for m in messages:
                if m.role == "tool" and self._in_scope(m, scope_turns):
                    meta = self._meta_of(m)
                    if meta and meta.get("exit_code", 0) not in (0, None):
                        failed.append("shell_exitcodes_ok")
                        advice_parts.append(
                            f"shell 退出码 {meta['exit_code']} 非 0，请检查错误输出并修正")
                        break

        # 规则 5：打转
        if stuck_fired:
            failed.append("no_stuck")
            advice_parts.append("检测到原地打转，已中断；请换思路或请求用户介入")

        # 规则 6：终答非空
        if not (final_text or "").strip():
            failed.append("final_nonempty")
            advice_parts.append("终答为空，请给出明确结论")

        return VerifyResult(passed=not failed, failed_rules=failed,
                            advice="；".join(advice_parts))

    # ---- meta/error_type 解析（存于 tool 消息 content 的 <meta> 标记，见 msgmeta.py） ----
    @staticmethod
    def _in_scope(m, scope_turns: set[int] | None) -> bool:
        """消息是否属于本回合（v0.4.27）。

        scope_turns=None → 不过滤（旧行为，全量扫描）；传入集合时只认集合内的
        turn。tool 消息的 turn 与发起它的 assistant 消息不同（append_message
        每包自增），因此调用方传入的必须是**本回合全部消息的 turn 集合**
        （openai loop 路按 turn_start 过滤；codebuddy runner 路由
        _mirror_tool_use/_mirror_tool_results 逐条纳入）。
        """
        return scope_turns is None or getattr(m, "turn", None) in scope_turns

    @staticmethod
    def _meta_of(m):
        from axon.agent.msgmeta import unpack_meta
        _, meta = unpack_meta(m.content)
        return meta

    @staticmethod
    def _error_type_of(m):
        meta = TurnVerifier._meta_of(m)
        return (meta or {}).get("error_type")
