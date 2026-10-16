"""生图模型客户端（v0.4.25）：文生图 / 图生图。

协议：OpenAI 兼容 images API——
- 文生图：`POST {base_url}/images/generations`  JSON {model, prompt, size}
- 图生图：`POST {base_url}/images/edits`        multipart(image, prompt, model, size)

覆盖主流兼容端点：OpenAI DALL·E、火山方舟 Seedream、SiliconFlow、本地 SD 兼容服务等。
响应兼容两种形态：`data[0].b64_json`（base64）或 `data[0].url`（回源下载）。
生成图落盘到 `<workspace>/images/`，文件名带时间戳防重名。

设计红线（用户明确要求，v0.4.25）：
- 本模块**只**被 `image_generate` 工具引用；显式生图任务之外任何路径不得调用
- 未配置 / 调用失败 → 由工具层把任务标记为"放弃"并告知用户；
  **禁止**用无生图能力的文本模型"生成"图片（prompt 规则层另有兜底）
"""
from __future__ import annotations

import base64
import binascii
import re
import time
from pathlib import Path

# 未配置时的放弃话术（工具层原样转给模型）
NOT_CONFIGURED_MSG = (
    "未配置生图模型（config.yml 的 image_gen.model / image_gen.base_url 为空，"
    "或 image_gen.enabled=false）——生图任务无法执行。"
)

# 生成失败时的放弃话术前缀（工具层附具体错误）
FAILED_MSG_PREFIX = "生图模型调用失败"


def is_configured(cfg) -> bool:
    """是否具备调用条件（model + endpoint 必填；key 可缺省——本地服务免鉴权）。"""
    ig = getattr(cfg, "image_gen", None)
    if ig is None or not getattr(ig, "enabled", False):
        return False
    return bool(getattr(ig, "configured", False))


def _sniff_ext(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return "png"


def _mime_of(path: Path) -> str:
    return {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".gif": "image/gif", ".webp": "image/webp"}.get(
        path.suffix.lower(), "image/png")


def _slug(text: str, n: int = 24) -> str:
    """提示词 → 文件名安全片段（中英文都保留可读部分，其余折叠为 _）。"""
    s = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "_", str(text or "")).strip()
    s = re.sub(r"\s+", "_", s)
    return (s[:n] or "image").rstrip("._")


def _unique(target: Path) -> Path:
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    i = 2
    while target.exists():
        target = target.with_name(f"{stem}-{i}{suffix}")
        i += 1
    return target


def _extract_image(payload: dict) -> tuple[bytes | None, str]:
    """从 OpenAI 兼容响应里取图片字节：b64_json 优先，其次 url（调用方下载）。"""
    data = (payload or {}).get("data") or []
    if not data:
        return None, "响应中无 data 字段"
    item = data[0] or {}
    b64 = item.get("b64_json")
    if b64:
        try:
            return base64.b64decode(b64), ""
        except (binascii.Error, ValueError) as e:
            return None, f"b64_json 解码失败: {e}"
    url = item.get("url")
    if url:
        return None, f"__URL__{url}"      # 哨兵：让调用方去下载
    return None, "响应 data[0] 中既无 b64_json 也无 url"


def _post(base_url: str, key: str, path: str, *, json_body: dict | None = None,
          files=None, data=None, timeout: float) -> dict:
    """统一 POST + 错误文本提取（生图服务的 4xx/5xx 常把原因放在 body 里）。"""
    import httpx
    headers = {"Authorization": f"Bearer {key}"}
    resp = httpx.post(f"{base_url.rstrip('/')}{path}", json=json_body, files=files,
                      data=data, headers=headers, timeout=timeout)
    if resp.status_code >= 400:
        detail = ""
        try:
            j = resp.json()
            err = j.get("error") if isinstance(j, dict) else None
            detail = str(err.get("message") if isinstance(err, dict) else err or j)[:300]
        except Exception:                 # noqa: BLE001
            detail = (resp.text or "")[:300]
        raise RuntimeError(f"HTTP {resp.status_code}: {detail or '(无响应体)'}")
    return resp.json()


def _download(url: str, timeout: float) -> bytes:
    import httpx
    resp = httpx.get(url, timeout=timeout, follow_redirects=True)
    if resp.status_code >= 400:
        raise RuntimeError(f"图片 URL 下载失败 HTTP {resp.status_code}")
    return resp.content


def generate(cfg, prompt: str, image_path: str | None = None,
             size: str | None = None) -> tuple[bool, str, dict]:
    """生成图片。返回 (ok, message, meta)。

    - ok=False 时 message 含放弃话术（未配置/失败），meta 为空
    - ok=True 时 message 含落盘路径与模型名，meta 带 path/model/size 等
    """
    if not is_configured(cfg):
        return False, NOT_CONFIGURED_MSG, {}
    ig = cfg.image_gen
    base = str(ig.base_url or "").strip()
    key = ig.resolve_key() or "not-needed"       # 本地免鉴权服务容忍占位 key
    size = (size or ig.size or "1024x1024").strip()
    timeout = float(getattr(ig, "timeout_sec", 180.0) or 180.0)
    out_dir = Path(cfg.workspace) / (ig.out_dir or "images")

    try:
        if image_path:
            src = Path(image_path)
            if not src.is_file():
                return False, f"{FAILED_MSG_PREFIX}：基准图不存在（{image_path}）", {}
            payload = _post(
                base, key, "/images/edits", timeout=timeout,
                files={"image": (src.name, src.read_bytes(), _mime_of(src))},
                data={"model": ig.model, "prompt": prompt, "size": size, "n": "1"},
            )
            mode = "图生图"
        else:
            payload = _post(
                base, key, "/images/generations", timeout=timeout,
                json_body={"model": ig.model, "prompt": prompt, "size": size, "n": 1},
            )
            mode = "文生图"
    except Exception as e:                       # noqa: BLE001 - 失败统一转放弃话术
        return False, f"{FAILED_MSG_PREFIX}（{type(e).__name__}）: {e}", {}

    data, marker = _extract_image(payload)
    if data is None and marker.startswith("__URL__"):
        url = marker[len("__URL__"):]
        try:
            data = _download(url, timeout)
        except Exception as e:                   # noqa: BLE001
            return False, f"{FAILED_MSG_PREFIX}（图片 URL 下载失败）: {e}", {}
    if not data:
        return False, f"{FAILED_MSG_PREFIX}：{marker or '未返回图片数据'}", {}

    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        target = _unique(out_dir / f"{mode}_{stamp}_{_slug(prompt)}.{_sniff_ext(data)}")
        target.write_bytes(data)
    except OSError as e:
        return False, f"{FAILED_MSG_PREFIX}（落盘失败）: {e}", {}

    msg = (f"🖼 已调用生图模型 `{ig.model}`（{mode}）生成图片。\n"
           f"路径: {target}\n"
           f"尺寸参数: {size} · 大小: {len(data)} 字节\n"
           f"提示词: {prompt[:200]}")
    return True, msg, {"path": str(target), "model": ig.model, "mode": mode,
                       "size": size, "bytes": len(data), "kind": "image_gen"}
