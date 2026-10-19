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
    """执行期间的键盘采集器（非 TTY 或平台不支持时自动禁用）。

    v0.4.31 模态输入（用户需求）：旧实现"任何可打印字符都进 buffer、回车即提交"
    ——思考期间没有入口、入口随渲染重启消失、且容易误把正常输入当插嘴。

    新语义（两阶段）：
      IDLE    —— 只监听 Enter（唤起插嘴输入），其余按键**丢弃**；
      EDITING —— 字符入 buffer；Enter 提交（空白=取消）；Esc 取消；
                 Backspace 删除；扩展键（方向键等）吞掉。

    渲染解耦：`on_mode_change(mode)` / `on_render(text)` 由 CLI 注入，
    键盘线程在状态变化时同步调用（渲染层自行加锁）。
    """

    IDLE = "idle"
    EDITING = "editing"

    def __init__(self, on_submit=None):
        self.buffer: list[str] = []          # 正在输入的内容（供状态行回显）
        self.queue: list[str] = []           # 已提交、待确认的插嘴
        self.on_submit = on_submit           # 提交回调（CLI 用来即时提示，v0.3.0）
        self.on_mode_change = None           # v0.4.31：mode 进出回调（渲染层用）
        self.on_render = None                # v0.4.31：编辑内容变化回调（重绘输入行）
        self._mode = self.IDLE
        self._ext_key = False                # 扩展键第二字节待吞（\x00/\xe0 前缀）
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

    @property
    def mode(self) -> str:
        return self._mode

    def is_editing(self) -> bool:
        return self._mode == self.EDITING

    def start(self) -> None:
        if not self._enabled or self._thread is not None:
            return
        # 新回合从干净状态开始（上回合未提交的残留不续写）
        with self._lock:
            self.buffer.clear()
            self._mode = self.IDLE
            self._ext_key = False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="axon-interject")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t, self._thread = self._thread, None
        if t is not None:
            t.join(timeout=0.5)     # v0.4.29：等待线程真正退出（防 stop→start 并行 poll）
        # v0.4.31：退出编辑态但不回调渲染（终端即将由 status.idle() 收尾）；
        # **保留 buffer**——_run_turn 的 finally 要提示"未提交的插嘴"
        with self._lock:
            self._mode = self.IDLE
            self._ext_key = False

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
        """按键分发（v0.4.31 模态语义）。

        IDLE：Enter 唤起输入（进入 EDITING），其余按键丢弃。
        EDITING：Enter 提交（空白=取消）/ Esc 取消 / Backspace / 可打印字符。
        """
        enter = ch in ("\r", "\n")
        esc = ch == "\x1b"
        with self._lock:
            # 扩展键前缀（方向键/Insert/Home… 产生 \x00 或 \xe0 + 第二字节）：
            # 吞掉第二字节，避免把 'H'/'P' 之类当正文
            if self._ext_key:
                self._ext_key = False
                return
            if ch in ("\x00", "\xe0"):
                self._ext_key = True
                return

            if self._mode == self.IDLE:
                if enter:
                    self._mode = self.EDITING
                    self._ext_key = False
                    _notify = ("editing", "")
                else:
                    return               # IDLE 下不采集正文（防误输入）
            else:                        # EDITING
                if enter:
                    text = "".join(self.buffer).strip()
                    self.buffer.clear()
                    self._mode = self.IDLE
                    _submit = text or None       # 空白 → None（视为取消）
                    _notify = ("idle", text)
                elif esc:
                    self.buffer.clear()
                    self._mode = self.IDLE
                    _submit = None
                    _notify = ("idle", "")
                elif ch == "\x08":               # Backspace
                    if self.buffer:
                        self.buffer.pop()
                        _notify = ("render", "".join(self.buffer))
                    else:
                        return
                elif ch == "\x03":               # Ctrl+C 交给主线程（瞬断语义不变）
                    return
                elif ch == "\t":
                    return                       # 制表符忽略（单行输入）
                elif ch.isprintable():
                    if len("".join(self.buffer)) < _MAX_LEN:
                        self.buffer.append(ch)
                    _notify = ("render", "".join(self.buffer))
                else:
                    return

        # 出锁后再调外部回调（渲染会写 stdout，持锁调用易放大阻塞）
        if _notify[0] == "render":
            cb = self.on_render
            if cb is not None:
                try:
                    cb(_notify[1])
                except Exception:                # noqa: BLE001
                    pass
            return
        # 模式切换：先退出编辑态（渲染层清输入行、恢复转圈），再提交
        cb = self.on_mode_change
        if cb is not None:
            try:
                cb(_notify[0])
            except Exception:                    # noqa: BLE001
                pass
        if _notify[0] == "idle" and _submit:
            if self.on_submit is not None:
                try:
                    self.on_submit(_submit)
                except Exception:
                    # v0.4.30：回调异常不得静默丢——回退队列由
                    # _settle_interjections 兜底
                    self.queue.append(_submit)
            else:
                self.queue.append(_submit)

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
            "④ **否决式指令优先**：若内容语义是「停止/取消/别做了/算了」，视为\n"
            "   要求立即停止当前任务——先停下并**简要说明已完成的部分**，然后等待\n"
            "   用户下一步指令；**不要**把它解释成「满足某条件才取消」继续执行，\n"
            "   也不要为了完成计划而寻找替代路径绕过用户的否决。\n"
            f"内容：{text}\n"
            "</user_interjection>")


