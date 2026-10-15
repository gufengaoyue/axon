"""CLI：argparse + REPL + 斜杠命令 + 流式输出 + 状态反馈 + 人在回路审批器。

v0.1.5 改版（参考 CodeBuddy CLI 风格）：
- rich 面板横幅 / 红框审批面板 / dim 页脚
- prompt_toolkit 提示符 + 底部状态栏（项目·模型·工作空间·快捷键）
- 思考中： braille 转圈 + 已耗时（消除"假死"感；Ctrl+C 可中断）
- 工具执行： 行动线（⚙ 名称 目标 → ✓/✗ 结果）
入口：axon = axon.cli:main
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from pathlib import Path

from axon import __version__
from axon import attachments
from axon.interject import InterjectBridge, InterjectReader, wrap_interjection
from axon.agent.compaction import Compactor
from axon.agent.context import ContextBuilder
from axon.agent.loop import AgentLoop, TurnResult
from axon.agent.verify import TurnVerifier
from axon.config import ensure_dirs, load_config
from axon.memory.journal import Journal
from axon.memory.store import MemoryStore
from axon.providers.router import ModelRouter
from axon.sandbox.audit import AuditLog
from axon.sandbox.gateway import Action, Approval, Decision, SandboxGateway
from axon.sandbox.hooks import register_builtin_hooks
from axon.session.db import SessionStore, connect
from axon.session.resume import load_for_resume
from axon.tools.builtin import ToolContext, build_builtin_registry
from axon.tools.mcp_client import MCPConnector, MCPUnavailable

HELP_TEXT = """\
可用命令：
  /help                 显示本帮助
  /save [补充说明]      沉淀项目记忆（调用模型总结本次会话，写入 AXON.md）
  /delproj [项目]       删除项目（与命令行 `python -m axon --delproj [项目]` 等价）
                        不带参数列出项目；需输入项目名确认（默认只删会话数据）
  /attach [路径]        附加图片/文件（不带参数读剪贴板）；截图/复制文件会被**自动收编**
                        收编到 <项目>/tmp/，提示中以 >./tmp/图片1.png 形式引用
  /sessions             列出最近会话（跨项目索引）
  /resume [last|id]     恢复会话（不带参数列出最近会话；跨项目需重启 --resume）
  /verbose [on|off]     详细模式：长内容全显/折叠（等同 Ctrl+O；v0.4.13）
  /compact              手动触发一次结构化压缩（openai 路）
  /memory               查看记忆（长期记忆 + 项目记忆 + 进度日志）
  /audit [--export csv|json <路径>]   查看/导出审计日志
  /exit                 退出（Ctrl+D 同效；退出前会自动沉淀项目记忆）
直接输入自然语言即与 agent 对话。执行中 Ctrl+C 中断当前回合。"""

SUMMARY_PROMPT = """\
请为本次会话沉淀「项目记忆」——目的是让**后续会话**能快速理解这个项目的内容、逻辑与进度。

请调用 project_note 工具（可多次调用，按类型分开写入）：
- kind=summary：一段简明总结：项目/任务目标、当前进度、交付物（含完整路径）
- kind=decision：关键决策与可复用经验（每条一行）
- kind=pitfall：踩过的坑——**尤其你自己犯的错**（格式：什么操作 → 什么错误 → 正确做法）
- kind=preference：用户明确表达的偏好与习惯
- kind=environment：环境事实（可用解释器、已装依赖、网络可达性等）
- kind=next：下一步待办（如有）

