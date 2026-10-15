"""Token 估算：优先 tiktoken(cl100k_base)，不可用时退回启发式。

口径说明（设计 §4.3）：阈值判定用估算；provider 返回 usage 后校正实际水位。
估算与模型真实词表存在 ±10% 误差，不改变三态语义。
"""
from __future__ import annotations

_enc = None
_tried = False


def _get_enc():
    global _enc, _tried
    if _tried:
        return _enc
    _tried = True
    try:
        import tiktoken

        _enc = tiktoken.get_encoding("cl100k_base")
    except Exception:
        _enc = None
    return _enc


def estimate_tokens(text: str) -> int:
    """估算单段文本 token 数。"""
    if not text:
        return 0
    enc = _get_enc()
    if enc is not None:
        try:
            return len(enc.encode(text))
        except Exception:
            pass
    # 启发式：ASCII 约 4 字符/token，CJK 约 1 字符/token
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    cjk_chars = len(text) - ascii_chars
    return max(1, ascii_chars // 4 + cjk_chars)


def estimate_messages_tokens(messages: list) -> int:
    """估算消息列表 token 数（含每条消息的固定开销）。"""
    total = 0
    for m in messages:
        total += 4  # role/分隔等固定开销
        content = getattr(m, "content", None)
        if content:
            total += estimate_tokens(content)
        for tc in getattr(m, "tool_calls", None) or []:
            total += estimate_tokens(tc.name) + estimate_tokens(str(tc.arguments))
    return total
