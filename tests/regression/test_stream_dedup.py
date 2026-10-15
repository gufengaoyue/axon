"""回归：流式文本去重（v0.2.4，用户实测"这些话总是重复"）+ shell 输出编码。"""
from __future__ import annotations

import pytest

from axon.providers.codebuddy import map_sdk_message


class _SE:
    def __init__(self, **kw):
        self.__class__.__name__ = "StreamEvent"
        for k, v in kw.items():
            setattr(self, k, v)


class _TB:
    pass


class _AM:
    def __init__(self, text):
        b = _TB()
        b.text = text
        self.content = [b]


_AM.__name__ = "AssistantMessage"
_TB.__name__ = "TextBlock"


def _p(text):
    return _SE(event={"type": "content_block_delta",
                      "delta": {"type": "text_delta", "text": text}})


def _run(events) -> str:
    state = {"partial": "", "seen_partial": False, "emitted": "",
             "last_full": "", "replay_src": "", "prev_stream": ""}
    return "".join(ev.text for e in events
                   for ev in map_sdk_message(e, state) if ev.kind == "text")


def test_incremental_partials_no_dup():
    assert _run([_p("你好"), _p("，世界"), _AM("你好，世界")]) == "你好，世界"


def test_cumulative_partials_no_dup():
    """累积型：每个事件携带"到目前为止的全量"（易造成重复的老坑）。"""
    assert _run([_p("你好"), _p("你好，世界"), _AM("你好，世界")]) == "你好，世界"


def test_stream_replayed_large_chunk():
    """整段文本再走一遍 partial 序列（用户截图场景）→ 只保留一份。"""
    s = "我先了解一下环境，同时开始搜集资料。"
    assert _run([_p(s), _AM(s), _p(s), _AM(s)]) == s


def test_stream_replayed_small_chunks():
    seq = [_p("这是"), _p("一段"), _p("测试")]
    assert _run(seq + [_AM("这是一段测试")] + seq + [_AM("这是一段测试")]) == "这是一段测试"


def test_partials_without_assistant_message():
    assert _run([_p("A"), _p("B")]) == "AB"


def test_legit_repetition_kept():
    """模型正当重复同一短语 → 必须保留（不能误杀）。"""
    out = _run([_p("好的。"), _p("好的。我来做"), _AM("好的。好的。我来做")])
    assert out == "好的。我来做好的。好的。我来做"


def test_new_message_sharing_prefix_compensated():
    """新消息与上一条同前缀 → 抑制后分叉时整段补发（不丢字）。"""
    out = _run([_p("好的。"), _p("我先做A"), _AM("好的。我先做A"),
                _p("好的。"), _p("这次改B"), _AM("好的。这次改B")])
    assert out == "好的。我先做A好的。这次改B"


def test_normal_two_turns():
    assert _run([_p("第一轮回答。"), _AM("第一轮回答。"),
                 _p("第二轮回答。"), _AM("第二轮回答。")]) == "第一轮回答。第二轮回答。"


def test_nested_stream_event_unwrapped():
    """事件多包一层（stream_event）→ 也能取到增量。"""
    ev = _SE(event={"type": "stream_event",
                    "event": {"type": "content_block_delta",
                              "delta": {"type": "text_delta", "text": "嵌套"}}})
    assert _run([ev, _AM("嵌套")]) == "嵌套"


# ---- shell 输出编码（subprocess text=True 的 GBK 线程解码崩溃） ----

def test_decode_output_utf8_then_fallback():
    from axon.tools.builtin.shell import decode_output
    assert decode_output("中文内容".encode("utf-8")) == "中文内容"
    # GBK 字节（Windows cmd 内建命令输出）→ 回退解码不崩
    assert decode_output("中文内容".encode("gbk")) == "中文内容"
    assert decode_output(b"") == ""
    # 混合/非法字节 → 不抛异常（errors=replace）
    assert isinstance(decode_output(b"\xff\xfe\xaf mixed"), str)


def test_shell_runs_chinese_output_without_crash(cfg, gw_approve, registry, ws):
    """子进程输出中文/UTF-8 字节时不再出现 _readerthread UnicodeDecodeError。"""
    (ws / "中文文件.txt").write_text("内容", encoding="utf-8")
    spec = registry.get("execute_shell")
    call = type("C", (), {"id": "t1", "name": "execute_shell"})()
    r = gw_approve.execute(call, spec, {"command": 'python -c "print(\'中文输出 OK\')"'})
    assert r.ok and "OK" in r.output
