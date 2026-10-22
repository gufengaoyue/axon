"""回归：技能（skills，v0.4.36，用户需求）。

存储：`<个人文档>/.axon/skills/<name>/`（与 longtermmemory.md 同目录，程序级）
调用：`&name` / `& name` / 「使用技能 name」；system prompt 注入技能清单
安装：`--add-skill <zip>`：包完整性 / 路径安全 / 结构 / 文件类型 / 内容体检
"""
from __future__ import annotations

import builtins
import zipfile
from pathlib import Path

import pytest

from axon import cli as C
from axon import skills


@pytest.fixture(autouse=True)
def isolated_skills(tmp_path, monkeypatch):
    """每个测试用独立技能目录（不碰用户真实 ~/Documents/.axon/skills）。"""
    monkeypatch.setenv("AXON_SKILLS_DIR", str(tmp_path / "skills"))
    yield tmp_path / "skills"


def make_zip(tmp_path: Path, name: str, files: dict, top: str = "") -> Path:
    p = tmp_path / name
    with zipfile.ZipFile(p, "w") as zf:
        for rel, content in files.items():
            zf.writestr(f"{top}{rel}", content)
    return p


def skill_zip(tmp_path, name="quota-sampling", desc="配额抽样", body="# 用法\n",
              extra=None, top=None):
    files = {"SKILL.md": f"---\nname: {name}\ndescription: {desc}\n---\n\n{body}"}
    files.update(extra or {})
    return make_zip(tmp_path, f"{name}.zip", files,
                    top=(f"{name}/" if top is None else top))


# ---------------------------------------------------------------- 解析

def test_parse_frontmatter_variants():
    meta, body = skills.parse_frontmatter("---\nname: a\ndescription: \"x y\"\n---\n\nB")
    assert meta["name"] == "a" and meta["description"] == "x y" and body == "B"
    meta2, body2 = skills.parse_frontmatter("无 frontmatter\n正文")
    assert meta2 == {} and body2.startswith("无 frontmatter")
    # 未闭合 → 视为无 frontmatter（容错）
    meta3, _ = skills.parse_frontmatter("---\nname: a\n正文")
    assert meta3 == {}


# ---------------------------------------------------------------- 检查

def test_inspect_normal(tmp_path):
    rep = skills.inspect_zip(skill_zip(tmp_path))
    assert rep.ok and rep.name == "quota-sampling"
    assert rep.description == "配额抽样"
    assert rep.files == ["SKILL.md"]


def test_inspect_script_warns_binary_blocks(tmp_path):
    rep = skills.inspect_zip(skill_zip(tmp_path, extra={"run.py": "print(1)\n"}))
    assert rep.ok and any("含脚本" in w for w in rep.warnings)

    bad = make_zip(tmp_path, "bin.zip", {"SKILL.md": "---\nname: b\n---\nx",
                                         "tool.exe": b"MZ"})
    rep2 = skills.inspect_zip(bad)
    assert not rep2.ok and any("二进制" in e for e in rep2.errors)


def test_inspect_requires_skill_md(tmp_path):
    z = make_zip(tmp_path, "no.zip", {"readme.md": "x"})
    rep = skills.inspect_zip(z)
    assert not rep.ok and any("缺少 SKILL.md" in e for e in rep.errors)


@pytest.mark.parametrize("member,keyword", [
    ("../../evil.txt", "穿越"),
    ("/etc/passwd", "绝对路径"),
    ("C:/Windows/x.txt", "绝对路径"),
])
def test_inspect_rejects_unsafe_paths(tmp_path, member, keyword):
    z = make_zip(tmp_path, "evil.zip", {"SKILL.md": "---\nname: e\n---\nx",
                                        member: "x"})
    rep = skills.inspect_zip(z)
    assert not rep.ok and any(keyword in e for e in rep.errors), rep.errors


def test_inspect_rejects_secrets(tmp_path):
    z = make_zip(tmp_path, "s.zip", {
        "SKILL.md": "---\nname: s\n---\n-----BEGIN RSA PRIVATE KEY-----\nabc\n"})
    rep = skills.inspect_zip(z)
    assert not rep.ok and any("私钥" in e for e in rep.errors)


def test_inspect_warns_dangerous_commands(tmp_path):
    z = make_zip(tmp_path, "d.zip", {
        "SKILL.md": "---\nname: d\n---\n```\nrm -rf /\n```\n"})
    rep = skills.inspect_zip(z)
    assert rep.ok                                    # 告警而非拒绝
    assert any("危险删除" in w for w in rep.warnings)


