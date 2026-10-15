"""回归：v0.4.21 三项（用户需求）。

① 系统级长期记忆（跨项目）：`<个人文档>/.axon/longtermmemory.md`，
   载入顺序「长期记忆 > 项目记忆」，由 `global_memory_write` 工具维护
② 会话结束显示时长
③ 思考过程重复（SSE 在回合/中断边界重放同一段思考流 → 两个相同思考框）
"""
from __future__ import annotations

from pathlib import Path

import pytest

from axon.memory.global_store import GlobalMemoryStore, default_long_term_path
from axon.providers.codebuddy import map_sdk_message

_ROOT = Path(__file__).resolve().parents[2]
_CLI_SRC = (_ROOT / "axon" / "cli.py").read_text(encoding="utf-8")
_SYS_SRC = (_ROOT / "axon" / "prompts" / "system.py").read_text(encoding="utf-8")


# ---- ① 长期记忆 ----

@pytest.fixture
def store(tmp_path):
    return GlobalMemoryStore(tmp_path / "longtermmemory.md")


def test_seed_template(store):
    store.ensure_seeded()
    assert store.path.is_file()
    body = store.read()
    assert "长期记忆" in body and "## 系统与环境" in body and "## 偏好" in body


def test_append_into_existing_section_no_bleed(store):
    store.ensure_seeded()
    ok, _ = store.append("偏好", "回复要简洁")
    assert ok
    body = store.read()
    assert "- 回复要简洁" in body
    assert "）## " not in body                      # v0.4.21 冒烟 bug：标题粘连
    assert body.count("## 偏好") == 1               # 不重复创建小节


def test_append_creates_new_section(store):
    store.ensure_seeded()
    ok, _ = store.append("构建环境", "CMake 3.28")
    assert ok
    assert "## 构建环境" in store.read()


def test_append_dedup(store):
    store.ensure_seeded()
    store.append("用户", "称呼 测试用户")
    ok, msg = store.append("用户", "称呼 测试用户")
    assert ok is False and "重复" in msg


def test_append_multiple_same_section_ordered(store):
    store.ensure_seeded()
    store.append("工具链与路径", "条目A")
    store.append("工具链与路径", "条目B")
    body = store.read()
    assert body.index("条目A") < body.index("条目B")


def test_render_block_strips_comments(store):
    store.ensure_seeded()
    store.append("用户", "称呼 测试用户")
    out = store.render_block()
    assert "<!--" not in out
    assert "称呼 测试用户" in out


def test_render_block_budget(store):
    store.ensure_seeded()
    s = GlobalMemoryStore(store.path, token_budget=40)
    for i in range(30):
        s.append("其他", f"第{i}条跨项目事实" * 3)
    out = s.render_block()
    assert "超预算" in out


def test_default_path_in_user_documents():
    p = default_long_term_path()
    assert p.name == "longtermmemory.md"
    assert ".axon" in str(p)


def test_prompt_injects_global_before_project():
    """载入顺序：长期记忆（global_memory）> 项目记忆（拼接顺序）。"""
    src = (_ROOT / "axon" / "providers" / "codebuddy.py").read_text(encoding="utf-8")
    g = src.index("<global_memory>")
    m = src.index('system += "\\n\\n" + mem')      # 项目记忆的拼接点
    assert g < m


def test_global_tool_registered():
    src = (_ROOT / "axon" / "tools" / "builtin" / "__init__.py").read_text(encoding="utf-8")
    assert "make_global_spec" in src
    tw = (_ROOT / "axon" / "tools" / "builtin" / "memory.py").read_text(encoding="utf-8")
    assert 'name="global_memory_write"' in tw


def test_long_term_file_is_trusted_single_file():
    src = (_ROOT / "axon" / "sandbox" / "path_policy.py").read_text(encoding="utf-8")
    assert "global_memory" in src and "long_term_path()" in src


def test_config_long_term_override(tmp_path, monkeypatch):
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "home"))
    (tmp_path / "c.yml").write_text(
        f'memory:\n  long_term_file: "{tmp_path.as_posix()}/mylt.md"\n'
        "model:\n  active_provider: openai\n", encoding="utf-8")
    from axon.config import load_config
    cfg = load_config(tmp_path / "c.yml")
    assert cfg.memory.long_term_path().name == "mylt.md"


def test_prompt_mentions_global_memory_tool():
    assert "global_memory_write" in _SYS_SRC
    assert "换一个项目这条还成立吗" in _SYS_SRC          # 跨项目判定标准


# ---- ② 会话时长 ----

def test_session_duration_printed_on_exit():
    """v0.4.22 起：改为**每回合**页脚显示用时（退出时的会话总时长已按用户要求移除）。"""
    assert "本次会话时长" not in _CLI_SRC
    assert "elapsed=time.monotonic() - t_turn" in _CLI_SRC


# ---- ③ 思考重放去重 ----

class StreamEvent:                     # 类名即 map_sdk_message 的门控
    def __init__(self, ev):
        self.event = ev


def _think_delta(text: str) -> dict:
    return {"type": "content_block_delta",
            "delta": {"type": "thinking_delta", "thinking": text}}


def test_thinking_replay_suppressed():
    """整段思考流重放（新块/新消息边界）→ 与已发全文比对后抑制。"""
    st: dict = {}
    seg = "Let me go with these. I'll handle whatever comes back. Actually, be efficient."
    first = [e.text for e in map_sdk_message(StreamEvent(_think_delta(seg)), st)
             if e.kind == "thinking"]
    assert first and first[0] == seg
    # 消息边界：模拟 SSE 新块（局部累积被重置），整段重放
    st["thinking_stream"] = ""
    replay = [e.text for e in map_sdk_message(StreamEvent(_think_delta(seg)), st)
              if e.kind == "thinking"]
    assert replay == []                              # 完全重放 → 抑制


def test_thinking_replay_then_new_content_keeps_tail():
    st: dict = {}
    seg = "先确认参考图风格，再看现有页面，决定重新搜索同风格图片并调整版式。"
    list(map_sdk_message(StreamEvent(_think_delta(seg)), st))
    st["thinking_stream"] = ""
    out = [e.text for e in map_sdk_message(
        StreamEvent(_think_delta(seg + "另外补充一张架构图。")), st)
        if e.kind == "thinking"]
    assert out and out[0].strip().startswith("另外补充")