class InterjectBridge:
    """插嘴桥（v0.4.15）：键盘线程 → asyncio.Queue → prompt 流（执行中注入）。

    线程模型：`submit()` 在 InterjectReader 的键盘线程被调用（线程安全地
    `call_soon_threadsafe` 投递）；`get()` 在 provider 的 async 生成器里等待。
    未 bind（无事件循环）时 submit 静默失败——退化为"回合后确认"旧路径。

    v0.4.29 P1 修复：openai 路没有 asyncio 循环，`submit()` 永远失败 →
    插嘴文本永久丢失（用户看到"已提交"后内容消失）。现在 submit 失败时
    把文本存入**线程安全 fallback 列表**，openai 路在轮次边界 `take_all()`
    消费；codebuddy 路 bind 成功后走 asyncio.Queue。
    """

    def __init__(self):
        self.queue: "asyncio.Queue[str] | None" = None
        self._loop = None
        self._fallback: list[str] = []          # v0.4.29：无 loop 时的落点
        self._lock = threading.Lock()

    def bind(self, loop) -> None:
        import asyncio
        self._loop = loop
        self.queue = asyncio.Queue()

    def reset(self) -> None:
        self.queue = None
        self._loop = None
        with self._lock:
            self._fallback.clear()

    def reset_queue(self) -> None:
        """清 asyncio 队列——v0.4.30：**先排水到 fallback**，否则 codebuddy 路
        "最后一个消息边界之后、回合结束之前"到达的插嘴随队列对象丢弃
        （既不注入也不进确认）。fallback 由 _settle_interjections 兜底。"""
        q, self.queue = self.queue, None
        self._loop = None
        drained: list[str] = []
        if q is not None:
            try:
                while True:
                    drained.append(q.get_nowait())
            except Exception:       # noqa: BLE001 - QueueEmpty / 已关闭
                pass
        if drained:
            with self._lock:
                self._fallback[0:0] = drained

    def submit(self, text: str) -> bool:
        """键盘线程调用：把插嘴投递到队列（返回是否成功入队）。"""
        if self.queue is not None and self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(self.queue.put_nowait, text)
                return True
            except RuntimeError:
                pass
        # v0.4.29：无 loop（openai 路）/loop 已关闭 → 存 fallback 供轮次边界消费
        with self._lock:
            self._fallback.append(text)
        return True

    def take_all(self) -> list[str]:
        """取走并清空 fallback 残留（openai 路轮次边界消费用；v0.4.29）。"""
        with self._lock:
            items = list(self._fallback)
            self._fallback.clear()
        return items

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
