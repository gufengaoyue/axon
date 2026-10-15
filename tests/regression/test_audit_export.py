"""回归：审计导出（设计 §4.6 test_audit_export）。"""
from __future__ import annotations

import csv
import json

from axon.sandbox.gateway import Action


def test_export_csv(audit, tmp_path):
    audit.record(Action(kind="fs_read", target="/ws/a.txt", session_id="s1"),
                 "allow", "path:workspace", "ok")
    audit.record(Action(kind="shell", target="rm x", session_id="s1"),
                 "deny", "command:deny:rm -rf /", "blocked")
    out = tmp_path / "audit.csv"
    n = audit.export("csv", str(out), "s1")
    assert n == 2
    with open(out, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 2
    assert {"id", "ts", "kind", "target", "verdict", "rule", "result"} <= set(rows[0])
    assert rows[1]["verdict"] == "deny"


def test_export_json(audit, tmp_path):
    audit.record(Action(kind="network", target="https://x.example.com",
                        session_id="s1", detail={"url": "https://x.example.com"}),
                 "ask_approved", "network:remote_ask", "ok")
    out = tmp_path / "audit.json"
    n = audit.export("json", str(out), "s1")
    assert n == 1
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data[0]["verdict"] == "ask_approved"
    assert json.loads(data[0]["detail"])["url"] == "https://x.example.com"


def test_export_bad_format(audit, tmp_path):
    import pytest
    with pytest.raises(ValueError):
        audit.export("xml", str(tmp_path / "a.xml"))


def test_turn_side_effects_only_mutating(audit):
    audit.record(Action(kind="fs_read", target="/ws/a.txt", session_id="s1"),
                 "allow", "path:workspace", "ok")
    audit.record(Action(kind="fs_write", target="/ws/b.txt", session_id="s1"),
                 "allow", "path:workspace", "ok")
    effects = audit.turn_side_effects("s1")
    assert len(effects) == 1 and effects[0]["kind"] == "fs_write"
