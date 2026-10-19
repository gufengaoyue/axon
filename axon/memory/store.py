"""长期记忆治理（设计 §4.4）：AXON.md，scope/confidence/预算。

条目格式（追加式，头部元信息）：
  <!-- entry: scope=project confidence=inferred added_turn=12 source=任务观察 hash=ab12cd34 -->
  条目文本（可多行）

治理规则：
  - inferred 不得被当作已验证事实引用（系统提示显式声明）
  - 总量超 token 预算 → 拒绝写入并提示整理
  - 条目数超 max_entries → 拒绝
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path

from axon.agent.tokenizer import estimate_tokens

_ENTRY_RE = re.compile(r"<!--\s*entry:\s*(.+?)\s*-->")
_JOURNAL_RE = re.compile(r"<!--\s*journal:([^>]*)-->")


class MemoryError_(Exception):
    pass


class MemoryBudgetError(MemoryError_):
    pass


@dataclass(frozen=True)
class MemoryEntry:
    scope: str            # project | global
    confidence: str       # verified | inferred
    text: str
    added_turn: int
    source: str

    @property
    def hash(self) -> str:
        return hashlib.sha1(self.text.encode("utf-8")).hexdigest()[:8]


def _parse_meta(line: str) -> dict:
    meta = {}
    for kv in line.split():
        if "=" in kv:
            k, v = kv.split("=", 1)
            meta[k] = v
    return meta


class MemoryStore:
    """AXON.md 双区结构（v0.2.0）：

    - `## 长期记忆`：模型经 memory_write 写入的治理条目（scope/confidence）
    - `## 进度日志`：harness 每回合结束自动追加的进度条目（journal，规则式零 token）
    render_block 两个区都注入（总预算内，记忆区优先 60%）。
    """

    MEMORY_SECTION = "## 长期记忆"
    PROJECT_SECTION = "## 项目记忆"      # 模型会话总结（v0.2.3）
    JOURNAL_SECTION = "## 进度日志"

    def __init__(self, path: Path, token_budget: int = 4000, max_entries: int = 200):
        self.path = path
        self.token_budget = token_budget
        self.max_entries = max_entries

    # ---- 读 ----
    def _read_text(self) -> str:
        return self.path.read_text(encoding="utf-8") if self.path.exists() else ""

    def _memory_section(self) -> str:
        """「长期记忆」区文本（MEMORY_SECTION 头 → 项目记忆区/进度日志区之前）。

        v0.4.29 P0/P1：旧实现取 `text[:JOURNAL_SECTION]`——把**项目记忆区**
        也包进来，于是 `_entries()` 解析时最后一个条目的 body 一直吸收到
        日志区头（含项目记忆全文）→ remove_entry 精确匹配失效、render_block
        双重注入、预算投影虚高。现在严格按区边界截取。
        """
        text = self._read_text()
        start = text.find(self.MEMORY_SECTION)
        if start == -1:
            return ""
        end = len(text)
        for sec in (self.PROJECT_SECTION, self.JOURNAL_SECTION):
            idx = text.find(sec, start + len(self.MEMORY_SECTION))
            if idx != -1:
                end = idx
                break
        return text[start:end]

    def _journal_section(self) -> str:
        """「进度日志」区文本。"""
        text = self._read_text()
        idx = text.find(self.JOURNAL_SECTION)
        return text[idx:] if idx != -1 else ""

    def _project_section(self) -> str:
        """「项目记忆」区文本（长期记忆之后、进度日志之前）。"""
        text = self._read_text()
        start = text.find(self.PROJECT_SECTION)
        if start == -1:
            return ""
        end = text.find(self.JOURNAL_SECTION)
        return text[start:end] if end > start else text[start:]

    def project_notes(self) -> list[str]:
        """项目记忆条目（按出现顺序）。"""
        import re as _re
        sec = self._project_section()
        parts = _re.split(r"<!--\s*project:[^>]*-->", sec)
        return [p.strip() for p in parts[1:] if p.strip()]

    def append_project_note(self, kind: str, text: str) -> None:
        """追加一条项目记忆（会话总结/决策/踩坑/偏好/环境/下一步）。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_skeleton()
        text_now = self._read_text()
        stamp = datetime.now(timezone.utc).strftime("%m-%d %H:%M")
        block = f"\n<!-- project: kind={kind} at={stamp} -->\n{text.strip()}\n"
        j = text_now.find(self.JOURNAL_SECTION)
        if j == -1:
            text_now = text_now.rstrip() + f"\n\n{self.JOURNAL_SECTION}\n"
            j = text_now.find(self.JOURNAL_SECTION)
        self.path.write_text(text_now[:j] + block.lstrip("\n") + "\n" + text_now[j:],
                             encoding="utf-8")

    def _ensure_skeleton(self) -> None:
        if not self.path.exists() or not self._read_text().strip():
            self.path.write_text(
                f"# AXON.md — AXON 项目记忆\n\n{self.MEMORY_SECTION}\n\n"
                f"{self.PROJECT_SECTION}\n\n{self.JOURNAL_SECTION}\n",
                encoding="utf-8")
            return
        text_now = self._read_text()
        if self.PROJECT_SECTION not in text_now:
            j = text_now.find(self.JOURNAL_SECTION)
            insert = f"{self.PROJECT_SECTION}\n\n"
            if j == -1:
                text_now = text_now.rstrip() + f"\n\n{insert}{self.JOURNAL_SECTION}\n"
            else:
                text_now = text_now[:j] + insert + text_now[j:]
            self.path.write_text(text_now, encoding="utf-8")

    def _entries(self) -> list[tuple[dict, str]]:
        parts = _ENTRY_RE.split(self._memory_section())
        # split 后: [前缀, meta1, body1, meta2, body2, ...]
        out = []
        for i in range(1, len(parts) - 1, 2):
            out.append((_parse_meta(parts[i]), parts[i + 1].strip()))
        return out

    def entries(self) -> list[MemoryEntry]:
        result = []
        for meta, body in self._entries():
            if not body:
                continue
            try:                       # v0.4.29：手改文件的非数字 added_turn 不炸
                added_turn = int(meta.get("added_turn", "0"))
            except (TypeError, ValueError):
                added_turn = 0
            result.append(MemoryEntry(
                scope=meta.get("scope", "project"),
                confidence=meta.get("confidence", "inferred"),
                text=body,
                added_turn=added_turn,
                source=meta.get("source", "unknown"),
            ))
        return result

    # ---- 进度日志区 ----
    def journal_entries(self) -> list[tuple[dict, str]]:
        """进度日志条目（旧→新）。"""
        parts = _JOURNAL_RE.split(self._journal_section())
        out = []
        for i in range(1, len(parts) - 1, 2):
            body = parts[i + 1].strip()
            if body:
                out.append((_parse_meta(parts[i]), body))
        return out

    def append_journal(self, text: str) -> None:
        """向进度日志区追加一条（文件不存在则创建双区骨架）。"""
        header = "<!-- journal: auto -->"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists() or not self.path.read_text(encoding="utf-8").strip():
            self.path.write_text(
                f"# AXON.md — AXON 项目记忆\n\n{self.MEMORY_SECTION}\n\n"
                f"{self.JOURNAL_SECTION}\n", encoding="utf-8")
        text_now = self.path.read_text(encoding="utf-8")
        if self.JOURNAL_SECTION not in text_now:
            text_now = text_now.rstrip() + f"\n\n{self.JOURNAL_SECTION}\n"
        with self.path.open("a", encoding="utf-8") as f:
            f.write(f"\n{header}\n{text.strip()}\n")

    def render_block(self) -> str:
        """注入稳定前缀的记忆块：长期记忆（优先 60% 预算）+ 进度日志（40%，新→旧）。"""
        entries = self.entries()
        journals = self.journal_entries()
        if not entries and not journals:
            return ""
        lines = ["<long_term_memory>"]
        used = estimate_tokens("\n".join(lines))
        mem_budget = int(self.token_budget * 0.5)
        for e in entries:
            block = (f"[{e.scope}/{e.confidence}/turn={e.added_turn}/src={e.source}] {e.text}")
            cost = estimate_tokens(block) + 1
            if used + cost > mem_budget:
                lines.append("...(长期记忆超预算，部分未注入；请用 memory_write 整理)")
                break
            lines.append(block)
            used += cost
        notes = self.project_notes()
        if notes:
            lines.append("<project_memory>")
            p_budget = int(self.token_budget * 0.3)
            p_used = 0
            shown = 0
            for body in reversed(notes):          # 新→旧
                cost = estimate_tokens(body) + 1
                if p_used + cost > p_budget:
                    break
                lines.append(body)
                p_used += cost
                shown += 1
            if shown < len(notes):
                lines.append(f"...(更早的 {len(notes) - shown} 条项目记忆未注入)")
            lines.append("</project_memory>")
        if journals:
            lines.append("<project_progress>")
            j_budget = self.token_budget - used
            j_used = 0
            shown = 0
            for meta, body in reversed(journals):     # 新→旧
                cost = estimate_tokens(body) + 1
                if j_used + cost > j_budget:
                    break
                lines.append(body)
                j_used += cost
                shown += 1
            if shown < len(journals):
                lines.append(f"...(更早的 {len(journals) - shown} 条进度日志未注入)")
            lines.append("</project_progress>")
        lines.append("</long_term_memory>")
        return "\n".join(lines)

    # ---- 写 ----
    def add_entry(self, entry: MemoryEntry) -> None:
        if entry.confidence not in ("verified", "inferred"):
            raise MemoryError_("confidence 必须是 verified 或 inferred")
        if entry.scope not in ("project", "global"):
            raise MemoryError_("scope 必须是 project 或 global")
        existing = self.entries()
        if len(existing) >= self.max_entries:
            raise MemoryBudgetError(
                f"记忆条目数达上限 {self.max_entries}，请先整理（删除陈旧条目）再写入")
        if any(e.hash == entry.hash for e in existing):
            return  # 去重：同文本已存在
        # v0.4.29：投影对**记忆区全量文本**计 token（旧实现用 render_block——
        # 它自带 50% 预算截断，投影值永远逼近不了上限，"超预算拒绝"从不触发）
        full_text = "".join(b for _, b in self._entries())
        # v0.4.30：投影口径与注入一致（记忆区预算 = token_budget 的 50%）——
        # 旧实现允许累计到全预算，但注入只给 50% → 后半段条目永远不可见
        _budget = int(self.token_budget * 0.5)
        projected = estimate_tokens(full_text) + estimate_tokens(entry.text) + 8
        if projected > _budget:
            raise MemoryBudgetError(
                f"写入后将超记忆预算（预计 {projected} > {self.token_budget} token），"
                "请先整理陈旧条目")
        header = (f'<!-- entry: scope={entry.scope} confidence={entry.confidence}'
                  f' added_turn={entry.added_turn} source={entry.source}'
                  f' hash={entry.hash} -->')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        block = f"\n{header}\n{entry.text}\n"
        if not self.path.exists() or not self.path.read_text(encoding="utf-8").strip():
            self.path.write_text(
                f"# AXON.md — AXON 项目记忆\n\n{self.MEMORY_SECTION}\n\n"
                f"{self.JOURNAL_SECTION}\n", encoding="utf-8")
        text_now = self.path.read_text(encoding="utf-8")
        # v0.4.30：确保「长期记忆」区头存在——手工/畸形的 AXON.md 可能没有它，
        # 此时按旧锚点会把条目插到区外 → entries() 永不可见（静默丢失 + 去重
        # 失效 → 反复追加膨胀）
        if self.MEMORY_SECTION not in text_now:
            j0 = text_now.find(self.JOURNAL_SECTION)
            if j0 == -1:
                text_now = text_now.rstrip() + f"\n\n{self.MEMORY_SECTION}\n"
            else:
                text_now = text_now[:j0] + f"{self.MEMORY_SECTION}\n\n" + text_now[j0:]
        # v0.4.29：插入锚点优先 PROJECT_SECTION——条目必须落在「长期记忆」区
        # （旧实现只找 JOURNAL_SECTION：有项目记忆时条目插到项目记忆区之后 →
        # 正文被项目记忆污染 → remove_entry 精确匹配失效 + 双重注入）
        idx = text_now.find(self.PROJECT_SECTION)
        if idx == -1:
            idx = text_now.find(self.JOURNAL_SECTION)
        if idx == -1:
            text_now = text_now.rstrip() + f"\n\n{self.JOURNAL_SECTION}\n"
            idx = text_now.find(self.JOURNAL_SECTION)
        self.path.write_text(text_now[:idx] + block.lstrip("\n") + "\n"
                             + text_now[idx:], encoding="utf-8")

    def remove_entry(self, text: str) -> bool:
        """按文本精确匹配删除条目。"""
        entries = self._entries()
        target = text.strip()
        kept = [(m, b) for (m, b) in entries if b.strip() != target]
        if len(kept) == len(entries):
            return False
        self._rewrite(kept)
        return True

    def _rewrite(self, entries: list[tuple[dict, str]]) -> None:
        """重写「长期记忆」区（v0.4.29 P0 修复：旧实现把整文件重构为
        "# 标题 + entry 列表"，**项目记忆与进度日志被整体抹掉**——数据丢失）。

        现按 SECTION 边界做局部手术：只替换 [MEMORY_SECTION, 下一区) 区间，
        标题前缀、项目记忆区、进度日志区原文原样保留。"""
        new_mem = self._render_memory_section(entries)
        text = self._read_text()
        start = text.find(self.MEMORY_SECTION)
        if start == -1:
            # 无骨架（旧文件/手工文件）→ 整体重建三区（保留不了其他区内容，
            # 但至少恢复完整骨架而不是丢成一页 entry 列表）
            body = f"# AXON.md — AXON 项目记忆\n\n{new_mem}\n\n{self.JOURNAL_SECTION}\n"
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(body, encoding="utf-8")
            return
        end = len(text)
        for sec in (self.PROJECT_SECTION, self.JOURNAL_SECTION):
            idx = text.find(sec, start + len(self.MEMORY_SECTION))
            if idx != -1:
                end = idx
                break
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(text[:start] + new_mem.rstrip() + "\n\n"
                             + text[end:], encoding="utf-8")

    def _render_memory_section(self, entries: list[tuple[dict, str]]) -> str:
        """把 entries 渲染为「长期记忆」区文本（含区头）。"""
        parts = [self.MEMORY_SECTION + "\n"]
        for meta, body in entries:
            if not body:
                continue
            kv = " ".join(f"{k}={v}" for k, v in meta.items())
            parts.append(f"<!-- entry: {kv} -->\n{body}\n")
        return "\n".join(parts)
