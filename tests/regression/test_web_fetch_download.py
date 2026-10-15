"""回归：web_fetch 下载能力与网络权限（v0.2.1 用户审定）。

- 常规文档（pdf/pptx/xlsx/txt…）→ 直接下载到 workspace/downloads，不询问
- 风险类型（zip/exe/bat…）→ 经 handle.confirm 询问；拒绝则不落盘
- 大小超限 → 拒绝
- HTML → 正文提取（原行为保持）
"""
from __future__ import annotations

import sys
import types

import pytest

from axon.sandbox.gateway import Action


class _FakeResp:
    def __init__(self, content: bytes, content_type: str, disposition: str = "",
                 status: int = 200):
        self.content = content
        self.headers = {"content-type": content_type}
        if disposition:
            self.headers["content-disposition"] = disposition
        self.status_code = status

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


class _FakeHttpx:
    """可注入的假 httpx（替换 sys.modules['httpx']）。"""

    response: _FakeResp = _FakeResp(b"", "text/plain")

    @classmethod
    def get(cls, url, **kwargs):
        return cls.response


@pytest.fixture(autouse=True)
def fake_httpx(monkeypatch):
    mod = types.ModuleType("httpx")
    mod.get = _FakeHttpx.get
    monkeypatch.setitem(sys.modules, "httpx", mod)
    yield _FakeHttpx


def _run(registry, gw, url: str, **extra):
    spec = registry.get("web_fetch")
    call = type("C", (), {"id": "t1", "name": "web_fetch"})()
    return gw.execute(call, spec, {"url": url, **extra})


# ---- 常规文档：直接下载 ----

def test_pdf_downloaded_without_prompt(fake_httpx, gw_deny, registry, ws):
    """gw_deny（一律拒审批）下 pdf 仍能下载 → 证明没走审批。"""
    fake_httpx.response = _FakeResp(b"%PDF-1.4 fake pdf bytes",
                                   "application/pdf")
    r = _run(registry, gw_deny, "https://example.com/paper.pdf")
    assert r.ok and r.meta.get("downloaded")
    assert (ws / "downloads" / "paper.pdf").read_bytes().startswith(b"%PDF")
    assert r.meta["sha256"] and r.meta["ext"] == "pdf"


def test_txt_by_content_type(fake_httpx, gw_deny, registry, ws):
    """URL 无扩展名 → 用 Content-Type 推断（text/plain → txt）。"""
    fake_httpx.response = _FakeResp("你好 AXON".encode("utf-8"), "text/plain")
    r = _run(registry, gw_deny, "https://example.com/raw")
    assert r.ok
    files = list((ws / "downloads").iterdir())
    assert files and files[0].suffix == ".txt"


def test_docx_by_disposition(fake_httpx, gw_deny, registry, ws):
    fake_httpx.response = _FakeResp(
        b"PK\x03\x04 fake docx",
        "application/octet-stream",
        'attachment; filename="report.docx"')
    r = _run(registry, gw_deny, "https://example.com/download?id=9")
    assert r.ok and r.meta["ext"] == "docx"
    assert (ws / "downloads" / "report.docx").exists()


def test_name_conflict_suffixed(fake_httpx, gw_deny, registry, ws):
    fake_httpx.response = _FakeResp(b"a", "application/pdf")
    _run(registry, gw_deny, "https://example.com/x.pdf")
    r2 = _run(registry, gw_deny, "https://example.com/x.pdf")
    assert r2.ok and r2.meta["path"].endswith("x-2.pdf")


# ---- 风险类型：询问 ----

def test_zip_denied_by_user(fake_httpx, gw_deny, registry, ws):
    fake_httpx.response = _FakeResp(b"PK\x03\x04", "application/zip")
    r = _run(registry, gw_deny, "https://example.com/pack.zip")
    assert not r.ok and "拒绝下载风险类型" in (r.recovery_hint or "")
    assert not (ws / "downloads" / "pack.zip").exists()


def test_zip_approved_downloads(fake_httpx, gw_approve, registry, ws):
    fake_httpx.response = _FakeResp(b"PK\x03\x04", "application/zip")
    r = _run(registry, gw_approve, "https://example.com/pack.zip")
    assert r.ok and (ws / "downloads" / "pack.zip").exists()


def test_exe_by_url_ext_asks(fake_httpx, gw_deny, registry, ws):
    """URL 直接带 .exe → 风险类型 → 询问（gw_deny 拒）。"""
    fake_httpx.response = _FakeResp(b"MZ fake", "application/octet-stream")
    r = _run(registry, gw_deny, "https://example.com/setup.exe")
    assert not r.ok and "拒绝下载风险类型" in (r.recovery_hint or "")


def test_bat_by_content_type_asks(fake_httpx, gw_deny, registry, ws):
    fake_httpx.response = _FakeResp(b"echo hi", "application/x-bat")
    r = _run(registry, gw_deny, "https://example.com/run")
    assert not r.ok


# ---- 大小上限 ----

def test_size_limit_rejected(fake_httpx, cfg, gw_approve, registry, ws):
    cfg.tools.max_download_mb = 0        # 0MB → 任何非空文件都超限
    fake_httpx.response = _FakeResp(b"x" * 1024, "application/pdf")
    r = _run(registry, gw_approve, "https://example.com/big.pdf")
    assert not r.ok and "超过下载上限" in (r.recovery_hint or "")


# ---- HTML 行为保持 ----

def test_html_still_extracted(fake_httpx, gw_approve, registry, ws):
    html = b"<html><head><style>x{}</style></head><body><h1>Hi</h1><p>Body</p></body></html>"
    fake_httpx.response = _FakeResp(html, "text/html; charset=utf-8")
    r = _run(registry, gw_approve, "https://example.com/page")
    assert r.ok and "<untrusted" in r.output and "Hi" in r.output
    assert not (ws / "downloads").exists()   # HTML 不落盘


# ---- 网络权限：远程默认放行 ----

def test_network_remote_allowed_by_default(gw_approve):
    d = gw_approve.check(Action(kind="network", target="https://api.example.com/v1"))
    assert d.verdict == "allow" and d.rule == "network:remote_allow"


def test_network_remote_ask_when_configured(cfg, audit):
    from axon.sandbox.gateway import SandboxGateway
    from axon.sandbox.hooks import register_builtin_hooks
    from tests.regression.conftest import AutoApprovePrompter
    cfg.sandbox.network.remote_default = "ask"
    gw = SandboxGateway(cfg, register_builtin_hooks(cfg, audit), audit, AutoApprovePrompter())
    d = gw.check(Action(kind="network", target="https://api.example.com/v1"))
    assert d.verdict == "ask" and d.rule == "network:remote_ask"


def test_network_domain_deny_still_wins(cfg, audit):
    from axon.sandbox.gateway import SandboxGateway
    from axon.sandbox.hooks import register_builtin_hooks
    from tests.regression.conftest import AutoApprovePrompter
    cfg.sandbox.network.domain_deny = ["evil.example.com"]
    gw = SandboxGateway(cfg, register_builtin_hooks(cfg, audit), audit, AutoApprovePrompter())
    d = gw.check(Action(kind="network", target="https://evil.example.com/x"))
    assert d.verdict == "deny"


def test_network_secret_scan_still_blocks(gw_approve):
    url = "https://api.example.com/v1?key=sk-" + "a" * 30
    d = gw_approve.check(Action(kind="network", target=url))
    assert d.verdict == "deny" and d.rule == "network:secret_scan"