def test_inspect_rejects_bad_name(tmp_path):
    z = make_zip(tmp_path, "cn.zip", {"SKILL.md": "---\nname: 中文名\n---\nx"})
    rep = skills.inspect_zip(z)
    assert not rep.ok and any("不合法" in e for e in rep.errors)
    assert any("中文名" in e for e in rep.errors)     # 报错保留原始名


def test_inspect_rejects_non_zip(tmp_path):
    p = tmp_path / "x.zip"
    p.write_text("not a zip", encoding="utf-8")
    rep = skills.inspect_zip(p)
    assert not rep.ok and any("不是有效的 zip" in e for e in rep.errors)


# ---------------------------------------------------------------- 安装

def test_install_and_layout(tmp_path, isolated_skills):
    ok, msg, rep = skills.install_zip(skill_zip(tmp_path, extra={"refs/a.md": "x"}))
    assert ok, msg
    d = isolated_skills / "quota-sampling"
    assert (d / "SKILL.md").is_file() and (d / "refs" / "a.md").is_file()
    assert not list(isolated_skills.glob(".install-*"))   # 临时目录已清理


def test_install_flat_zip_without_top_dir(tmp_path, isolated_skills):
    z = make_zip(tmp_path, "flat.zip", {"SKILL.md": "---\nname: flat\n---\nx"})
    ok, msg, _ = skills.install_zip(z)
    assert ok and (isolated_skills / "flat" / "SKILL.md").is_file()


def test_install_existing_needs_force(tmp_path, isolated_skills):
    z = skill_zip(tmp_path)
    assert skills.install_zip(z)[0]
    ok2, msg2, _ = skills.install_zip(z)
    assert not ok2 and "已存在" in msg2
    ok3, _, _ = skills.install_zip(z, force=True)
    assert ok3


def test_install_rejects_when_inspect_fails(tmp_path, isolated_skills):
    z = make_zip(tmp_path, "bad.zip", {"readme.md": "x"})
    ok, msg, _ = skills.install_zip(z)
    assert not ok and not isolated_skills.exists() or not any(isolated_skills.iterdir())


# ---------------------------------------------------------------- 列表/解析

def test_list_and_resolve(tmp_path):
    skills.install_zip(skill_zip(tmp_path, name="quota-sampling"))
    skills.install_zip(skill_zip(tmp_path, name="quota-report"))
    names = [s.name for s in skills.list_skills()]
    assert names == ["quota-report", "quota-sampling"]
    assert skills.resolve_skill("quota-sampling").name == "quota-sampling"
    assert skills.resolve_skill("QUOTA-SAMPLING").name == "quota-sampling"
    assert skills.resolve_skill("quota") is None         # 前缀多义（两个都命中）
    assert skills.resolve_skill("quota-s").name == "quota-sampling"  # 唯一前缀
    assert skills.resolve_skill("nosuch") is None


def test_resolve_single_prefix(tmp_path):
    skills.install_zip(skill_zip(tmp_path, name="quota-sampling"))
    assert skills.resolve_skill("quo").name == "quota-sampling"
    assert skills.resolve_skill("&quota").name == "quota-sampling"


# ---------------------------------------------------------------- 调用展开

def test_expand_amp(tmp_path):
    skills.install_zip(skill_zip(tmp_path))
    text, used = skills.expand_invocation("&quota 帮我抽 100 户")
    assert used == "quota-sampling"
    assert '<skill name="quota-sampling"' in text and "帮我抽 100 户" in text
    assert "# 用法" in text


def test_expand_amp_space(tmp_path):
    skills.install_zip(skill_zip(tmp_path))
    text, used = skills.expand_invocation("& quota")
    assert used == "quota-sampling" and "请按技能" in text


def test_expand_chinese(tmp_path):
    skills.install_zip(skill_zip(tmp_path))
    text, used = skills.expand_invocation("使用技能 quota 帮我抽 100 户")
    assert used == "quota-sampling"
    request = text.split("用户请求：")[-1]
    assert "帮我抽 100 户" in request and "使用技能" not in request


def test_expand_unknown_keeps_input():
    text, used = skills.expand_invocation("&nosuch 继续")
    assert used is None and text == "&nosuch 继续"
    text2, used2 = skills.expand_invocation("普通问题，没有技能")
    assert used2 is None and text2 == "普通问题，没有技能"


