"""图片/文件附件（v0.2.5，用户需求）：粘贴图片 → 项目临时目录 → 提示中 @引用。

约定：
- 附件目录：`<workspace>/tmp/attach/`（工作空间内，属信任区；env_cleanup 明确保护不清理）
- 命名：`图片1.png`、`图片2.jpg`…（按剪贴板/复制来源的扩展名）
- 引用语法：`@图片1.png`（`@` 为文件引用转义符）；输入中的图片路径会被自动
  收编为附件并替换成 `@名称`，避免把超长绝对路径塞进提示
- 发送时把「名称 → 绝对路径」清单附在用户输入末尾，模型据此定位文件

剪贴板读取用 PIL.ImageGrab（Windows/macOS）；Pillow 缺失或读取失败时静默降级。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import threading
from pathlib import Path

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".ico"}
ATTACH_DIRNAME = ("tmp", "attach")


def attach_dir(workspace) -> Path:
    """旧约定目录（兼容保留）。"""
    return Path(workspace).joinpath(*ATTACH_DIRNAME)


def inbox_dir(project) -> Path:
    """粘贴收件目录：**项目内的临时目录** `<项目>/tmp`（v0.4.5 用户约定）。

    引用写法 `>./tmp/图片1.png`（相对项目目录）。
    """
    return Path(project) / "tmp"


def reference_of(path, project) -> str:
    """生成引用标志：项目内 → `>./相对路径`；项目外 → 返回绝对路径原样。"""
    try:
        rel = Path(path).resolve().relative_to(Path(project).resolve())
        rel_posix = str(rel).replace("\\", "/")
        if "/" not in rel_posix:
            return ">" + rel_posix            # 项目根下的文件：>harness.md
        return ">./" + rel_posix              # 子目录：>./tmp/图片1.png
    except (OSError, ValueError):
        return str(path)


def ingest(paths, project) -> list:
    """把来源文件纳入项目：**已在项目内则原位**，否则复制到 `<项目>/tmp/`。

    返回 [(引用标志, 绝对路径)]（v0.4.5 用户要求：不在项目内才复制；项目内不搬动）。
    """
    proj = Path(project)
    inbox = inbox_dir(proj)
    out: list = []
    for src in paths:
        sp = Path(src)
        if not sp.is_file():
            continue
        try:
            sp.resolve().relative_to(proj.resolve())
            out.append((reference_of(sp, proj), str(sp.resolve())))
            continue                       # 已在项目内 → 无需额外操作
        except (OSError, ValueError):
            pass
        inbox.mkdir(parents=True, exist_ok=True)
        ext = sp.suffix or ".bin"
        dest = inbox / _next_name(inbox, ext)
        try:
            shutil.copy2(str(sp), str(dest))
        except OSError:
            continue
        out.append((reference_of(dest, proj), str(dest.resolve())))
    return out


def save_image_to_inbox(workspace, project, raw: bytes, ext: str = ".png"):
    """剪贴板位图 → 存到项目临时目录，返回 (引用标志, 绝对路径)。"""
    inbox = inbox_dir(project)
    inbox.mkdir(parents=True, exist_ok=True)
    dest = inbox / _next_name(inbox, ext if ext.startswith(".") else "." + ext)
    dest.write_bytes(raw)
    return reference_of(dest, project), str(dest.resolve())


REF_RE = None


def parse_references(text: str, project, workspace=None):
    """解析输入里的 `>引用`（v0.4.5；v0.4.9 起支持**目录**引用）。

    支持：`>./tmp/图片1.png`、`>tmp/图片1.png`（项目内）、`>C:/abs/a.png`（绝对）、
    `>docs/` 或 `>docs`（目录）。返回 (清理后的文本, [(引用, 绝对路径)])；
    不存在的引用原样保留。
    """
    import re
    proj = Path(project)
    found: list = []
    kept_lines: list = []
    for raw_line in str(text).splitlines():
        stripped = raw_line.strip()
        if stripped.startswith(">"):
            token = stripped[1:].strip().strip('"').strip("'")
            # 只剥 `./` / `.\` 前缀；**不能**用 lstrip("./\\")——那会把
            # `.workspace/...` 开头的点也剥掉（v0.4.10 实测 bug）
            while token[:2] in ("./", ".\\"):
                token = token[2:]
            cand = Path(token)
            if not cand.is_absolute():
                cand = proj / token
            if cand.is_file() or cand.is_dir():
                found.append((stripped, str(cand.resolve())))
                continue                   # 引用行从正文中移除（由清单承载）
        kept_lines.append(raw_line)
    return "\n".join(kept_lines).strip(), found


class ClipboardWatcher:
    """实时监测"复制/粘贴动作"（v0.4.5，用户要求）。

    机制：后台线程轮询**剪贴板版本号**；一旦变化（用户截图/复制文件）→ 立即读取，
    若其中是**图片或文件**则收编到项目临时目录并生成 `>引用`；纯文本剪贴板忽略
    （不再像旧实现那样在回车时报"没有图片/文件"的噪声）。
    """

    def __init__(self, project, on_ingest=None, interval: float = 0.35):
        self.project = project
        self.on_ingest = on_ingest
        self.interval = interval
        self._last_seq = clipboard_sequence()
        self._thread = None
        self._stop = threading.Event()
        self.pending: list = []            # [(引用, 绝对路径)]

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="axon-clipwatch")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t, self._thread = self._thread, None
        if t is not None:
            t.join(timeout=0.5)     # v0.4.29：等线程真正退出（防 stop→start 并行）

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(self.interval)
            if self._stop.is_set():
                return
            seq = clipboard_sequence()
            if seq < 0 or seq == self._last_seq:
                continue
            self._last_seq = seq
            try:
                pairs = self.poll_once()
            except Exception:
                continue
            if pairs:
                self.pending.extend(pairs)
                if self.on_ingest is not None:
                    try:
                        self.on_ingest(pairs)
                    except Exception:
                        pass

    def poll_once(self) -> list:
        """读一次剪贴板并收编（返回 [(引用, 绝对路径)]；无图片/文件时返回 []）。"""
        try:
            from PIL import ImageGrab                     # type: ignore
        except ImportError:
            return self._from_files_only()
        try:
            data = ImageGrab.grabclipboard()
        except Exception:
            return self._from_files_only()
        if data is None:
            return []
        if isinstance(data, list):                        # 复制的文件
            pairs = ingest(data, self.project)
            return pairs
        try:                                              # 位图（截图）
            import hashlib
            import io as _io
            buf = _io.BytesIO()
            data.save(buf, format="PNG")
            raw = buf.getvalue()
        except Exception:
            return []
        key = hashlib.sha256(raw).hexdigest()[:16]
        global _clip_seen
        if key in _clip_seen:
            return []
        _clip_seen.add(key)
        ref, path = save_image_to_inbox(None, self.project, raw, ".png")
        return [(ref, path)]

    def _from_files_only(self) -> list:
        files = _grab_files_only(None)
        return ingest(files, self.project) if files else []

    def take_pending(self) -> list:
        pairs, self.pending = self.pending, []
        return pairs


def _next_name(directory: Path, ext: str) -> str:
    ext = ext if ext.startswith(".") else f".{ext}"
    i = 1
    while (directory / f"图片{i}{ext}").exists():
        i += 1
    return f"图片{i}{ext}"


def _register(directory: Path, ext: str, writer) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    dest = directory / _next_name(directory, ext)
    writer(dest)
    return dest


def save_bytes(workspace, data: bytes, ext: str = ".png") -> Path:
    directory = attach_dir(workspace)
    return _register(directory, ext, lambda d: d.write_bytes(data))


def copy_files(workspace, paths: list) -> list[Path]:
    """把外部文件复制进附件目录（保持扩展名，重名自动编号）。"""
    directory = attach_dir(workspace)
    out: list[Path] = []
    for src in paths:
        sp = Path(src)
        if not sp.is_file():
            continue
        ext = sp.suffix or ".bin"
        try:
            out.append(_register(directory, ext,
                                 lambda d, s=sp: shutil.copy2(str(s), str(d))))
        except OSError:
            continue
    return out


# ---------------------------------------------------------------- 剪贴板

_clip_seen: set[str] = set()      # 会话级去重：同一张剪贴板图片不重复附加


def clipboard_sequence() -> int:
    """剪贴板内容版本号（Windows：user32.GetClipboardSequenceNumber）。

    每次剪贴板内容变化（复制/截图）该值递增 —— 据此判断"用户是否刚做了粘贴动作"：
    提示符出现时的值 vs 回车时的值不同 ⇒ 用户在本次输入期间动过剪贴板（v0.3.1 用户要求：
    只有主动粘贴才收编；躺在剪贴板里的旧图片不应被莫名附加）。非 Windows 返回 -1（不支持）。
    """
    if os.name != "nt":
        return -1
    try:
        import ctypes
        return int(ctypes.windll.user32.GetClipboardSequenceNumber())
    except Exception:
        return -1


def clipboard_changed(before: int) -> bool:
    """剪贴板是否在 before 之后被改动过（-1 = 平台不支持 → False）。"""
    if before < 0:
        return False
    now = clipboard_sequence()
    return now >= 0 and now != before


def grab_clipboard(workspace, dedup: bool = True) -> list[Path]:
    """读取剪贴板：图片数据 → 存为文件；复制的文件 → 复制进来。返回新增附件。"""
    try:
        from PIL import ImageGrab                                   # type: ignore
    except ImportError:
        # Pillow 缺失时尝试用 Windows 原生方式读文件列表（复制文件的场景）
        return _grab_files_only(workspace)
    try:
        data = ImageGrab.grabclipboard()
    except Exception:
        return _grab_files_only(workspace)
    if data is None:
        return []
    # 情形 A：复制的是文件 → 返回文件名列表
    if isinstance(data, list):
        new = copy_files(workspace, data)
        if dedup:
            new = [p for p in new if _mark(p)]
        return new
    # 情形 B：截图/复制的位图 → 存 PNG
    try:
        import io as _io
        buf = _io.BytesIO()
        data.save(buf, format="PNG")
        raw = buf.getvalue()
    except Exception:
        return []
    key = hashlib.sha256(raw).hexdigest()[:16]
    if dedup and key in _clip_seen:
        return []
    _clip_seen.add(key)
    try:
        return [save_bytes(workspace, raw, ".png")]
    except OSError:
        return []


def _mark(path: Path) -> bool:
    key = f"f:{path.name}:{path.stat().st_size if path.exists() else 0}"
    if key in _clip_seen:
        return False
    _clip_seen.add(key)
    return True


def _grab_files_only(workspace) -> list[Path]:
    """无 Pillow 时的降级：Windows 剪贴板的 CF_HDROP 文件列表。"""
    try:
        import ctypes

        from ctypes import wintypes
        CF_HDROP = 15
        user32, shell32 = ctypes.windll.user32, ctypes.windll.shell32
        if not user32.IsClipboardFormatAvailable(CF_HDROP) or not user32.OpenClipboard(None):
            return []
        try:
            h = user32.GetClipboardData(CF_HDROP)
            if not h:
                return []
            count = shell32.DragQueryFileW(h, 0xFFFFFFFF, None, 0)
            files = []
            for i in range(count):
                buf = ctypes.create_unicode_buffer(32768)
                shell32.DragQueryFileW(h, i, buf, 32768)
                files.append(buf.value)
            return copy_files(workspace, files)
        finally:
            user32.CloseClipboard()
    except Exception:
        return []


# ---------------------------------------------------------------- 输入解析

def parse_input(text: str, workspace) -> tuple[str, list[Path]]:
    """把输入里的图片路径收编为附件并替换成 @名称；解析已存在的 @名称引用。

    返回 (改写后的文本, 本次涉及的附件路径列表)。
    """
    import re
    directory = attach_dir(workspace)
    found: list[Path] = []
    rewritten = text
    ext_alt = "|".join(e.lstrip(".") for e in sorted(IMAGE_EXTS))
    # v0.4.29：同一路径只收编一次——旧实现 ①-a（引号）/①-b（裸路径）会
    # 各匹配一次 → 复制出 图片2.png 孤儿副本，第二次 replace 已无目标
    absorbed: dict[str, Path] = {}

    def _absorb(token: str, whole: str) -> None:
        """把 token 指向的图片收编为附件，并把 whole 替换成 @名称。"""
        nonlocal rewritten
        p = Path(token)
        if p.suffix.lower() not in IMAGE_EXTS or not p.is_file():
            return
        try:
            key = str(p.resolve())
            if key in absorbed:                        # 已收编 → 复用，不再复制
                found.append(absorbed[key])
                if whole in rewritten:
                    rewritten = rewritten.replace(whole, f"@{absorbed[key].name}")
                return
            if directory in p.resolve().parents:
                absorbed[key] = p
                found.append(p)                       # 已在附件目录 → 直接引用
                return
        except OSError:
            return
        copied = copy_files(workspace, [p])
        if copied:
            absorbed[key] = copied[0]
            found.append(copied[0])
            if whole in rewritten:
                rewritten = rewritten.replace(whole, f"@{copied[0].name}")

    # ①-a 引号包裹的路径（可含空格）
    for m in re.finditer(r"[\"']([^\"'\n]+)[\"']", text):
        _absorb(m.group(1), m.group(0))
    # ①-b 裸路径：以图片扩展名锚定（容忍空格，Windows 截图文件名常带空格）
    for m in re.finditer(rf"([A-Za-z]:[^\n\"'|<>]*?\.(?:{ext_alt})|"
                         rf"[^\s\"']*?/[^\s\"']*?\.(?:{ext_alt}))", text, re.I):
        _absorb(m.group(1), m.group(1))

    # ② @名称 → 附件目录内定位
    for name in re.findall(r"@([^\s@]+)", rewritten):
        cand = directory / name
        if cand.is_file():
            found.append(cand)

    # 去重保序
    seen: set[str] = set()
    uniq: list[Path] = []
    for p in found:
        k = str(p)
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    return rewritten, uniq


def build_attachment_note(paths: list[Path]) -> str:
    """生成附加给模型的文件清单（名称 → 绝对路径）。"""
    if not paths:
        return ""
    return "\n\n[用户附件]\n" + "\n".join(f"- {p.name} → {p}" for p in paths)
