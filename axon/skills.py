"""技能（skills，v0.4.36，用户需求）：程序级可复用指令包。

**存储**（用户要求：与 longtermmemory.md 同目录，不放程序核心目录）
    `<个人文档>/.axon/skills/<name>/SKILL.md` + 附件
    - 程序级：所有项目共享（单用户程序 → 全局可用）
    - 程序升级/重装不影响技能；技能也不随项目走
    - 环境变量 `AXON_SKILLS_DIR` 可覆盖（测试/多套技能库）

**调用**
    - `&name` / `& name`（行首）→ 加载 SKILL.md 全文注入本轮
    - 自然语言 `使用技能 name` / `用技能 name` → 同上（程序侧解析，不依赖模型猜）
    - system prompt 注入技能清单（名称 + 描述），模型也知道有哪些可用

**安装** `--add-skill <zip>`（用户要求"要有检查技能的必要步骤"）
    ① 包完整性：可打开、≤20MB、≤500 文件、解压后 ≤60MB
    ② 路径安全：拒绝绝对路径 / `..` / 盘符 / 符号链接
    ③ 结构：必须含 SKILL.md（根或单一顶层目录）；name 必须是 ASCII 安全名
    ④ 文件类型：可执行二进制（.exe/.dll/.msi…）与脚本（.py/.sh/.ps1…）分别告警
    ⑤ 内容体检：危险命令模式（rm -rf /、curl|sh、powershell -enc、base64|sh、
       eval/os.system、reg delete…）+ 私钥/云密钥硬拦
    ⑥ 用户确认（有 warning 时必须 y；有 error 直接拒绝）→ 解压到临时目录 →
       校验解压结果 → 原子移动到 `skills/<name>`
"""
from __future__ import annotations

import io
import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

# ---------------------------------------------------------------- 限制与白名单

MAX_ZIP_BYTES = 20 * 1024 * 1024          # 压缩包上限
MAX_TOTAL_BYTES = 60 * 1024 * 1024        # 解压后总量上限
MAX_FILE_BYTES = 8 * 1024 * 1024          # 单文件上限
MAX_FILES = 500
MAX_NAME_LEN = 64
INJECT_MAX_CHARS = 12000                   # 单次注入 SKILL.md 的上限（超长截断）

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

# 可执行二进制：默认拒绝（技能是"指令 + 脚本"，不该夹带二进制）
BINARY_EXTS = {".exe", ".dll", ".msi", ".com", ".scr", ".sys", ".so", ".dylib"}
# 脚本：允许但要告警（技能常见内容，需用户确认来源）
SCRIPT_EXTS = {".py", ".sh", ".bash", ".ps1", ".bat", ".cmd", ".js", ".ts",
               ".rb", ".pl", ".vbs"}
# 其余允许的文本/办公/图片类型
TEXT_EXTS = {".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
             ".csv", ".tsv", ".html", ".htm", ".css", ".xml", ".sql", ".log",
             ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp",
             ".pdf", ".docx", ".xlsx", ".pptx"}

