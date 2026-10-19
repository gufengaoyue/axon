"""图片理解：多模态注入 + 视觉副模型兜底（v0.4.6）。

三件套（对齐 CodeBuddy/WorkBuddy 做法）：
  ① 落盘 + `>引用`（attachments.py，v0.4.5 已完成）
  ② **多模态注入**：图片编码为 data URI，作为消息内容块直接进入模型视野
     （`Message.to_openai()` → image_url；codebuddy 路 → Claude 风格 image block）
  ③ 模型无视觉时**降级**：交给视觉副模型（`model.vision_helper`）生成
     "客观描述 + 关键文字"文本注入；或退到元数据提示

设计要点
- 大图先缩放（长边 ≤1568px、JPEG 重编码）→ 控制 token 与请求体积
- 描述结果按图片 sha256 缓存（同一张图只调用一次）
- 能力判定 `vision_available(cfg, model)`：`model.vision` = auto|on|off，
  auto 走模型名白名单（gpt-4o/claude-3+/qwen-vl/glm-4v/gemini…）
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
from pathlib import Path

MAX_SIDE = 1568                 # 长边上限（Claude 建议值；超出即缩放）
JPEG_QUALITY = 85

# 视觉模型名特征（auto 判定用；命中即认为主模型能"看见"）
VISION_HINTS = ("gpt-4o", "gpt-4.1", "gpt-4-turbo", "gpt-4-vision", "gpt-5",
                "o3", "o4-", "claude-3", "claude-4", "sonnet", "opus", "haiku",
                "qwen-vl", "qwen2-vl", "qwen2.5-vl", "glm-4v", "gemini",
                "internvl", "minicpm-v", "llama-3.2", "llava", "yi-vision",
                "step-1v", "step-1o", "step-5-preview", "hunyuan-vision", "moonshot-v1-vision")


def looks_vision_model(model: str) -> bool:
    m = (model or "").lower()
    return any(h in m for h in VISION_HINTS)


def vision_available(cfg, model: str | None = None) -> bool:
    """本回合是否走**原生多模态注入**。"""
    mode = str(getattr(cfg.model, "vision", "auto")).lower()
    if mode == "off":
        return False
    if mode == "on":
        return True
    return looks_vision_model(model or getattr(cfg.model, "default_model", "") or "")


# ---------------------------------------------------------------- 编码

def _encode_size(raw: bytes) -> tuple[bytes, str]:
    """缩放 + 重编码：返回 (字节, mime)。PIL 不可用时原样返回。"""
    try:
        from PIL import Image                    # type: ignore
    except ImportError:
        return raw, "image/png"
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
        if max(img.size) > MAX_SIDE:
            ratio = MAX_SIDE / max(img.size)
            img = img.resize((max(1, int(img.width * ratio)),
                              max(1, int(img.height * ratio))))
        buf = io.BytesIO()
        if img.mode in ("RGBA", "P", "LA"):
            img.convert("RGBA").save(buf, format="PNG")
            return buf.getvalue(), "image/png"
        img.convert("RGB").save(buf, format="JPEG", quality=JPEG_QUALITY)
        return buf.getvalue(), "image/jpeg"
    except Exception:
        return raw, _guess_mime(raw)


def _guess_mime(raw: bytes) -> str:
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def to_data_url(path) -> str | None:
    """图片 → `data:<mime>;base64,...`（供 OpenAI 兼容 image_url 使用）。"""
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return None
    data, mime = _encode_size(raw)
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def to_claude_block(path) -> dict | None:
    """图片 → Claude 风格 image block（codebuddy CLI 的 stream-json 输入格式）。"""
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return None
    data, mime = _encode_size(raw)
    return {"type": "image",
            "source": {"type": "base64", "media_type": mime,
                       "data": base64.b64encode(data).decode("ascii")}}


def is_image(path) -> bool:
    return Path(str(path)).suffix.lower() in {
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"}


def is_image_content(path) -> bool:
    """内容级校验（v0.4.30）：读前 16 字节验魔数——防 .pem/.db 改名 .png
    冒充图片上传（is_image 只看扩展名）。"""
    try:
        with Path(str(path)).open("rb") as f:
            head = f.read(16)
    except OSError:
        return False
    png = b"\x89PNG\r\n\x1a\n"
    jpg = b"\xff\xd8\xff"
    return (head.startswith(png)
            or head.startswith(jpg)
            or head.startswith(b"GIF8")
            or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")
            or head.startswith(b"BM")
            or head[:2] == b"II" or head[:2] == b"MM")

# ---------------------------------------------------------------- 描述兜底（B 方案）

def _cache_path(cfg) -> Path:
    from axon.config import program_root
    return program_root() / "vision_cache.json"


def _load_cache(cfg) -> dict:
    try:
        return json.loads(_cache_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_cache(cfg, cache: dict) -> None:
    try:
        p = _cache_path(cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        # v0.4.29：上限 500 条（按插入序淘汰最旧）——旧实现只增不删，
        # 每张唯一图片一条描述，永久驻留
        if len(cache) > 500:
            for k in list(cache)[: len(cache) - 500]:
                cache.pop(k, None)
        p.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def _helper_config(cfg) -> dict:
    """视觉副模型配置：优先 `model.vision_helper`，缺省回退 openai 路。"""
    h = getattr(cfg.model, "vision_helper", None) or {}
    if isinstance(h, dict):
        h = dict(h)
    else:
        h = {}

    def _cfg_get(container, key, default=None):
        if isinstance(container, dict):
            return container.get(key, default)
        return getattr(container, key, default)

    providers = getattr(cfg.model, "providers", {}) or {}
    openai_cfg = providers.get("openai")
    base = h.get("base_url") or (_cfg_get(openai_cfg, "base_url") if openai_cfg else None)
    env_name = h.get("api_key_env", "OPENAI_API_KEY")
    key = os.environ.get(env_name, "")
    if not key and openai_cfg is not None:
        # ProviderCfg 存的是环境变量名 → 取实际值
        env_name = _cfg_get(openai_cfg, "env_api_key", "OPENAI_API_KEY") \
            or "OPENAI_API_KEY"
        key = os.environ.get(env_name, "")
    model = h.get("model") or (_cfg_get(openai_cfg, "default_model") if openai_cfg else None) \
        or "gpt-4o-mini"
    return {"base_url": base, "api_key": key, "model": model}


def describe_images(paths, cfg, prompt: str | None = None) -> list[str]:
    """视觉副模型描述（B 方案）：返回与 paths 等长的描述列表（失败给空串）。

    - 命中缓存直接返回；调用失败不影响主流程（返回空串，调用方降级为元数据提示）
    - 描述要求"客观、含关键文字（OCR）、含布局要点"，便于主模型据此工作
    """
    out: list[str] = []
    helper = _helper_config(cfg)
    cache = _load_cache(cfg)
    ask = prompt or ("请客观描述这张图片：① 主体内容与场景；② 所有可见文字（逐字誊写）；"
                     "③ 布局/结构要点；④ 与设计或排版有关的细节。用中文，条列输出。")
    for path in paths:
        p = Path(str(path))
        try:
            raw = p.read_bytes()
        except OSError:
            out.append("")
            continue
        key = hashlib.sha256(raw).hexdigest()[:24]
        if key in cache:
            out.append(cache[key])
            continue
        text = _call_vision_api(helper, raw, ask)
        if text:
            cache[key] = text
            out.append(text)
        else:
            out.append("")
    _save_cache(cfg, cache)
    return out


def _call_vision_api(helper: dict, raw: bytes, ask: str) -> str:
    """调用 OpenAI 兼容的视觉接口；任何失败返回空串（静默降级）。"""
    if not helper.get("base_url"):
        return ""
    data, mime = _encode_size(raw)
    b64 = base64.b64encode(data).decode("ascii")
    try:
        from openai import OpenAI
        client = OpenAI(base_url=helper["base_url"],
                        api_key=helper.get("api_key") or "not-needed")
        resp = client.chat.completions.create(
            model=helper["model"],
            messages=[{"role": "user", "content": [
                {"type": "text", "text": ask},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ]}],
            max_tokens=700,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception:
        return ""


def meta_note(path) -> str:
    """元数据兜底（C 档）：尺寸/格式等浅层信息。"""
    p = Path(str(path))
    try:
        raw = p.read_bytes()
    except OSError:
        return f"{p.name}（读取失败）"
    mime = _guess_mime(raw)
    size = len(raw)
    try:
        from PIL import Image                     # type: ignore
        with Image.open(io.BytesIO(raw)) as img:
            dim = f"{img.width}×{img.height}"
    except Exception:
        dim = "?"
    return f"{p.name}（{mime}, {dim}, {size // 1024} KB）"


def build_vision_note(paths, cfg, model: str | None = None) -> str:
    """为**无视觉**模型构造图片说明块（B/C 档），随用户消息注入。"""
    if not paths:
        return ""
    descs = describe_images(paths, cfg)
    lines = ["", "[图片附件说明]（主模型无视觉，以下由视觉模型/元数据生成）"]
    for i, (path, desc) in enumerate(zip(paths, descs), 1):
        if desc:
            lines.append(f"图片 {i}：{Path(str(path)).name}")
            lines.append(desc)
        else:
            lines.append(f"图片 {i}：{meta_note(path)}（未能生成描述："
                         "视觉副模型不可用，可配置 model.vision_helper）")
    return "\n".join(lines)