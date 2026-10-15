"""回归：多模态图片理解（v0.4.6）。

业界做法 = 图片作为**内容块**进入模型消息（模型直接"看见"），而不是让工具读字节。
本组测试覆盖：能力判定 / data-URI 与 Claude 块 / 缩放 / Message 多模态转换 /
副模型描述兜底与缓存 / CLI 引用收集。
"""
from __future__ import annotations

import pathlib

import pytest
from PIL import Image

from axon import vision
from axon.providers.base import Message


@pytest.fixture
def image_path(tmp_path):
    p = tmp_path / "proj" / "tmp" / "图片1.png"
    p.parent.mkdir(parents=True)
    Image.new("RGB", (100, 60), (200, 30, 30)).save(p)
    return p


# ---- 能力判定 ----

def test_vision_auto_detects_known_models(cfg):
    assert vision.vision_available(cfg, "gpt-4o-mini") is True
    assert vision.vision_available(cfg, "claude-3-5-sonnet") is True
    assert vision.vision_available(cfg, "qwen-vl-max") is True
    assert vision.vision_available(cfg, "hy3") is False        # 未知模型 → 降级兜底


def test_vision_on_off_override(cfg):
    cfg.model.vision = "on"
    assert vision.vision_available(cfg, "anything") is True
    cfg.model.vision = "off"
    assert vision.vision_available(cfg, "gpt-4o") is False


def test_looks_vision_model_hints():
    assert vision.looks_vision_model("GLM-4V-Flash")
    assert vision.looks_vision_model("gpt-4o-2024")
    assert not vision.looks_vision_model("step-2")
    assert not vision.looks_vision_model("")


# ---- 编码 ----

def test_to_data_url(image_path):
    url = vision.to_data_url(image_path)
    assert url.startswith("data:image/")
    assert ";base64," in url


def test_to_claude_block(image_path):
    block = vision.to_claude_block(image_path)
    assert block["type"] == "image"
    src = block["source"]
    assert src["type"] == "base64"
    assert src["media_type"] in ("image/png", "image/jpeg")
    assert len(src["data"]) > 100


def test_large_image_is_downscaled(tmp_path):
    p = tmp_path / "big.png"
    Image.new("RGB", (4000, 3000), (10, 120, 200)).save(p)
    url = vision.to_data_url(p)
    assert url.startswith("data:image/jpeg")            # 被重编码为 JPEG（体积更小）
    import base64, io
    raw = base64.b64decode(url.split(",", 1)[1])
    with Image.open(io.BytesIO(raw)) as img:
        assert max(img.size) <= vision.MAX_SIDE


def test_is_image():
    assert vision.is_image("a.PNG") and vision.is_image("b.jpg")
    assert not vision.is_image("c.md") and not vision.is_image("d.pdf")


def test_meta_note(image_path):
    note = vision.meta_note(image_path)
    assert "图片1.png" in note and "100×60" in note


# ---- 多模态消息转换 ----

def test_message_to_openai_with_images(image_path):
    m = Message(role="user", content="照这张图做网页", images=[str(image_path)])
    out = m.to_openai()
    assert isinstance(out["content"], list)
    assert out["content"][0]["type"] == "text"
    assert out["content"][1]["type"] == "image_url"
    assert out["content"][1]["image_url"]["url"].startswith("data:image/")


def test_message_without_images_stays_string():
    m = Message(role="user", content="普通消息")
    out = m.to_openai()
    assert out["content"] == "普通消息"


def test_message_missing_image_file_falls_back_to_text(tmp_path):
    m = Message(role="user", content="图片丢了", images=[str(tmp_path / "nope.png")])
    out = m.to_openai()
    assert out["content"] == "图片丢了"


# ---- 副模型描述兜底（B 方案） ----

def test_build_vision_note_meta_fallback(cfg, image_path):
    """未配置 vision_helper → 元数据兜底（不抛异常、有提示）。"""
    cfg.model.vision_helper = {}
    note = vision.build_vision_note([image_path], cfg, "hy3")
    assert "图片附件说明" in note and "图片1.png" in note
    assert "未能生成描述" in note


def test_describe_images_uses_cache(cfg, image_path, monkeypatch, tmp_path):
    """同一张图只调用一次副模型（按 sha256 缓存）。"""
    cfg.model.vision_helper = {"base_url": "http://x/v1", "api_key": "k",
                               "model": "m"}
    calls = {"n": 0}

    def fake_call(helper, raw, ask):
        calls["n"] += 1
        return "红色方块图片"

    monkeypatch.setattr(vision, "_call_vision_api", fake_call)
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "home"))
    d1 = vision.describe_images([image_path], cfg)
    d2 = vision.describe_images([image_path], cfg)
    assert d1 == ["红色方块图片"] and d2 == ["红色方块图片"]
    assert calls["n"] == 1                      # 第二次走缓存


def test_describe_images_api_failure_returns_empty(cfg, image_path, monkeypatch,
                                                   tmp_path):
    cfg.model.vision_helper = {"base_url": "http://x/v1", "model": "m"}
    monkeypatch.setattr(vision, "_call_vision_api", lambda *a, **k: "")
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "home2"))
    assert vision.describe_images([image_path], cfg) == [""]


# ---- CLI 引用收集 ----

def test_cli_collects_images_from_refs(tmp_path):
    from axon import attachments
    from axon import vision as v
    proj = tmp_path / "proj"
    (proj / "tmp").mkdir(parents=True)
    img = proj / "tmp" / "a.png"
    Image.new("RGB", (10, 10), (0, 0, 0)).save(img)
    doc = proj / "harness.md"
    doc.write_text("x", encoding="utf-8")
    text, refs = attachments.parse_references(">./tmp/a.png\n>harness.md\n做网页", proj)
    images = [p for _r, p in refs if v.is_image(p)]
    assert images == [str(img.resolve())]
    assert "做网页" in text