# 危险命令模式（在文本文件里出现 → 告警；review 用户自行判断）
_DANGER_PATTERNS: list[tuple[str, str]] = [
    (r"rm\s+-[a-z]*[rf][a-z]*\s+(/|~|\*|C:\\|D:\\)", "危险删除（rm -rf 根/家目录）"),
    (r"\b(mkfs|fdisk|diskpart|format\s+[a-z]:)", "磁盘格式化/分区操作"),
    (r"\bdd\s+if=.*\bof=/dev/", "dd 直写设备"),
    (r"(curl|wget)[^\n|]{0,200}\|\s*(sudo\s+)?(ba|z|k|)sh", "下载后直接执行（curl|sh）"),
    (r"Invoke-WebRequest[^\n]{0,200}(iex|Invoke-Expression)", "PowerShell 下载执行"),
    (r"powershell(\.exe)?[^\n]{0,80}-(enc|encodedcommand)\b", "PowerShell 编码命令"),
    (r"base64\s+(-d|--decode)[^\n]{0,80}\|\s*(sudo\s+)?(ba|z|k|)sh", "base64 解码后执行"),
    (r"\beval\s*\(", "eval 动态执行"),
    (r"os\.system\s*\(|subprocess\.[a-z_]+\([^)]*shell\s*=\s*True", "shell=True/ os.system"),
    (r"shutil\.rmtree\s*\(\s*['\"](/|~|[A-Za-z]:)", "shutil.rmtree 根目录"),
    (r"\breg\s+delete\b", "删除注册表项"),
    (r"\b(vssadmin|wbadmin)\b.*\bdelete\b", "删除卷影/备份"),
    (r"\bnetsh\b.*\bfirewall\b.*\b(disable|off)\b", "关闭防火墙"),
    (r"chmod\s+777\b", "chmod 777（过度授权）"),
    (r"chown\s+-R\s+root", "chown root"),
    (r"history\s+-c|clear\s+.*history", "清理命令历史"),
]
_DANGER_RE = [(re.compile(p, re.I), why) for p, why in _DANGER_PATTERNS]

# 私钥 / 云密钥：硬拦（技能包里不该有凭据）
_SECRET_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "私钥（PRIVATE KEY）"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS Access Key"),
    (re.compile(r"\bsk-[A-Za-z0-9]{32,}\b"), "疑似 API Key（sk-…）"),
    (re.compile(r"\bghp_[A-Za-z0-9]{30,}\b"), "GitHub Token"),
]

_TEXT_SCAN_EXTS = {".md", ".txt", ".py", ".sh", ".bash", ".ps1", ".bat", ".cmd",
                   ".js", ".ts", ".json", ".yaml", ".yml", ".toml", ".ini",
                   ".cfg", ".html", ".xml", ".sql", ".rb", ".pl", ".vbs"}


# ---------------------------------------------------------------- 路径

def docs_axon_dir() -> Path:
    """`<个人文档>/.axon`（与 longtermmemory.md 同目录，用户可见）。

    复用 `global_store.default_long_term_path()` 的探测口径：优先 `~/Documents`，
    中文系统若只有 `~/我的文档` 则用它；`AXON_DOCS_DIR` 可覆盖。
    """
    env = os.environ.get("AXON_DOCS_DIR")
    if env:
        return Path(env).expanduser()
    for cand in (Path.home() / "Documents", Path.home() / "我的文档"):
        if (cand / ".axon").exists():
            return cand / ".axon"
    return Path.home() / "Documents" / ".axon"


def skills_root() -> Path:
    """技能根目录：`<个人文档>/.axon/skills`（`AXON_SKILLS_DIR` 可覆盖）。"""
    env = os.environ.get("AXON_SKILLS_DIR")
    if env:
        return Path(env).expanduser()
    return docs_axon_dir() / "skills"


# ---------------------------------------------------------------- 技能模型

