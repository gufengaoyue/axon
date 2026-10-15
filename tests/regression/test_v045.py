"""回归：v0.4.5 四项修复（用户实测）。

1) 流式尾部重叠去重  2) echo 等只读  3) 空闲看门狗转圈
4) 剪贴板实时收编 + `>引用`（项目 tmp / 项目内原位 / 真实路径解析）
"""
from __future__ import annotations

import time
from pathlib import Path

from axon.providers.codebuddy import _dedup_tail_overlap


# ---- 1) 尾部重叠去重 ----

def test_dedup_full_repeat_suppressed():
    """整段重发（用户截图场景）→ 全部抑制。"""
    seg = ("Anthropic 的文章成功返回，内容很权威。我再补充抓取 MCP"
           "（agent 与外部连接的协议）和尝试下载几张配图（测试图片能力）：")
    emitted = "先并行抓取几类资料图片：" + seg
    assert _dedup_tail_overlap(emitted, seg) == ""


def test_dedup_partial_overlap_trims():
    old = "abc" * 30
    new = old[-40:] + "新内容继续"
    out = _dedup_tail_overlap(old, new)
    assert out == "新内容继续"


def test_dedup_short_repeat_kept():
    """短重复（<12 字符）不误杀——模型正当复述要保留。"""
    assert _dedup_tail_overlap("你好世界", "世界你好") == "世界你好"


def test_dedup_no_overlap_untouched():
    assert _dedup_tail_overlap("抓取失败。", "换几个来源") == "换几个来源"
    assert _dedup_tail_overlap("", "任意") == "任意"


# ---- 2) echo 等只读命令 ----

def test_echo_is_read_only(gw_approve, ws):
    """用户截图场景：`cd … && python -u script 2>&1 || echo "EXIT"`（脚本存在）。"""
    (ws / "verify.py").write_text("print('ok')\n", encoding="utf-8")
    from axon.sandbox.gateway import Action
    cmd = f'cd /d {ws} && python -u verify.py 2>&1 || echo "EXIT %errorlevel%"'
    d = gw_approve.check(Action(kind="shell", target=cmd))
    assert d.verdict == "allow", (d.verdict, d.rule)


def test_echo_alone_allowed(gw_approve):
    from axon.sandbox.gateway import Action
    for cmd in ('echo "EXIT 0"', "printf ok", "date /t"):
        d = gw_approve.check(Action(kind="shell", target=cmd))
        assert d.verdict == "allow", cmd


# ---- 3) 空闲看门狗 ----

def test_watchdog_restarts_spinner_after_idle(capsys):
    from axon.cli import StatusLine
    sl = StatusLine(console=None)
    sl._tty = True
    sl.start_watchdog()
    try:
        sl._last_event = time.monotonic() - 5.0     # 模拟静默 5 秒
        deadline = time.time() + 3.0
        while time.time() < deadline and not sl._running:
            time.sleep(0.1)
        assert sl._running is True                  # 看门狗把转圈重新拉起
        out = capsys.readouterr().out
        assert "等待模型响应" in out or sl._msg == "等待模型响应…"
    finally:
        sl.stop_watchdog()
        sl._stop()


def test_watchdog_not_started_when_not_tty():
    from axon.cli import StatusLine
    sl = StatusLine(console=None)
    sl._tty = False
    sl.start_watchdog()
    assert sl._watchdog is None


# ---- 4) 附件：收编与 `>引用` ----

def test_ingest_outside_project_copies_to_project_tmp(tmp_path):
    from axon import attachments
    proj = tmp_path / "proj"
    proj.mkdir()
    src = tmp_path / "shot.png"
    src.write_bytes(b"\x89PNG")
    pairs = attachments.ingest([src], proj)
    assert pairs, "应被收编"
    ref, path = pairs[0]
    assert ref == ">./tmp/图片1.png"                    # 引用格式（相对项目）
    assert Path(path) == (proj / "tmp" / "图片1.png").resolve()
    assert Path(path).exists()
    assert not src.exists() is False                    # 源文件保留（只复制不搬）


def test_ingest_inside_project_keeps_in_place(tmp_path):
    from axon import attachments
    proj = tmp_path / "proj"
    (proj / "sub").mkdir(parents=True)
    src = proj / "sub" / "harness.md"
    src.write_text("x", encoding="utf-8")
    pairs = attachments.ingest([src], proj)
    assert pairs and pairs[0][0] == ">./sub/harness.md"
    ref, path = pairs[0]
    assert Path(path) == src.resolve()
    assert not (proj / "tmp").exists()                  # 项目内 → 不复制


def test_ingest_root_file_reference(tmp_path):
    from axon import attachments
    proj = tmp_path / "proj"
    proj.mkdir()
    f = proj / "harness.md"
    f.write_text("x", encoding="utf-8")
    pairs = attachments.ingest([f], proj)
    assert pairs and pairs[0][0] == ">harness.md"        # 根下文件无 ./ 前缀


def test_parse_references_resolves_absolute_paths(tmp_path):
    from axon import attachments
    proj = tmp_path / "proj"
    (proj / "tmp").mkdir(parents=True)
    img = proj / "tmp" / "图片1.png"
    img.write_bytes(b"x")
    text, refs = attachments.parse_references(">./tmp/图片1.png\n请照这张图做网页", proj)
    assert refs and refs[0][1] == str(img.resolve())
    assert ">./tmp/图片1.png" not in text               # 引用行被移出正文
    assert "请照这张图做网页" in text


def test_parse_references_missing_kept(tmp_path):
    from axon import attachments
    text, refs = attachments.parse_references(">./tmp/none.png\n正文", tmp_path)
    assert not refs and ">./tmp/none.png" in text        # 不存在 → 原样保留


def test_watcher_take_pending(tmp_path):
    from axon import attachments
    watch = attachments.ClipboardWatcher(tmp_path, on_ingest=None)
    watch.pending.extend([(">./tmp/a.png", "X:/a.png")])
    got = watch.take_pending()
    assert got == [(">./tmp/a.png", "X:/a.png")]
    assert watch.take_pending() == []                    # 取走即清空
