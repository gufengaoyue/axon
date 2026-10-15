"""网络策略 + 敏感信息扫描（设计 §4.5 沙箱 #3）。

本地回环/私网默认放行；远程默认询问；域名黑白名单（deny 优先）；
出网前 secret scan（命中即拒）。
"""
from __future__ import annotations

import ipaddress
import re
from fnmatch import fnmatchcase
from urllib.parse import urlsplit

from axon.sandbox.gateway import Action, Decision

_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(api[_-]?key|secret|token|password|passwd)\b\s*[:=]\s*[\"']?[A-Za-z0-9/_+\-]{12,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"),
]


def scan_secrets(text: str) -> str | None:
    for pat in _SECRET_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(0)[:24] + "..."
    return None


def _is_local(host: str) -> bool:
    if not host:
        return False
    h = host.lower()
    if h in ("localhost", "::1", "0.0.0.0"):
        return True
    try:
        ip = ipaddress.ip_address(h)
        return ip.is_loopback or ip.is_private or ip.is_link_local
    except ValueError:
        return False


class NetworkPolicyHook:
    name = "NetworkPolicy"

    def __init__(self, config):
        self.config = config
        self.policy = config.sandbox.network

    def pre(self, action: Action, ctx) -> Decision | None:
        if action.kind != "network":
            return None
        host = (urlsplit(action.target).hostname or "").lower()

        for d in self.policy.domain_deny:
            if fnmatchcase(host, d.lower()):
                return Decision("deny", f"network:deny:{d}", f"域名命中黑名单 {d}")

        for a in self.policy.domain_allow:
            if fnmatchcase(host, a.lower()):
                return Decision("allow", f"network:allow:{a}", f"域名命中白名单 {a}")

        if _is_local(host) and self.policy.local_allow:
            return Decision("allow", "network:local", "本地/内网请求默认放行")

        if self.policy.remote_default == "allow":
            return Decision("allow", "network:remote_allow", "远程请求默认放行（配置）")
        if self.policy.remote_default == "deny":
            return Decision("deny", "network:remote_deny", "远程请求默认拒绝（配置）")
        return Decision("ask", "network:remote_ask", "远程网络请求默认询问")

    def post(self, action, result, ctx) -> None:
        return None


class SecretScanHook:
    name = "SecretScan"

    def __init__(self, config):
        self.enabled = config.sandbox.network.secret_scan

    def pre(self, action: Action, ctx) -> Decision | None:
        if action.kind != "network" or not self.enabled:
            return None
        import json as _json
        blob = action.target + " " + _json.dumps(action.detail, ensure_ascii=False)
        hit = scan_secrets(blob)
        if hit:
            return Decision("deny", "network:secret_scan",
                            f"检测到疑似敏感信息（{hit}），已拦截出网请求")
        return None

    def post(self, action, result, ctx) -> None:
        return None