@dataclass
class Skill:
    name: str
    path: Path                 # SKILL.md 所在目录
    description: str = ""
    version: str = ""
    body: str = ""
    files: list[str] = field(default_factory=list)

    @property
    def md_path(self) -> Path:
        return self.path / "SKILL.md"

    def to_context(self, max_chars: int = INJECT_MAX_CHARS) -> str:
        """注入模型上下文的文本块（限长 + 资源清单）。"""
        body = self.body or ""
        truncated = len(body) > max_chars
        if truncated:
            body = body[:max_chars].rstrip() + "\n...(技能正文超长已截断)"
        extra = ""
        others = [f for f in self.files if f.upper() != "SKILL.MD"]
        if others:
            shown = "、".join(others[:20]) + ("…" if len(others) > 20 else "")
            extra = (f"\n技能目录：{self.path}\n其他文件：{shown}\n"
                     f"（需要细节或脚本时用 read_file 读取绝对路径）")
        return (f'<skill name="{self.name}"'
                + (f' version="{self.version}"' if self.version else "")
                + f">\n{body}{extra}\n</skill>")


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """解析 `---\\nkey: value\\n---` 头（手写极简解析，容错优先）。

    返回 (meta, body)。无 frontmatter → ({}, 原文)。
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    meta: dict[str, str] = {}
    end = -1
    for i in range(1, min(len(lines), 80)):
        ln = lines[i].strip()
        if ln in ("---", "..."):
            end = i
            break
        if not ln or ln.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", ln)
        if m:
            key, val = m.group(1).lower(), m.group(2).strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            meta[key] = val
    if end < 0:
        return {}, text
    body = "\n".join(lines[end + 1:]).lstrip("\n")
    return meta, body


def load_skill(path: Path) -> Skill | None:
    """读取一个技能目录（无 SKILL.md 返回 None）。"""
    md = path / "SKILL.md"
    if not md.is_file():
        return None
    try:
        text = md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    meta, body = parse_frontmatter(text)
    name = (meta.get("name") or path.name).strip()
    desc = (meta.get("description") or "").strip()
    if not desc:
        for ln in body.splitlines():
            ln = ln.strip().lstrip("#").strip()
            if ln:
                desc = ln[:160]
                break
    files: list[str] = []
    try:
        for p in sorted(path.rglob("*")):
            if p.is_file():
                files.append(str(p.relative_to(path)).replace("\\", "/"))
    except OSError:
        pass
    return Skill(name=name[:MAX_NAME_LEN], path=path, description=desc,
                 version=(meta.get("version") or "").strip(), body=body,
                 files=files)


def list_skills() -> list[Skill]:
    """列出已安装技能（按名称排序；目录不存在返回空）。"""
    root = skills_root()
    if not root.is_dir():
        return []
    out: list[Skill] = []
    try:
        for d in sorted(root.iterdir()):
            if d.is_dir() and not d.name.startswith("."):
                sk = load_skill(d)
                if sk is not None:
                    out.append(sk)
    except OSError:
        pass
    return out


def resolve_skill(name: str) -> Skill | None:
    """按名称解析技能：精确（忽略大小写）→ 唯一前缀。"""
    key = (name or "").strip().strip("&").strip()
    if not key:
        return None
    skills = list_skills()
    low = key.lower()
    for sk in skills:
        if sk.name.lower() == low or sk.path.name.lower() == low:
            return sk
    cands = [sk for sk in skills
             if sk.name.lower().startswith(low) or sk.path.name.lower().startswith(low)]
    return cands[0] if len(cands) == 1 else None


# ---------------------------------------------------------------- 调用解析

_INVOKE_LINE_RE = re.compile(r"^\s*&\s*([A-Za-z0-9][A-Za-z0-9._-]{0,63})\s*(.*)$",
                            re.S)
_INVOKE_CN_RE = re.compile(
    r"(?:使用技能|用技能|加载技能)\s*[「\"']?([A-Za-z0-9][A-Za-z0-9._-]{0,63})",
    re.I)


def expand_invocation(text: str) -> tuple[str, str | None]:
    """把 `&name` / `使用技能 name` 展开为技能上下文块。

    返回 (新文本, 技能名或 None)。解析不到技能时**原样返回**（不破坏输入）。
    """
    raw = str(text or "")
    name = None
    rest = raw
    m = _INVOKE_LINE_RE.match(raw)
    if m:
        name, rest = m.group(1), m.group(2)
    else:
        m2 = _INVOKE_CN_RE.search(raw)
        if m2:
            name = m2.group(1)
            # v0.4.36：把"使用技能 xxx"从请求里剥掉，避免原样回显到用户请求
            rest = (raw[:m2.start()] + raw[m2.end():]).strip()
    if not name:
        return raw, None
    sk = resolve_skill(name)
    if sk is None:
        return raw, None
    tail = (rest or "").strip()
    req = tail if tail else f"请按技能「{sk.name}」的说明执行。"
    block = sk.to_context()
    return (f"{block}\n\n用户请求：{req}", sk.name)


def render_index(max_chars: int = 2000) -> str:
    """给 system prompt 的技能清单（名称 + 描述 + 调用方式）。"""
    skills = list_skills()
    if not skills:
        return ""
    lines = ["# 可用技能（skills）",
             "- 用户可用 `&名称` 或「使用技能 名称」显式加载；"
             "skill 内容会以 <skill> 块注入本轮。",
             "- 判断相关时也可以直接 `read_file` 读取表中路径下的 SKILL.md 与脚本。"]
    used = 0
    for sk in skills:
        line = f"- {sk.name}：{sk.description or '(无描述)'}  路径：{sk.md_path}"
        if used + len(line) > max_chars:
            lines.append("- …（技能较多，其余未列出；用 /skills 查看全部）")
            break
        lines.append(line)
        used += len(line)
    return "\n".join(lines)


# ---------------------------------------------------------------- 检查（安装前）

@dataclass
class SkillReport:
    zip_path: Path
    name: str = ""
    description: str = ""
    version: str = ""
    files: list[str] = field(default_factory=list)
    total_bytes: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    root_prefix: str = ""          # zip 内的技能根（可能有一层目录）

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary_lines(self) -> list[str]:
        lines = [f"技能包：{self.zip_path.name}",
                 f"  名称：{self.name or '(未识别)'}"
                 + (f"  版本：{self.version}" if self.version else ""),
                 f"  描述：{self.description or '(无)'}",
                 f"  文件：{len(self.files)} 个 · 解压后约 {self.total_bytes / 1024:.0f} KB"]
        if self.files:
            shown = "、".join(self.files[:10]) + ("…" if len(self.files) > 10 else "")
            lines.append(f"  内容：{shown}")
        for w in self.warnings:
            lines.append(f"  ⚠ {w}")
        for e in self.errors:
            lines.append(f"  ✗ {e}")
        return lines


def _decode_member_name(info: zipfile.ZipInfo) -> str:
    """修正老中文 zip（GBK 无 UTF-8 标志）的文件名乱码。"""
    name = info.filename
    if info.flag_bits & 0x800:            # 已声明 UTF-8
        return name
    try:
        fixed = name.encode("cp437").decode("gbk")
        return fixed
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name


def _member_is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0o170000
    return mode == 0o120000


def inspect_zip(zip_path: Path) -> SkillReport:
    """安装前检查（用户要求"检查技能的必要步骤"）。"""
    rep = SkillReport(zip_path=Path(zip_path))
    p = rep.zip_path
    if not p.is_file():
        rep.errors.append(f"文件不存在：{p}")
        return rep
    try:
        size = p.stat().st_size
    except OSError as e:
        rep.errors.append(f"无法读取文件信息：{e}")
        return rep
    if size > MAX_ZIP_BYTES:
        rep.errors.append(f"压缩包过大（{size / 1024 / 1024:.1f} MB > "
                          f"{MAX_ZIP_BYTES / 1024 / 1024:.0f} MB）")
        return rep

    try:
        zf = zipfile.ZipFile(p)
    except (zipfile.BadZipFile, OSError) as e:
        rep.errors.append(f"不是有效的 zip 包：{e}")
        return rep

    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if not infos:
            rep.errors.append("压缩包内没有文件")
            return rep
        if len(infos) > MAX_FILES:
            rep.errors.append(f"文件数过多（{len(infos)} > {MAX_FILES}）")

        # ---- 路径安全 + 扩展名 + 大小 ----
        name_parts: set[str] = set()
        skip_macosx = 0
        for info in infos:
            name = _decode_member_name(info).replace("\\", "/")
            if name.startswith("__MACOSX/") or name.endswith("/.DS_Store"):
                skip_macosx += 1
                continue
            pp = PurePosixPath(name)
            if pp.is_absolute() or re.match(r"^[A-Za-z]:", name):
                rep.errors.append(f"包含绝对路径成员：{name}")
                continue
            if ".." in pp.parts:
                rep.errors.append(f"包含路径穿越成员：{name}")
                continue
            if _member_is_symlink(info):
                rep.errors.append(f"包含符号链接（不安全）：{name}")
                continue
            if info.file_size > MAX_FILE_BYTES:
                rep.errors.append(
                    f"单文件过大：{name}（{info.file_size / 1024 / 1024:.1f} MB）")
            rep.total_bytes += int(info.file_size or 0)
            ext = pp.suffix.lower()
            if ext in BINARY_EXTS:
                rep.errors.append(f"包含可执行二进制（不允许）：{name}")
            elif ext in SCRIPT_EXTS:
                rep.warnings.append(f"含脚本（请确认来源可信）：{name}")
            elif ext not in TEXT_EXTS:
                rep.warnings.append(f"非常见类型（请确认）：{name}")
            name_parts.add(pp.parts[0] if pp.parts else "")

        if rep.total_bytes > MAX_TOTAL_BYTES:
            rep.errors.append(
                f"解压后体积过大（{rep.total_bytes / 1024 / 1024:.1f} MB > "
                f"{MAX_TOTAL_BYTES / 1024 / 1024:.0f} MB）")

        # ---- 结构：必须含 SKILL.md（根或单一顶层目录内）----
        md_candidates = []
        for info in infos:
            name = _decode_member_name(info).replace("\\", "/")
            if name.startswith("__MACOSX/"):
                continue
            pp = PurePosixPath(name)
            if pp.name.lower() == "skill.md":
                md_candidates.append((pp, info))
        if not md_candidates:
            rep.errors.append("缺少 SKILL.md（技能必须有说明文件）")
            return rep
        # 优先浅层（根目录或一层子目录）
        md_candidates.sort(key=lambda x: len(x[0].parts))
        md_pp, md_info = md_candidates[0]
        rep.root_prefix = "/".join(md_pp.parts[:-1])

        # ---- 读取 SKILL.md，解析元数据 + 内容体检 ----
        try:
            raw = zf.read(md_info)
            text = raw.decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            rep.errors.append(f"SKILL.md 无法读取：{e}")
            return rep
        meta, body = parse_frontmatter(text)
        rep.name = (meta.get("name") or "").strip()
        rep.description = (meta.get("description") or "").strip()
        rep.version = (meta.get("version") or "").strip()
        if not rep.name:
            # 用 SKILL.md 的父目录名（若为根则用 zip 文件名）
            rep.name = md_pp.parts[-2] if len(md_pp.parts) >= 2 \
                else p.stem
        raw_name = rep.name
        rep.name = re.sub(r"[^A-Za-z0-9._-]+", "-", raw_name).strip("-")[:MAX_NAME_LEN]
        if not NAME_RE.match(rep.name or ""):
            rep.errors.append(
                f"技能名不合法：「{raw_name}」——只允许小写字母/数字/._-，"
                "且以字母或数字开头（中文名请写在 description 里）")

        # 全文扫描（其他文本文件也查危险模式）
        scan_targets = [(md_pp, text)]
        for info in infos:
            name = _decode_member_name(info).replace("\\", "/")
            pp = PurePosixPath(name)
            if pp.name.lower() == "skill.md" or name.startswith("__MACOSX/"):
                continue
            if pp.suffix.lower() not in _TEXT_SCAN_EXTS:
                continue
            if info.file_size > 1024 * 1024:      # 只扫 1MB 以内文本
                continue
            try:
                scan_targets.append((pp, zf.read(info).decode("utf-8", "replace")))
            except Exception:                     # noqa: BLE001
                continue

        for pp, content in scan_targets:
            for rx, why in _DANGER_RE:
                if rx.search(content):
                    rep.warnings.append(f"命中危险模式「{why}」：{pp}")
            for rx, why in _SECRET_PATTERNS:
                if rx.search(content):
                    rep.errors.append(f"包含凭据「{why}」，拒绝安装：{pp}")

        if skip_macosx:
            rep.warnings.append(f"包含 {skip_macosx} 个 macOS 元数据文件（已忽略）")
        rep.files = [str(PurePosixPath(_decode_member_name(i).replace("\\", "/"))
                         .relative_to(rep.root_prefix) if rep.root_prefix else
                         _decode_member_name(i))
                     for i in infos
                     if not _decode_member_name(i).startswith("__MACOSX/")]
    return rep


# ---------------------------------------------------------------- 安装

def install_zip(zip_path: Path, force: bool = False) -> tuple[bool, str, SkillReport]:
    """检查 → 解压到临时目录 → 校验 → 原子安装。返回 (ok, 消息, 报告)。"""
    rep = inspect_zip(zip_path)
    if not rep.ok:
        return False, "检查未通过，未安装", rep
    if not NAME_RE.match(rep.name or ""):
        return False, "技能名不合法，未安装", rep

    root = skills_root()
    dest = root / rep.name
    if dest.exists() and not force:
        return False, f"技能已存在：{dest}（用 --force 覆盖）", rep

    root.mkdir(parents=True, exist_ok=True)
    tmp_parent = Path(tempfile.mkdtemp(prefix=".install-", dir=str(root)))
    try:
        with zipfile.ZipFile(rep.zip_path) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                name = _decode_member_name(info).replace("\\", "/")
                if name.startswith("__MACOSX/"):
                    continue
                pp = PurePosixPath(name)
                if rep.root_prefix:
                    try:
                        pp = pp.relative_to(PurePosixPath(rep.root_prefix))
                    except ValueError:
                        continue                      # 根外的散文件忽略
                if not pp.parts or ".." in pp.parts:
                    continue
                target = tmp_parent.joinpath(*pp.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst, length=256 * 1024)

        # 解压后校验：必须有 SKILL.md，且没有逃出临时目录
        if not (tmp_parent / "SKILL.md").is_file():
            return False, "解压后未找到 SKILL.md，未安装", rep
        base = str(tmp_parent.resolve())
        for p in tmp_parent.rglob("*"):
            if not str(p.resolve()).startswith(base + os.sep):
                return False, f"解压路径越界：{p}，未安装", rep

        if dest.exists():                       # force 覆盖
            old = root / f".old-{rep.name}-{os.getpid()}"
            try:
                dest.rename(old)
            except OSError as e:
                return False, f"无法替换旧技能（{e}），未安装", rep
            try:
                tmp_parent.rename(dest)
            except OSError as e:
                try:
                    old.rename(dest)            # 回滚
                except OSError:
                    pass
                return False, f"安装失败（{e}），旧技能已保留", rep
            shutil.rmtree(old, ignore_errors=True)
        else:
            tmp_parent.rename(dest)
        return True, f"已安装技能「{rep.name}」→ {dest}", rep
    except Exception as e:                      # noqa: BLE001
        return False, f"安装失败：{type(e).__name__}: {e}", rep
    finally:
        if tmp_parent.exists():
            shutil.rmtree(tmp_parent, ignore_errors=True)


def uninstall(name: str) -> tuple[bool, str]:
    """卸载技能（整目录移入回收站，不直接删）。"""
    sk = resolve_skill(name)
    if sk is None:
        return False, f"未找到技能：{name}"
    from axon.config import program_root
    from axon.sandbox.backup import move_to_trash
    moved = move_to_trash([str(sk.path)], program_root() / "trash", sk.path.parent)
    if not moved:
        return False, f"卸载失败（未能移入回收站）：{sk.path}"
    return True, f"已卸载技能「{sk.name}」→ {moved[0]}"
