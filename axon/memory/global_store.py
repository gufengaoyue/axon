"""系统级长期记忆（v0.4.21）：跨项目通用的稳定事实。

与项目记忆的分工：
- **长期记忆**（本模块）＝ 所有项目通用：系统环境、用户信息、偏好、工具链路径……
  文件：`<个人文档>/.axon/longtermmemory.md`（用户可见、可编辑、可直接删）
- **项目记忆**（memory/store.py 的 MemoryStore）＝ 仅当前项目：AXON.md 三区

注入顺序（用户要求）：长期记忆 > 项目记忆（先全局后项目，同 prompt 内）。
"""
from __future__ import annotations

import io
import os
import re
from pathlib import Path

# 默认模板（首次使用时播种；用户可直接编辑本文件）
TEMPLATE = """# 长期记忆（跨项目 · Axon 自动维护）

> 本文件记录**所有项目通用**的稳定事实：系统环境、用户信息、个人偏好、工具链路径等。
> 项目专属的信息请写在各项目的 `AXON.md`（项目记忆），不要写在这里。
> Axon 会在发现跨项目事实时，用 `global_memory_write` 工具更新本文件；你也可以直接编辑。

## 系统与环境

<!-- 例：OS: Windows 11 (win32) / Shell: Git Bash (MSYS) / 文件编码 UTF-8 -->

## 用户

<!-- 例：称呼 <你的名字/昵称> · 邮箱 <you>@example.com · 中文沟通，保留英文技术术语 -->

## 偏好

<!-- 例：回复要简洁结构化；UI 用内联 SVG 图标（禁 emoji）；大文件用多段 patch -->

## 工具链与路径

<!-- 例：MSVC 18 Community: D:\\Program Files\\...\\VC\\Tools\\MSVC\\14.51.x（注意 /utf-8 与 /MT） -->

## 其他

"""

_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.M)


class GlobalMemoryStore:
    """长期记忆读写（去重追加 + 预算内注入）。"""

    def __init__(self, path: Path, token_budget: int = 1200,
                 max_lines: int = 400):
        self.path = Path(path)
        self.token_budget = token_budget
        self.max_lines = max_lines

    # ---- 读写 ----
    def exists(self) -> bool:
        return self.path.is_file()

    def ensure_seeded(self) -> None:
        """首次使用：播种模板（父目录不存在则创建）。"""
        if self.exists():
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            io.open(self.path, "w", encoding="utf-8").write(TEMPLATE)
        except OSError:
            pass

    def read(self) -> str:
        """读文件。v0.4.29：解码失败**抛错**而非返回空串——旧实现返回 ""
        会让 append 误判"空文件"并以空 body 全量覆写，非 UTF-8 损坏的记忆
        被静默清空。"""
        try:
            return io.open(self.path, encoding="utf-8").read()
        except FileNotFoundError:
            return ""              # 尚未创建 → 空文件语义正确
        except OSError as e:
            # v0.4.30：其它读失败（共享冲突/权限）不得返回空串——旧实现会让
            # append 误判"空文件"并以空 body 全量覆写
            raise ValueError(f"长期记忆文件读取失败（{e}）：{self.path}")
        except UnicodeDecodeError as e:
            raise ValueError(
                f"长期记忆文件编码损坏（{e}）：{self.path}——请用 UTF-8 保存或手工修复")

    # ---- 注入 ----
    def render_block(self) -> str:
        """注入 system 上下文：预算内截断（超预算加提示行）。"""
        body = self.read().strip()
        if not body:
            return ""
        try:
            from axon.context.tokens import estimate_tokens
        except Exception:                       # pragma: no cover - 兼容旧布局
            estimate_tokens = lambda s: len(s) // 2   # noqa: E731

        # 去掉模板注释行（`<!-- ... -->`）与引导说明块，减小注入体积
        lines = [ln for ln in body.splitlines()
                 if not ln.strip().startswith("<!--") and "<!--" not in ln]
        text = "\n".join(lines)
        if estimate_tokens(text) <= self.token_budget:
            return text
        # 超预算：逐行累加至预算
        out, used = [], 0
        for ln in lines:
            cost = estimate_tokens(ln) + 1
            if used + cost > self.token_budget:
                out.append("...(长期记忆超预算，部分未注入；可用 global_memory_write 整理)")
                break
            out.append(ln)
            used += cost
        return "\n".join(out)

    # ---- 写入（工具调用）----
    def append(self, section: str, text: str) -> tuple[bool, str]:
        """向指定小节追加一条（自动创建小节；完全重复的条目跳过）。

        v0.4.21 修正：**行级操作**（首版按字符区间拼接，会在小节末尾插入时
        与下一节标题粘连、进而导致后续小节匹配失败重复创建——冒烟实测）。
        返回 (是否写入, 说明)。
        """
        text = " ".join(str(text).split()).strip()
        if not text:
            return False, "内容为空"
        self.ensure_seeded()
        body = self.read()
        section = (section or "其他").strip().lstrip("#").strip() or "其他"

        lines = body.splitlines()
        if len(lines) >= self.max_lines:
            return False, f"长期记忆已达 {self.max_lines} 行上限，请先整理（删除过时条目）"

        bullet = f"- {text}"

        sec_idx = None
        for i, ln in enumerate(lines):
            if ln.strip() == f"## {section}":
                sec_idx = i
                break
        if sec_idx is None:
            lines += ["", f"## {section}", "", bullet]      # 新小节追加到末尾
        else:
            end_idx = len(lines)
            for j in range(sec_idx + 1, len(lines)):
                if lines[j].startswith("## "):
                    end_idx = j
                    break
            if any(lines[j].strip() == bullet for j in range(sec_idx + 1, end_idx)):
                return False, "相同内容已存在（跳过重复）"
            last_bullet = None
            for j in range(sec_idx + 1, end_idx):
                if lines[j].strip().startswith("- "):
                    last_bullet = j
            if last_bullet is not None:
                lines.insert(last_bullet + 1, bullet)       # 该节最后一条之后
            else:
                ins = sec_idx + 1                            # 无条目：标题后的注释块之后
                while ins < end_idx and not lines[ins].strip():
                    ins += 1
                lines.insert(ins, bullet)

        body = "\n".join(lines).rstrip() + "\n"
        try:
            io.open(self.path, "w", encoding="utf-8").write(body)
            return True, f"已写入「{section}」：{text[:60]}"
        except OSError as e:
            return False, f"写入失败：{e}"


def default_long_term_path() -> Path:
    """默认位置：`<个人文档>/.axon/longtermmemory.md`（用户可见可编辑）。"""
    docs = Path.home() / "Documents"
    # 中文系统可能把"文档"改过名；优先已存在的 .axon 目录，否则用 Documents
    cand = docs / ".axon"
    if not cand.exists():
        alt = Path.home() / "我的文档" / ".axon"
        if alt.exists():
            cand = alt
    return cand / "longtermmemory.md"
