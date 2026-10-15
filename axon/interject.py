"""插嘴（interjection，v0.2.6，用户需求）。

场景：智能体正在执行任务（模型调用/工具执行），用户想补充内容。
约定：**执行中采集 → 回合结束后确认 → 注入下一轮**（用户审定）。

- 采集：守护线程轮询键盘（Windows msvcrt；POSIX select 降级），只在 TTY 且
  执行期间启用；字符进 buffer（回显交给 StatusLine 状态行，避免与流式输出打架），
  回车提交到队列（可多条）；Backspace 回退；Esc 清空。
- 确认：轮到主线程后由 CLI 弹出确认（回车=发送 / n=丢弃），满足"再次确认"。
- 注入：包装为 `<user_interjection>` 作为新一轮用户输入，模型据此调整决策或增加需求。
"""
from __future__ import annotations

import sys
import threading

_MAX_LEN = 2000


class InterjectReader:
    """执行期间的键盘采集器（非 TTY 或平台不支持时自动禁用）。"""

    def __init__(self, on_submit=None):
        self.buffer: list[str] = []          # 正在输入的内容（供状态行回显）
        self.queue: list[str] = []           # 已提交、待确认的插嘴
        self.on_submit = on_submit           # 提交回调（CLI 用来即时提示，v0.3.0）
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._enabled = bool(getattr(sys.stdin, "isatty", lambda: False)())
        self._lock = threading.Lock()
        self._paused = threading.Event()     # v0.4.19：审批期间暂停采集（防抢键盘）

    # ---- 暂停/恢复（v0.4.19）----
    def pause(self) -> None:
        """暂停键盘采集（审批 input() 期间调用）——避免抢走用户的 y/n。"""
        self._paused.set()

    def resume(self) -> None:
        """恢复键盘采集（审批结束后调用）。"""
        self._paused.clear()

    # ---- 生命周期 ----
    @property
    def enabled(self) -> bool:
        return self._enabled

    def start(self) -> None:
        if not self._enabled or self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="axon-interject")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t, self._thread = self._thread, None
        if t is not None:
            t.join(timeout=0.5)

    # ---- 采集循环 ----
    def _loop(self) -> None:
        try:
            import msvcrt                        # Windows
            while not self._stop.is_set():
                if self._paused.is_set():
                    # v0.4.19：审批（input）期间**必须暂停**——否则 msvcrt 先把
                    # 用户的 y/n 吃掉，审批的 input() 永远等不到 → 卡死（用户实测）
                    self._stop.wait(0.05)
                    continue
                if msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    self.feed(ch)
                else:
                    self._stop.wait(0.05)
            return
        except ImportError:
            pass
        try:                                     # POSIX 降级
            import select
            while not self._stop.is_set():
                r, _, _ = select.select([sys.stdin], [], [], 0.1)
                if r:
                    ch = sys.stdin.read(1)
                    if ch:
                        self.feed(ch)
        except Exception:
            return

    # ---- 字符处理（可单测，无需真实键盘） ----
    def feed(self, ch: str) -> None:
        with self._lock:
            if ch in ("\r", "\n"):
                text = "".join(self.buffer).strip()
                self.buffer.clear()
                if text:
                    self.queue.append(text[:_MAX_LEN])
                    if self.on_submit is not None:
                        try:
                            self.on_submit(text)
                        except Exception:
                            pass
                return
            if ch == "\x08":                     # Backspace
                if self.buffer:
                    self.buffer.pop()
                return
            if ch == "\x1b":                     # Esc → 清空
                self.buffer.clear()
                return
            if ch == "\x03":                     # Ctrl+C 交给主线程（瞬断语义不变）
                return
            if ch.isprintable():
                self.buffer.append(ch)

    # ---- 待确认队列 ----
    def has_pending(self) -> bool:
        with self._lock:
            return bool(self.queue)

    def pop_all(self) -> str:
        with self._lock:
            text = "\n".join(self.queue)
            self.queue.clear()
            return text

    def current_input(self) -> str:
        with self._lock:
            return "".join(self.buffer)


def wrap_interjection(text: str) -> str:
    """包装插嘴内容（v0.4.15：**执行中注入**——下一个动作前生效）。

    与旧版的区别：旧版是"回合结束后作为新一轮注入"；现在通过 prompt 的
    AsyncIterable 通道在**执行中** yield，SDK 会在当前回合的下一个决策点
    （工具调用之间）处理它。
    """
    return ("<user_interjection>\n"
            "用户在你的任务执行过程中补充了以下内容。请在**下一个动作之前**：\n"
            "① 用一句话明确确认你已收到并理解（复述关键点）；\n"
            "② 评估对当前计划的影响并**重新规划**：若影响方向则调整后续步骤，\n"
            "   若是新增需求则纳入计划；已完成的部分不要重复或回滚（除非用户明确要求）；\n"
            "③ 不要中断正在进行的单个工具调用，在本条消息之后的下一个决策点生效。\n"
            f"内容：{text}\n"
            "</user_interjection>")


class InterjectBridge:
    """插嘴桥（v0.4.15）：键盘线程 → asyncio.Queue → prompt 流（执行中注入）。

    线程模型：`submit()` 在 InterjectReader 的键盘线程被调用（线程安全地
    `call_soon_threadsafe` 投递）；`get()` 在 provider 的 async 生成器里等待。
    未 bind（无事件循环）时 submit 静默失败——退化为"回合后确认"旧路径。
    """

    def __init__(self):
        self.queue: "asyncio.Queue[str] | None" = None
        self._loop = None

    def bind(self, loop) -> None:
        import asyncio
        self._loop = loop
        self.queue = asyncio.Queue()

    def reset(self) -> None:
        self.queue = None
        self._loop = None

    def submit(self, text: str) -> bool:
        """键盘线程调用：把插嘴投递到队列（返回是否成功入队）。"""
        if self.queue is None or self._loop is None:
            return False
        try:
            self._loop.call_soon_threadsafe(self.queue.put_nowait, text)
            return True
        except RuntimeError:
            return False

    async def get(self, timeout: float = 0.4):
        """async 侧等待一条插嘴；超时/空返回 None（让出控制权，保持流活跃）。

        timeout=0 → 纯非阻塞（先走 get_nowait 快路径，避免 wait_for(0) 的
        立即超时竞态——v0.4.16 实测：即使队列有内容也取不到）。
        """
        import asyncio
        if self.queue is None:
            return None
        try:
            return self.queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        if timeout <= 0:
            return None
        try:
            return await asyncio.wait_for(self.queue.get(), timeout)
        except asyncio.TimeoutError:
            return None
