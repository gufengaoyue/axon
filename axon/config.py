"""config.yml 加载 / 校验 / 占位符展开 / 默认值合并。

占位符（加载时展开）：
  <workspace>  工作空间绝对路径
  <home>       用户主目录
  <system>     系统目录根（Windows: C:\\Windows；POSIX: /usr）
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


# ---------------------------------------------------------------- 程序级目录（v0.2.0）

def is_cloud_synced(path) -> bool:
    """路径是否位于常见的云同步目录（OneDrive/Dropbox/iCloud/坚果云…）。

    v0.4.3：**SQLite 库绝不能放在云同步目录**——主库 + `-wal` + `-shm` 是三个文件，
    同步工具/企业 DLP 逐文件回滚会造成"主库与 WAL 不匹配"→ `database disk image
    is malformed`（用户实测事故的机制）。
    """
    try:
        s = str(Path(path).resolve()).replace("/", "\\").lower()
    except (OSError, ValueError):
        return False
    markers = ("\\onedrive", "\\dropbox", "\\icloud", "\\nutstore", "\\坚果云",
               "\\google drive", "\\googledrive", "\\box sync", "\\sync\\")
    env = os.environ.get("OneDrive", "").lower().replace("/", "\\")
    if env and env in s:
        return True
    return any(m in s for m in markers)


def program_root() -> Path:
    """AXON 程序级数据目录（v0.4.3：改到**本地不受管目录**）。

    - Windows: `%LOCALAPPDATA%\\Axon`（回退 `~/AppData/Local/Axon`）
    - POSIX:   `~/.local/share/axon`（回退 `~/.axon`）
    - 环境变量 `AXON_HOME` 覆盖一切

    **为什么不再用 `~/Documents/.axon`**：该位置常被云同步/企业 DLP 管理，而 SQLite
    由主库 + `-wal` + `-shm` 三个文件组成，逐文件回滚会造成库损坏（实测事故）。
    本地 AppData 是"不进云、不进漫游"的位置，最适合数据库。
    """
    env = os.environ.get("AXON_HOME") or os.environ.get("OPENWP_HOME")
    if env:
        return Path(env).expanduser().resolve()
    home = Path.home()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        root = (Path(base) / "Axon") if base else (home / "AppData" / "Local" / "Axon")
        return root.resolve()
    xdg = os.environ.get("XDG_DATA_HOME")
    root = (Path(xdg) / "axon") if xdg else (home / ".local" / "share" / "axon")
    return root.resolve()


def legacy_roots() -> list[Path]:
    """需要迁移的历史数据目录（新→旧顺序）。"""
    home = Path.home()
    out = [home / "Documents" / ".axon", home / ".axon",
           home / "Documents" / ".openwp", home / ".openwp"]
    seen, uniq = set(), []
    for p in out:
        try:
            rp = p.resolve()
        except OSError:
            continue
        if rp != program_root() and rp not in seen and rp.is_dir():
            seen.add(rp)
            uniq.append(rp)
    return uniq


def migrate_legacy_root() -> list[str]:
    """把历史数据目录并入当前 program_root（只搬不删，绝不覆盖已有文件）。

    v0.4.3 关键修复：**用 SQLite backup API 做一致性快照**，而不是复制裸文件——
    旧实现直接 `shutil.copy2(sessions.db)`，若源库处于 WAL 模式且 WAL 有未回写内容，
    复制出的主库与 WAL 不匹配 → 损坏（用户实测事故根因）。
    """
    import shutil

    moved: list[str] = []
    root = program_root()
    root.mkdir(parents=True, exist_ok=True)
    for legacy in legacy_roots():
        try:
            items = list(legacy.iterdir())
        except OSError:
            continue
        for item in items:
            target = root / item.name
            try:
                if item.name == "sessions.db":
                    if target.exists():
                        continue
                    moved.append(f"{item} → {target}（SQLite 一致性快照）"
                                 if _snapshot_sqlite(item, target) else "")
                    continue
                if item.suffix in (".db-wal", ".db-shm"):
                    continue                      # WAL/SHM 由快照处理，不单独搬
                if target.exists():
                    continue
                if item.is_dir():
                    shutil.copytree(item, target)
                else:
                    shutil.copy2(item, target)
                moved.append(f"{item} → {target}")
            except OSError:
                continue
    return [m for m in moved if m]


def _snapshot_sqlite(src: Path, dst: Path) -> bool:
    """用 SQLite backup API 复制数据库（含 WAL 内容，保证一致）。"""
    import sqlite3
    try:
        s = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
        d = sqlite3.connect(str(dst))
        try:
            s.backup(d)
        finally:
            d.close()
            s.close()
        return True
    except (sqlite3.Error, OSError):
        # 源库不可读（已损坏）→ 不留半成品
        try:
            if dst.exists():
                dst.unlink()
        except OSError:
            pass
        return False



def strip_root_prefix(value: str) -> str:
    """剥离相对路径里的程序目录名前缀（`.axon/`、`.openwp/`）。

    相对路径的基准**已经是** program_root，若配置里再写 `.axon/trash`，
    会拼成 `<program_root>/.axon/trash`（历史实测出现过 `.openwp/.openwp/trash`
    这类嵌套残渣）。这里统一规范化。
    """
    v = str(value).replace("\\", "/")
    for pref in (".axon/", ".openwp/", "axon/", "openwp/"):
        if v.lower().startswith(pref):
            return v[len(pref):]
    return value


def program_dir() -> Path:
    """主程序所在目录（axon 包的上级）——v0.2.0 起禁止任何文件写入。"""
    return Path(__file__).resolve().parent.parent


def is_program_dir(path) -> bool:
    """是否主程序目录（含仓库/安装根）。"""
    try:
        return Path(path).resolve() == Path(program_dir()).resolve()
    except OSError:
        return False


def default_project_dir() -> str:
    """项目目录默认值（v0.4.8 用户约定）：

    - 未指定 `--project` 且配置未写 `project` → **当前目录**
    - 当前目录是**主程序目录**（禁止写入）→ 在 `<用户目录>/axon/` 下
      新建以时间戳命名的项目目录 `YYYY-MM-dd_HH-MM-SS`（Windows 文件名不含冒号，
      故用连字符；创建后返回其绝对路径）
    """
    cwd = Path.cwd()
    if is_program_dir(cwd):
        from datetime import datetime
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        root = Path.home() / "axon" / stamp
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError:
            return str(cwd)
        return str(root)
    return str(cwd)


# ---------------------------------------------------------------- 配置结构

@dataclass
class ProviderCfg:
    env_api_key: str = ""
    env_environment: str = ""          # 仅 codebuddy 用
    base_url: str = ""                 # 仅 openai 路用
    default_model: str = ""
    timeout_sec: float = 180.0
    cli_port: int = 0                  # 仅 codebuddy 用：CLI 控制服务器端口，0=自动挑选空闲端口


@dataclass
class ModelConfig:
    active_provider: str = "codebuddy"  # codebuddy | openai
    vision: str = "auto"                # auto | on | off（原生多模态注入，v0.4.6）
    vision_helper: dict = field(default_factory=dict)   # 副视觉模型（主模型无视觉时描述兜底）
    context_window: int = 128000
    max_output_tokens: int = 8192
    session_token_budget: int = 400000
    providers: dict[str, ProviderCfg] = field(default_factory=lambda: {
        "codebuddy": ProviderCfg(
            env_api_key="CODEBUDDY_API_KEY",          # 存**环境变量名**，不是值（v0.4.7 修复）
            env_environment="CODEBUDDY_INTERNET_ENVIRONMENT",
            default_model="hy3",
        ),
        "openai": ProviderCfg(
            env_api_key="OPENAI_API_KEY",
            base_url=os.environ.get("OPENAI_BASE_URL",''),
            default_model="qwen3.7-flash",
        ),
    })


@dataclass
class ImageGenConfig:
    """生图模型接入（v0.4.25）：文生图 / 图生图**独立入口**。

    用户红线：
    - 只有**显式的生图任务**才调用生图模型；平时仍用现有对话模型
    - 未配置（model/base_url 为空）或调用失败 → 放弃生图任务并告知用户，
      **禁止**用无生图能力的模型"生成"图片（prompt 规则层兜底）
    - 协议：OpenAI 兼容 images API（generations=文生图；edits=图生图），
      覆盖 OpenAI DALL·E / 火山方舟 Seedream / SiliconFlow / 本地兼容服务
    """
    enabled: bool = True                  # 总开关；false = 工具直接返回"未配置"
    provider: str = "openai_compat"       # 预留：目前仅 openai_compat 一种
    base_url: str = ""                    # 例: https://ark.cn-beijing.volces.com/api/v3
    api_key_env: str = "IMAGE_GEN_API_KEY"    # 环境变量名（推荐，不落盘）
    api_key: str = ""                     # 或直接填 key 值（兼容写法，会留在配置文件里）
    model: str = ""                       # 生图模型名，如 doubao-seedream-4-0-250828
    size: str = "1024x1024"               # 默认尺寸
    timeout_sec: float = 180.0
    out_dir: str = "images"               # 生成图落盘目录（相对 workspace）

    @property
    def configured(self) -> bool:
        """是否具备调用条件（model + endpoint 必填；key 可缺省——本地服务免鉴权）。"""
        return bool(self.model) and bool(self.base_url)

    def resolve_key(self) -> str | None:
        """解析 API key：直接值 > 环境变量名（与 ProviderCfg 同一套口径）。"""
        if self.api_key:
            return self.api_key
        return resolve_api_key(self.api_key_env)


@dataclass
class ContextConfig:
    compact_warn_ratio: float = 0.8
    compact_force_ratio: float = 0.9
    tool_result_max_tokens: int = 1500
    memory_max_tokens: int = 4000


@dataclass
class PathRule:
    path: Path
    mode: str  # allow | ask | deny


@dataclass
class CommandRules:
    allow: list[str] = field(default_factory=list)
    ask: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)


@dataclass
class NetworkPolicy:
    local_allow: bool = True
    remote_default: str = "allow"      # allow | ask | deny（v0.2.1 用户审定：web_fetch 直接访问网络）
    domain_allow: list[str] = field(default_factory=list)
    domain_deny: list[str] = field(default_factory=list)
    secret_scan: bool = True


@dataclass
class RiskGrading:
    green_auto: list[str] = field(default_factory=lambda: ["fs_read"])
    yellow_notify: list[str] = field(default_factory=lambda: ["fs_write", "fs_edit"])
    red_confirm: list[str] = field(
        default_factory=lambda: ["fs_delete", "shell", "network", "mcp"])
    black_block: list[str] = field(default_factory=list)


@dataclass
class SandboxConfig:
    backup_enabled: bool = True
    backup_dir: str = "./snapshots"   # 相对路径基于 program_root（v0.2.0 程序级）
    trash_dir: str = "./trash"        # 相对路径基于 program_root
    batch_delete_threshold: int = 100
    approval_granularity: str = "prefix"   # prefix | exact
    read_auto_allow: bool = True           # 读操作无规则命中时默认放行（v0.1.4 用户审定）
    paths: list[PathRule] = field(default_factory=list)
    commands: CommandRules = field(default_factory=CommandRules)
    command_bypass_whitelist: list[str] = field(default_factory=list)
    network: NetworkPolicy = field(default_factory=NetworkPolicy)
    risk_grading: RiskGrading = field(default_factory=RiskGrading)


@dataclass
class MemoryConfig:
    file: str = "AXON.md"
    max_entries: int = 200
    auto_journal: bool = True       # 每回合结束自动追加进度日志（规则式，零 token）
    auto_summary_on_exit: bool = True    # 会话结束沉淀项目记忆（模型总结，v0.2.3）
    summary_min_messages: int = 4        # 会话消息数低于此值不触发总结
    recover_prompt: bool = True          # 启动时检测"异常结束"的会话并提示补沉淀（v0.2.9）
    long_term_file: str = ""             # 系统级长期记忆文件（v0.4.21）；空=默认 <个人文档>/.axon/longtermmemory.md

    def long_term_path(self):
        """解析长期记忆文件路径（空则用默认位置）。"""
        from pathlib import Path
        if self.long_term_file:
            return Path(self.long_term_file).expanduser()
        from axon.memory.global_store import default_long_term_path
        return default_long_term_path()


@dataclass
class ToolsConfig:
    """工具级配置（v0.2.1）。"""
    downloads_dir: str = "downloads"          # 相对 workspace
    max_download_mb: int = 100                 # 单文件下载上限
    doc_extensions: list[str] = field(default_factory=lambda: [
        "pdf", "pptx", "ppt", "xlsx", "xls", "docx", "doc", "txt", "csv",
        "json", "xml", "md", "html", "png", "jpg", "jpeg", "gif", "svg",
        "webp", "mp3", "mp4", "wav"])
    risk_extensions: list[str] = field(default_factory=lambda: [
        "zip", "rar", "7z", "tar", "gz", "bz2", "xz", "bat", "cmd", "exe",
        "msi", "scr", "ps1", "vbs", "js", "jar", "reg", "lnk", "com", "dll",
        "so", "apk", "iso", "img"])
    cleanup_temp_each_turn: bool = True         # 回合结束清理临时文件（v0.2.2）
    clipboard_auto_attach: bool = False         # 每次输入后自动读剪贴板（v0.3.0 默认关，
                                                # 改为显式 /attach 或 @剪贴板 触发）
    move_products_to_project: bool = True       # 回合结束把工作空间产物移到项目目录（v0.3.0）
    auto_create_venv: bool = True               # 程序级共享 venv 不存在则自动创建（v0.4.4）
    clipboard_watch: bool = False               # 后台监听剪贴板（v0.4.8 默认关：
                                                # 截图≠粘贴，改为 Ctrl+V 键 + /attach 触发）
    temp_patterns: list[str] = field(default_factory=lambda: [
        "*.tmp", "*.temp", "*.bak", "*.orig", "*.rej", "~$*",
        "*.crdownload", "Thumbs.db", ".DS_Store"])


@dataclass
class SessionConfig:
    db: str = "./sessions.db"       # 相对路径基于 program_root（v0.2.0 程序级）
    audit_retention_days: int = 90


@dataclass
class MCPServerConfig:
    name: str
    transport: str = "stdio"           # stdio | http
    url: str | None = None
    command: str | None = None
    args: list[str] = field(default_factory=list)
    enabled: bool = True
    tools: dict[str, bool] = field(default_factory=dict)


@dataclass
class MCPConfig:
    servers: list[MCPServerConfig] = field(default_factory=list)


@dataclass
class AgentLoopConfig:
    max_turns: int = 50
    model_timeout_sec: float = 180.0
    tool_timeout_sec: float = 60.0
    web_timeout_sec: float = 30.0
    parallel_limit: int = 8
    stuck_threshold: int = 3
    show_thinking: bool = True              # 显示模型思考/决策过程（v0.4.11）
    # v0.4.28：双行状态行（状态行 + 常驻输入行）默认**关闭**——裸 ANSI 占位/
    # 上移重绘与流式输出、思考框、审批面板互相干扰，实测多次错位残留
    # （"正在执行…"残行停住不消失）；底部工具栏已常驻等价提示。
    # true = 开回双行（占位重复 bug 已修）
    two_line_status: bool = False
    stream_interject: bool = True           # 插嘴执行中即时注入（v0.4.15；关则回退回合后确认）
    # 记忆沉淀策略（v0.4.24，三层方案 L0/L1/L2）：
    #   trigger = 有信号（失败恢复/打转中断/插嘴纠正/自检⚠）才调模型沉淀本回合教训（默认）
    #   always  = 每回合结束都调模型沉淀（成本高：每回合多一次模型调用）
    #   off     = 关闭 L1（L0 规则式教训 + L2 退出总结仍生效）
    auto_note: str = "trigger"


@dataclass
class AXONConfig:
    workspace: Path
    project: Path
    model: ModelConfig = field(default_factory=ModelConfig)
    image_gen: ImageGenConfig = field(default_factory=ImageGenConfig)   # v0.4.25 生图入口
    context: ContextConfig = field(default_factory=ContextConfig)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    mcp: MCPConfig = field(default_factory=MCPConfig)
    agent: AgentLoopConfig = field(default_factory=AgentLoopConfig)
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    source_path: Path | None = None
    workspace_explicit: bool = False   # config.yml 是否显式配置了 workspace（resume 切项目时用）

    # ---- 派生路径 ----
    @property
    def venv_dir(self) -> Path:
        """程序级共享虚拟环境（所有项目共用，v0.4.4 落地设计承诺）。"""
        return program_root() / "venv"

    @property
    def venv_python(self) -> Path:
        """venv 里的解释器路径（不存在则返回预期路径，供提示词/引导使用）。"""
        v = self.venv_dir
        return (v / "Scripts" / "python.exe") if os.name == "nt" else (v / "bin" / "python")

    @property
    def runtime_python(self) -> str:
        """Axon **当前运行时**的解释器（首选：已含项目依赖）。"""
        import sys as _sys
        return _sys.executable

    @property
    def db_path(self) -> Path:
        p = Path(strip_root_prefix(self.session.db))
        # 相对路径基于 program_root（v0.2.0：会话库程序级全局共享，支持跨项目 resume）
        return p if p.is_absolute() else (program_root() / p).resolve()

    @property
    def memory_path(self) -> Path:
        p = Path(self.memory.file)
        base = self.project
        # project 就是主程序目录时（用户在程序目录直接启动），记忆文件回落到
        # 程序级目录——否则会被"主程序禁写"规则挡死（v0.2.0）
        try:
            if base.resolve() == program_dir():
                base = program_root()
        except OSError:
            pass
        return p if p.is_absolute() else (base / p).resolve()

    @property
    def snapshots_dir(self) -> Path:
        p = Path(strip_root_prefix(self.sandbox.backup_dir))
        return p if p.is_absolute() else (program_root() / p).resolve()

    @property
    def trash_dir(self) -> Path:
        p = Path(strip_root_prefix(self.sandbox.trash_dir))
        return p if p.is_absolute() else (program_root() / p).resolve()

    def active_provider_cfg(self) -> ProviderCfg:
        return self.model.providers[self.model.active_provider]

    def active_model(self) -> str:
        return self.active_provider_cfg().default_model


# ---------------------------------------------------------------- 加载逻辑

def _expand(value: str, workspace: Path, project: Path) -> str:
    home = Path.home()
    system = Path("C:/Windows") if os.name == "nt" else Path("/usr")
    repl = {
        "<workspace>": str(workspace),
        "<home>": str(home),
        "<system>": str(system),
    }
    for k, v in repl.items():
        value = value.replace(k, v)
    return value


def _to_abs(p: str, base: Path) -> Path:
    path = Path(p)
    return path if path.is_absolute() else (base / path).resolve()


def config_search_paths(explicit: str | Path | None = None) -> list[Path]:
    """配置查找链（v0.4.7：`pip install -e .` 后要在**任何目录**都能启动）。

    顺序：--config 显式 > 当前目录 > **程序数据目录**（`<AXON_HOME>/config.yml`）>
    包自带默认模板（随 pip 安装，兜底）。
    """
    out: list[Path] = []
    if explicit is not None:
        return [Path(explicit)]
    out.append(Path.cwd() / "config.yml")
    try:
        out.append(program_root() / "config.yml")
    except Exception:
        pass
    out.append(Path(__file__).resolve().parent.parent / "config.yml")   # 安装自带的模板
    return out


def seed_user_config() -> Path | None:
    """把默认模板**播种**到数据目录（一次性，v0.4.7）。

    让用户在任意目录敲 `axon` 都读同一份配置；返回播种的文件路径（已存在则 None）。
    """
    dst = program_root() / "config.yml"
    if dst.exists():
        return None
    template = Path(__file__).resolve().parent.parent / "config.yml"
    if not template.exists() or not template.is_file():
        return None
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(template), str(dst))
        return dst
    except OSError:
        return None


def resolve_api_key(spec: str | None) -> str | None:
    """把 `env_api_key` 配置解析成真实 key（v0.4.7 兼容两种写法）。

    - 正规写法：环境**变量名**（`CODEBUDDY_API_KEY`）→ 取其值
    - 兼容写法：有人直接把 **key 值**填进配置 → 原样当作 key 使用
      （旧版默认值曾把 key 值塞进来，见偏差 #51）
    """
    if not spec:
        return None
    val = os.environ.get(spec)
    if val:
        return val
    # 不像变量名（含密钥前缀或过长）→ 视为直接值
    if not spec.isupper() and (len(spec) > 24 or spec[:3] in ("ck_", "sk-", "pk-")):
        return spec
    return None


# CLI 覆盖写进「当前 provider」段时，缺省段先按已知默认补齐（v0.4.26：
# 修复 `--model`/`--base-url` 在「只写 active_provider、无 providers 段」的
# 配置上 KeyError 的潜在 bug）
_PROVIDER_DEFAULTS = {
    "codebuddy": {"env_api_key": "CODEBUDDY_API_KEY",
                  "env_environment": "CODEBUDDY_INTERNET_ENVIRONMENT"},
    # v0.4.30：与 ProviderCfg 默认一致——尊重 OPENAI_BASE_URL 环境变量
    # （旧实现硬编码 api.openai.com，静默丢弃用户配置的中转端点）
    "openai": {"env_api_key": "OPENAI_API_KEY",
               "base_url": os.environ.get("OPENAI_BASE_URL")
               or "https://api.openai.com/v1"},
}


def resolve_base_url(value: str) -> str:
    """base_url 解析：支持「环境变量名」间接写法（v0.4.26）。

    背景（用户实测 `openai.APIConnectionError: Connection error.`）：用户把
    `env_api_key` 的「填环境变量名」惯例套用到 base_url——配置里写
    `base_url: OPENAI_BASE_URL` 并 `set OPENAI_BASE_URL=<端点>`，但旧实现把
    **字面量当主机名** → DNS 解析 "OPENAI_BASE_URL" 失败 → 连接错误。
    规则：
    - 含 `://` 或含 `.`（URL 特征，如 https://… / 127.0.0.1:8000）→ 原样使用
    - 形如变量名（全大写 + 数字/下划线）→ 取环境变量的值；**未设置则原样返回**
      （不在此处 fail-fast——配置加载不该被单个字段炸掉；由
      `OpenAICompatProvider` 构造时检查并报可操作错误，见 openai_compat.py）
    - 其他（小写主机名等）→ 原样使用
    """
    v = str(value or "").strip()
    if not v or "://" in v or "." in v:
        return v
    if v.isupper() and v.replace("_", "").isalnum():
        return os.environ.get(v) or v
    return v


def looks_like_unset_env_name(value: str) -> bool:
    """是否「形如环境变量名但不是 URL」——provider 构造时据此 fail-fast。"""
    v = str(value or "").strip()
    return bool(v) and "://" not in v and "." not in v \
        and v.isupper() and v.replace("_", "").isalnum()


def _override_provider_field(raw: dict, key: str, value) -> None:
    """把 CLI 覆盖写进 raw 的当前 provider 段（段不存在则先按已知默认建）。"""
    model = raw.setdefault("model", {})
    name = str(model.get("active_provider", "codebuddy"))
    entry = model.setdefault("providers", {}).setdefault(name, {})
    for k, v in _PROVIDER_DEFAULTS.get(name, {}).items():
        entry.setdefault(k, v)
    entry[key] = value


def load_config(path: str | Path | None = None,
                overrides: dict | None = None) -> AXONConfig:
    """加载 config.yml；path=None 时按查找链定位，找不到用纯默认（v0.4.7）。

    查找链：--config > 当前目录 > `<数据目录>/config.yml` > 安装自带模板。
    overrides（CLI 参数覆盖，在解析前打进 raw，保证占位符展开一致）：
      workspace / project / active_provider / model / max_turns
    """
    cfg_path: Path | None = None
    if path is not None:
        cfg_path = Path(path)
        if not cfg_path.exists():
            raise FileNotFoundError(f"config not found: {cfg_path}")
    else:
        for cand in config_search_paths():
            if cand.exists():
                cfg_path = cand
                break

    raw: dict[str, Any] = {}
    base = Path.cwd()
    if cfg_path is not None:
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        base = cfg_path.resolve().parent

    if overrides:
        # v0.4.30：YAML 里 `model:` / `providers:` / `agent:` 留空（null）时
        # setdefault 返回 None → 后续 .get/setdefault 抛 AttributeError/
        # TypeError，且只有带 CLI 覆盖时才触发
        for _sec in ("model", "agent"):
            if raw.get(_sec) is None:
                raw[_sec] = {}
        if (raw.get("model") or {}).get("providers") is None:
            raw.setdefault("model", {})["providers"] = {}
        for key in ("workspace", "project"):
            if overrides.get(key):
                raw[key] = overrides[key]
        if overrides.get("active_provider"):
            raw.setdefault("model", {})["active_provider"] = overrides["active_provider"]
        if overrides.get("model"):
            _override_provider_field(raw, "default_model", overrides["model"])
        if overrides.get("base_url"):
            # v0.4.26：--base-url 临时覆盖当前 provider 的端点（换服务商试模型时
            # 不必改 config.yml；openai 路生效，codebuddy 路忽略）
            _override_provider_field(raw, "base_url", overrides["base_url"])
        if overrides.get("max_turns"):
            raw.setdefault("agent", {})["max_turns"] = overrides["max_turns"]

    # 工作空间默认落在项目目录下：<project>/.workspace（v0.1.5 用户审定：
    # 每个项目有自己的工作空间；config.yml 显式配置则优先）
    raw_project = raw.get("project")
    if raw_project in (None, "", "."):
        # 未指定项目目录 → 当前目录；当前目录是主程序目录则新建时间戳项目（v0.4.8）
        project = Path(default_project_dir())
    else:
        project = _to_abs(str(raw_project), base)
    if raw.get("workspace"):
        workspace = _to_abs(str(raw["workspace"]), base)
        ws_explicit = True
    else:
        workspace = (project / ".workspace").resolve()
        ws_explicit = False

    cfg = AXONConfig(workspace=workspace, project=project, source_path=cfg_path,
                       workspace_explicit=ws_explicit)

    m = raw.get("model", {}) or {}
    cfg.model.active_provider = m.get("active_provider", cfg.model.active_provider)
    cfg.model.vision = str(m.get("vision", cfg.model.vision))
    if isinstance(m.get("vision_helper"), dict):
        cfg.model.vision_helper = dict(m["vision_helper"])
    cfg.model.context_window = int(m.get("context_window", cfg.model.context_window))
    cfg.model.max_output_tokens = int(m.get("max_output_tokens", cfg.model.max_output_tokens))
    cfg.model.session_token_budget = int(m.get("session_token_budget", cfg.model.session_token_budget))
    for name, p in (m.get("providers", {}) or {}).items():
        cur = cfg.model.providers.get(name, ProviderCfg())
        cur.env_api_key = p.get("env_api_key", cur.env_api_key)
        cur.env_environment = p.get("env_environment", cur.env_environment)
        cur.base_url = resolve_base_url(p.get("base_url", cur.base_url))
        cur.default_model = p.get("default_model", cur.default_model)
        cur.timeout_sec = float(p.get("timeout_sec", cur.timeout_sec))
        cur.cli_port = int(p.get("cli_port", cur.cli_port))
        cfg.model.providers[name] = cur

    # ---- 生图模型接入（v0.4.25）----
    ig = raw.get("image_gen", {}) or {}
    cfg.image_gen = ImageGenConfig(
        enabled=bool(ig.get("enabled", cfg.image_gen.enabled)),
        provider=str(ig.get("provider", cfg.image_gen.provider) or "openai_compat"),
        base_url=str(ig.get("base_url", "") or "").strip(),
        api_key_env=str(ig.get("api_key_env", cfg.image_gen.api_key_env) or ""),
        api_key=str(ig.get("api_key", "") or "").strip(),
        model=str(ig.get("model", "") or "").strip(),
        size=str(ig.get("size", cfg.image_gen.size) or "1024x1024"),
        timeout_sec=float(ig.get("timeout_sec", cfg.image_gen.timeout_sec)),
        out_dir=str(ig.get("out_dir", cfg.image_gen.out_dir) or "images"),
    )

    c = raw.get("context", {}) or {}
    cfg.context.compact_warn_ratio = float(c.get("compact_warn_ratio", cfg.context.compact_warn_ratio))
    cfg.context.compact_force_ratio = float(c.get("compact_force_ratio", cfg.context.compact_force_ratio))
    cfg.context.tool_result_max_tokens = int(c.get("tool_result_max_tokens", cfg.context.tool_result_max_tokens))
    cfg.context.memory_max_tokens = int(c.get("memory_max_tokens", cfg.context.memory_max_tokens))

    s = raw.get("sandbox", {}) or {}
    cfg.sandbox.backup_enabled = bool(s.get("backup", {}).get("enabled", cfg.sandbox.backup_enabled))
    cfg.sandbox.backup_dir = s.get("backup", {}).get("dir", cfg.sandbox.backup_dir)
    cfg.sandbox.trash_dir = s.get("trash", {}).get("dir", cfg.sandbox.trash_dir)
    cfg.sandbox.batch_delete_threshold = int(
        s.get("batch_delete_threshold", cfg.sandbox.batch_delete_threshold))
    cfg.sandbox.approval_granularity = s.get("approval_granularity", cfg.sandbox.approval_granularity)
    cfg.sandbox.read_auto_allow = bool(s.get("read_auto_allow", cfg.sandbox.read_auto_allow))
    rules = []
    for r in s.get("paths", []) or []:
        expanded = _expand(str(r["path"]), workspace, project)
        rules.append(PathRule(path=Path(expanded), mode=str(r["mode"])))
    cfg.sandbox.paths = rules
    cmds = s.get("commands", {}) or {}
    cfg.sandbox.commands = CommandRules(
        allow=list(cmds.get("allow", []) or []),
        ask=list(cmds.get("ask", []) or []),
        deny=list(cmds.get("deny", []) or []),
    )
    cfg.sandbox.command_bypass_whitelist = list(s.get("command_bypass_whitelist", []) or [])
    net = s.get("network", {}) or {}
    cfg.sandbox.network = NetworkPolicy(
        local_allow=bool(net.get("local_allow", True)),
        remote_default=str(net.get("remote_default", "allow")),
        domain_allow=list(net.get("domain_allow", []) or []),
        domain_deny=list(net.get("domain_deny", []) or []),
        secret_scan=bool(net.get("secret_scan", True)),
    )
    rg = s.get("risk_grading", {}) or {}
    cfg.sandbox.risk_grading = RiskGrading(
        green_auto=list(rg.get("green_auto", ["fs_read"]) or []),
        yellow_notify=list(rg.get("yellow_notify", ["fs_write", "fs_edit"]) or []),
        red_confirm=list(rg.get("red_confirm", ["fs_delete", "shell", "network", "mcp"]) or []),
        black_block=list(rg.get("black_block", []) or []),
    )

    mem = raw.get("memory", {}) or {}
    cfg.memory.file = mem.get("file", cfg.memory.file)
    cfg.memory.max_entries = int(mem.get("max_entries", cfg.memory.max_entries))
    cfg.memory.auto_journal = bool(mem.get("auto_journal", cfg.memory.auto_journal))
    cfg.memory.auto_summary_on_exit = bool(
        mem.get("auto_summary_on_exit", cfg.memory.auto_summary_on_exit))
    cfg.memory.long_term_file = mem.get("long_term_file", cfg.memory.long_term_file)
    if "summary_min_messages" in mem:
        cfg.memory.summary_min_messages = int(mem["summary_min_messages"])
    if "recover_prompt" in mem:
        cfg.memory.recover_prompt = bool(mem["recover_prompt"])

    tl = raw.get("tools", {}) or {}
    if "downloads_dir" in tl:
        cfg.tools.downloads_dir = str(tl["downloads_dir"])
    if "max_download_mb" in tl:
        cfg.tools.max_download_mb = int(tl["max_download_mb"])
    if tl.get("doc_extensions"):
        cfg.tools.doc_extensions = [str(x).lower().lstrip(".")
                                    for x in tl["doc_extensions"]]
    if tl.get("risk_extensions"):
        cfg.tools.risk_extensions = [str(x).lower().lstrip(".")
                                     for x in tl["risk_extensions"]]
    if "cleanup_temp_each_turn" in tl:
        cfg.tools.cleanup_temp_each_turn = bool(tl["cleanup_temp_each_turn"])
    if tl.get("temp_patterns"):
        cfg.tools.temp_patterns = [str(x) for x in tl["temp_patterns"]]
    if "clipboard_auto_attach" in tl:
        cfg.tools.clipboard_auto_attach = bool(tl["clipboard_auto_attach"])
    if "move_products_to_project" in tl:
        cfg.tools.move_products_to_project = bool(tl["move_products_to_project"])
    if "auto_create_venv" in tl:
        cfg.tools.auto_create_venv = bool(tl["auto_create_venv"])
    if "clipboard_watch" in tl:
        cfg.tools.clipboard_watch = bool(tl["clipboard_watch"])

    ses = raw.get("session", {}) or {}
    cfg.session.db = ses.get("db", cfg.session.db)
    cfg.session.audit_retention_days = int(ses.get("audit_retention_days", cfg.session.audit_retention_days))

    mcp_cfg = raw.get("mcp", {}) or {}
    servers = []
    for sv in mcp_cfg.get("servers", []) or []:
        servers.append(MCPServerConfig(
            name=str(sv["name"]),
            transport=str(sv.get("transport", "stdio")),
            url=sv.get("url"),
            command=sv.get("command"),
            args=list(sv.get("args", []) or []),
            enabled=bool(sv.get("enabled", True)),
            tools=dict(sv.get("tools", {}) or {}),
        ))
    cfg.mcp = MCPConfig(servers=servers)

    a = raw.get("agent", {}) or {}
    cfg.agent = AgentLoopConfig(
        max_turns=int(a.get("max_turns", cfg.agent.max_turns)),
        model_timeout_sec=float(a.get("model_timeout_sec", cfg.agent.model_timeout_sec)),
        tool_timeout_sec=float(a.get("tool_timeout_sec", cfg.agent.tool_timeout_sec)),
        web_timeout_sec=float(a.get("web_timeout_sec", cfg.agent.web_timeout_sec)),
        parallel_limit=int(a.get("parallel_limit", cfg.agent.parallel_limit)),
        stuck_threshold=int(a.get("stuck_threshold", cfg.agent.stuck_threshold)),
        show_thinking=bool(a.get("show_thinking", cfg.agent.show_thinking)),
        two_line_status=bool(a.get("two_line_status", cfg.agent.two_line_status)),
        stream_interject=bool(a.get('stream_interject', cfg.agent.stream_interject)),
        auto_note=str(a.get('auto_note', cfg.agent.auto_note) or 'trigger'),
    )

    _validate(cfg)
    return cfg


def _validate(cfg: AXONConfig) -> None:
    if cfg.model.active_provider not in cfg.model.providers:
        raise ValueError(
            f"active_provider={cfg.model.active_provider!r} 未配置（可用: {list(cfg.model.providers)}）")
    if not (0 < cfg.context.compact_warn_ratio < cfg.context.compact_force_ratio < 1):
        raise ValueError("compact_warn_ratio 必须 < compact_force_ratio 且都在 (0,1)")
    for r in cfg.sandbox.paths:
        if r.mode not in ("allow", "ask", "deny"):
            raise ValueError(f"path rule mode 非法: {r.mode}")
    # v0.4.29：删除空循环（旧实现 `for mode in (...): pass`——疑似风控校验
    # 未写完，给人"已校验"的错觉）；risk_grading 的四档键由 dataclass 默认
    # 保证存在，未知键在网关侧按"未命中"处理
    if cfg.sandbox.approval_granularity not in ("prefix", "exact"):
        raise ValueError("approval_granularity 必须是 prefix 或 exact")


def ensure_dirs(cfg: AXONConfig) -> None:
    """确保程序级/项目级目录存在，并做一次性迁移。

    必要性（v0.1.3 实测坑）：codebuddy 路 SDK 以 cwd=workspace 拉起 CLI 子进程，
    workspace 不存在时 Windows 抛 NotADirectoryError [WinError 267]。
    v0.2.0：程序级目录（program_root）统管 sessions/snapshots/trash/tmp；
    首次启动把旧版落在项目下的 sessions.db 迁到程序级。
    """
    migrate_legacy_root()          # 旧 .openwp → .axon（v0.4.0 改名迁移）
    root = program_root()
    root.mkdir(parents=True, exist_ok=True)
    cfg.workspace.mkdir(parents=True, exist_ok=True)
    cfg.project.mkdir(parents=True, exist_ok=True)
    (root / "tmp").mkdir(parents=True, exist_ok=True)
    cfg.snapshots_dir.mkdir(parents=True, exist_ok=True)
    cfg.trash_dir.mkdir(parents=True, exist_ok=True)
    _migrate_legacy_session_db(cfg)
    _migrate_legacy_memory(cfg)
    ensure_venv(cfg, quiet=True)      # v0.4.4：程序级 venv 不存在则自动创建
    seed_user_config()                # v0.4.7：首次启动播种配置到数据目录


def ensure_venv(cfg: AXONConfig, quiet: bool = False) -> str | None:
    """确保**程序级共享 venv** 存在（v0.4.4 用户要求：不存在就创建）。

    - 位置：`<program_root>/venv`（本地不受管目录，所有项目共用）
    - 用**当前运行时解释器**创建（`sys.executable -m venv`），保证版本一致
    - 幂等：已存在直接返回；失败静默（不阻塞启动），提示词会引导 agent 自行创建
    - 返回解释器路径；失败返回 None
    """
    vp = cfg.venv_python
    if vp.exists():
        return str(vp)
    if not getattr(cfg.tools, "auto_create_venv", True):
        return None
    import subprocess
    import sys as _sys
    try:
        cfg.venv_dir.parent.mkdir(parents=True, exist_ok=True)
        # --system-site-packages：继承系统已装的库（pptx/PIL/numpy…），
        # 新 venv 立刻可用、免重复下载；需要纯净隔离时可用 --init-env --clean 重建
        proc = subprocess.run([_sys.executable, "-m", "venv", "--system-site-packages",
                               str(cfg.venv_dir)],
                              capture_output=True, timeout=300)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or not vp.exists():
        return None
    try:                                    # 顺手升级 pip（失败不影响）
        subprocess.run([str(vp), "-m", "pip", "install", "-q", "--upgrade", "pip"],
                       capture_output=True, timeout=300)
    except (OSError, subprocess.SubprocessError):
        pass
    if not quiet:
        print(f"[Axon] 已创建程序级 Python 环境: {vp}")
    return str(vp)


def _migrate_legacy_memory(cfg: AXONConfig) -> None:
    """旧 OPENWP.md → AXON.md（改名 Axon，v0.4.0）；只改名不删内容。"""
    legacy = cfg.project / "OPENWP.md"
    target = cfg.memory_path
    if legacy.is_file() and not target.exists():
        try:
            legacy.rename(target)
        except OSError:
            try:
                import shutil
                shutil.copy2(str(legacy), str(target))
            except OSError:
                pass


def _migrate_legacy_session_db(cfg: AXONConfig) -> None:
    """v0.1.x → v0.2.0：项目下的 sessions.db 迁到程序级（只搬不删原件）。"""
    import shutil
    for legacy in (cfg.project / "sessions" / "sessions.db",):
        if legacy.exists() and not cfg.db_path.exists():
            cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(legacy), str(cfg.db_path))