要求：只写有依据的内容，不编造；每项一行、简洁；不要抄进度日志里的流水细节；
不要把一次性命令粘贴进去。写完用一句中文回复"已沉淀项目记忆"。"""


# ---------------------------------------------------------------- 控制台/状态

def _enable_vt() -> bool:
    """尝试开启 Windows 控制台 VT 处理（双行状态显示需要）。返回是否可用。"""
    if os.name != "nt":
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)          # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        if mode.value & ENABLE_VIRTUAL_TERMINAL_PROCESSING:
            return True
        return bool(kernel32.SetConsoleMode(
            handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING))
    except Exception:
        return False


_VT_OK = _enable_vt()


def _make_console():
    try:
        from rich.console import Console
        return Console()
    except ImportError:  # pragma: no cover
        return None


class StatusLine:
    """状态反馈：思考中转圈+计时；工具执行打行动线；流式文本时让位。

    线程安全约定：所有方法只在主线程调用；转圈线程只写 \r 刷新，
    主线程任何输出前先 stop()（join 线程）再写。
    """

    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, console):
        self.console = console
        self._tty = bool(getattr(sys.stdout, "isatty", lambda: False)())
        self._evt = threading.Event()
        self._thread: threading.Thread | None = None
        self._msg = "思考中"
        self._t0 = 0.0
        self._running = False
        self.pending_line = False      # 当前行有未换行的流式输出（v0.2.2 面板错位修复）
        self._last_tool = ""           # 连续同类工具合并（v0.2.3 降噪）
        self._last_tool_count = 0
        self._notes: list[str] = []    # 操作概要轮换（v0.2.4）
        self._note_idx = 0
        self.keyboard = None           # InterjectReader（v0.2.6：执行中插嘴回显）
        self._last_event = 0.0         # 最近一次事件时间（看门狗用，v0.4.5）
        self._thinking_open = False    # 思考行是否打开（增量流式，v0.4.12）
        self._live_active = False      # rich Live 是否独占终端（v0.4.20）
        self.verbose = False           # 详细模式：关闭时 >3 行的块折叠（v0.4.13）
        self._thinking_at_line_start = True   # 思考增量是否处于行首（决定是否补缩进）
        self._watchdog: threading.Thread | None = None
        self._watching = False
        self._line_len = 0             # 上一帧长度（擦除行尾用，v0.2.9 修残影）
        self._two_line = False         # 双行模式：状态行 + 常驻输入行（v0.2.9）

    # ---- 内部 ----
    def _input_line(self) -> str:
        """常驻输入行（v0.2.9）：插嘴内容显示在这里，提示符始终占位。

        v0.4.23：有输入时**明确提示"按回车发送"**——用户实测"插不进嘴"
        正是因为打出内容后不知道该按回车（显示停留在缓冲态）。
        """
        cur = self.keyboard.current_input() if self.keyboard is not None else ""
        if cur:
            return f"axon ❯ 插嘴: {cur[-160:]}  ← 按回车发送"
        return "axon ❯ （执行中可直接输入补充，回车提交）"

    def _write(self, text: str) -> None:
        """重写当前行并**擦除行尾**（此前仅 \r 覆盖 → 短行留下长行残影，v0.2.9 修复）。"""
        pad = max(0, self._line_len - len(text))
        try:
            sys.stdout.write("\r" + text + " " * pad)
            sys.stdout.flush()
        except (ValueError, OSError):
            return
        self._line_len = len(text)

    def _render_once(self, i: int = 0, el: float | None = None) -> str:
        """渲染一帧（抽出来便于单测；返回写入的文本）。"""
        if getattr(self, "_live_active", False):
            return ""                        # v0.4.20：Live 活动期间不写（防冲突刷屏）
        if el is None:
            el = time.monotonic() - self._t0
        if self._notes and i % 20 == 0:          # 每 ~2s 轮换操作概要
            self._msg = self._notes[(i // 20) % len(self._notes)]
        frame = self.FRAMES[i % len(self.FRAMES)]
        status = f"{frame} {self._msg} ({el:.0f}s)"
        if self._two_line:
            try:
                sys.stdout.write("\x1b[1A\r\x1b[2K" + status
                                 + "\n\r\x1b[2K" + self._input_line())
                sys.stdout.flush()
            except (ValueError, OSError):
                pass
            return status + "\n" + self._input_line()
        tail = ""
        if self.keyboard is not None:
            cur = self.keyboard.current_input()
            if cur:
                tail = f"   ⌨ 插嘴: {cur[-48:]}"
        self._write(f"{status}{tail}")
        return f"{status}{tail}"

    def _spin(self) -> None:
        i = 0
        while not self._evt.wait(0.1):
            self._render_once(i)
            i += 1
        # 收尾：清干净状态行（双行模式连输入行一起清）
        try:
            if self._two_line:
                # 清两行并把光标移回上（状态）行开头 → 不留残行（v0.3.0）
                sys.stdout.write("\x1b[1A\r\x1b[2K" + "\n\r\x1b[2K" + "\x1b[1A\r")
            else:
                sys.stdout.write("\r" + " " * (self._line_len + 4) + "\r")
            sys.stdout.flush()
            self._line_len = 0
        except (ValueError, OSError):
            pass

    def _start(self) -> None:
        if not self._tty or self._running:
            return
        # v0.4.20：rich Live（思考滚动框）活动期间**禁止**裸 ANSI 转圈——
        # Live 独占终端渲染，裸 \r\x1b[2K 会与之冲突（用户实测：刷出一屏
        # "[2Kaxon ❯ …"字面序列）。关闭思考框后看门狗会自动恢复转圈。
        if getattr(self, "_live_active", False):
            return
        self._evt.clear()
        self._t0 = time.monotonic()
        self._running = True
        self._line_len = 0
        # 双行模式：先占一行给状态行（输入行由首帧写出），使提示符"一直存在"
        self._two_line = bool(_VT_OK)
        if self._two_line:
            try:
                sys.stdout.write("\n")
                sys.stdout.flush()
            except (ValueError, OSError):
                self._two_line = False
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def _stop(self) -> None:
        if not self._running:
            return
        self._running = False
        self._evt.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    # ---- 状态协议（AgentLoop / CodebuddyRunner 调用） ----
    def thinking(self, note: str = "") -> None:
        """等待模型期间：转圈 + 已耗时 + 轮换操作概要（消除"假死"感）。"""
        self.bump()
        self._notes = ([note] if note else
                       ["正在分析请求…", "梳理上下文与进度…", "规划下一步…",
                        "仔细推敲中…"])
        self._msg = self._notes[0]
        self._start()

    def bump(self) -> None:
        """记录一次活动（文本/工具/思考）——看门狗据此判断是否"静默太久"。"""
        self._last_event = time.monotonic()

    def start_watchdog(self) -> None:
        """空闲看门狗（v0.4.5，用户实测"输出后停住不转圈"）。

        首个文本输出会停掉转圈；此后若 SDK 迟迟无事件（模型思考/网络等待），
        界面就"停着不动"。看门狗每隔 0.4s 检查：距上次活动 > 1.2s 且当前无
        转圈 → 重新启动（消息"等待模型响应…"），下次事件到来即停。
        """
        if not self._tty or self._watchdog is not None:
            return
        self._watching = True
        self._last_event = time.monotonic()

        def _loop() -> None:
            while self._watching:
                time.sleep(0.4)
                if not self._watching:
                    return
                idle = time.monotonic() - self._last_event
                if idle > 1.2 and not self._running:
                    self.thinking("等待模型响应…")

        self._watchdog = threading.Thread(target=_loop, daemon=True,
                                          name="axon-watchdog")
        self._watchdog.start()

    def stop_watchdog(self) -> None:
        self._watching = False
        self._watchdog = None

    def text(self) -> None:
        self._stop()

    # ---- 流式限行（v0.4.16，用户实测"思考/决策霸屏"）----
    THINK_MAX_LINES = 4          # 思考流最多显示行数（超出静默累积）
    TEXT_MAX_LINES = 30          # 正文流最多显示行数（超出静默累积）

    def _stream_line_count(self, text: str) -> int:
        return str(text).count("\n")

    def _note_stream_capped(self, kind: str, hidden: int) -> None:
        """流式内容超限的折叠提示（verbose 关闭时）；开启则补打隐藏内容。"""
        dim = "\x1b[2m"
        if self.verbose:
            return                  # 详细模式：内容已全显，无需提示
        sys.stdout.write(f"{dim}  …（后续 {hidden} 行未显示，"
                         f"Ctrl+O 或 /verbose on 展开详细模式；全文已入会话记录）\x1b[0m")
        sys.stdout.flush()

    def stream_write(self, text: str) -> None:
        self.bump()
        self._close_thinking()
        """模型流式文本直写（记录未换行状态，供面板/页脚前补换行）。

        v0.4.16：正文流超过 TEXT_MAX_LINES 行的部分静默累积（防霸屏），
        超限时打一行折叠提示；verbose 模式不受限。
        """
        self._stop()
        self._text_full = getattr(self, "_text_full", "") + str(text)
        if not self.verbose:
            shown = getattr(self, "_text_shown", 0)
            newlines = self._stream_line_count(text)
            if shown >= self.TEXT_MAX_LINES:
                self._text_hidden = getattr(self, "_text_hidden", 0) + newlines
                self.pending_line = True
                if not getattr(self, "_text_capped_noted", False):
                    self._text_capped_noted = True       # 首次超限打一次提示
                    self._note_stream_capped("text", self._text_hidden)
                return
            if shown + newlines > self.TEXT_MAX_LINES:
                # 本次会越过上限：只写到上限处，其余累积
                keep = self.TEXT_MAX_LINES - shown
                parts = str(text).split("\n")
                head = "\n".join(parts[:keep + 1])
                sys.stdout.write(head)
                sys.stdout.flush()
                self._text_shown = shown + keep
                self._text_hidden = getattr(self, "_text_hidden", 0) + (newlines - keep)
                self._note_stream_capped("text", self._text_hidden)
                self.pending_line = True
                return
            self._text_shown = shown + newlines
        sys.stdout.write(text)
        sys.stdout.flush()
        self.pending_line = True

    def thinking_write(self, text: str) -> None:
        """思考**增量**流式写入（v0.4.12）：行内追加，遇换行才换行并补前缀。

        与 thinking_line（整块）分工：SSE thinking_delta 逐字到达走这里，
        避免"每字一行"刷屏；文本/工具输出前由 _close_thinking() 收尾。

        v0.4.17（用户定稿）：**滚动窗口 + 边框**——不截断、不隐藏，
        固定高度滚动区（SCROLL_N 行）始终显示最新内容（1-6 行 → 2-7 行推进），
        历史内容随框滚出屏幕，既"全部流出来过"又不霸屏；框样式仿沙箱面板。
        非 TTY（重定向/管道）退化为直接全显（无 ANSI 重绘）。
        """
        self.bump()
        self._stop()
        if not text:
            return
        if not self._tty or self.console is None:
            # 无 rich 控制台（非 TTY / 纯 stdout）：直接全显（无法原地重绘）
            if not self._thinking_open:
                sys.stdout.write("💭 深度思考\n")
                self._thinking_open = True
            sys.stdout.write(str(text))
            sys.stdout.flush()
            self.pending_line = True
            return
        if not self._thinking_open:
            self._thinking_begin()
        # 累积 → 按换行切成行 → 只保留尾部 SCROLL_N 行 → rich Live 原地刷新
        self._scroll_buf = getattr(self, "_scroll_buf", "") + str(text)
        lines = self._scroll_buf.split("\n")
        self._scroll_buf = lines.pop() if lines else ""
        if lines:
            self._scroll_lines = (getattr(self, "_scroll_lines", []) + lines)[-self.SCROLL_N:]
        self._scroll_redraw()

    # ---- 滚动区（v0.4.17；v0.4.19 改 rich Live 渲染，ANSI 手写重绘在真实终端错位）----
    SCROLL_N = 6                  # 滚动区可见行数

    def _render_panel(self):
        """构造滚动窗口的 rich 面板（高度固定，宽度由 rich 按终端管理）。"""
        from rich.panel import Panel
        from rich.text import Text
        lines = list(getattr(self, "_scroll_lines", []))
        tail = getattr(self, "_scroll_buf", "")
        if tail:
            lines = (lines + [tail])[-self.SCROLL_N:]
        while len(lines) < self.SCROLL_N:
            lines.insert(0, "")
        lines = lines[-self.SCROLL_N:]
        body = Text("\n".join(lines), style="dim")
        # v0.4.23：Live 期间的插嘴回显直接显示在面板底部（即时反馈，不被吞）
        pending = getattr(self, "_pending_echo", None)
        if pending:
            body.append("\n" + pending[-1][0], style=pending[-1][1])
        return Panel(body, title="💭 深度思考", title_align="left",
                     border_style="dim", height=self.SCROLL_N + 2, padding=(0, 1))

    def _thinking_begin(self) -> None:
        """打开思考滚动框（rich Live，原地刷新，跨平台可靠）。"""
        self._stop()
        self.ensure_newline()
        self._scroll_lines = []
        self._scroll_buf = ""
        live = None
        try:
            from rich.live import Live
            live = Live(self._render_panel(), console=self.console,
                        refresh_per_second=8, transient=False)
            live.__enter__()          # 手动管理生命周期（跨多次 write 调用）
        except Exception:             # noqa: BLE001
            live = None
        self._live = live
        if live is None:
            self._print("💭 深度思考", style="dim")     # 降级：无 Live 则直接输出
        else:
            self._live_active = True                     # v0.4.20：独占终端
        self._thinking_open = True
        self.pending_line = False

    def _scroll_redraw(self, force: bool = False) -> None:
        """刷新滚动窗口（rich Live 负责光标与重绘）。"""
        live = getattr(self, "_live", None)
        if live is None:
            return
        try:
            live.update(self._render_panel(), refresh=True)
        except Exception:             # noqa: BLE001
            pass

    def _close_thinking(self) -> None:
        """结束思考块：关闭 rich Live（内容定格保留在屏幕上）。"""
        if getattr(self, "_thinking_open", False):
            live = getattr(self, "_live", None)
            if live is not None:
                try:
                    live.update(self._render_panel(), refresh=True)
                    live.__exit__(None, None, None)    # 非 transient：内容保留 ✓
                except Exception:                      # noqa: BLE001
                    pass
            elif not self._tty or self.console is None:
                sys.stdout.write("\x1b[0m\n")
                sys.stdout.flush()
            self._live = None
            self._live_active = False                    # v0.4.20：释放终端
            self._thinking_open = False
            # v0.4.23：补打 Live 期间排队的回显（插嘴提交确认等，防被吞）
            pending = getattr(self, "_pending_echo", None)
            if pending:
                self._pending_echo = []
                for msg, style in pending:
                    self._print(msg, style=style)
            self._scroll_area = 0
            self._scroll_lines = []
            self._scroll_buf = ""
            self.pending_line = False
        self._think_shown = 0            # 兼容旧计数（v0.4.16）
        self._think_hidden = 0
        self._think_capped_noted = False
        self._thinking_full = ""

    def thinking_line(self, text: str) -> None:
        """模型思考/决策过程（整块模式，v0.4.11）：dim + 💭 前缀，多行逐行加前缀。

        过长时保留首尾、中间省略；流式增量请用 thinking_write。
        """
        self.bump()
        self._stop()
        self._close_thinking()
        body = str(text).strip()
        if not body:
            return
        lines = body.splitlines() or [body]
        if len(lines) > 8:
            lines = lines[:6] + [f"…（中间省略 {len(lines) - 7} 行）…"] + lines[-1:]
        dim, reset = "\x1b[2m", "\x1b[0m"
        for ln in lines[:9]:
            sys.stdout.write(f"  {dim}💭 {ln.strip()[:220]}{reset}\n")
        sys.stdout.flush()
        self.pending_line = False

    def ensure_newline(self) -> None:
        """若当前行有未换行的流式输出，补一个换行（防面板/提示错位）。"""
        if self.pending_line:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self.pending_line = False

    def tool(self, name: str, target: str = "") -> None:
        """工具行动线：连续同类只打一行（v0.2.3 降噪，避免几十条重复刷屏）。"""
        self._stop()
        self.ensure_newline()
        if name == self._last_tool:
            self._last_tool_count += 1
            if not self._running:          # v0.3.0 修复：合并分支曾让转圈永久停下
                self._start()
            return
        self._flush_tool_run()
        self._last_tool = name
        self._last_tool_count = 1
        self.bump()
        tgt = f"  {target}" if target else ""
        self._close_thinking()
        self._print(f"⚙ 运行 · {name}{tgt}", style="cyan")
        # 工具执行期间同样转圈计时（长命令/慢工具不再像卡死）。
        # v0.4.12：两条轮播（"执行 xxx…" / "等待执行结果…"）合并为一条——
        # 用户指出它们表达的是同一件事（都在等执行结果）。
        self._notes = [f"正在执行 {name}，请稍等..."]
        self._msg = self._notes[0]
        self._start()

    def _flush_tool_run(self) -> None:
        if self._last_tool_count >= 3:
            self._print(f"   ↳ 同类调用共 {self._last_tool_count} 次",
                        style="dim")
        self._last_tool = ""
        self._last_tool_count = 0

    def tool_result(self, name: str, ok: bool, detail: str = "") -> None:
        """成功静默（降噪，**转圈继续**——下一个工具/后续等待仍需反馈）；
        失败才停转圈并显示 ✗ 与原因。v0.3.0：原实现在每次成功结果后停转圈，
        而连续同类工具走"合并"分支不再启动 → 长任务中途再无任何动效。"""
        if ok:
            # v0.4.13：成功也给一行简短反馈（模块化可分辨），detail 折叠
            self.ensure_newline()
            first = ""
            if detail:
                first = str(detail).splitlines()[0][:70]
            line = f"✓ 完成 · {name}" + (f" → {first}" if first else "")
            self._print(line, style="dim")
            return
        self._stop()
        self.ensure_newline()
        self._flush_tool_run()
        line = f"✗ 失败 · {name}"
        if detail:
            line += f" — {detail}"
        self._print(line, style="yellow")

    def block(self, lines, head: int = 3) -> list:
        """多行内容折叠（v0.4.13，用户要求"太长可展开/收缩"）。

        终端**无法回删已打印的行**，因此采用"打印前折叠 + 详细模式开关"：
        verbose 关闭且行数 > head → 只保留前 2 行 + 省略提示；
        按 Ctrl+O 或 /verbose on 切换详细模式后全显。
        """
        lines = [str(x) for x in lines]
        if self.verbose or len(lines) <= head:
            return lines
        return lines[:2] + [f"  …（共 {len(lines)} 行，Ctrl+O 或 /verbose on 展开）"]

    def set_verbose(self, on: bool) -> None:
        self.verbose = bool(on)

    def note(self, text: str) -> None:
        self._stop()
        for ln in self.block(str(text).splitlines()):
            self._print(ln, style="dim")

    def idle(self) -> None:
        self._stop()
        if self._last_tool:
            self.ensure_newline()
            self._flush_tool_run()

    def _print(self, text: str, style: str = "") -> None:
        if self.console is not None:
            self.console.print(text, markup=False, highlight=False, style=style)
        else:
            sys.stdout.write(text + "\n")
            sys.stdout.flush()


# ---------------------------------------------------------------- 审批器

class TTYPrompter:
    """交互式 TTY 审批（人在回路 🔴 档），红框面板。

    v0.4.14：底层 CLI **并发执行**同一条消息里的多个 tool_use（已从 CLI changelog
    实证），因此多个 ask 审批可能**同时到达**。若无锁，两个审批面板会交错打印、
    两个 input() 互相消费输入——用户对 A 的 "y" 可能答到 B 上（误批准危险操作）。
    故 confirm() 全程持互斥锁：**并发审批串行化**（一个问完再问下一个）。
    """

    def __init__(self, console, status=None):
        self.console = console
        self.status = status
        self._lock = threading.RLock()

    def confirm(self, action: Action, decision: Decision) -> Approval:
        with self._lock:                        # v0.4.14：并发审批串行化（防串话/误批准）
            return self._confirm_locked(action, decision)

    def _confirm_locked(self, action: Action, decision: Decision) -> Approval:
        if self.status is not None:
            self.status.idle()
            self.status.ensure_newline()   # 流式文本未换行时先断行，避免面板错位
        # v0.4.19：审批期间**暂停键盘采集与看门狗**——
        # ① InterjectReader（msvcrt）会先吃掉用户的 y/n → input() 永远等不到 → 卡死；
        # ② 看门狗会重启"等待模型响应…"与审批面板并存（语义混乱）。
        kb = getattr(self.status, "keyboard", None) if self.status is not None else None
        if kb is not None:
            kb.pause()
        if self.status is not None:
            self.status.stop_watchdog()
        try:
            return self._confirm_input(action, decision)
        finally:
            if kb is not None:
                kb.resume()
            if self.status is not None:
                self.status.start_watchdog()

    def _confirm_input(self, action: Action, decision: Decision) -> Approval:
        """实际打印面板并读取 y/n（在暂停采集/看门狗的保护下执行）。"""
        if self.console is not None:
            from rich.panel import Panel
            from rich.text import Text
            body = Text(decision.reason + "\n", style="bold")
            body.append("类型: ", style="dim")
            body.append(f"{action.kind}\n")
            body.append("目标: ", style="dim")
            body.append(f"{action.target[:200]}\n")
            body.append("规则: ", style="dim")
            body.append(decision.rule)
            hi = "⚠ " if getattr(decision, "severity", "") == "high" else ""
            if hi:
                body = ("⚠ 高危操作：路径涉及盘符根目录/系统目录，请务必核对该命令！\n"
                        + body)
            self.console.print(Panel(body, title=f"[沙箱确认{hi}]", border_style="red",
                                     padding=(0, 1)))
        else:  # pragma: no cover
            print(f"[沙箱确认] {decision.reason}\n  类型: {action.kind}\n"
                  f"  目标: {action.target[:200]}\n  规则: {decision.rule}")
        try:
            ans = input("  执行？[y]es / [n]o / [a]lways(本会话记住此模式): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return Approval(False)
        if ans in ("y", "yes"):
            return Approval(True)
        if ans in ("a", "always"):
            return Approval(True, remember_session=True)
        return Approval(False)


class AutoDenyPrompter:
    """非 TTY 环境 fail-closed：一律拒绝。"""

    def __init__(self, console=None, status=None):
        self.console = console

    def confirm(self, action: Action, decision: Decision) -> Approval:
        msg = (f"[沙箱] 非交互环境，拒绝执行: {action.kind} {action.target[:80]}"
               f"（{decision.rule}）")
        if self.console is not None:
            self.console.print(msg, style="yellow", markup=False)
        else:
            print(msg, file=sys.stderr)
        return Approval(False)


class AutoApprovePrompter:
    """测试替身：一律放行。"""

    def confirm(self, action: Action, decision: Decision) -> Approval:
        return Approval(True)


# ---------------------------------------------------------------- 装配

def build_app(cfg, prompter=None, status=None):
    store = SessionStore(connect(cfg.db_path))
    audit = AuditLog(store.conn)
    memory_store = MemoryStore(cfg.memory_path, cfg.context.memory_max_tokens,
                               cfg.memory.max_entries)
    # v0.4.21：系统级长期记忆（跨项目）——首次自动播种；注入顺序：长期 > 项目
    from axon.memory.global_store import GlobalMemoryStore
    global_memory = GlobalMemoryStore(cfg.memory.long_term_path())
    global_memory.ensure_seeded()
    registry = build_builtin_registry(ToolContext(cfg.workspace, cfg, memory_store,
                                                  global_memory=global_memory))

    hooks = register_builtin_hooks(cfg, audit)
    gateway = SandboxGateway(cfg, hooks, audit, prompter or TTYPrompter(_make_console()))

    # MCP 连接器（可选；失败不阻塞启动）
    mcp_errors: list[str] = []
    for sv in cfg.mcp.servers:
        if not sv.enabled:
            continue
        try:
            conn = MCPConnector(sv, gateway)
            conn.connect()
            for spec in conn.tool_specs():
                registry.register(spec)
        except (MCPUnavailable, Exception) as e:  # noqa: BLE001 - 启动不阻塞
            mcp_errors.append(f"MCP {sv.name}: {e}")

    ctx_builder = ContextBuilder(cfg, memory_store, registry)
    compactor = Compactor(cfg)
    verifier = TurnVerifier(store, audit, cfg)
    router = ModelRouter(cfg)
    provider = router.get(gateway=gateway, registry=registry,
                          prompter=gateway.prompter, store=store,
                          memory_store=memory_store)
    journal = Journal(memory_store, cfg) if cfg.memory.auto_journal else None
    loop = AgentLoop(provider, gateway, registry, ctx_builder, compactor,
                     verifier, store, cfg, status=status, journal=journal)
    return {
        "cfg": cfg, "store": store, "audit": audit, "memory": memory_store,
        "pending_resume_note": None, "keyboard": None,
        "registry": registry, "gateway": gateway, "ctx": ctx_builder,
        "compactor": compactor, "verifier": verifier, "provider": provider,
        "loop": loop, "mcp_errors": mcp_errors, "console": _make_console(),
    }


# ---------------------------------------------------------------- REPL

def _stream_write(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def _fmt_duration(sec: float) -> str:
    """秒 → 人类可读时长（v0.4.22）。"""
    total = int(sec)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h} 小时 {m} 分 {s} 秒"
    return f"{m} 分 {s} 秒" if m else f"{s} 秒"


def _print_turn_result(console, r: TurnResult, elapsed: float | None = None) -> None:
    reason_cn = {
        "verified": "已完成（V 层验证通过）",
        "max_turns": f"达到轮次上限（{r.turns_used}）",
        "budget_exhausted": "会话 token 预算耗尽",
        "user_interrupt": "已被用户中断",
        "stuck_break": "检测到原地打转，已中断",
        "provider_error": "模型调用失败",
        "verification_failed": "V 层验证未通过",
    }.get(r.stop_reason, r.stop_reason)
    line = f"── {reason_cn} · 轮次 {r.turns_used} · token {r.tokens_used}"
    if elapsed is not None:
        line += f" · 用时 {_fmt_duration(elapsed)}"      # v0.4.22：本回合用时（用户要求）
    if console is not None:
        console.print(line, style="dim", markup=False)
        if r.note:
            console.print(f"── {r.note}", style="yellow", markup=False)
            if r.stop_reason == "provider_error":
                console.print("── 排查建议：确认模型提供商可用（codebuddy 路检查 "
                              "CODEBUDDY_API_KEY / CODEBUDDY_INTERNET_ENVIRONMENT 与网络）；"
                              "或临时切换 openai 路（--provider openai）", style="dim",
                              markup=False)
        if r.products:
            console.print("── 本轮产物：", style="bold cyan", markup=False)
            for p in r.products:
                size = p.get("size")
                size_s = (f"{size / 1048576:.2f} MB" if isinstance(size, int) and size >= 1048576
                          else f"{size / 1024:.1f} KB" if isinstance(size, int)
                          else "?")
                tag = "下载" if p.get("kind") == "download" else "文件"
                if p.get("archived"):
                    tag += "→项目"
                console.print(f"   [{tag}] {p['path']}  ({size_s})",
                              style="cyan", markup=False)
        acc = getattr(r, "acceptance", ()) or ()
        if acc:
            _ok, acc_lines = acc
            style = "green" if _ok else "yellow"
            console.print("── 完成度自检：", style=style, markup=False)
            for ln in acc_lines[:4]:
                console.print(f"   {ln}", style=style, markup=False)
        changes = [e for e in r.side_effects
                   if e.get("kind") in ("fs_write", "fs_edit", "fs_delete")]
        if changes:
            agg: dict[tuple, int] = {}
            order: list[tuple] = []
            for e in changes:
                key = (e["kind"], " ".join(str(e["target"]).split()), e.get("result"))
                if key not in agg:
                    agg[key] = 0
                    order.append(key)
                agg[key] += 1
            console.print("── 本会话已变更的文件（去重）：", style="dim", markup=False)
            for kind, target, result in order[-10:]:
                shown = target if len(target) <= 68 else target[:65] + "..."
                n = agg[(kind, target, result)]
                suffix = f" ×{n}" if n > 1 else ""
                console.print(f"   {kind.replace('fs_', '')}  {shown}{suffix}  ({result})",
                              style="dim", markup=False)
    else:  # pragma: no cover
        print(line)
        if r.note:
            print(f"── {r.note}")


def _compose_user_input(app: dict, text: str) -> str:
    """resume 观察消息（<resume_context>）只在恢复后的首轮注入模型（设计 §4.4）。"""
    note = app.pop("pending_resume_note", None)
    if not note:
        return text
    return f"{note}\n\n---\n{text}"


def _run_turn(app: dict, session, text: str, on_stuck, images: list | None = None):
    """执行一个回合：流式输出 + 状态反馈 + 执行中插嘴采集（v0.2.6）。"""
    t_turn = time.monotonic()                    # v0.4.22：本回合用时（用户要求）
    console = app["console"]
    loop = app["loop"]
    text = _compose_user_input(app, text)
    kb = app.get("keyboard")
    # v0.4.16：每回合重置正文流式计数（防霸屏的限行按回合计）
    st = loop.status
    st._text_shown = 0
    st._text_hidden = 0
    st._text_capped_noted = False
    st._text_full = ""
    if kb is not None and kb.enabled and kb._thread is None:
        kb.start()
    app["loop"].status.start_watchdog()      # v0.4.5：静默期自动转圈
    show_thinking = bool(getattr(app["cfg"].agent, "show_thinking", True))
    interject = app.get("interject_bridge")
    if interject is not None and getattr(app["cfg"].agent, "stream_interject", True):
        import asyncio
        try:
            interject.bind(asyncio.get_running_loop())
        except RuntimeError:
            interject = None
    else:
        interject = None
    try:
        result = loop.run(session, text, on_text=app["loop"].status.stream_write,
                          on_stuck=on_stuck, images=images or [],
                          on_thinking=(loop.status.thinking_write
                                       if show_thinking else None),
                          interject=interject)
    finally:
        if interject is not None:
            interject.reset()
        app["loop"].status.stop_watchdog()
        if kb is not None:
            kb.stop()
        left = kb.current_input() if kb is not None else ""
        if left:                       # v0.3.0：未提交的插嘴不再"凭空消失"
            if console is not None:
                console.print(f"   ⌨ 插嘴（未提交，可重打）：{left}",
                              style="dim", markup=False)
            else:  # pragma: no cover
                print(f"   ⌨ 插嘴（未提交）：{left}")
    sys.stdout.write("\n")
    sys.stdout.flush()
    _print_turn_result(console, result, elapsed=time.monotonic() - t_turn)
    return result


def _settle_interjections(app: dict, session) -> None:
    """回合结束的插嘴兜底（v0.4.15）。

    - `stream_interject` 开启（默认）：插嘴已在执行中即时注入，这里只处理
      **未入队的残留**（bridge 未绑定/绑定失败时键盘线程 put 不进去的内容）；
    - 关闭时退回旧行为：弹确认 → 作为新一轮注入。
    """
    kb = app.get("keyboard")
    if kb is None:
        return
    console = app.get("console")
    stream_on = bool(getattr(app["cfg"].agent, "stream_interject", True))
    if stream_on:
        # 执行中注入模式下，queue 已被 provider 消费；残留仅来自未提交缓冲
        # （_run_turn 的 finally 已提示），此处无需再弹确认
        return
    while kb.has_pending():
        text = kb.pop_all()
        if console is not None:
            console.print(f"── 你在任务执行中输入：{text}", style="yellow", markup=False)
        else:  # pragma: no cover
            print(f"── 你在任务执行中输入：{text}")
        try:
            ans = input("  作为补充发送给智能体？[Y/n]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            if console is not None:
                console.print("  已保留，未发送", style="dim")
            return
        if ans in ("", "y", "yes"):
            _run_turn(app, session, wrap_interjection(text), _ask_continue)
        else:
            if console is not None:
                console.print("  已丢弃", style="dim")


def run_repair_mode(cfg, console) -> int:
    """`--repair-db`：自检 + 修复（v0.4.3）。

    流程：quick_check → 健康则退出；损坏则**备份坏库三件套 → 用官方 schema 重建 →
    逐行搬迁 → 校验 → 替换**（备份保留在同目录 quarantine/）。
    """
    from axon.repair import check_integrity, repair_database
    from axon.session.db import SCHEMA, connect

    def say(text, style: str = "") -> None:
        if console is not None:
            console.print(str(text), style=style, markup=False)
        else:  # pragma: no cover
            print(text)

    db = cfg.db_path
    say(f"数据库: {db}")
    if not db.exists():
        say("数据库不存在（首次运行会自动创建）", style="yellow")
        return 0
    con = connect(db)
    healthy, detail = check_integrity(con)
    try:
        con.close()
    except Exception:
        pass
    if healthy:
        say("✓ 完整性检查通过，无需修复", style="green")
        from axon.config import is_cloud_synced
        if is_cloud_synced(db):
            say("⚠ 数据目录位于云同步路径，建议迁移到本地目录（否则易损坏）",
                style="yellow")
        return 0
    say(f"✗ 检测到损坏: {detail[:200]}", style="yellow")
    say("开始修复（原库会先备份，只搬不删）…")
    result = repair_database(db, SCHEMA)
    if not result.get("ok"):
        say(f"修复失败: {result.get('note')}", style="red")
        say(f"备份与半成品已保留：{result.get('backup')} / {result.get('repaired')}")
        return 1
    say("✓ 修复完成", style="green")
    say(f"  会话 {result.get('sessions')} · 消息 {result.get('messages')} · "
        f"压缩记录 {result.get('compaction_events')} · 审计 {result.get('audit_log')}")
    say(f"  原库备份: {result.get('backup')}")
    return 0


def run_delproj_mode(cfg, console, target: str, with_files: bool = False) -> int:
    """`--delproj` 命令行模式（v0.2.8，用户要求）：只依赖会话库，不初始化模型。

    好处：无需 API key、无需进入 REPL、**在项目目录内也能删项目**
    （不会因为"当前项目"而被自身上下文卡住）。
    """
    from axon.sandbox.audit import AuditLog
    from axon.session.db import SessionStore, connect

    store = SessionStore(connect(cfg.db_path))
    audit = AuditLog(store.conn)
    app = {"store": store, "cfg": cfg, "audit": audit, "console": console}

    def out(text, style: str = "") -> None:
        if console is not None:
            console.print(str(text), style=style, markup=False)
        else:  # pragma: no cover
            print(text)

    arg = "" if target in ("", "__list__") else str(target)
    if with_files and arg and "--with-files" not in arg:
        arg = f"{arg} --with-files"
    _cmd_delproj(app, None, arg, out)
    return 0


def _cmd_delproj(app: dict, session, arg: str, out) -> object | None:
    """`/delproj`：删除项目（v0.2.7）。破坏性操作 → 列表 / 强确认 / 回收站。

    用法：`/delproj`（列表）、`/delproj <项目目录|项目名|session前缀> [--with-files]`
    安全约定：① 默认只删**会话数据**；② 必须手输项目名确认；③ `--with-files`
    把项目目录移入回收站（二次确认），永不直接删除；④ 程序目录/盘根/用户根拒绝。
    """
    import os as _os
    from pathlib import Path as _P

    store = app["store"]
    cfg = app["cfg"]
    projects = store.list_projects()

    if not arg or arg == "--list":
        if not projects:
            out("(没有项目记录)")
            return session
        out("项目列表（用 --delproj <序号|名称|目录|session前缀> 删除；默认只删会话数据）：")
        for i, pj in enumerate(projects, 1):
            out(f"  {i:>2}. {pj['name']:<14} 会话 {pj['sessions']:>2} · 消息 "
                f"{pj['messages']:>5} · 最近 {str(pj['last_updated'])[:16]}  "
                f"[{pj['sid_prefix']}]  {pj['project_dir']}")
        return session

    with_files = "--with-files" in arg
    token = arg.replace("--with-files", "").strip().strip('"').strip("'")
    if not token:
        out("用法: /delproj <项目目录|项目名|session前缀> [--with-files]")
        return session

    # ---- 匹配项目 ----
    target = None
    low = token.lower()
    if token.isdigit():                      # 按列表序号（同名项目时更方便）
        idx = int(token)
        if 1 <= idx <= len(projects):
            target = projects[idx - 1]
        # 超出范围 → 不报错：session 前缀可能恰好全是数字（v0.4.3 修复），继续走前缀匹配
    if target is None and (_os.sep in token or "/" in token or ":" in token):
        want = str(_P(token).resolve())
        for pj in projects:
            if pj["project_dir"].lower() == want.lower():
                target = pj
                break
    if target is None:
        for pj in projects:
            if pj["name"].lower() == low:
                target = pj
                break
    if target is None:
        for pj in projects:
            if pj["sid_prefix"].lower() == low:
                target = pj
                break
    if target is None:
        cands = [pj for pj in projects
                 if pj["name"].lower().startswith(low) or low in pj["project_dir"].lower()]
        if len(cands) == 1:
            target = cands[0]
        elif len(cands) > 1:
            out("匹配到多个项目，请用更精确的名称/目录：", style="yellow")
            for pj in cands:
                out(f"   {pj['name']}  {pj['project_dir']}")
            return session
    if target is None:
        out(f"未找到项目: {token}（/delproj 查看列表）", style="yellow")
        return session

    name = target["name"]
    is_current = _os.path.normcase(target["project_dir"]) == _os.path.normcase(
        str(cfg.project))
    out(f"目标项目：{name}")
    out(f"  目录：{target['project_dir']}")
    out(f"  会话 {target['sessions']} 个 · 消息 {target['messages']} 条")
    if is_current:
        out("  ⚠ 这是**当前项目**：删除后当前会话即消失，需要重启。", style="yellow")
    out("  默认只删除会话数据（数据库记录），磁盘文件不受影响；"
        "加 --with-files 会把项目目录移入回收站。", style="dim")
    out("  此操作不可撤销（会话数据无备份）。", style="yellow")

    try:
        typed = input(f"  请输入项目名「{name}」以确认删除: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        out("已取消", style="dim")
        return session
    if typed != name:
        out("名称不匹配，已取消", style="yellow")
        return session

    stats = store.delete_project_data(target["project_dir"])
    try:
        app["audit"].record(
            __import__("axon.sandbox.gateway", fromlist=["Action"]).Action(
                kind="fs_delete", target=target["project_dir"],
                detail={"scope": "project_sessions", "stats": stats},
                session_id=getattr(session, "id", "") or "", turn=0),
            "allow", "project_delete:data", "ok")
    except Exception:
        pass
    out(f"已删除会话数据：会话 {stats['sessions']} · 消息 {stats['messages']} · "
        f"压缩记录 {stats['events']} · 审计 {stats['audit']}")

    # ---- 可选：项目目录移入回收站（二次确认） ----
    if with_files:
        pdir = _P(target["project_dir"])
        from axon.config import program_dir, program_root
        guards = {str(_P(_P.cwd().anchor).resolve()), str(_P.home()),
                  str(program_dir()), str(program_root())}
        wp = _P(cfg.workspace).resolve()
        if str(pdir.resolve()) in guards or pdir.resolve() == wp \
                or pdir.resolve() in wp.parents or wp in pdir.resolve().parents:
            out("拒绝：目标是盘根/用户根/程序目录/工作空间，不能移入回收站", style="red")
            return session
        try:
            typed2 = input(
                f"  再次确认：把 {pdir} **整个目录**移入回收站？输入 DELETE 继续: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            out("已取消（会话数据已删除）", style="dim")
            return session
        if typed2 != "DELETE":
            out("已取消文件删除（会话数据已删除）", style="dim")
            return session
        from axon.sandbox.backup import move_to_trash
        moved = move_to_trash([str(pdir)], cfg.trash_dir, cfg.workspace)
        out(f"已移入回收站：{moved[0] if moved else '(未移动)'}")

    if is_current:
        out("当前项目已被删除，请重启 AXON（或 --project 指定其他项目）。", style="yellow")
        return None
    return session


def _mark_ended(app: dict, session) -> None:
    """正常退出标记（v0.2.9：用于下次启动识别"异常结束"的会话）。"""
    try:
        app["store"].set_status(getattr(session, "id", ""), "ended")
    except Exception:
        pass


def _replay_history(app: dict, session, limit: int = 14) -> None:
    """resume 后把最近的历史对话展示出来（v0.2.5，用户反馈"只看到智能体输出"）。

    展示：用户提问 + 智能体叙述（截断）+ 工具调用（紧凑）；观察消息（<...>）跳过。
    """
    console = app["console"]
    store = app["store"]
    try:
        msgs = store.load_messages(session.id)
    except Exception:
        return
    items = [m for m in msgs if m.role in ("user", "assistant")]
    if not items:
        return
    shown = items[-limit:]

    def _flat(s: str, n: int) -> str:
        s = " ".join(str(s).split())
        return s if len(s) <= n else s[: n - 1] + "…"

    if console is not None:
        from rich.text import Text
        console.print(f"── 历史对话（最近 {len(shown)} 条）", style="dim")
        for m in shown:
            if m.role == "user":
                text = (m.content or "").strip()
                if not text or text.startswith("<"):
                    continue
                console.print(Text(f"你 ❯ {_flat(text, 160)}", style="bold cyan"))
            else:
                if (m.content or "").strip():
                    console.print(Text(f"axon ❯ {_flat(m.content, 300)}", style="green"))
                for tc in (m.tool_calls or []):
                    name = tc.name[len("mcp__axon__"):] \
                        if tc.name.startswith("mcp__axon__") else tc.name
                    console.print(Text(f"   ⚙ {name}", style="dim"))
        if not any(m.role == "user" and (m.content or "").strip()
                   and not str(m.content).startswith("<") for m in shown):
            console.print("   （该会话未记录提问文本——旧版本创建的会话无此数据；"
                          "任务摘要见下方 <resume_context>）", style="dim")
            mem = app.get("memory")
            if mem is not None:                 # 从项目记忆里捞任务线索（v0.3.0）
                try:
                    hints = [n for n in mem.project_notes() if "未完成" in n
                             or "任务" in n or "需求" in n]
                except Exception:
                    hints = []
                for h in hints[-2:]:
                    console.print(f"   （项目记忆）{_flat(h, 150)}", style="dim")
        console.print("── 以上为历史，输入新指令继续（/help 查看命令）", style="dim")
    else:  # pragma: no cover
        for m in shown:
            if m.role == "user" and (m.content or "").strip() \
                    and not str(m.content).startswith("<"):
                print(f"你 ❯ {_flat(m.content, 160)}")
            elif m.role == "assistant" and (m.content or "").strip():
                print(f"axon ❯ {_flat(m.content, 300)}")


def _recover_stale_sessions(app: dict, current) -> None:
    """启动检测：上次窗口被直接关闭（未走收尾）→ 补沉淀项目记忆（v0.2.9）。

    用户问："不用 /exit 或 Ctrl+D 而是直接关窗口，还会沉淀记忆吗？"——原实现不会
    （进程被终止，收尾逻辑没机会跑）。现改为**下次启动时补做**：正常退出会把会话
    标记为 ended；残留 active 且超过 10 分钟的会话即视为异常结束。
    """
    cfg = app["cfg"]
    if not getattr(cfg.memory, "recover_prompt", True):
        return
    store = app["store"]
    console = app.get("console")
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc)
              - timedelta(minutes=10)).isoformat(timespec="milliseconds")
    try:
        stale = [s for s in store.list_stale_sessions(str(cfg.project), cutoff)
                 if s.id != getattr(current, "id", None)]
    except Exception:
        return
    for s in stale[:3]:
        try:
            n = len(store.load_messages(s.id))
        except Exception:
            n = 0
        if n < cfg.memory.summary_min_messages:
            store.set_status(s.id, "ended")      # 内容太少 → 无需沉淀，直接归档
            continue
        msg = (f"── 检测到会话 {s.id[:8]}（{n} 条消息）上次**未正常结束**"
               f"（窗口被直接关闭？）。是否现在补沉淀项目记忆？[Y/n]")
        if console is not None:
            console.print(msg, style="yellow", markup=False)
        else:  # pragma: no cover
            print(msg)
        try:
            ans = input("  补沉淀？[Y/n]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if ans in ("", "y", "yes"):
            if console is not None:
                console.print("── 正在补沉淀项目记忆…", style="dim", markup=False)
            try:
                _run_turn(app, s, SUMMARY_PROMPT, _ask_continue)
            except Exception as e:  # noqa: BLE001 - 失败不阻塞启动
                if console is not None:
                    console.print(f"── 补沉淀失败: {type(e).__name__}: {e}",
                                  style="yellow", markup=False)
        store.set_status(s.id, "ended")


def _maybe_summarize(app: dict, session) -> None:
    """会话结束沉淀项目记忆（v0.2.3，用户审定）。"""
    cfg = app["cfg"]
    if not cfg.memory.auto_summary_on_exit:
        return
    try:
        msgs = app["store"].load_messages(session.id)
    except Exception:
        return
    if len(msgs) < cfg.memory.summary_min_messages:
        return
    last_user = next((m for m in reversed(msgs) if m.role == "user"), None)
    if last_user is not None and "沉淀「项目记忆」" in (last_user.content or ""):
        return                      # 刚刚总结过，避免重复
    console = app.get("console")
    if console is not None:
        console.print("── 正在沉淀项目记忆（会话总结）…", style="dim", markup=False)
    else:  # pragma: no cover
        print("── 正在沉淀项目记忆（会话总结）…")
    try:
        _run_turn(app, session, SUMMARY_PROMPT, _ask_continue)
    except Exception as e:  # noqa: BLE001 - 总结失败不阻塞退出
        if console is not None:
            console.print(f"── 项目记忆沉淀失败（不影响退出）: {type(e).__name__}: {e}",
                          style="yellow", markup=False)


def _ask_continue() -> bool:
    try:
        ans = input("检测到原地打转，继续尝试？[y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return ans in ("y", "yes")


SLASH_COMMANDS = [
    ("/help", "显示帮助"),
    ("/save", "立即沉淀项目记忆"),
    ("/attach", "附加图片/文件（读剪贴板或路径）"),
    ("/sessions", "列出最近会话（跨项目）"),
    ("/resume", "恢复会话"),
    ("/memory", "查看三层记忆"),
    ("/audit", "查看/导出审计日志"),
    ("/verbose", "详细模式（长内容全显/折叠）"),
    ("/compact", "手动压缩上下文"),
    ("/delproj", "删除项目"),
    ("/exit", "退出"),
]

_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv",
              "dist", "build", ".idea", ".vscode", ".pytest_cache"}
# 允许被 `>` 引用补全的点目录（v0.4.10：智能体工作区也允许引用——
# 下载物/生成物/粘贴的图片都在里面；但**排序靠后**，项目资产优先）
_ALLOW_DOT_DIRS = {".workspace"}


def scan_project_entries(project, limit: int = 15, max_depth: int = 3) -> list[str]:
    """扫描项目内**文件与文件夹**（供 `>` 补全；v0.4.9 起支持目录）。

    目录以 `/` 结尾；排除版本库/依赖目录与隐藏项，但**保留 `.workspace`**
    （v0.4.10：智能体工作区里的下载物/生成物/粘贴图片同样可被引用）；
    排序：项目资产优先，工作区内容靠后。
    """
    root = Path(project)
    out: list[str] = []

    def _ws_weight(p: Path) -> int:
        try:
            return 1 if ".workspace" in p.relative_to(root).parts else 0
        except ValueError:
            return 0

    def walk(d: Path, depth: int) -> None:
        if len(out) >= limit:
            return
        try:
            entries = sorted(d.iterdir(),
                             key=lambda p: (_ws_weight(p), not p.is_dir(), p.name.lower()))
        except OSError:
            return
        for e in entries:
            if len(out) >= limit:
                return
            name = e.name
            if name in _SKIP_DIRS:
                continue
            if name.startswith(".") and name not in _ALLOW_DOT_DIRS:
                continue
            if e.is_dir():
                out.append(e.relative_to(root).as_posix() + "/")
                if depth < max_depth:
                    walk(e, depth + 1)
            elif e.is_file():
                out.append(e.relative_to(root).as_posix())

    walk(root, 0)
    return out[:limit]


def scan_project_files(project, limit: int = 15, max_depth: int = 3) -> list[str]:
    """兼容别名：只返回文件（v0.4.8 行为）。"""
    return [e for e in scan_project_entries(project, limit * 3, max_depth)
            if not e.endswith("/")][:limit]


def _make_completers(state: dict):
    """补全器（v0.4.8）：`/` → 斜杠命令；行首 `>` → 项目文件引用（≤15 个）。"""
    from prompt_toolkit.completion import Completer, Completion

    class SlashCompleter(Completer):
        def get_completions(self, document, complete_event):
            text = document.text_before_cursor
            if not text.startswith("/") or " " in text:
                return
            for name, _desc in SLASH_COMMANDS:
                if name.startswith(text):
                    yield Completion(name, start_position=-len(text), display=name)

    class RefCompleter(Completer):
        _CACHE_TTL = 5.0

        def get_completions(self, document, complete_event):
            text = document.text_before_cursor
            if not text.startswith(">"):
                return
            typed = text[1:].strip().lstrip("./")
            entries = state.get("project_entries")
            stamp = time.monotonic()
            if entries is None or stamp - state.get("files_ts", 0) > self._CACHE_TTL:
                entries = scan_project_entries(state.get("project", "."))
                state["project_entries"] = entries
                state["files_ts"] = stamp
            for entry in entries:                       # 目录带尾 `/`，可直接续拼子路径
                rel = entry.rstrip("/")
                if not typed or typed.lower() in rel.lower():
                    yield Completion(">" + entry, start_position=-len(text),
                                     display=">" + entry)

    class MergedCompleter(Completer):
        def __init__(self, *subs):
            self.subs = subs

        def get_completions(self, document, complete_event):
            for sub in self.subs:
                yield from sub.get_completions(document, complete_event)

    return MergedCompleter(SlashCompleter(), RefCompleter())


def _paste_from_clipboard(state: dict, event) -> None:
    """Ctrl+V 粘贴处理（v0.4.8）：读剪贴板 → 收编 → **引用插入输入行** + toast 提示。

    与旧实现的区别：只在**粘贴动作**发生时触发（不是后台轮询截图）；
    提示走底部工具栏（toast），不再直接 print —— 那会破坏 prompt_toolkit 的输入行。
    """
    watcher = state.get("clipwatch")
    if watcher is None:
        return
    pairs = watcher.poll_once()
    if pairs:
        watcher.pending.extend(pairs)
        refs = " ".join(ref for ref, _ in pairs) + " "
        event.current_buffer.insert_text(refs)      # 引用进输入行，可继续打字
        state["toast"] = "已收编 " + "、".join(Path(p).name for _, p in pairs)
    else:
        state["toast"] = "剪贴板中没有图片/文件"
    event.app.invalidate()


def _make_prompt_session(state: dict):
    """prompt_toolkit 提示符 + 底部状态栏 + 补全 + 粘贴键（CodeBuddy 风格）。"""
    if not sys.stdin.isatty():
        return None
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.formatted_text import HTML
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.styles import Style
    except ImportError:  # pragma: no cover
        return None
    style = Style.from_dict({
        "p": "bold ansicyan",
        "a": "bold ansigreen",
        "tb": "bg:#1f1f1f #aaaaaa",
    })

    def toolbar():
        toast = state.get("toast") or ""
        base = (f" <b>{state['project_name']}</b> · {state['model']} · "
                f"{state['workspace']} · 会话 {state['session'][:8]} · "
                "💬 执行中可直接输入补充（回车提交） · "
                "/help · Ctrl+C 中断 · Ctrl+D 退出 ")
        if toast:
            base = f" <b>{toast}</b> " + base
        return HTML(base)

    # ---- 收编键（v0.4.9）：Ctrl+G / Alt+V / F2 / Ctrl+V ----
    # Windows Terminal 会**接管** Ctrl+V 与右键粘贴（直接粘贴文本，应用收不到事件），
    # 因此额外提供终端不吞的键；/attach 命令始终可用。
    kb = KeyBindings()

    @kb.add("c-v", eager=True)
    @kb.add("c-g", eager=True)
    @kb.add("escape", "v", eager=True)
    @kb.add("f2", eager=True)
    def _paste(event) -> None:
        _paste_from_clipboard(state, event)

    @kb.add("c-o", eager=True)
    def _toggle_verbose(event) -> None:
        """Ctrl+O 切换详细模式（v0.4.13）：关闭时 >3 行的块折叠。"""
        st = state.get("status")
        if st is None:
            return
        st.set_verbose(not st.verbose)
        state["toast"] = ("详细模式：开（长内容全显）" if st.verbose
                          else "详细模式：关（长内容折叠）")
        event.app.invalidate()

    return PromptSession(message=HTML("<p>axon</p> <a>❯</a> "), style=style,
                         bottom_toolbar=toolbar, reserve_space_for_menu=0,
                         completer=_make_completers(state), key_bindings=kb,
                         complete_while_typing=True)


def repl(app: dict, session, resume_note: str | None = None) -> int:
    cfg = app["cfg"]
    console = app["console"]
    store, loop = app["store"], app["loop"]
    status = loop.status

    if console is not None:
        from rich.panel import Panel
        from rich.text import Text
        body = Text()
        body.append("项目     ", style="dim")
        body.append(f"{cfg.project}\n")
        body.append("工作空间 ", style="dim")
        body.append(f"{cfg.workspace}\n")
        body.append("模型     ", style="dim")
        body.append(f"{session.provider}/{session.model}\n")
        body.append("会话     ", style="dim")
        body.append(session.id[:8])
        console.print(Panel(body, title=f"AXON v{__version__}",
                            border_style="cyan", padding=(0, 2)))
        console.print("输入 /help 查看命令；Ctrl+C 中断当前回合；Ctrl+D 退出。",
                      style="dim", markup=False)
        console.print("执行中可直接打字补充（回车提交），回合结束后会请你确认是否发送。",
                      style="dim", markup=False)
        console.print("提示：输入 / 补全命令；输入 > 补全并引用**项目内文件/文件夹**；",
                      style="dim", markup=False)
        console.print("      粘贴图片/文件请按 Ctrl+G（或 Alt+V）收编——Ctrl+V 在部分终端被系统接管。",
                      style="dim", markup=False)
    else:  # pragma: no cover
        print(f"AXON v{__version__} · 项目 {cfg.project}")
        print(f"工作空间 {cfg.workspace} · 模型 {session.provider}/{session.model} · "
              f"会话 {session.id[:8]}")
    for err in app["mcp_errors"]:
        (console.print(f"[警告] {err}", style="yellow", markup=False) if console
         else print(f"[警告] {err}"))
    if resume_note is not None:
        note_text = getattr(resume_note, "content", str(resume_note))
        app["pending_resume_note"] = note_text
        _replay_history(app, session)      # 先展示历史对话（含用户提问）
        if console is not None:
            console.print("── <resume_context> 观察消息将在本轮注入模型：",
                          style="dim", markup=False)
            console.print(note_text, style="dim", markup=False)
        else:  # pragma: no cover
            print(note_text)

    state = {"project_name": cfg.project.name, "model": f"{session.provider}/{session.model}",
             "workspace": str(cfg.workspace), "session": session.id,
             "project": str(cfg.project), "toast": "", "status": status}

    _recover_stale_sessions(app, session)     # 补沉淀上次异常结束的会话（v0.2.9）

    # 剪贴板（v0.4.8 用户纠正）：**只监听粘贴动作（Ctrl+V）**，不再后台轮询
    # ——截图/复制不等于要贴给 Axon，后台监听还会打断输入行。收编逻辑复用
    # ClipboardWatcher.poll_once()（由 Ctrl+V 键绑定与 /attach 触发）；
    # 确需后台监听可在 config.yml 设 tools.clipboard_watch: true。
    clipwatch = attachments.ClipboardWatcher(cfg.project, on_ingest=None)
    if getattr(cfg.tools, "clipboard_watch", False):
        clipwatch.start()
    state["clipwatch"] = clipwatch
    app["clipwatch"] = clipwatch
    ps = _make_prompt_session(state)
    session_ref = [session]      # /resume 可能换会话 → 退出总结用最新会话
    pending_attach: list = []    # 待发送附件（v0.2.5）
    while True:
        try:
            line = (ps.prompt() if ps is not None else input("axon> ")).strip()
        except EOFError:
            print()
            _maybe_summarize(app, session_ref[0])
            _mark_ended(app, session_ref[0])
            return 0
        except KeyboardInterrupt:
            print("\n(已取消本次输入，输入 /exit 退出)")
            continue
        # 剪贴板由后台 ClipboardWatcher **实时监测**（v0.4.5，用户要求）：
        # 用户一截图/复制文件就被收编到 <项目>/tmp 并生成 `>引用`，
        # 不再依赖"回车时探测"（旧实现会误报"没有图片/文件"）。
        if line and not line.startswith("/"):
            line, refs = attachments.parse_input(line, cfg.workspace)
            for r in refs:
                if r not in pending_attach:
                    pending_attach.append(r)
        if not line:
            if pending_attach:
                line = "（请查看附件）"
            else:
                continue
        if line.startswith("/"):
            session = handle_slash(app, session, line, state, ps)
            if session is None:
                _maybe_summarize(app, session_ref[0])
                _mark_ended(app, session_ref[0])
                return 0
            session_ref[0] = session
            continue
        # ① 后台实时收编的粘贴（引用在前，模型先看到附件）
        watcher = app.get("clipwatch")
        if watcher is not None:
            for ref, _path in watcher.take_pending():
                if ref not in line:
                    line = f"{ref}\n{line}"
        # ② 输入里手写/粘贴进来的 `>引用` → 解析为真实路径清单
        line, refs = attachments.parse_references(line, cfg.project)
        images: list = []
        if refs:
            from axon import vision as _vision
            images = [path for _ref, path in refs if _vision.is_image(path)]
            ref_lines = [f"- {ref} → {path}" + ("（目录）" if Path(path).is_dir() else "")
                         for ref, path in refs]
            folded = app["loop"].status.block(ref_lines)
            note = "\n\n[用户附件]\n" + "\n".join(folded)
            if images and _vision.vision_available(cfg, session_ref[0].model):
                note += "\n（其中的图片已随消息附带，可直接查看内容）"
            line += note
        if pending_attach:
            line += attachments.build_attachment_note(pending_attach)
            pending_attach = []
        try:
            _run_turn(app, session_ref[0], line, _ask_continue, images=images)
        except KeyboardInterrupt:
            status.idle()
            print("\n(回合被中断)")
        status.idle()
        _settle_interjections(app, session_ref[0])


def handle_slash(app: dict, session, line: str, state: dict, ps) -> object | None:
    """处理斜杠命令；返回 None 表示退出 REPL，否则返回（可能更新的）session。"""
    parts = line.split(maxsplit=2)
    cmd = parts[0]
    console = app["console"]
    store, audit, memory = app["store"], app["audit"], app["memory"]

    def out(text: str, style: str = "") -> None:
        if console is not None:
            console.print(text, style=style, markup=False, highlight=False)
        else:  # pragma: no cover
            print(text)

    if cmd in ("/exit", "/quit"):
        return None
    if cmd == "/help":
        out(HELP_TEXT)
    elif cmd == "/sessions":
        for item in store.list_sessions_all(30):
            s = item["session"]
            mark = "*" if s.id == session.id else " "
            out(f"{mark} {s.id[:8]}  {s.updated_at[:16]}  {item['project_name']:<12} "
                f"{s.provider}/{s.model}  {item['msg_count']:>3}条  "
                f"{(item['title'] or '(无标题)')[:36]}")
    elif cmd == "/resume":
        arg = parts[1] if len(parts) > 1 else "__pick__"
        if arg == "__pick__":
            out("最近的会话（/resume <id前缀> 恢复；跨项目会话请重启并用 --resume）：")
            for item in store.list_sessions_all(15):
                s = item["session"]
                out(f"  {s.id[:8]}  {s.updated_at[:16]}  {item['project_name']:<12} "
                    f"{(item['title'] or '(无标题)')[:36]}")
            return session
        try:
            sess, _, note = load_for_resume(store, arg)
        except ValueError as e:
            out(str(e), style="yellow")
            return session
        if sess is None:
            out("没有可恢复的会话", style="yellow")
            return session
        _mark_ended(app, session)          # 切走的会话视为正常结束（v0.2.9）
        if sess.project_dir != str(app["cfg"].project):
            out(f"该会话属于项目 {sess.project_dir}；请重启并用 "
                f"--resume {sess.id[:8]} 恢复（将自动切换项目）", style="yellow")
            return session
        session.__dict__.update(sess.__dict__)
        app["gateway"].bind(session.id, 0)
        state.update({"model": f"{session.provider}/{session.model}",
                      "session": session.id})
        out(note)
    elif cmd == "/delproj":
        return _cmd_delproj(app, session, line[len(cmd):].strip(), out)
    elif cmd == "/attach":
        cfg = app["cfg"]
        arg = line[len(cmd):].strip()
        if arg:
            srcs = [Path(a) for a in arg.split() if Path(a).is_file()]
            pairs = attachments.ingest(srcs, cfg.project) if srcs else []
            out("已附加: " + "、".join(ref for ref, _ in pairs) if pairs
                else "未找到可附加的文件（支持图片/任意文件路径）")
            if pairs:
                watcher = app.get("clipwatch")
                if watcher is not None:
                    watcher.pending.extend(pairs)
        else:
            watcher = app.get("clipwatch")
            pairs = watcher.poll_once() if watcher is not None else []
            if pairs:
                watcher.pending.extend(pairs)
                state["toast"] = "已收编 " + "、".join(Path(p).name for _, p in pairs)
                out("已从剪贴板收编: " + "、".join(ref for ref, _ in pairs))
            else:
                state["toast"] = "剪贴板中没有图片/文件"
                out("剪贴板中没有图片/文件（或缺少 Pillow）")
    elif cmd == "/verbose":
        arg = line[len(cmd):].strip().lower()
        st = app["loop"].status
        if arg in ("on", "1", "true", "开"):
            st.set_verbose(True)
        elif arg in ("off", "0", "false", "关"):
            st.set_verbose(False)
        else:
            st.set_verbose(not st.verbose)
        out(f"详细模式：{'开（长内容全显）' if st.verbose else '关（>3 行的块折叠，Ctrl+O 切换）'}")
    elif cmd == "/save":
        extra = line[len(cmd):].strip()
        prompt = SUMMARY_PROMPT + (f"\n\n补充说明：{extra}" if extra else "")
        r = _run_turn(app, session, prompt, _ask_continue)
        if r.products or r.stop_reason == "verified":
            out("已沉淀项目记忆 → " + str(app["cfg"].memory_path), style="dim")
    elif cmd == "/compact":
        if hasattr(app["loop"].provider, "run_turn"):
            out("codebuddy 路：上下文由 SDK 自管（自动压缩），/compact 不适用")
        else:
            app["compactor"].compact(
                session.id, store.load_messages(session.id),
                app["loop"]._summarize, store)
            out("已执行结构化压缩")
    elif cmd == "/memory":
        entries = memory.entries()
        notes = memory.project_notes()
        journals = memory.journal_entries()
        out(f"长期记忆 {len(entries)} 条 · 项目记忆 {len(notes)} 条 · 进度日志 {len(journals)} 条"
            f"（{memory.path}）")
        if notes:
            out("── 项目记忆（最近 3 条）:")
            for n in notes[-3:]:
                first = [ln for ln in n.splitlines() if ln.strip()][:1]
                out("   " + (first[0][:90] if first else "(空)"))
    elif cmd == "/audit":
        rest = line[len(cmd):].strip()
        if rest.startswith("--export"):
            fmt, _, path = rest[len("--export"):].strip().partition(" ")
            fmt = fmt or "csv"
            path = path or f"./audit-{session.id[:8]}.{fmt}"
            n = audit.export(fmt, path, session.id)
            out(f"已导出 {n} 条审计记录 → {path}")
        else:
            rows = audit.rows(session.id)
            for r in rows[-20:]:
                out(f"  {r['ts'][11:19]} [{r['kind']}] {r['verdict']:<12} "
                    f"{r['rule']:<28} {r['target'][:60]}")
            out(f"(共 {len(rows)} 条，显示最近 20 条)")
    else:
        out(f"未知命令: {cmd}（/help 查看可用命令）", style="yellow")
    return session


# ---------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="axon", description="AXON - single-user CLI agent harness")
    parser.add_argument("--config", help="config.yml 路径（默认 ./config.yml）")
    parser.add_argument("--project", help="项目目录（覆盖配置）；工作空间默认为 <项目>/.workspace")
    parser.add_argument("--workspace", help="工作空间目录（覆盖配置，默认 <项目>/.workspace）")
    parser.add_argument("--provider", choices=["codebuddy", "openai"],
                        help="模型接入路（覆盖配置）")
    parser.add_argument("--model", help="模型名（覆盖配置）")
    parser.add_argument("--repair-db", action="store_true",
                        help=("检测并修复会话数据库（损坏时自动备份坏库并尽力重建；"
                              "不需要 API key）"))
    parser.add_argument("--delproj", nargs="?", const="__list__", default=None,
                        metavar="项目",
                        help=("删除项目（匹配目录/名称/会话前缀；不带值列出项目）。"
                              "不需要 API key、不进 REPL，可在任意目录运行；"
                              "默认只删会话数据，配 --with-files 把项目目录移入回收站"))
    parser.add_argument("--with-files", action="store_true",
                        help="配合 --delproj：把项目目录移入回收站（需二次确认）")
    parser.add_argument("--resume", nargs="?", const="__pick__", default=None,
                        help="恢复会话：last / session_id 前缀 / 不带值则列出选择（跨项目生效）")
    parser.add_argument("--max-turns", type=int, help="单回合最大轮次（覆盖配置）")
    parser.add_argument("-p", "--prompt", help="非交互单发模式：执行一句话任务后退出")
    parser.add_argument("--version", action="version", version=f"axon {__version__}")
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config, overrides={
            "workspace": args.workspace, "project": args.project,
            "active_provider": args.provider, "model": args.model,
            "max_turns": args.max_turns,
        })
        ensure_dirs(cfg)   # workspace/project/临时目录必须存在
    except (FileNotFoundError, ValueError) as e:
        print(f"[配置错误] {e}", file=sys.stderr)
        return 2

    console = _make_console()
    status = StatusLine(console)

    # --repair-db：最优先处理——库坏了也要能修（v0.4.3）
    if args.repair_db:
        return run_repair_mode(cfg, console)

    # --delproj：最优先处理——不加载模型、不需要 key、不进 REPL（v0.2.8）
    if args.delproj is not None:
        return run_delproj_mode(cfg, console, args.delproj, args.with_files)

    # 会话恢复前置（v0.2.0：sessions.db 程序级全局共享，支持跨项目恢复）
    resume_note = None
    session = None
    if args.resume:
        try:
            pre_store = SessionStore(connect(cfg.db_path))
            session, _, resume_note = load_for_resume(pre_store, args.resume)
        except ValueError as e:
            print(f"[提示] {e}", file=sys.stderr)
            session = None
        if session is not None and session.project_dir != str(cfg.project):
            # 跨项目恢复：切换项目上下文（workspace 未显式配置时跟随项目）
            cfg.project = Path(session.project_dir)
            if not cfg.workspace_explicit:
                cfg.workspace = (cfg.project / ".workspace").resolve()
            ensure_dirs(cfg)
            print(f"[提示] 已切换到项目 {cfg.project}", file=sys.stderr)
    if args.resume == "__pick__" and session is None:
        pre_store = SessionStore(connect(cfg.db_path))
        items = pre_store.list_sessions_all(15)
        if not items:
            print("[提示] 没有历史会话", file=sys.stderr)
            return 2
        print("最近的会话（输入编号恢复，回车取消）：")
        for i, item in enumerate(items, 1):
            s = item["session"]
            print(f"  {i:>2}. {s.id[:8]}  {s.updated_at[:16]}  "
                  f"{item['project_name']:<12} {(item['title'] or '(无标题)')[:36]}")
        if sys.stdin.isatty():
            try:
                pick = input("编号/id 前缀: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 2
            if pick:
                arg = items[int(pick) - 1]["session"].id[:8] if pick.isdigit()                     and 1 <= int(pick) <= len(items) else pick
                try:
                    session, _, resume_note = load_for_resume(pre_store, arg)
                except ValueError as e:
                    print(f"[提示] {e}", file=sys.stderr)
                    return 2
                if session is not None and session.project_dir != str(cfg.project):
                    cfg.project = Path(session.project_dir)
                    if not cfg.workspace_explicit:
                        cfg.workspace = (cfg.project / ".workspace").resolve()
                    ensure_dirs(cfg)
                    print(f"[提示] 已切换到项目 {cfg.project}", file=sys.stderr)
        else:
            return 2

    try:
        app = build_app(cfg, prompter=TTYPrompter(console, status), status=status)
    except Exception as e:  # noqa: BLE001 - 启动失败如实报
        print(f"[启动失败] {type(e).__name__}: {e}", file=sys.stderr)
        if "API key" in str(e) or "KEY" in str(e):
            p = cfg.active_provider_cfg()
            print(f"  提示：请设置环境变量 {p.env_api_key}"
                  + (f" 和 {p.env_environment}" if p.env_environment else ""),
                  file=sys.stderr)
        return 2
    def _on_submit(text: str) -> None:
        # v0.4.15：独立一行回显；v0.4.23：**Live 期间排队防吞** + **送达失败可见**
        status = app["loop"].status
        msg = f"💬 [插嘴] 已提交（将在下一个动作前生效）：{text}"
        status.ensure_newline()
        warn = None
        bridge = app.get("interject_bridge")
        if bridge is None or not bridge.submit(text):
            warn = "⚠ 插嘴未能送达（任务可能刚结束）——请把它作为下一条消息发送"
        queued = getattr(status, "_pending_echo", None)
        if getattr(status, "_live_active", False):
            # 思考滚动框（rich Live）独占终端：直接 print 会被吞/破坏布局
            # → 排队 + 立即刷新面板（面板底部显示回显，v0.4.23）；
            # _close_thinking 时补打完整回显
            status._pending_echo = (queued or []) + [(msg, "green")]
            if warn:
                status._pending_echo.append((warn, "yellow"))
            try:
                status._scroll_redraw(force=True)
            except Exception:       # noqa: BLE001
                pass
            return
        if console is not None:
            console.print(msg, style="green", markup=False)
            if warn:
                console.print(warn, style="yellow", markup=False)
        else:  # pragma: no cover
            print(msg)
            if warn:
                print(warn)

    reader = InterjectReader(on_submit=_on_submit)
    app["keyboard"] = reader
    app["loop"].status.keyboard = reader
    app["interject_bridge"] = InterjectBridge()
    store = app["store"]
    if session is None:
        sid = store.create_session(str(cfg.project), cfg.model.active_provider,
                                   cfg.active_model())
        session = store.get_session(sid)
    app["gateway"].bind(session.id, 0)

    if args.prompt is not None:
        _run_turn(app, session, args.prompt, _ask_continue)
        status.idle()
        return 0

    return repl(app, session, resume_note)


if __name__ == "__main__":
    raise SystemExit(main())
