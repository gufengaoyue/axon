"""execute_shell：执行 Shell 命令（cwd=工作空间；纯删除命令路由回收站）。"""
from __future__ import annotations

import locale
import os
import subprocess
from pathlib import Path

from axon.sandbox.gateway import Action
from axon.sandbox.risk import RiskClass
from axon.tools.context import ToolContext
from axon.tools.registry import ToolResult, ToolSpec, tool_result_err, tool_result_ok

PARAMETERS = {
    "type": "object",
    "properties": {
        "command": {"type": "string", "description": "要执行的 shell 命令串"},
        "timeout_sec": {"type": "integer", "description": "超时秒数（默认 60）", "default": 60},
    },
    "required": ["command"],
}


def decode_output(data: bytes) -> str:
    """子进程输出解码（v0.2.4 编码修复）。

    原实现用 subprocess(text=True) → Python 以**系统默认编码**（中文 Windows = GBK）
    在内部读取线程解码；子进程吐 UTF-8（中文文件名/UTF-8 文本）时线程抛
    UnicodeDecodeError 打到 stderr（用户看到的 "Exception in thread Thread-N
    (_readerthread)"）。改为 bytes 模式 + 依次尝试 utf-8 → 系统首选编码 → gbk，
    彻底避开 text 模式的线程解码路径。
    """
    if not data:
        return ""
    candidates = ["utf-8"]
    try:
        pref = locale.getpreferredencoding(False)
        if pref and pref.lower() not in ("utf-8", "utf8"):
            candidates.append(pref)
    except (LookupError, ValueError):
        pass
    for enc in candidates + ["gbk", "cp936"]:
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _child_env() -> dict:
    """给子进程注入 UTF-8 IO 环境（python/git 等输出统一 UTF-8，便于解码）。"""
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    return env


def make_spec(ctx: ToolContext) -> ToolSpec:
    def handler(call, args: dict, gw) -> ToolResult:
        command = args["command"]
        timeout = int(args.get("timeout_sec", ctx.config.agent.tool_timeout_sec))

        # 纯删除命令 → 回收站（需求：删除优先移回收站；批量超阈值已在 CommandRule 升红审批）
        import glob as _glob
        from axon.sandbox.backup import move_to_trash
        from axon.sandbox.destructive import parse_delete_command
        raw_paths = parse_delete_command(command)
        if raw_paths:
            # v0.4.29：glob 展开（`rm *.log` 旧实现把 '*.log' 当字面名 →
            # exists() False → "未找到目标"静默不删）+ 去引号已在上游做
            ws = Path(ctx.workspace)
            paths: list[str] = []
            for raw in raw_paths:
                if raw.startswith("~"):
                    raw = str(Path.home() / raw[2:]) if len(raw) > 2 \
                        else str(Path.home())
                p = Path(raw)
                if any(ch in raw for ch in "*?["):
                    pat = str(p if p.is_absolute() else ws / p)
                    paths.extend(_glob.glob(pat, recursive=True))
                else:
                    paths.append(str(p if p.is_absolute() else (ws / p)))
            moved = move_to_trash(paths, ctx.config.trash_dir, ctx.workspace)
            for p in moved:
                gw.note_subaction(Action(kind="fs_delete", target=p,
                                         detail={"via": "shell-trash"}, session_id=gw.session_id,
                                         turn=gw.turn))
            if moved:
                return tool_result_ok(
                    call, "已移入回收站:\n" + "\n".join(moved),
                    meta={"trashed": len(moved), "paths": moved})
            # v0.4.29：paths 非空但没移成 → 明确报错（旧实现一律"路径不存在"，
            # glob/带空格路径的真实删除被静默吞掉）
            return tool_result_err(
                call, "permanent",
                "删除目标未找到（路径不存在或 glob 未命中）: "
                + "、".join(raw_paths[:5]))

        try:
            proc = subprocess.run(
                command, shell=True, cwd=str(ctx.workspace),
                capture_output=True, timeout=timeout, env=_child_env())
        except subprocess.TimeoutExpired:
            return tool_result_err(call, "transient",
                                   f"命令超时（{timeout}s）已被终止；可重试或拆分命令")
        except OSError as e:
            return tool_result_err(call, "transient",
                                   f"命令启动失败: {type(e).__name__}: {e}")
        out = decode_output(proc.stdout or b"") + decode_output(proc.stderr or b"")
        if len(out) > 60000:
            out = out[:60000] + "\n...(输出超长已截断)"
        if proc.returncode != 0:
            return tool_result_err(call, "model",
                                   f"退出码 {proc.returncode}", output=out)
        return tool_result_ok(call, out or "(无输出)",
                              meta={"exit_code": proc.returncode, "command": command[:200]})

    def to_action(args: dict) -> Action:
        return Action(kind="shell", target=args["command"],
                      detail={"command": args["command"],
                              "timeout_sec": args.get("timeout_sec", 60)})

    return ToolSpec(
        name="execute_shell",
        description=("在工作空间目录执行 shell 命令，返回 stdout+stderr 与退出码。"
                     "高危命令（rm/mv/pip install/curl 等）与未识别命令会暂停等待用户确认；"
                     "rm/unlink/del 等纯删除命令会自动改走回收站而非真删。"
                     "退出码非 0 会作为错误观察返回，请修正后重试。"),
        parameters=PARAMETERS,
        handler=handler,
        to_action=to_action,
        risk=RiskClass.SHELL,
        mutating=True,
        parallel_safe=False,
    )