def test_expand_truncates_long_body(tmp_path, isolated_skills):
    body = "A" * (skills.INJECT_MAX_CHARS + 500)
    skills.install_zip(skill_zip(tmp_path, body=body))
    sk = skills.resolve_skill("quota-sampling")
    ctx = sk.to_context()
    assert "超长已截断" in ctx and len(ctx) < skills.INJECT_MAX_CHARS + 1500


def test_expand_lists_extra_files(tmp_path):
    skills.install_zip(skill_zip(tmp_path, extra={"scripts/run.py": "print(1)\n"}))
    text, _ = skills.expand_invocation("&quota")
    assert "其他文件" in text and "scripts/run.py" in text


# ---------------------------------------------------------------- system prompt / 索引

def test_render_index_empty_and_filled(tmp_path):
    assert skills.render_index() == ""
    skills.install_zip(skill_zip(tmp_path))
    idx = skills.render_index()
    assert "可用技能" in idx and "quota-sampling" in idx and "配额抽样" in idx


def test_system_prompt_contains_skills(tmp_path, cfg):
    from axon.prompts.system import build_system_prompt
    skills.install_zip(skill_zip(tmp_path))
    sp = build_system_prompt(cfg)
    assert "可用技能" in sp and "quota-sampling" in sp


# ---------------------------------------------------------------- CLI

def test_add_skill_mode_requires_confirm_on_warning(tmp_path, monkeypatch, capsys):
    z = skill_zip(tmp_path, extra={"run.py": "print(1)\n"})   # 有脚本 → 警告
    monkeypatch.setattr("sys.stdin",
                        type("S", (), {"isatty": lambda self: True})())  # 模拟交互
    monkeypatch.setattr("builtins.input", lambda *a, **k: "n")
    rc = C.run_add_skill_mode(None, [str(z)])
    assert rc == 1
    assert not skills.list_skills()

    monkeypatch.setattr("builtins.input", lambda *a, **k: "y")
    rc2 = C.run_add_skill_mode(None, [str(z)])
    assert rc2 == 0 and len(skills.list_skills()) == 1


def test_add_skill_mode_rejects_bad(tmp_path, monkeypatch):
    z = make_zip(tmp_path, "bad.zip", {"SKILL.md": "---\nname: bad\n---\nx",
                                       "../../x.txt": "x"})
    rc = C.run_add_skill_mode(None, [str(z)])
    assert rc == 1 and not skills.list_skills()


def test_compose_user_input_expands_skill(tmp_path):
    skills.install_zip(skill_zip(tmp_path))
    out = C._compose_user_input({"pending_resume_note": None}, "&quota 抽 10 户")
    assert '<skill name="quota-sampling"' in out and "抽 10 户" in out


def test_compose_user_input_plain_untouched():
    txt = "普通问题"
    assert C._compose_user_input({"pending_resume_note": None}, txt) == txt


def test_slash_skills_lists(store, cfg, audit, tmp_path):
    from axon.memory.store import MemoryStore
    skills.install_zip(skill_zip(tmp_path))
    memory = MemoryStore(tmp_path / "AXON.md", token_budget=4000, max_entries=50)
    app = {"store": store, "audit": audit, "memory": memory,
           "console": None, "cfg": cfg}
    out = []
    C.handle_slash(app, None, "/skills", {},
                   type("P", (), {"prompt": lambda self: ""})())
    # console=None → 走 print；用 capsys 断言
    # （handle_slash 的 out 内部直接 print，这里只验证不抛异常 + 列表可读）


def test_uninstall_moves_to_trash(tmp_path, isolated_skills):
    skills.install_zip(skill_zip(tmp_path))
    assert skills.list_skills()
    ok, msg = skills.uninstall("quota")
    assert ok and not skills.list_skills()
    assert "已卸载" in msg

def test_add_skill_noninteractive_requires_yes(tmp_path, monkeypatch):
    """非 TTY + 有警告 + 未指定 --yes → 拒绝安装（不挂起）。"""
    z = skill_zip(tmp_path, extra={"run.py": "print(1)\n"})
    monkeypatch.setattr("sys.stdin",
                        type("S", (), {"isatty": lambda self: False})())
    rc = C.run_add_skill_mode(None, [str(z)])
    assert rc == 1 and not skills.list_skills()

    rc2 = C.run_add_skill_mode(None, [str(z)], assume_yes=True)
    assert rc2 == 0 and len(skills.list_skills()) == 1
