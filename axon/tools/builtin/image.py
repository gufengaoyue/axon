"""image_generate：生图工具（v0.4.25）——文生图 / 图生图独立入口。

用户红线（prompt 规则层同样兜底）：
- 只有**显式的生图任务**才调用本工具；平时一律使用现有对话模型
- 调用时 CLI 会**特别提醒**用户（StatusLine.tool 对 image_generate 特判）
- 未配置生图模型 / 生成失败 → 本工具返回"放弃"话术，模型应放弃生图任务
  并告知用户；**禁止**用无生图能力的模型输出 SVG/HTML/ASCII/代码冒充图片
"""
from __future__ import annotations

from pathlib import Path

from axon.sandbox.gateway import Action
from axon.sandbox.risk import RiskClass
from axon.tools.context import ToolContext
from axon.tools.registry import ToolResult, ToolSpec, tool_result_err, tool_result_ok

PARAMETERS = {
    "type": "object",
    "properties": {
        "prompt": {"type": "string",
                   "description": "生图提示词（描述要生成的画面；文生图必填）"},
        "image_path": {"type": "string",
                       "description": "图生图的基准图**绝对路径**（可选；提供则走图生图 /images/edits）"},
        "size": {"type": "string",
                 "description": "图片尺寸，如 1024x1024（可选；默认取 config image_gen.size）"},
    },
    "required": ["prompt"],
}

# 放弃话术（未配置 / 失败共用骨架）——模型收到后必须放弃生图任务并告知用户
_ABANDON_TAIL = (
    "请**放弃生图任务**并明确告知用户：① 本次生图未执行（原因见上）；"
    "② 如需生图，请在 config.yml 配置 image_gen 段（model + base_url，"
    "key 走环境变量 IMAGE_GEN_API_KEY）后重试。"
    "**严禁**用无生图能力的模型'生成'图片：不要输出 SVG/HTML/ASCII 艺术或绘图代码"
    "冒充图片，不要把提示词本身当作图片交付，也不要用 execute_shell 装 Pillow 等库"
    "画图来替代生图模型。"
)


def make_spec(ctx: ToolContext) -> ToolSpec:
    def handler(call, args: dict, gw) -> ToolResult:
        from axon import image_gen

        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            return tool_result_err(call, "model", "prompt 不能为空")
        image_path = str(args.get("image_path") or "").strip() or None
        if image_path:
            if not Path(image_path).is_file():
                return tool_result_err(
                    call, "model",
                    f"基准图不存在: {image_path}（图生图需要已存在的图片文件）")
            # v0.4.30：图生图基准图的读取边界——旧实现直接 read_bytes 外发，
            # 任意本地文件可上传到生图端点。网关句柄没有 check 接口（v0.4.29
            # 误用 gw.check → AttributeError），改为**结构性约束**：
            # 只允许工作空间/项目目录内的图片（用户贴的图在 <项目>/tmp、
            # 产物在 .workspace，均覆盖），并做扩展名 + 魔数双重校验。
            from axon import vision as _vision
            try:
                _p = Path(image_path).resolve()
                _ws = Path(ctx.workspace).resolve()
                _proj = Path(ctx.config.project).resolve()
            except (OSError, ValueError) as e:
                return tool_result_err(call, "model", f"基准图路径非法: {e}")
            if not (_ws in _p.parents or _p == _ws
                    or _proj in _p.parents or _p == _proj):
                return tool_result_err(
                    call, "sandbox",
                    f"基准图必须位于工作空间或项目目录内: {image_path}"
                    "（先把图片复制进项目，或用 Ctrl+G 收编）")
            if not _vision.is_image(image_path):
                return tool_result_err(
                    call, "model",
                    f"基准图不是支持的图片格式: {image_path}"
                    "（支持 png/jpg/jpeg/gif/webp/bmp/tif/tiff）")
            if not _vision.is_image_content(image_path):
                return tool_result_err(
                    call, "model",
                    f"基准图内容不是图片（扩展名伪装）: {image_path}")

        # v0.4.29：size 缺省时不得把 None 字符串化——旧实现 str(None)="None"
        # 传入 API（真值，不回落默认）→ 每次不显式传 size 的生图必 400
        _s = args.get("size")
        size = (str(_s).strip() or None) if _s else None

        ok, msg, meta = image_gen.generate(
            ctx.config, prompt, image_path=image_path, size=size)
        if not ok:
            # 未配置 / 失败 → 统一转"放弃"话术（用户红线：不换模型硬凑）
            return tool_result_err(call, "permanent", f"{msg}\n{_ABANDON_TAIL}")
        return tool_result_ok(call, msg, meta=meta)

    def to_action(args: dict) -> Action:
        ig = getattr(ctx.config, "image_gen", None)
        return Action(kind="network",
                      target=(getattr(ig, "base_url", "") or "image_gen"),
                      detail={"action": "image_generate",
                              "model": getattr(ig, "model", ""),
                              "mode": "image2image" if args.get("image_path")
                                      else "text2image",
                              "prompt_len": len(str(args.get("prompt") or ""))})

    return ToolSpec(
        name="image_generate",
        description=(
            "调用**生图模型**生成图片（文生图；传 image_path 则图生图）。"
            "仅当用户明确要求生成/绘制图片时使用；未配置生图模型或生成失败时，"
            "按工具返回的放弃话术处理——不要用其他方式冒充生成图片。"
            "生成的图片落盘到 <workspace>/images/ 并返回路径。"),
        parameters=PARAMETERS,
        handler=handler,
        to_action=to_action,
        risk=RiskClass.NETWORK,
        mutating=False,
        parallel_safe=False,          # 生图按次计费，禁止并发刷量
        timeout=300.0,
    )
