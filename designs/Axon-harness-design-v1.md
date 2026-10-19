# AXON Agent Harness 架构设计

> 设计台账：2026-10-13 · AXON · v1 · 关键决策 43 项 · 未知区 8 项 · 独立评审 通过（第 1 轮存疑 13 项 → 回修 30 处 → 第 2 轮复审 10 闭环 3 残留 → 残留已最终修正）

---

## 1. 需求规格

| 维度 | 内容 | 状态 |
|---|---|---|
| 目标与用途 | 自托管单机 CLI agent「AXON」：本地文件/命令/网络操作 + 编码辅助，参考 Hermes Agent 的基本形态，Python 实现 | 默认假设（可推翻） |
| 成功标准 | v1：① 两种模型接入 + 核心工具 + 会话恢复全链路跑通；② 沙箱三态/风险分级拦截 100% 有效（越界必拦、高危必确认）；③ 90% 压缩触发后任务完成率不劣化；④ 全部沙箱动作有审计记录可导出 | 默认假设（可推翻） |
| 验收来源（oracle） | 自建脚本化回归集 `tests/regression/`（路径三态、命令规则、压缩触发、会话恢复、审计导出、危险命令拦截各若干用例）；Hermes / Claude Code 公开行为仅作对照物，不照搬 | 默认假设（可推翻） |
| 任务结构 | 对话式单 agent 循环：用户输入 → 模型推理 → 工具调用（经沙箱网关）→ 观察回喂 → 直至 V 层验证通过；支持多轮会话、项目目录绑定、断点恢复 | 已确认（需求文档） |
| 数据/资源边界 | 工作空间 `./workspace`（config.yml 可改）；会话 `./sessions/sessions.db`；长期记忆 `AXON.md`；越界访问触发确认；系统/用户根/密钥目录默认拒绝 | 已确认（需求文档） |
| 硬约束 | Python；CLI 优先（其他端后置）；命名 AXON；README 用 GitHub 风格；模型两种连接方式用户自选；开发期用 codebuddy_agent_sdk + hy3 测试 | 已确认（需求文档） |
| 用户与场景 | 单用户本地 CLI（Windows/macOS/Linux），交互式 TTY；无多租户、无远程协作 | 默认假设（可推翻） |
| 边界（不做什么） | v1 不做：GUI/Web 端、多租户鉴权（JWT/SSO 写 N/A）、远程分布式子代理、向量检索记忆、OS 级网络强隔离（容器/防火墙）。以上列 v2+ 候选 | 默认假设（可推翻） |
| 技术栈约束 | Python 3.11+；`openai` 包接 OpenAI 兼容模型；`codebuddy_agent_sdk` 接 codebuddy（env: `CODEBUDDY_API_KEY` / `CODEBUDDY_INTERNET_ENVIRONMENT`）；SQLite3；`config.yml` 声明式配置 | 已确认（需求文档） |
| 模型策略与成本 | v1 单智能体 + 单主模型（provider 用户自选，harness 不硬编码模型名）；风险分级/路径三态/命令规则/打转检测全部确定性代码（零 token）；快筛模型与分档 v1 不引入，预留 ModelRouter 接口；无总预算硬上限，设单会话 token 预算默认值 | 默认假设（可推翻） |
| 效率与体感 | CLI 流式增量输出（TTFT 体感）；压缩双水位 80% 预警 / 90% 强制（需求文档指定 90%）；工具表固定序 + 历史只追加保前缀稳定；互不依赖工具调用可并行（上限 8）；Ctrl+C 可中断并打印副作用清单 | 默认假设（可推翻） |

**决策归属说明**：标注「已确认」的来自需求文档原文；「默认假设」为本次澄清轮由架构师建议、用户答复"使用你的默认和建议，试用后再改"，全部可推翻。

---

## 2. 架构范式 P

| 子维度 | 决策 | 理由 |
|---|---|---|
| 扩展方式 | **插件化** | 工具注册表 + MCP 连接器 + 配置驱动；核心循环极薄，能力可增删。需求文档的「MCP 整体/单独开关」天然要求工具层可插拔；对照知识库 Hermes Agent（Skill 插件系统 + 40+ 内置工具）与 Claude Code（内置工具 + MCP） |
| 配置方式 | **声明式** | 需求文档指定 `config.yml`：workspace、路径三态、命令规则、网络策略、压缩阈值、预算全部配置化，代码只实现引擎不写死策略 |
| 部署拓扑 | **单机** | 单用户本地 CLI；子代理/远程派发 v2+（边界清单已砍） |
| 编排模式 | **中心化** | 单 agent 循环即唯一调度器；工具并行由循环内有界池完成，无二级调度器 |

---

## 3. 技术栈选型

| 项 | 选型 | 理由 |
|---|---|---|
| 语言 | Python 3.11+（开发机 3.11.8 / 3.13.12 验证） | 需求文档指定 |
| CLI 框架 | `argparse`（stdlib）+ `prompt_toolkit`（REPL 历史/多行）+ `rich`（流式 Markdown 渲染） | 交互质量与零魔控依赖兼顾；不引 typer/click 减少依赖面 |
| 模型接入 | `openai>=1.0`（OpenAI 兼容路）+ `codebuddy_agent_sdk`（懒加载，仅选中该 provider 时 import） | 需求文档指定两条路 |
| 网页抓取 | `httpx` + `beautifulsoup4`（html.parser，免 lxml 编译） | 跨平台安装稳；正文提取用 `get_text` |
| Token 估算 | `tiktoken`（cl100k_base 近似）+ provider 返回 usage 校正 | 压缩阈值必须有可复现的计数口径 |
| 回收站 | `send2trash`（跨平台；Windows 走 Shell API） | 需求文档「删除优先移回收站」 |
| MCP 客户端 | `mcp>=2.0`（v2 API：`from mcp import Client`；`async with Client(url_or_stdio)`；`await client.call_tool(name, args)`） | 2026-10-13 核实官方文档；MCP 是需求文档沙箱章节明确要管控的对象 |
| 存储 | `sqlite3`（stdlib，WAL 模式） | 需求文档指定 SQLite3 |
| 配置解析 | `PyYAML`（safe_load） | 需求文档指定 config.yml |
| 运行环境 | venv 隔离安装；shell 工具内脚本落在 `workspace/.venv` / npm workspace | 需求文档「不污染主机环境」 |

**模型组合**（四分类档位；候选来源：用户指定 + 知识库；知识库未收录者如实标注，不虚构规格）

| 档位 | 候选模型 | 职责 | 单价量级（核实日期） | 预估 token 占比 |
|---|---|---|---|---|
| 确定性代码 | —（纯 Python 规则） | 路径三态、命令规则、风险分级、打转检测、token 估算、审计 | — | 0 |
| 决策型模型（System One） | v1 不引入（v2 候选：知识库 Jev / CLM-8B 条目） | 预留：审批快筛、紧急度路由 | 知识库未收录实时价格（2026-10-13） | 0% |
| 快速低价 LLM | v1 不引入（压缩摘要暂由主模型承担，见 §5 代价注记） | 预留：分诊、中间摘要 | 以用户所选 provider 实时价格为准 | ~10%（若启用） |
| 强力 LLM | 用户自选：hy3（codebuddy 路，开发测试指定）/ 任意 OpenAI 兼容模型（openai 路） | 主推理循环、压缩摘要、最终答复 | 知识库未收录（hy3 非知识库模型，按能力特征归入本档；价格以 codebuddy 服务商为准，2026-10-13） | ~80% |

> 占比口径注记（独立评审回修）：上表为**新增 token 的用途构成**（增量口径）；每轮前缀重发是乘数项（轮次 × 平均前缀大小），不在本表口径内，另立双账核算（见 §5）。

---

## 4. 六层设计（每层 = 决策 + 落地规格）

### 4.1 E 执行循环

**设计决策**
- 执行模式：**静态循环**（对话式 tool-calling loop）。任务结构清晰且单一，不引入动态脚本编排（h-framework 模式库中的六种静态模式 v1 只用「循环直到完成」）。
- 终止条件：归 **V 层**确定性规则（§4.6），模型自评无效。
- 防跑飞三道保险：`max_turns=50`（默认，config 可调）+ 单会话 token 预算 `400_000`（默认）+ 每节点超时（模型 180s / 工具 60s / web_fetch 30s）。
- 失败回喂：错误一律作为 `role=tool` 观察消息回喂（禁吞异常），按四类分类恢复——瞬时（429/5xx/超时：指数退避 3 次 ±抖动）、模型错误（参数缺错：附 `recovery_hint` 重 prompt 同一工具）、永久（工具不存在/权限拒：fallback 告知用户换路）、资源（预算耗尽：checkpoint + 升级人类）。
- 打转检测：① 同指纹——连续 3 次相同 `tool_name + args 指纹`（canonical JSON 的 sha256）→ 第 3 次回喂纠正性观察，第 4 次仍相同 → 中断询问用户；② 无进展——连续 3 轮工具结果 `error_type` 同为 permanent，或连续 3 轮无新 target（文件路径/命令/URL 全集无变化）→ 同样中断询问用户（对照 Hermes「重复失败/无进展」guardrails）。
- 墙钟与并行：交互式场景不设单轮硬墙钟（节点超时即事实上限）；互不依赖的工具调用并行，**有界池上限 8**；`mutating=true` 的工具强制串行；**同 target 指纹的读写互斥**（同批出现 read(X) 与 write(X) 时写排他，防竞态）；部分失败独立回喂、互不阻塞。
- 流式与可中断：模型流式增量 → CLI 实时渲染；Ctrl+C 在回合边界捕获 → 取消流与子进程 → 打印本回合副作用清单 → 保存 checkpoint。

**落地规格**

```python
# axon/agent/loop.py
@dataclass
class TurnResult:
    final_text: str
    stop_reason: Literal["verified", "max_turns", "budget_exhausted", "user_interrupt", "stuck_break"]
    turns_used: int
    tokens_used: int

class AgentLoop:
    def __init__(self, provider: LLMProvider, gateway: SandboxGateway,
                 ctx_builder: ContextBuilder, verifier: TurnVerifier,
                 config: AgentConfig) -> None: ...
    def run(self, session: Session, user_input: str) -> TurnResult:
        # 1. append(user msg) → 2. while turn < max_turns:
        #    try:  # C 层每轮重组装；超水位抛 CompactionNeeded（见 §4.3）
        #        ctx = ctx_builder.assemble(session, history)
        #    except CompactionNeeded:
        #        compactor.compact(session, history)   # 80/90 双水位，前瞻触发
        #        ctx = ctx_builder.assemble(session, history)   # 压缩后重组装
        #    resp = provider.chat(ctx.messages, ctx.tools, stream=True)
        #    if resp.tool_calls:
        #        results = gateway.execute_batch(resp.tool_calls)  # T+L 层
        #        append(assistant msg + tool results)               # 失败也回喂
        #        if stuck_detector.observe(resp.tool_calls): break → 问用户
        #    else:
        #        v = verifier.verify(session, resp.text)   # V 层
        #        return TurnResult(...)
```

### 4.2 T 工具注册

**设计决策**
- **注册 ≠ 披露 ≠ 授权**三层分离：注册表（进程启动时全量注册）→ 披露（每轮送给模型的工具子集）→ 授权（每次调用过沙箱网关单独判定）。敏感动作（shell/delete）可单独收紧披露或授权。
- v1 工具集小（8 内置 + 启用中的 MCP 工具），**全量固定序披露**——不做按需检索（那会重排工具表、破坏前缀稳定；Codex 式动态加载留 v2）。序列化确定性：`json.dumps(schema, sort_keys=True, separators=(",", ":"))`。
- 执行环境：**全部工具经沙箱网关进程内软隔离执行**（需求文档明确非内核隔离）；写操作影响半径 = 路径策略决定的范围。
- **凭证不进执行环境**：API key 只存在于 Provider 层内存；工具 handler 签名中无凭证参数；`web_fetch` 出网前经 secret scan。

**落地规格**

```python
# axon/tools/registry.py
@dataclass(frozen=True)
class ToolSpec:
    name: str                       # 稳定 id，进前缀后永不改名
    description: str                # 写"做什么/输出格式/约束/非直观参数示例"
    parameters: dict                # JSON Schema（openai function 格式）
    handler: Callable[[ToolArgs, GatewayHandle], ToolResult]
    risk: RiskClass = RiskClass.READ   # READ|WRITE|MUTATE|DELETE|SHELL|NETWORK|MCP
    mutating: bool = False          # True → 强制串行
    parallel_safe: bool = True
    timeout: float = 60.0

class ToolRegistry:
    def register(self, spec: ToolSpec) -> None: ...      # 幂等，重名拒绝
    def disclosed(self) -> list[ToolSpec]: ...           # 固定注册序
    def openai_tools(self) -> list[dict]: ...            # 序列化确定性

@dataclass
class ToolResult:
    call_id: str
    name: str
    ok: bool
    output: str                     # C 层统一截断（默认 ≤1500 token）
    error_type: Literal["transient","model","permanent","resource"] | None = None
    recovery_hint: str | None = None
    meta: dict = field(default_factory=dict)   # path/exit_code/bytes...
```

内置工具清单（v1）：

| 工具 | 风险档 | 并行安全 | 说明 |
|---|---|---|---|
| `read_file` | 绿 | 是 | path + offset/limit；超大全文截断回引用 |
| `write_file` | 黄 | 否 | 新建/覆写；写前快照（L 层 BackupHook） |
| `edit_file` | 黄 | 否 | old_string→new_string 唯一匹配替换；失败回 `model` 类错误+hint |
| `search_files` | 绿 | 是 | glob 名匹配 + 内容正则；剪枝 `.git/node_modules/__pycache__`；命中上限 100 |
| `web_fetch` | 按目标分档 | 是 | 本地回环/内网 → 绿；远程 → 红（默认询问）；secret scan 后出网 |
| `execute_shell` | 红 | 否 | 命令过 CommandRuleHook；cwd=workspace；超时 kill |
| `memory_read` | 绿 | 是 | 读 `AXON.md`（受 memory_max_tokens 约束） |
| `memory_write` | 黄 | 否 | 治理校验（§4.4）后追加/更新条目 |

MCP 接入（`axon/tools/mcp_client.py`）：

```python
# 依赖 mcp>=2.0（v2 一等公民 Client；2026-10-13 核实官方文档）
from mcp import Client
# StdioServerParameters 若 v2 调整导出位置，按官方迁移指南改导入路径（见 §15 未知区）
from mcp import StdioServerParameters

@dataclass
class MCPServerConfig:           # MCP 连接器配置（对应 §10 mcp.servers[]）
    name: str                     # 披露名前缀：mcp__<name>__<tool>
    transport: Literal["stdio","http"]
    url: str | None = None        # transport=http
    command: str | None = None    # transport=stdio
    args: list[str] = field(default_factory=list)
    enabled: bool = True          # 整体开关
    tools: dict[str, bool] = field(default_factory=dict)   # 单独开关，缺省全开

class MCPConnector:
    def __init__(self, cfg: MCPServerConfig, gateway: SandboxGateway): ...
    async def connect(self) -> None:
        target = cfg.url if cfg.transport == "http" else StdioServerParameters(command=cfg.command, args=cfg.args)
        self._client_cm = Client(target)      # async with 进入即协商协议版本
        self._client = await self._client_cm.__aenter__()
    async def list_tools(self) -> list[ToolSpec]:
        # 每个 MCP 工具包一层 gateway 代理 handler：调用 = Action(kind="mcp") 过网关
        # 披露名：mcp__<server>__<tool>，避免与内置工具撞名
```

### 4.3 C 上下文管理

**设计决策**
- 窗口预算（默认 128k，按模型 config 调整）分配：系统指令 ≤8% / 工具定义 ≤10% / 长期记忆 ≤4% / 历史与工具结果可变 / 预留 10% 给新输入+输出。
- 双水位：**80% 预警**（清理阶段：丢弃最旧工具输出原文，留 1 行占位）/ **90% 强制压缩**（结构化摘要，需求文档指定 90% 为触发线）。**前瞻触发**：当前占用 + 本轮预估增量（上轮增量滑动均值）≥ 水位即触发；force 触发后目标压至 ≤70%，留 margin 吸收单轮最坏增量（输出 8k + 并行 8×1.5k ≈ 20k，超过 128k 窗的 10% 预留 12.8k 的情形）。**80% 清理与 90% 压缩同为前缀重写事件，双双记失效账**（Cache-Aware Invalidation）。
- 每轮从 DB 重放**重新组装**；稳定前缀 = 系统指令 + 工具定义 + 记忆块（序列化确定、无时间戳/随机 id）；历史只追加。更新放尾部，稳定留头部。
- 外部内容消毒：web/page/file 结果包 `<untrusted source="...">…</untrusted>` 截断标记，系统指令含防注入声明。
- 结果复用：同回合同参数只读工具结果按内容寻址缓存（键 = 工具名 + canonical args + 文件 fingerprint(mtime,size)；web_fetch 显式设 TTL=0，仅同回合有效）；**写操作禁缓存**；失效条件先于缓存实现定义（版本/TTL/权限）。

**落地规格**

```python
# axon/agent/context.py
class ContextBuilder:
    def assemble(self, session: Session, history: list[Message]) -> AssembledContext:
        # [system(固定模板, 无时间戳)]
        # + [memory_block(MemoryStore.render_block(), ≤4000 token)]
        # + [tool_defs(固定注册序, sort_keys 序列化)]
        # + [history(只追加; 超期工具输出已折叠)]
        # + [本轮新消息]
        # 组装后 estimate_tokens + 预估增量（上轮滑动均值）；
        # 占用+增量 ≥ warn_ratio → 标记待清理；≥ force_ratio → 抛 CompactionNeeded（前瞻触发）
```

```python
# axon/agent/compaction.py —— 90% 强制压缩四阶段（对齐 Hermes 双压缩思路，代码自实现）
class Compactor:
    def compact(self, session: Session, messages: list[Message]) -> CompactResult:
        # 1. 清旧工具输出：最旧 50% 工具结果 → 占位 "[tool output elided, turn N]"
        # 2. 定保护边界：最近 K 轮（默认 6）原文不动
        # 3. 结构化摘要（主模型一次调用，9 字段）：
        #    目标/约束/进展/关键决策/涉及文件/错误与修复/待办/用户偏好/下一步
        # 4. 清孤立工具对（call 没有配对 result 或反之）；
        #    保护边界内（最近 K 轮）的未配对 call 不清——resume 断点依赖它，见 §4.4
        # 结果写 compaction_events；摘要消息插在被压区间头部
```

摘要消息结构（9 字段，入窗格式）：

```
<conversation_summary turns="12-58" tokens="41000">
goal: ...
constraints: ...
progress: ...
decisions: ...
files_touched: ...
errors_and_fixes: ...
open_questions: ...
user_preferences: ...
next_steps: ...
</conversation_summary>
```

### 4.4 S 状态存储

**设计决策**
- SQLite3（`./sessions/sessions.db`，WAL，单写者——CLI 单进程天然成立；`busy_timeout=5000` 兜底）。
- 修改前自动快照到 `workspace/.axon/snapshots/<session>/<ts>/<相对路径>`；删除优先 `send2trash`，失败降级 `workspace/.axon/trash/`；批量删除 > 阈值（默认 10）→ 红档审批。
- 记忆治理：`AXON.md` 条目带头信息（`scope=project|global`、`confidence=verified|inferred`、`added`、`source`）；inferred 不得被当事实引用（系统提示显式声明）；记忆总量超 `memory_max_tokens` → `memory_write` 拒绝并提示整理。
- 证据版本绑定：压缩摘要带 turn 范围；记忆条目带 `added` turn；恢复时按 turn 校验旧结论有效性。
- **会话恢复 ≠ 外部行动恢复**：resume 只重放消息，不重放执行；上回合副作用清单从 `audit_log` 重建后展示给用户；并向模型注入 `<resume_context>` 观察消息（中断点 turn 号 + 已完成副作用清单 + 未配对工具调用状态），防止非幂等 shell 被重复执行、孤立 tool_call 被静默清除。

**落地规格**

```python
# axon/session/db.py —— schema DDL（建表即此三条 + 索引）
# sessions(id TEXT PK, project_dir, title, provider, model, status, created_at, updated_at)
# messages(id INTEGER PK AUTOINCREMENT, session_id FK, turn, role, content,
#          tool_calls JSON, tool_call_id, name, tokens_est, created_at)
# compaction_events(id PK, session_id, at_turn, trigger[warn|force],
#                   before_tokens, after_tokens, summary, created_at)
# audit_log(id PK, session_id, ts, kind, target, detail JSON, verdict,
#           rule, result, created_at)

class SessionStore:
    def create_session(self, project_dir: str, provider: str, model: str) -> str: ...
    def append_message(self, session_id: str, msg: Message) -> None: ...
    def load_messages(self, session_id: str) -> list[Message]: ...
    def latest_session(self, project_dir: str) -> Session | None: ...
    def record_compaction(self, ev: CompactionEvent) -> None: ...
    def last_turn_side_effects(self, session_id: str) -> list[AuditRecord]: ...
```

```python
# axon/memory/store.py
@dataclass(frozen=True)
class MemoryEntry:
    scope: Literal["project","global"]
    confidence: Literal["verified","inferred"]
    text: str
    added_turn: int
    source: str          # 用户明说 / 任务观察 / 推断
class MemoryStore:
    def render_block(self) -> str: ...          # 注入稳定前缀，≤4000 token
    def add_entry(self, e: MemoryEntry) -> None  # 治理：预算/去重/禁推测升级
```

### 4.5 L 生命周期钩子

**设计决策**
- 单一入口 `SandboxGateway.execute()`：所有工具调用（含 MCP）先 `check()` 后执行；模型无法绕过（工具 handler 只收到绑定的 `GatewayHandle`，拿不到裸 fs/shell）。
- Hook 管线（pre 短路、deny 优先）：`PathPolicy → CommandRule → NetworkPolicy → SecretScan → RiskGrade →（ask 时 Prompter）→ Audit`。
- 路径三态：最长前缀匹配；同长 `deny > ask > allow`；`realpath` 解析后判定（symlink 逃逸检测）；默认策略 = workspace allow / 其余 ask / 内置 deny 表（系统目录、用户根、密钥目录，占位符 `<workspace>` `<home>` `<system>` 加载时展开）。
- 命令规则：glob 前缀三态表；**未命中默认 ask**（fail-closed：未知命令=潜在外部程序）；白名单排除（git/docker 等）手动开启后仅跳过 CommandRule，仍过路径/网络/审计。
- 网络策略：本地回环/内网默认放行；远程默认询问；域名黑白名单（deny 优先）；出网前 secret scan（`sk-`、`api_key=`、`Bearer `、`AKIA`、私钥头等正则）。
- 人在回路四档：🟢自动（读/检索/本地网）/ 🟡执行后告知（写，留快照）/ 🔴执行前确认（删、批量改、shell、远程网、跨目录、MCP）/ ⚫阻断（黑名单）。审批降级：同会话同**命令前缀指纹**重复审批 → 用户可选"本会话记住"（授权缓存键 = 命令首 token + 参数骨架；`approval_granularity: prefix|exact`，默认 `prefix`——完整指纹对高频变参命令如 python/node 形同虚设；`/audit` 可查可清）。
- 纵深防御：路径/命令/网络三层**互相独立**，任一命中即拦截；v1 门控**无模型参与**（纯确定性）→ 无提示注入面；快筛模型 v2 候选且不得看执行者推理。

**落地规格**

```python
# axon/sandbox/gateway.py
@dataclass(frozen=True)
class Action:
    kind: Literal["fs_read","fs_write","fs_edit","fs_delete","shell","network","mcp"]
    target: str                    # 路径 / 命令串 / URL / mcp__server__tool
    detail: dict                   # canonical JSON（sort_keys）args 快照
    session_id: str
    turn: int

@dataclass(frozen=True)
class Decision:
    verdict: Literal["allow","ask","deny"]
    rule: str                      # 命中哪条规则（审计用）
    reason: str

@dataclass
class Approval:
    approved: bool
    remember_session: bool = False # 会话级授权缓存键 = 命令前缀指纹（prefix/exact 由 approval_granularity 定，§4.5/§10）
    edited_detail: dict | None = None  # 用户可改参数后放行（如改命令）

class SandboxGateway:
    def __init__(self, config: SandboxConfig, hooks: list[Hook],
                 audit: AuditLog, prompter: Prompter) -> None: ...
    def check(self, action: Action) -> Decision: ...       # 纯函数管线，无 IO 外审批
    def execute(self, call: ToolCall, spec: ToolSpec) -> ToolResult:
        # 1. spec.handler 包装成 Action（kind 由 spec.risk 映射）
        # 2. d = self.check(action)；deny → ToolResult(ok=False, error_type="permanent", reason)
        # 3. ask → prompter.confirm(action, d)；拒绝同上；remember → 授权缓存
        # 3.5. 副作用预备：risk ∈ {WRITE,MUTATE} → BackupHook 快照到 snapshots/；
        #      risk = DELETE → TrashHook 接管（send2trash，失败降级 trash/）
        # 4. handler(ToolArgs, GatewayHandle) 执行（工具内部不再自查）
        # 5. post hooks（Backup/Trash 已在前置完成；此处落 result 归一化）
        # 6. audit.record(action, verdict, rule, result)   # 含被拒动作
```

```python
# axon/sandbox/hooks.py
class Hook(Protocol):
    name: str
    def pre(self, action: Action, ctx: HookCtx) -> Decision | None: ...   # None=不表态
    def post(self, action: Action, result: ToolResult, ctx: HookCtx) -> None: ...
# 内置 hooks：PathPolicyHook / CommandRuleHook / NetworkPolicyHook /
#             SecretScanHook / RiskGradeHook / BackupHook / TrashHook / AuditHook
# 测试替身：AutoApprovePrompter / AutoDenyPrompter（回归集无 TTY 跑通）

# ---- 动作分类统一口径（独立评审回修：RiskClass / Action.kind / config 三套归一） ----
# Action.kind : fs_read | fs_write | fs_edit | fs_delete | shell | network | mcp
# RiskClass   : READ | WRITE | MUTATE | DELETE | SHELL | NETWORK | MCP
# 映射：fs_read→READ, fs_write→WRITE, fs_edit→MUTATE, fs_delete→DELETE,
#       shell→SHELL, network→NETWORK, mcp→MCP
# 档位升级：跨目录访问由 PathPolicy 把 fs_* 升一档（黄→红）；
#       批量删除（> batch_delete_threshold）由 fs_delete 升红档审批；
#       本地/远程网络由 NetworkPolicy 分档（local_allow→绿，remote→红）
```

### 4.6 V 评估接口

**设计决策**
- v1 验证手段 = **确定性规则集**（验证强度阶梯第一档）：① 无未解决工具错误（或已带 hint 重试 ≥1 次）；② 本回合红档动作全部有审批通过记录；③ 写文件回读校验（内容一致或明确失败）；④ shell 退出码检查（非 0 → 观察回喂而非算完成）；⑤ 打转检测未触发；⑥ 终答非空。
- **独立读路径（CQRS）**：verifier 从 `audit_log` / DB 读证据，不经过模型上下文。
- **生成者 ≠ 评估者**：v1 评估者 = 确定性规则 + 人类用户（每个红档动作即人工闸门）+ 回归测试集（独立上下文）；模型自评禁止。独立模型裁判列 v2。
- 工具回执 ≠ 业务事实：write 看回读、shell 看退出码+输出、web_fetch 看状态码+内容长度——全部转成 V 层可断言条件。
- 迭代预算：`max_turns` + token 预算双闸，防评估死循环。

**落地规格**

```python
# axon/agent/verify.py
class TurnVerifier:
    def __init__(self, store: SessionStore) -> None: ...
    def verify(self, session: Session, final_text: str) -> VerifyResult:
        # rules = [no_unresolved_errors, red_actions_approved, writes_readback_ok,
        #          shell_exitcodes_ok, no_stuck, final_nonempty]
        # no_unresolved_errors 定义（独立评审回修，消二义）：末态不存在
        # error_type ∈ {transient, resource} 的悬挂结果；permanent 错误若已向用户
        # 明示替代路径并继续，记审计但不判负（避免永久判负与重试即过宽两个极端）
        # 每条规则读 audit_log/messages 断言；全过 → stop_reason="verified"
        # 任一不过 → 生成"未通过项 + 建议"观察消息回喂模型（计入 turns 预算）
```

回归集（`tests/regression/`，CI 可跑）：`test_path_tristate`（越界 ask、deny 表命中、symlink 逃逸）、`test_command_rules`（allow/ask/deny/默认 ask/白名单 bypass）、`test_compaction`（80%/90% 触发、摘要 9 字段、孤立工具对清理）、`test_resume`（消息重放、副作用清单、不重放执行）、`test_audit_export`（CSV/JSON 导出字段完整）、`test_risk_gate`（红档必须审批、批量删除阈值）、`test_memory_governance`（inferred 标记、预算拒绝）。

---

## 5. 模型组合与任务分派

- **形态判定：A（单智能体 + 单模型）**。
  - 显式质疑记录：任务确实异质（编码/检索/执行混杂），形态 B（循环内分档）看似有收益——但 v1 用户自选单一 provider+model，harness 无法假设存在第二档可用模型；且风险门控已全部下沉确定性代码（零 token），剩余认知步骤同质（主推理 + 摘要）。结论：v1 形态 A，`ModelRouter` 接口预留（v2 分档主战场）。
- **任务-模型分派表**：

| 步骤 | 档位 | 理由 | 成本/延迟预算 |
|---|---|---|---|
| 主推理循环（每轮） | 强力 LLM（用户自选） | 开放推理+工具选择 | ~180s 超时；占 session token ~70% |
| 90% 压缩摘要（9 字段） | 强力 LLM（同一主模型） | 需要跨步因果压缩；v1 无第二档可用 | 单次 ~4k token 输出上限；占 ~10% |
| 风险分级/路径三态/命令规则 | 确定性代码 | 答案集可枚举、可断言 | 0 token，<10ms |
| 打转检测/参数指纹 | 确定性代码 | sha256 比对 | 0 token |
| token 估算 | 确定性代码（tiktoken） | 可复现计数口径 | 0 token（本地算力） |
| 终答验证（V 层） | 确定性代码 + 人类 | 禁模型自评 | 0 token |
| 工具结果截断/占位折叠 | 确定性代码 | 纯文本工程 | 0 token |

- **token 预算双账**（独立评审回修：累计口径下比例无法同时成立，拆账）：
  - **窗口账**（单请求）：≤ `context_window`（默认 128k），由 C 层管；压缩触发按此计算。
  - **会话累计账**（计价 token）：默认 400k，由 E 层管；**含每轮前缀重发**（乘数项：轮次 × 平均前缀），命中缓存部分按 cached 价计（usage 观测）。
  - 70/10/15/5 为**新增 token 的用途构成**（增量口径）：主推理 ~70% / 压缩摘要 ~10% / 工具结果重述 ~15% / 系统+工具+记忆 ~5%；评估/验证步骤计入预算（V 层规则 0 token，人工审批不计模型 token）。
  - 一致性检查：50 轮 × 平均前缀（~10-28k）的累计重发会先于 400k 增量耗尽预算——这是**设计意图**（预算闸本身就是触发压缩/收尾的信号），不是口径错误；窗口账保证任何单轮不越窗。
- **缓存与重算账**：前缀稳定区（系统+工具+记忆）全程只追加不动 → 理论全命中；**80% 清理与 90% 压缩均为主动重写事件，双双记录 `before/after_tokens` 与估算重算代价**（compaction_events 表即账本，trigger 区分 warn/force）；命中率观测口径 = provider 返回 `usage.prompt_cache_hit_tokens`（hy3/OpenAI 兼容端多支持；无该字段时记 "unavailable"）。目标：**无重写轮命中率 ≥90%**（工程目标，非厂商承诺）。
- **级联升级条件**：v1 无低档模型 → N/A；`ModelRouter` v2 启用后按 h-framework 规则补（置信度阈值/重试次数）。
- **降级链**：primary provider 瞬时错误 → 指数退避 3 次；仍失败 → 错误观察回喂 + 告知用户 + checkpoint；**不静默切换 provider**（用户自选是显式决策，切换须用户确认）。codebuddy 路 SDK 异常 → 按同一链路处理。
- **评估者档位与生成者关系**：v1 评估者 = 确定性规则 + 人类 + 回归集，均独立于生成上下文，不共享推理过程。

---

## 6. 多 Agent 编排

**不适用**。编排决策卡六步全过：

| 步 | 结论 |
|---|---|
| 1 真的需要吗 | 单用户 CLI 对话式任务，子任务不独立、并行收益 < 协调成本；"专业化"需求已被「主模型 + 确定性门控 + ModelRouter 预留」覆盖 → 单 Agent |
| 2 拓扑 | N/A |
| 3 委派契约 | N/A |
| 4 隔离边界 | N/A |
| 4.5 模型档位 | N/A |
| 5 熔断 | 由 E 层 max_turns/token/超时三道 + 打转检测承担 |
| 6 反模式自查 | 无共享可变状态（SQLite 单写）、无无界扇出（并行上限 8）、无过度分解 |

v2 候选触发条件（记录备查）：任务真可拆分且并行收益显著 / 需要独立上下文隔离的子代理 / 远程执行需求——命中任一且形态 B 无法替代时才启动决策卡。

---

## 7. 模块 / 目录结构（代码骨架）

```
axon/                          # Python 包（pyproject.toml，entry point: axon）
├── __init__.py                  # 版本号 __version__
├── __main__.py                  # python -m axon
├── cli.py                       # argparse + REPL + rich 流式渲染 + 斜杠命令
│                                 #   /help /sessions /resume /compact /memory /audit /exit
├── config.py                    # config.yml 加载/校验/占位符展开/默认值合并
├── agent/
│   ├── loop.py                  # E：AgentLoop（轮次/预算/中断/并行调度）
│   ├── context.py               # C：ContextBuilder.assemble（每轮重组装）
│   ├── compaction.py            # C：Compactor 四阶段 90% 压缩
│   ├── verify.py                # V：TurnVerifier 确定性规则集
│   └── tokenizer.py             # token 估算（tiktoken + 启发式兜底）
├── providers/
│   ├── base.py                  # LLMProvider 协议 + Message/StreamEvent 归一化
│   ├── codebuddy.py             # codebuddy_agent_sdk 适配（懒加载，见 §15）
│   ├── openai_compat.py         # openai 包适配（Chat Completions）
│   └── router.py                # ModelRouter（v1 直通单 provider；v2 分档入口）
├── tools/
│   ├── registry.py              # T：ToolSpec/ToolRegistry（注册≠披露≠授权）
│   ├── builtin/
│   │   ├── file_read.py  file_write.py  file_edit.py
│   │   ├── file_search.py             # glob+内容搜索，剪枝+命中上限
│   │   ├── web_fetch.py               # httpx+bs4，本地/远程分档
│   │   ├── shell.py                   # subprocess，cwd=workspace，超时 kill
│   │   └── memory.py                  # memory_read / memory_write
│   └── mcp_client.py            # MCP v2 Client 适配 + 网关代理包装
├── sandbox/
│   ├── gateway.py               # L：SandboxGateway.check/execute 唯一入口
│   ├── hooks.py                 # Hook 协议 + 8 个内置 hook
│   ├── path_policy.py           # 三态+realpath+占位符+deny 默认表
│   ├── command_rules.py         # glob 前缀三态 + fail-closed 默认 ask
│   ├── network_policy.py        # 本地判定/域名双单/secret scan
│   ├── risk.py                  # RiskClass 分级 + 四档映射
│   ├── backup.py                # 快照/回收站降级
│   └── audit.py                 # audit_log 读写 + CSV/JSON 导出
├── memory/
│   └── store.py                 # AXON.md 治理（scope/confidence/预算）
├── session/
│   ├── db.py                    # SQLite schema + SessionStore
│   └── resume.py                # 恢复加载 + 副作用清单重建
├── prompts/
│   └── system.py                # 系统指令模板（稳定前缀，无时间戳）
├── tests/regression/            # V 层回归集（无 TTY，AutoApprove/AutoDeny 替身）
├── config.yml                   # 示例配置（随包分发）
├── README.md                    # GitHub 风格（需求文档 #6）
└── pyproject.toml               # 依赖与 entry point

sessions/sessions.db             # 运行时生成（默认位置，config 可改）
workspace/                       # 默认工作空间（config 可改）
AXON.md                        # 长期记忆（项目目录）
```

---

## 8. 关键接口签名

### 8.1 数据源接口（模型 Provider）

```python
# axon/providers/base.py
@dataclass
class Message:
    role: Literal["system","user","assistant","tool"]
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)   # assistant 消息
    tool_call_id: str | None = None                            # tool 消息
    name: str | None = None

@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict          # 已解析 JSON

@dataclass
class StreamEvent:           # provider.chat 的产出流
    kind: Literal["text","tool_call","usage","done"]
    text: str = ""           # kind=text 的增量
    tool_call: ToolCall | None = None
    usage: dict = field(default_factory=dict)   # prompt_tokens/completion_tokens/prompt_cache_hit_tokens

class LLMProvider(Protocol):
    name: str
    def chat(self, messages: list[Message], tools: list[dict] | None, *,
             model: str, timeout: float = 180.0) -> Iterator[StreamEvent]: ...
    # openai 路：client.chat.completions.create(model=..., messages=..., tools=...,
    #                stream=True, stream_options={"include_usage": True})
    #          chunk.choices[0].delta.content / .tool_calls；chunk.usage
    # codebuddy 路：SDK 适配（导入面以实现时为准，见 §15），输出归一同上
```

### 8.2 工具接口

```python
# axon/tools/registry.py（字段定义见 §4.2 ToolSpec / ToolResult）
def tool_result_ok(call: ToolCall, output: str, meta: dict | None = None) -> ToolResult: ...
def tool_result_err(call: ToolCall, error_type: str, hint: str) -> ToolResult: ...

# axon/tools/builtin/shell.py
def execute_shell(args: ToolArgs, gw: GatewayHandle) -> ToolResult:
    # args: {"command": str, "timeout_sec": int=60}
    # 命令串作为 Action(kind="shell", target=command) 交 gw.execute 前置判定；
    # subprocess.run(cmd, shell=True, cwd=workspace, capture_output=True,
    #                text=True, timeout=args.timeout_sec)；超时 kill + error_type=transient
    # 退出码非 0 → ok=False, error_type="model", recovery_hint=stderr 摘要

# ---- 支撑类型（可编码三门槛·类型完整） ----
ToolArgs = dict[str, Any]        # 已按 ToolSpec.parameters 完成 JSON Schema 校验的入参

@dataclass
class GatewayHandle:             # 绑定会话上下文的网关句柄（只读上下文 + 子动作上报）
    session_id: str
    turn: int
    workspace: str               # 绝对路径（shell cwd / 快照根）
    def note_subaction(self, action: Action) -> None: ...
    # 用途：shell 等复合工具把派生动作（如生成的临时文件路径）上报审计；
    # 工具自身不做权限自查——授权判定唯一发生在 SandboxGateway.execute
```

### 8.3 钩子接口

```python
# axon/sandbox/hooks.py（字段定义见 §4.5 Action / Decision / Hook）
# ---- 支撑类型（可编码三门槛·类型完整；独立评审回修补齐） ----
@dataclass
class VerifyResult:              # V 层单轮验证结论
    passed: bool
    failed_rules: list[str]       # 未过规则名（回喂模型的观察消息素材）
    advice: str = ""              # 给模型的纠正建议

@dataclass
class HookCtx:                   # Hook 执行上下文（只读）
    action: Action
    session_id: str
    turn: int
    workspace: str

@dataclass
class CompactionEvent:           # S 层压缩/清理事件（失效账本一行）
    session_id: str
    at_turn: int
    trigger: Literal["warn","force"]
    before_tokens: int
    after_tokens: int
    summary: str                  # force 事件为 9 字段摘要；warn 事件为占位说明
    created_at: str

class Prompter(Protocol):        # 人在回路确认接口（CLI 实现 + 测试替身）
    def confirm(self, action: Action, decision: Decision) -> Approval: ...

def register_builtin_hooks(config: SandboxConfig) -> list[Hook]:
    return [PathPolicyHook(config), CommandRuleHook(config), NetworkPolicyHook(config),
            SecretScanHook(config), RiskGradeHook(config), BackupHook(config),
            TrashHook(config), AuditHook(audit)]
```

### 8.4 上下文与压缩接口

```python
# axon/agent/context.py
@dataclass
class AssembledContext:
    messages: list[Message]
    tools: list[dict]
    tokens_est: int
    prefix_tokens: int          # 稳定前缀大小（观测命中率用）

class ContextBuilder:
    def assemble(self, session: Session, history: list[Message]) -> AssembledContext: ...

# axon/agent/compaction.py
@dataclass
class CompactResult:
    messages: list[Message]
    before_tokens: int
    after_tokens: int
    summary: str
class Compactor:
    def compact(self, session: Session, messages: list[Message]) -> CompactResult | None: ...
```

---

## 9. 核心数据结构

### 9.1 状态结构

```python
@dataclass
class Session:
    id: str                     # uuid4 hex
    project_dir: str            # 绝对路径
    title: str
    provider: str               # codebuddy | openai
    model: str
    status: Literal["active","compacted","closed"]
    created_at: str             # ISO8601 UTC
    updated_at: str

@dataclass
class AuditRecord:
    ts: str; session_id: str; kind: str; target: str
    detail: dict                # canonical JSON args 快照
    verdict: str                # allow | ask_approved | ask_denied | deny
    rule: str                   # 命中规则标识
    result: str                 # ok | error | blocked
```

### 9.2 消息/任务结构

`Message` / `ToolCall` / `StreamEvent` / `ToolResult` / `Action` / `Decision` / `Approval` / `TurnResult` / `VerifyResult` —— 字段定义见 §4 与 §8（每个结构均在该节给出完整字段，无仅出现名字的类型）。

### 9.3 配置结构

```python
@dataclass
class AXONConfig:
    workspace: str              # 绝对路径（占位符展开后）
    project: str
    model: ModelConfig          # active_provider / context_window / max_output_tokens /
                                # session_token_budget / providers{codebuddy,openai}
    context: ContextConfig      # compact_warn_ratio=0.8 / compact_force_ratio=0.9 /
                                # tool_result_max_tokens=1500 / memory_max_tokens=4000
    sandbox: SandboxConfig      # paths/commands/network/risk_grading/batch_delete_threshold
    memory: MemoryConfig        # file=AXON.md / max_entries=200
    session: SessionConfig      # db=./sessions/sessions.db / audit_retention_days=90
    mcp: MCPConfig              # servers[]
```

---

## 10. 配置文件格式（config.yml 完整示例）

```yaml
# ============ AXON 配置（声明式单一事实源；CLI 参数可覆盖） ============
version: 1

workspace: ./workspace          # 默认工作空间；文件工具默认在此操作（不强制，越界走确认）
project: .                      # 项目目录（区分不同项目 + 绑定会话），默认当前目录

model:
  active_provider: codebuddy    # codebuddy | openai（用户自选）
  context_window: 128000        # 模型上下文窗口（token），压缩阈值按此计算
  max_output_tokens: 8192
  session_token_budget: 400000  # 单会话 token 总预算（E 层第二道保险）
  providers:
    codebuddy:
      # 凭证只走环境变量，禁止写进本文件（L 层 SecretScan 会拦）
      env_api_key: CODEBUDDY_API_KEY
      env_environment: CODEBUDDY_INTERNET_ENVIRONMENT
      default_model: hy3        # 需求文档指定：开发期用 hy3 测试
      timeout_sec: 180
    openai:
      env_api_key: OPENAI_API_KEY
      base_url: https://api.openai.com/v1   # 任意 OpenAI 兼容端点
      default_model: gpt-4o-mini            # 示例值，用户自选
      timeout_sec: 180

context:
  compact_warn_ratio: 0.8       # 80% 水位：清理最旧工具输出（留占位）
  compact_force_ratio: 0.9      # 90% 水位：结构化强制压缩（需求文档指定）
  tool_result_max_tokens: 1500  # 单条工具结果入窗截断
  memory_max_tokens: 4000       # AXON.md 注入稳定前缀的上限

sandbox:
  backup:
    enabled: true
    dir: .axon/snapshots      # 相对 workspace；修改前快照
  trash:
    dir: .axon/trash          # send2trash 失败时的降级回收目录
  batch_delete_threshold: 10    # 单次删除文件数超过 → 红档审批
  paths:                       # 路径三态；最长前缀匹配，同长 deny>ask>allow
    - { path: "<workspace>", mode: allow }
    - { path: "<system>", mode: deny }     # 展开为 C:\Windows / /usr / /etc 等
    - { path: "<home>/.ssh", mode: deny }
    - { path: "<home>", mode: deny }       # 用户根默认拒；需要就单独开子路径，如：
    # - { path: "<home>/Documents", mode: ask }
  commands:                    # glob 前缀三态；deny 优先；未命中默认 ask（fail-closed）
    allow: ["ls","cat","pwd","echo","find","grep","rg","git status","git diff","git log","git add","git commit"]
    ask:   ["rm","mv","cp","pip install","npm install","curl","wget","docker","ssh","scp","chmod","chown","kill","python","node"]
    deny:  ["rm -rf /","rm -rf /*","mkfs","format","dd if=","shutdown","reboot",":(){"," > /dev/sd"]
  command_bypass_whitelist: [] # 手动开启后这些前缀跳过命令规则（仍过路径/网络/审计）
  network:
    local_allow: true          # localhost / 127.0.0.1 / ::1 / 私网段 默认放行
    remote_default: ask        # 远程请求默认询问（需求文档）
    domain_allow: []           # 白名单（命中 → 自动放行档）
    domain_deny: []            # 黑名单（命中 → 直接阻断，优先于白名单）
    secret_scan: true          # 出网前敏感信息扫描
  approval_granularity: prefix # 授权缓存键粒度：prefix=命令首token+参数骨架 | exact=完整指纹
  risk_grading:                # 动作 → 人在回路档位映射（四档）；键 = Action.kind 统一口径
    green_auto:   ["fs_read"]
    yellow_notify:["fs_write","fs_edit"]
    red_confirm:  ["fs_delete","shell","network","mcp"]
    black_block:  []           # 直接阻断清单（域名黑名单由 network.domain_deny 承担）
    # 档位升级：跨目录访问 → PathPolicy 将 fs_* 升一档；批量删除超阈值 → fs_delete 升红；
    #         本地/远程网络 → NetworkPolicy 分档（local_allow 绿 / remote 红）；memory_write 按 fs_write 黄档

memory:
  file: AXON.md              # 长期记忆文件（相对 project）
  max_entries: 200

session:
  db: ./sessions/sessions.db   # SQLite3（需求文档指定默认位置）
  audit_retention_days: 90

mcp:
  servers: []                  # MCP 连接器（整体/单独开关）；示例：
  # - name: filesystem
  #   transport: stdio         # stdio | http
  #   command: npx
  #   args: ["-y","@modelcontextprotocol/server-filesystem","<workspace>"]
  #   enabled: true            # false = 整体关闭
  #   tools: {}                # {tool_name: true|false} 单独禁用某工具
```

---

## 11. 层间交叉检查

| 关系 | 说明 |
|---|---|
| E ↔ V | 循环终止权在 V（确定性规则），E 只提供预算/超时/打转三道机械保险；V 不通过 → 观察回喂继续循环（受 turns 预算约束） |
| E ↔ C | E 每轮调用 C.assemble；压缩由 C 在组装时按 80/90 双水位**前瞻触发**（当前占用+预估增量≥水位），E 负责把 CompactionNeeded 转成一次摘要调用并重组装 |
| T ↔ L | 工具 handler 只拿 GatewayHandle；授权判定全在 L 层管线，T 层零自查——防"某工具忘记过网关" |
| T ↔ C | 工具结果统一经 C 层截断（tool_result_max_tokens）后才入窗；MCP 工具披露名 `mcp__server__tool` 固定序 |
| C ↔ S | 组装的唯一事实源是 S 层 DB 消息表（每轮重放），内存不缓存跨轮上下文；压缩事件落 compaction_events |
| S ↔ L | 快照/回收站/批量阈值是 L 层 hook 调用 S 层 IO；审计记录本身也落 S 层（audit_log 表） |
| L ↔ V | V 的红档审批核对读 L 层审计（独立读路径），不经过模型上下文 |
| 模型 ↔ 各层 | Provider 只被 E 层调用；凭证不出 Provider；压缩摘要也走同一 Provider（v1 单模型） |

---

## 12. 六层决策表

| 层 | 决策 | 依据 | 状态 |
|---|---|---|---|
| P | 插件化扩展（工具注册表 + MCP + 配置驱动） | 需求文档 MCP 管控诉求；Hermes「Skill 插件系统」对照 | 默认假设 |
| P | 声明式配置（config.yml 单一事实源） | 需求文档指定 | 已确认 |
| P | 单机部署 | 单用户本地 CLI（Q4） | 默认假设 |
| P | 中心化编排（循环即调度器） | 无子代理（§6 决策卡） | 默认假设 |
| E | 静态 tool-calling 循环，不用动态脚本编排 | 任务结构清晰单一 | 默认假设 |
| E | 终止归 V 层确定性规则，禁模型自评 | production-checklist 验证强度阶梯 | 默认假设 |
| E | 防跑飞三道：max_turns=50 + 400k token + 节点超时 | Hermes max_turns=90 对照下调（CLI 交互场景） | 默认假设 |
| E | 错误四分类回喂（瞬时/模型/永久/资源） | 禁吞异常；Hermes error_type+recovery_hint 对照 | 默认假设 |
| E | 打转检测：同指纹 3 次纠正/第 4 次中断 + 无进展（连续 3 轮同 permanent 错误或无新 target）中断 | Hermes loop_guardrails「重复失败/无进展」对照 | 默认假设 |
| E | 并行有界池 8；mutating 工具强制串行；同 target 指纹读写互斥；部分失败独立回喂 | wall-clock-bounded-parallelism 模式 + 独立评审竞态反例回修 | 默认假设 |
| E | 流式输出 + Ctrl+C 回合边界中断 + 副作用清单 | streaming-interruptibility 模式；Q7 | 默认假设 |
| T | 注册≠披露≠授权三层分离 | production-checklist T 层硬规则 | 默认假设 |
| T | v1 全量固定序披露（不做按需工具检索） | 工具集小；保前缀稳定（deterministic-serialization） | 默认假设 |
| T | 内置 8 工具（read/write/edit/search/web/shell + memory_read/memory_write） | 需求文档 #3 + 记忆系统节 | 已确认 |
| T | MCP 经 mcp>=2 Client 适配，调用包网关代理 | 2026-10-13 官方文档核实；需求文档沙箱 #5 | 默认假设 |
| T | 凭证不进执行环境（Provider 层持有） | production-checklist 硬规则 | 默认假设 |
| T | 执行环境=进程内软隔离（非内核/容器） | 需求文档明确 | 已确认 |
| C | 双水位压缩：80% 清理 / 90% 强制结构化摘要；前瞻触发（占用+预估增量），压后目标 ≤70% | 需求文档指定 90%；Hermes 85%+50% 仅取「网关层+主压缩」形态对照（结构不同：Hermes 主压缩 50% 远早于 90%） | 默认假设 |
| C | 预算分配：系统≤8%/工具≤10%/记忆≤4%/预留10% | production-checklist「预算要分配」 | 默认假设 |
| C | 每轮重组装 + 稳定前缀只追加（无时间戳/随机 id） | prefix-stable-context 模式 | 默认假设 |
| C | 外部内容 `<untrusted>` 截断标记 + 系统提示防注入声明 | 防提示注入 | 默认假设 |
| C | 同回合同参只读结果内容寻址缓存（web_fetch TTL=0）；写禁缓存；80% 清理与 90% 压缩双双记失效账 | result-memoization + cache-aware-invalidation 模式 | 默认假设 |
| C | token 估算 tiktoken + provider usage 校正 | 阈值必须可复现 | 默认假设 |
| S | SQLite WAL 单写；四表 schema（sessions/messages/compaction_events/audit_log） | 需求文档指定 SQLite3 | 已确认 |
| S | 改前快照 + 删除回收站（send2trash 降级 trash/）+ 批量删除阈值 10 | 需求文档沙箱 #1 | 已确认 |
| S | 记忆治理：scope/confidence/预算；inferred 禁升级为事实 | production-checklist 记忆治理 | 默认假设 |
| S | 证据版本绑定：摘要带 turn 范围、记忆带 added turn | evidence-version-binding | 默认假设 |
| S | 会话恢复≠外部行动恢复（副作用清单重建，不重放执行） | production-checklist S 层 | 默认假设 |
| L | 网关唯一入口 + Hook 管线（deny 优先短路） | 安检门不可绕过 | 默认假设 |
| L | 路径三态最长前缀 + realpath + 默认 deny 表（系统/用户根/密钥） | 需求文档沙箱 #1 | 已确认 |
| L | 命令 glob 前缀三态 + 未命中默认 ask（fail-closed） | 需求文档「外部程序默认暂停」推导 | 默认假设 |
| L | 网络本地放行/远程询问 + 域名黑白名单 + secret scan | 需求文档沙箱 #3 | 已确认 |
| L | 人在回路四档 + 会话级授权缓存（键=命令前缀指纹，prefix/exact 可选） | 审批量降级不失控；完整指纹对变参命令形同虚设（独立评审回修） | 默认假设 |
| L | 纵深三层独立 + v1 门控无模型参与（零注入面） | 高危拦截不单点 | 默认假设 |
| V | v1 确定性规则集（6 条断言） | 验证强度阶梯第一档 | 默认假设 |
| V | 独立读路径（CQRS：verifier 读 audit/DB） | 评估不经过生成路径 | 默认假设 |
| V | 生成者≠评估者：规则+人类+回归集 | 禁模型自评 | 默认假设 |
| V | 工具回执≠业务事实（回读/退出码/状态码核对） | production-checklist V 层 | 默认假设 |
| V | 迭代预算=max_turns + token 双闸 | 防评估死循环 | 默认假设 |
| 模型 | 形态 A（单智能体+单模型），ModelRouter 预留 | 显式质疑后结论：无第二档可用模型假设 | 默认假设 |
| 模型 | 降级链：退避→回喂→ checkpoint，不静默切换 provider | 用户自选是显式决策 | 默认假设 |
| 模型 | 缓存账本：compaction_events 记重写代价；命中率按 usage 字段观测 | cache-aware-invalidation 模式 | 默认假设 |

---

## 13. 生产级必答项（逐项回答）

| # | 必答项 | 结论 |
|---|---|---|
| 1 | 执行环境：工具在哪跑、凭证是否进执行环境 | 进程内沙箱网关软隔离（§4.2）；凭证只在 Provider 层，工具 handler 无凭证参数，出网前 secret scan |
| 2 | 错误回喂：失败如何作为观察回到上下文、如何分类恢复 | 统一 `role=tool` 观察消息；四分类：瞬时退避 3 次 / 模型错误带 hint 重 prompt / 永久 fallback / 资源 checkpoint+升级人类（§4.1） |
| 3 | 打转检测与熔断 | 连续 3 次同 tool_name+args 指纹 → 纠正观察；第 4 次 → 中断问用户；另有 max_turns/token/节点超时三保险（§4.1） |
| 4 | 记忆治理 | scope(project/global)+confidence(verified/inferred)+added_turn+source；预算超限拒绝；inferred 禁当事实（§4.4） |
| 5 | 外部副作用幂等 | shell/web 为非幂等操作：执行记录全量入 audit_log；resume 不重放执行、注入 `<resume_context>` 观察消息（中断点+副作用清单+未配对工具状态）并重建副作用清单；写文件有快照可回滚；工具结果缓存只覆盖只读路径（§4.3/§4.4） |
| 6 | 证据版本绑定 | 压缩摘要带 turn 范围、记忆条目带 added_turn；恢复时校验；旧版本验证结论不跨版本复用（§4.4） |
| 7 | 生成者与评估者分离 | v1 评估者=确定性规则+人类闸门+回归集，均独立上下文、不看推理；模型自评禁止；独立模型裁判列 v2（§4.6） |
| 8 | 前缀稳定与缓存命中 | 稳定区=系统+工具+记忆（固定序、sort_keys 序列化、无时间戳）；历史只追加；压缩=重写事件，compaction_events 记账（before/after tokens）；命中率观测=provider usage.prompt_cache_hit_tokens，目标非压缩轮 ≥90%（§4.3/§5） |

### 13.1 可编码三门槛自查（硬性）

| 门槛 | 结论 |
|---|---|
| 类型完整 | 接口签名引用的每个类型均有字段定义：`Message`/`ToolCall`/`StreamEvent`（§8.1）、`ToolSpec`/`ToolResult`/`ToolArgs`/`GatewayHandle`（§4.2/§8.2）、`Action`/`Decision`/`Approval`/`Hook`/`HookCtx`（§4.5/§8.3）、`AssembledContext`/`CompactResult`/`CompactionEvent`/`Prompter`（§8.3/§8.4）、`Session`/`AuditRecord`（§9.1）、`TurnResult`/`VerifyResult`（§4.1/§4.6/§8.3）、各级 `*Config`（§9.3）。无仅出现名字的类型 |
| API 真实 | `openai`（`client.chat.completions.create(..., stream=True, stream_options={"include_usage": True})`，delta.content/tool_calls、chunk.usage）；`mcp>=2`（`from mcp import Client`，`async with Client(url|StdioServerParameters)`，`await client.call_tool(name, args)`；来源：官方文档 py.sdk.modelcontextprotocol.io/v2《v2 的新功能》，2026-10-13 检索核实）；`httpx`/`bs4(html.parser)`/`send2trash`/`sqlite3`(WAL)/`tiktoken`(cl100k_base)/`PyYAML`(safe_load)/`prompt_toolkit`/`rich` 均为真实 API。唯一未核实项 `codebuddy_agent_sdk` 导入面 → §15 #1 占位方案（实现第一步 dir() 核对，OpenAI 兼容则复用适配器） |
| 标准可达 | 成功标准四条均有验证路径：① 全链路跑通 → `tests/regression/` 各用例；② 沙箱拦截 100% → `test_path_tristate`/`test_command_rules`/`test_risk_gate`（无 TTY，AutoApprove/AutoDeny 替身）；③ 压缩后完成率不劣化 → `test_compaction` + 同任务基线的 turns/tokens 对比；④ 审计可导出 → `test_audit_export` 校验字段完整。延迟/成本量化指标以用户实测为准（能力边界：不代厂商承诺指标） |

**占位符约定**：本文 prose 中残留的 `<...>`（`<workspace>`/`<home>`/`<system>`/`<server>`/`<tool>`/`<untrusted ...>` 等）均为**有意约定**而非未填写空位——前者为配置加载时展开的占位符（§4.5/§10），中者为 MCP 披露名模板（§4.2），后者为外部内容消毒标记（§4.3）。

---

## 14. 参考架构图

```svg
<svg viewBox="0 0 680 470" xmlns="http://www.w3.org/2000/svg" font-family="Segoe UI, sans-serif">
  <rect x="0" y="0" width="680" height="470" fill="#ffffff"/>
  <!-- User / CLI -->
  <rect x="20" y="20" width="150" height="54" rx="8" fill="#eef2ff" stroke="#6366f1" stroke-width="1.5"/>
  <text x="95" y="43" text-anchor="middle" font-size="13" fill="#1e1b4b" font-weight="600">用户 CLI（REPL）</text>
  <text x="95" y="61" text-anchor="middle" font-size="10" fill="#4b5563">流式渲染 / 审批确认 / 斜杠命令</text>
  <!-- AgentLoop -->
  <rect x="240" y="20" width="200" height="54" rx="8" fill="#f0fdf4" stroke="#16a34a" stroke-width="1.5"/>
  <text x="340" y="43" text-anchor="middle" font-size="13" fill="#14532d" font-weight="600">AgentLoop（E 层）</text>
  <text x="340" y="61" text-anchor="middle" font-size="10" fill="#4b5563">轮次/token/超时三道 + 打转检测 + 并行池≤8</text>
  <!-- Provider -->
  <rect x="510" y="20" width="150" height="54" rx="8" fill="#fefce8" stroke="#ca8a04" stroke-width="1.5"/>
  <text x="585" y="43" text-anchor="middle" font-size="13" fill="#713f12" font-weight="600">LLMProvider</text>
  <text x="585" y="61" text-anchor="middle" font-size="10" fill="#4b5563">codebuddy / openai 兼容（凭证仅此层）</text>
  <!-- Sandbox Gateway -->
  <rect x="240" y="120" width="200" height="60" rx="8" fill="#fef2f2" stroke="#dc2626" stroke-width="1.5"/>
  <text x="340" y="143" text-anchor="middle" font-size="13" fill="#7f1d1d" font-weight="600">SandboxGateway（L 层）</text>
  <text x="340" y="161" text-anchor="middle" font-size="10" fill="#4b5563">唯一入口 · Hook 管线 deny 优先 · 四档人在回路</text>
  <!-- Hooks -->
  <rect x="500" y="120" width="160" height="120" rx="8" fill="#fff7ed" stroke="#ea580c" stroke-width="1.2"/>
  <text x="580" y="138" text-anchor="middle" font-size="11" fill="#7c2d12" font-weight="600">Hooks（按序短路）</text>
  <text x="580" y="156" text-anchor="middle" font-size="10" fill="#4b5563">PathPolicy 路径三态</text>
  <text x="580" y="172" text-anchor="middle" font-size="10" fill="#4b5563">CommandRule 前缀三态</text>
  <text x="580" y="188" text-anchor="middle" font-size="10" fill="#4b5563">NetworkPolicy + SecretScan</text>
  <text x="580" y="204" text-anchor="middle" font-size="10" fill="#4b5563">RiskGrade 绿/黄/红/黑</text>
  <text x="580" y="220" text-anchor="middle" font-size="10" fill="#4b5563">Backup / Trash / Audit</text>
  <!-- Tools -->
  <rect x="240" y="230" width="200" height="60" rx="8" fill="#f8fafc" stroke="#475569" stroke-width="1.2"/>
  <text x="340" y="253" text-anchor="middle" font-size="13" fill="#0f172a" font-weight="600">Tools（T 层）</text>
  <text x="340" y="271" text-anchor="middle" font-size="10" fill="#4b5563">read/write/edit/search/web/shell/memory + MCP</text>
  <!-- Side effects -->
  <rect x="20" y="230" width="170" height="60" rx="8" fill="#f5f3ff" stroke="#7c3aed" stroke-width="1.2"/>
  <text x="105" y="253" text-anchor="middle" font-size="12" fill="#4c1d95" font-weight="600">外部世界</text>
  <text x="105" y="271" text-anchor="middle" font-size="10" fill="#4b5563">文件系统 / shell / 网络 / MCP 服务</text>
  <!-- Context -->
  <rect x="240" y="330" width="200" height="54" rx="8" fill="#ecfeff" stroke="#0891b2" stroke-width="1.5"/>
  <text x="340" y="353" text-anchor="middle" font-size="13" fill="#164e63" font-weight="600">ContextBuilder（C 层）</text>
  <text x="340" y="371" text-anchor="middle" font-size="10" fill="#4b5563">每轮重组装 · 80/90 双水位压缩 · 前缀稳定</text>
  <!-- Storage -->
  <rect x="510" y="330" width="150" height="54" rx="8" fill="#f1f5f9" stroke="#334155" stroke-width="1.5"/>
  <text x="585" y="353" text-anchor="middle" font-size="13" fill="#0f172a" font-weight="600">SQLite（S 层）</text>
  <text x="585" y="371" text-anchor="middle" font-size="10" fill="#4b5563">sessions.db · audit_log · WAL</text>
  <!-- Memory -->
  <rect x="20" y="330" width="170" height="54" rx="8" fill="#fdf4ff" stroke="#a21caf" stroke-width="1.2"/>
  <text x="105" y="353" text-anchor="middle" font-size="12" fill="#701a75" font-weight="600">AXON.md（S 层）</text>
  <text x="105" y="371" text-anchor="middle" font-size="10" fill="#4b5563">长期记忆 · scope/confidence 治理</text>
  <!-- Verifier -->
  <rect x="240" y="415" width="200" height="40" rx="8" fill="#f0fdf4" stroke="#16a34a" stroke-width="1.2"/>
  <text x="340" y="439" text-anchor="middle" font-size="12" fill="#14532d" font-weight="600">TurnVerifier（V 层）· 独立读 audit/DB</text>
  <!-- Arrows -->
  <defs>
    <marker id="arr" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">
      <path d="M0,0 L8,4 L0,8 z" fill="#64748b"/>
    </marker>
  </defs>
  <line x1="170" y1="47" x2="236" y2="47" stroke="#64748b" stroke-width="1.5" marker-end="url(#arr)"/>
  <line x1="236" y1="60" x2="174" y2="60" stroke="#64748b" stroke-width="1.5" marker-end="url(#arr)"/>
  <line x1="440" y1="47" x2="506" y2="47" stroke="#64748b" stroke-width="1.5" marker-end="url(#arr)"/>
  <line x1="506" y1="60" x2="444" y2="60" stroke="#64748b" stroke-width="1.5" marker-end="url(#arr)"/>
  <line x1="340" y1="74" x2="340" y2="116" stroke="#64748b" stroke-width="1.5" marker-end="url(#arr)"/>
  <line x1="340" y1="180" x2="340" y2="226" stroke="#64748b" stroke-width="1.5" marker-end="url(#arr)"/>
  <line x1="340" y1="226" x2="340" y2="180" stroke="#64748b" stroke-width="1.5" stroke-dasharray="4,3" marker-end="url(#arr)"/>
  <line x1="440" y1="150" x2="496" y2="150" stroke="#64748b" stroke-width="1.5" marker-end="url(#arr)"/>
  <line x1="240" y1="260" x2="194" y2="260" stroke="#64748b" stroke-width="1.5" marker-end="url(#arr)"/>
  <line x1="194" y1="272" x2="240" y2="272" stroke="#64748b" stroke-width="1.5" stroke-dasharray="4,3" marker-end="url(#arr)"/>
  <line x1="340" y1="290" x2="340" y2="326" stroke="#64748b" stroke-width="1.5" marker-end="url(#arr)"/>
  <line x1="326" y1="357" x2="506" y2="357" stroke="#64748b" stroke-width="1.5" marker-end="url(#arr)"/>
  <line x1="506" y1="369" x2="328" y2="369" stroke="#64748b" stroke-width="1.5" stroke-dasharray="4,3" marker-end="url(#arr)"/>
  <line x1="240" y1="357" x2="194" y2="357" stroke="#64748b" stroke-width="1.5" marker-end="url(#arr)"/>
  <line x1="440" y1="384" x2="440" y2="411" stroke="#64748b" stroke-width="1.5" marker-end="url(#arr)"/>
  <line x1="240" y1="435" x2="194" y2="435" stroke="#64748b" stroke-width="1.5" marker-end="url(#arr)"/>
</svg>
```

数据流：用户输入 → AgentLoop →（每轮）ContextBuilder 从 S 层重放组装（稳定前缀+历史）→ Provider 流式推理 → 工具调用全部进 SandboxGateway（Hook 管线判定，红档弹 TTY 确认）→ 执行外部世界 → 结果截断回喂 → V 层读审计验证 → 通过则终答，否则继续循环（受三道保险约束）。

---

## 15. 遗留问题与未知区（诚实标注）

| # | 未知项 | 影响 | 占位方案（编码时临时采用） |
|---|---|---|---|
| 1 | `codebuddy_agent_sdk` 的导出面未公开核实（官方文档未检索到） | codebuddy Provider 适配器 | 实现第一步：`import codebuddy_agent_sdk; print(dir())` 核对；若其暴露 OpenAI 兼容 client/endpoint，`CodebuddyProvider` 直接复用 `openai_compat.py`（base_url 指向 SDK 端点）；否则按 SDK 实际方法薄适配。env 变量名以需求文档为准 |
| 2 | 前缀缓存收益取决于 provider 是否支持 prompt caching（hy3 / OpenAI 兼容端多支持但未逐家核实） | §5 缓存账与命中率目标 | 以 `usage.prompt_cache_hit_tokens` 实测定标；无该字段记 "unavailable"，不影响功能正确性 |
| 3 | `mcp` v2 中 `StdioServerParameters` 的导入路径（v2 迁移指南可能有调整） | MCP stdio 传输 | 首选 `from mcp import StdioServerParameters`；失败改 `from mcp.client.stdio import StdioServerParameters`（以实现时安装版本为准）。`Client` 一等公民形态已核实：官方文档 py.sdk.modelcontextprotocol.io/v2（2026-10-13 检索）；安装版本以现场 `dir(mcp)` 复核为编码第一步 |
| 4 | tiktoken cl100k 与各模型真实词表存在误差（±10%） | 压缩阈值精度 | 阈值判定用估算，provider 返回 usage 后校正实际水位；误差不改变三态语义 |
| 5 | Windows 回收站依赖 Shell API，`send2trash` 在个别环境可能失败 | 删除降级路径 | 已设降级：失败即移 `workspace/.axon/trash/` 并记录审计 |
| 6 | 快筛模型/模型分档（形态 B）未设计细节 | v2 成本优化 | v1 零快筛；`ModelRouter` 接口预留，v2 按 h-framework 分派表补 |
| 7 | macOS 网络严格模式（系统级拦截绕过代理的连接） | 平台安全上限 | v1 标注平台可选模块；代理层拦截对所有平台一致生效 |
| 8 | 多 Agent（子代理/远程派发）未设计 | v2 扩展 | 边界清单已砍；触发条件记录于 §6 |

---

**交付标注**：「已确认」= 需求文档原文或用户拍板；「默认假设」= 架构师建议、用户授权"试用后再改"，全部可推翻。本文档过 `scripts/check_design_doc.py` 结构自检；关键决策内容核查见台账行「独立评审」。

---

## 16. 实现偏差记录（编码期，v0.1.0）

设计决策不变；以下为落地时必要的实现细化（均不改变六层语义）：

1. **handler 签名**为 `handler(call, args, gw)`（设计伪码写作 `(args, gw)`）——`ToolResult` 需 `call_id/name`，由网关把 `ToolCall` 一并传入。
2. **Hook 判定语义**：deny 优先短路 + 其余取**最严格**（deny>ask>allow），管线顺序不变。否则 PathPolicy 对 workspace 内路径的 allow 会吞掉 RiskGrade 的红档升级判（fs_delete 将永不确认）——这是"首个非 None 生效"语义的设计漏洞，实现期发现并修正。
3. **无独立 TrashHook**：v1 工具清单 8 个无 delete 工具，删除语义由 shell 工具的**纯删除命令路由**承担（`rm/unlink/del/rmdir` → `move_to_trash`，`send2trash` 失败降级 `workspace/.axon/trash/`），批量阈值检测在 CommandRuleHook；BackupHook 覆盖 fs_write/fs_edit/fs_delete 快照。
4. **`ToolResult.meta`/`error_type` 落库**：messages 表无 meta 列（设计 DDL 固定），以 `<meta>{json}</meta>` 尾部标记序列化进 tool 消息 content；`ContextBuilder` 组装时剥离，模型上下文保持干净确定；V 层回读校验从该标记取 sha256/exit_code。
5. **`audit_log.detail` 含 `_turn`**：V 层规则②按"最后一条 assistant 消息所在回合"核对审批——往回合被拒后模型已合理变通的不判负（避免永久判负死循环）。
6. **MCP 工具 `parallel_safe=False`**：远端工具语义未知，保守串行执行。
7. **`codebuddy_agent_sdk` 适配**：懒加载 + 三级策略（OpenAI 兼容 client 复用 → 自定义接口薄适配 → 报错附 `dir()` 线索），与 §15 #1 占位方案一致。

8. **codebuddy 路 = SDK 自驱执行引擎（v0.1.1 重写，2026-10-13 实地勘察后）**：SDK 0.3.274 实为 Claude-Agent-SDK 形态的完整运行时（`query()` 异步生成器 + SubprocessTransport 拉起内置 `codebuddy-headless` CLI，stream-json 控制协议），不接收消息列表、自管对话 → 不适配 `LLMProvider.chat()` 协议。改为 `CodebuddyRunner.run_turn()` 直驱，AgentLoop 检测到 `run_turn` 即整体委派：
   - 工具经 `create_sdk_mcp_server` 进程内注册（`@tool` 装饰器，handler 返回 `{"content":[…],"isError":…}`）；
   - **权限唯一闸门 = `can_use_tool` 回调** → `SandboxGateway.check` + `TTYPrompter`（与 openai 路同一网关语义）；`tools=[""]` 禁全部内置工具 + `disallowed_tools` 双保险，模型只能用 `mcp__axon__*`；
   - 会话镜像：AssistantMessage/ToolUseBlock/UserMessage(ToolResultBlock) → 我们的 messages 表；`ResultMessage.session_id` 落库，次轮 `options.resume` 续接；
   - **本机故障根因（用户实测）**：CLI 默认在 57521 起自有控制服务器，与本机已运行的 CodeBuddy IDE 服务冲突（`Failed to start server. Is port 57521 in use?`）→ `extra_args={"port": ...}` 传 `--port`，默认自动挑选空闲端口（`model.providers.codebuddy.cli_port: 0`）；
   - **SDK 坑**：`async for` 内提前 break/return 触发 anyio cancel scope 跨任务退出崩溃（Windows 实测）→ 遇 ErrorMessage 记录后继续消费流至自然终结；
   - 该路径下 C 层压缩/stuck 检测不适用（SDK 自管上下文），V 层验证走审计口径（`scope_turns` 显式回合集合，跳过回读/退出码规则）。

9. **路径策略读语义（v0.1.4，用户试用后审定，可推翻）**：需求文档「🟢读文件/检索 → 直接放行」原样成立，但 v0.1.0-00.1.3 实现把「workspace 之外的读」也升成 ask，与绿档定义自相矛盾。修正：① 隐式信任区 = workspace **+ project**（`--project` 是用户显式声明的工作上下文）；② `fs_read` 无规则命中 → 默认放行；③ deny 表（系统目录/用户根/密钥目录）对读写都硬拦——读会把内容发给模型 API，属数据外泄通道，不以"用户在场"兜底（需求文档原文即定默认拒绝）；④ 写/改/删越界仍 fail-closed ask；⑤ 显式 `sandbox.paths` 规则永远优先；⑥ 开关 `sandbox.read_auto_allow`（默认 true）可一键回退旧语义。

10. **工作空间移入项目目录（v0.1.5，用户审定）**：默认 `workspace = <project>/.workspace`（不配置时），每个项目独立工作空间；其中 `.workspace/.axon/tmp` 为程序临时目录。**工作空间 = 全权限区**：智能体在此可处理所有事情（读写/删除/建目录/跑脚本/装依赖均免确认）；批量删除超阈值（默认 100）仍询问（用户上一轮明确要求的安全阀）。

11. **shell 命令安全分析器（v0.1.5 重写 `sandbox/command_rules.py`）**：替代原 glob 前缀三态为主判定。复合命令按 `&& || ; | &`（尊重引号）拆原子命令 → 逐程序分类（只读/删除/文件写/系统级/不透明）→ 汇总定级 + 路径证据：① 全只读且无文件重定向（`>nul`/`2>&1`/`>/dev/null` 不算写）→ 直接放行（路径命中 deny 表仍硬拦）；② 删除/写：全权限区 → 放行，信任区（+项目）→ 放行，区外 → ask；③ `powershell -Command`/`cmd /c`/`bash -c` 解包递归分析；④ git/npm/pip/docker 等按只读子命令表白名单；⑤ `find -delete/-exec`、命令替换 `$()`/反引号 → 不透明处理；⑥ 解释器（python/node）仅当有工作空间内脚本路径证据才放行，`-c` 内联一律询问；⑦ deny 清单/白名单/显式配置/fail-closed 兜底不变。批量删除阈值默认 100（v0.1.4 及以前为 10）。

12. **CLI 改版 + 状态反馈（v0.1.5）**：参考 CodeBuddy CLI 风格——rich 面板横幅/红框审批面板/dim 页脚；prompt_toolkit 提示符 + 底部状态栏（项目·模型·工作空间·会话·快捷键）；**思考中 braille 转圈 + 已耗时秒数**（消除用户反馈的"屏幕突然没反馈像死机"）；工具执行打行动线（⚙ 名称 目标 → ✓/✗ 结果）；模型流式文本原样直写（`markup=False`，防 rich 吞掉方括号内容——v0.1.5 修掉的显示 bug）。状态回调协议（`thinking/text/tool/tool_result/note/idle`）由 AgentLoop 与 CodebuddyRunner 共用，库调用走 NullStatus。

13. **重命名（v0.1.5）**：项目 Axon → AXON（包目录 `axon/`、命令 `axon`、记忆文件 `AXON.md`、内部目录 `.axon/`、MCP 前缀 `mcp__axon__`）；需求文档 `智能体开发.md` 中的名称与记忆文件引用同步更新（用户要求"修改相关的地方"）。

14. **程序级目录 + 主程序禁写（v0.2.0，用户审定）**：参照 WorkBuddy，所有项目共用的资源默认落 `~/文档/.axon/`（`AXON_HOME` 可覆盖）：sessions.db（全局共享）、snapshots、trash、tmp；**主程序所在目录（axon 包上级）为绝对禁写区**——优先级高于一切规则（含 project 信任区与显式 allow），读放行（agent 查看自身源码合理且无外泄风险）；shell 命令触及即拒。工程后果：不要把安装目录当项目用（`project == 主程序目录` 时 AXON.md 自动回落程序级，避免记忆功能被自己的规则挡死）。首次启动自动把旧版落在项目下的 sessions.db 迁/并入库。

15. **会话索引 + 跨项目恢复（v0.2.0，修复用户实测 bug）**：`--resume <id前缀>` 在**全局**库匹配（原实现按 project_dir 过滤 → 用户在 A 目录恢复 B 项目的会话必报"未找到会话"）；命中且项目不符 → 自动切换项目上下文（workspace 未显式配置时跟随）；`--resume` 不带值 → 列出最近会话（编号/id 恢复，参考 codebuddy -c）；`/sessions` 出跨项目索引（id·时间·项目·模型·消息数·标题）；会话 title 由首条 user 消息自动填充。

16. **AXON.md 进度日志（v0.2.0，用户审定）**：双区结构——「长期记忆」（模型 memory_write 维护，治理不变）+「进度日志」（harness 每回合结束**规则式自动追加**：请求摘要/工具执行链/结果/文件变更，零 token；`memory.auto_journal` 开关）。选规则式而非模型式的理由（经济性三视角·成本）：每回合一次摘要调用的 token 成本随会话数线性增长，而进度信息的 90% 可从消息与审计确定性提取；模型侧仍保留 memory_write 写"重要记忆"的高价值通道。

17. **venv 程序级共享（v0.2.0，用户审定，附保留意见）**：不为每个项目建 `.venv`（磁盘/构建时间随项目数线性浪费）；默认直接复用 AXON 运行所在的 Python 环境（所有项目共用、随用随装）。**保留意见（已向用户陈述）**：共享环境的经典代价是依赖冲突（项目 A 升级包可能弄坏项目 B）——故系统提示词引导 agent 在发现冲突时告知用户并可项目隔离；个人工具场景下默认共享、按需隔离比默认隔离更经济。

18. **program_dir 回归修复（v0.2.1，用户实测）**：v0.2.0 的"主程序目录禁写"把整个程序根划为禁区，导致**建在程序目录下的用户项目**（如 `<程序目录>\Project1`）连同其 `.workspace` 全部写入被拒——用户观测到"同一会话早些时候成功、之后一律被拒"。修正：禁写只保护程序自身文件，**project 为其真子目录时显式豁免**（project == 程序目录本身仍拒——那是"把安装目录当项目用"的极端用法，用户明确要拒）。

19. **网络默认放行 + web_fetch 下载能力（v0.2.1，用户审定）**：原 `remote_default: ask` 使 web_fetch 每条远程请求都弹确认（实测体验差、且无法取二进制至使 agent 反复绕行）。改：① 远程网络默认 **allow**（domain_deny 优先、secret_scan 仍拒——防数据外泄底线不动）；shell 的 curl/wget 仍走 CommandRule 的 system_mutate→ask，路径不变；② web_fetch 按响应内容分级：HTML→正文提取（原行为）；pdf/pptx/xlsx/docx/txt/csv/图片/音视频→直接下载到 `<workspace>/downloads/`（文件名优先级 save_as > Content-Disposition > URL 末段；返回路径/大小/类型/sha256）；zip/rar/7z/bat/exe/msi/ps1/dll 等**风险类型下载前询问**；单文件上限 `tools.max_download_mb`（默认 100MB）。③ 新增**工具内确认通道** `GatewayHandle.confirm(action, reason)`——解决"网关层无法预判、拿到响应才知道要审批"的场景（风险判定在 handler 内做，避免 URL 预判与内容协商双问）；非 TTY fail-closed。

20. **解释器代码级风险分析（v0.2.2，用户审定"python 不涉及删除就放行"）**：原实现把 python/node 一律当"不透明 → 询问"，用户实测被反复打断。改：对解释器做**代码级危险信号扫描**——`-c/-e` 内联代码或脚本文件内容（≤512KB）过五类正则：① 删除类（os.remove/unlink/rmdir/rmtree/send2trash）→ 归 delete（**rmtree 目录目标展开 bounded walk 计数**，防大目录被低估成 1 而绕过阈值；含变量/glob/遍历 → 计数 -1 → 显式询问）；② shell 逃逸（subprocess/os.system/os.popen/child_process）→ 保持不透明（询问）；③ 动态执行（eval/exec/compile）→ 不透明；④ 文件级变更（open(...,'w')/write_text/copy/move/mkdir）→ 归 file_write（信任区内放行）；⑤ 其余（计算/打印/import/读/网络）→ **read_only 直接放行**。`-m 模块` 与无代码可析仍按不透明处理。

21. **授权缓存键修复（v0.2.2，用户实测"按了 always 仍反复询问"）**：旧 prefix 键把**参数个数**写进键（`shell|python|5args`），同一类命令每次参数数不同 → 永不命中。改为 `shell|<程序>|<首参数特征>`：flag 类用 flag 本身（`-c`/`--version`），路径类用父目录（`@<dir>`）——`python -c "<任意代码>"` 同类共享授权，符合用户对 [a]lways 的直觉。

22. **回合收尾：临时文件清理 + 产物展示（v0.2.2，用户审定）**：① 每回合结束清理临时文件（`axon/env_cleanup.py`）——程序临时目录 `program_root/tmp/**` 全清 + 工作空间内明确模式（`*.tmp/*.temp/*.bak/*.orig/*.rej/~$*/*.crdownload/Thumbs.db/.DS_Store`）+ `__pycache__`/`*.pyc`；**产物一律不动**（`downloads/**`、非上述模式的用户文件、`*.log` 保留——可能是有效运行记录）；审计记 `env_cleanup` 一条，进度日志记"清理 N 个"。开关 `tools.cleanup_temp_each_turn`（默认 true）。② `TurnResult.products`：本回合产物（工具 meta 含 path 的写入/下载物，带大小），页脚以"── 本轮产物：[下载]/[文件] 路径 (大小)"列出；进度日志同步记录。③ CLI 面板错位修复：流式文本未换行时，审批面板/工具行动线前先补换行（`StatusLine.ensure_newline`）。

**实现产物**：`axon/` 包 10 模块（cli/config/agent×4/providers×4/tools×7+builtin×7+sandbox×9/memory/session×2/prompts）+ `tests/regression/` 8 文件 50 用例（全绿）+ `config.yml` + `README.md` + `pyproject.toml`（`pip install -e .` 后 `axon` 命令可用）。

23. **会话记忆三层 + resume 上下文注入（v0.2.3，用户问询驱动的缺陷修复）**：澄清并修正"resume 后模型能看到什么"。
    **记忆分层**：① 会话历史（`messages` 表，openai 路每轮全量组装；codebuddy 路由 SDK `session_id`/`options.resume` 续接 + 我们的镜像消息）；
    ② **项目记忆块**（`AXON.md` 三区：长期记忆 memory_write / 项目记忆 project_note / 进度日志 自动；每轮经 `render_block()` 注入 system 上下文，预算 50/30/20，**不依赖 resume**——读的是当前 project 的文件，跨项目 resume 时随 cfg.project 切换）；
    ③ resume 观察消息 `<resume_context>`（副作用清单 + 未配对工具调用防重复执行）。
    **修掉的 bug**：`load_for_resume` 返回的是 `Message` 对象，CLI 却直接 `print(resume_note)` → 打印 dataclass repr 且**该观察消息从未真正进入模型上下文**（设计 §4.4 意图未落地）。改为经 `_compose_user_input` 在**恢复后首轮**拼接到用户输入前注入，注入后即清空（不重复）。
    另修 codebuddy 路**用户输入从未落库**（导致进度日志"请求"恒为空）。

24. **对话噪声治理（v0.2.3，用户实测）**：① **模型文本重复**——partial 流式与完整 AssistantMessage 双发，两条防线：累积型 partial 只取增量、完整消息与已发内容重合则不发（`state.emitted/last_full`）；② 工具行动线连续同类合并为一行（≥3 次附"同类调用共 N 次"），成功静默、失败显 ✗+原因；③ 页脚副作用列表只列变更类（fs_write/edit/delete）、按 kind+路径去重计数（×N）、单行截断 68 字符；④ 进度日志：工具短名（去 `mcp__axon__`）、去重、变更计数与相对路径、token 为 0 不显示、新增**踩坑行**（失败工具与原因）。

25. **项目记忆与踩坑沉淀（v0.2.3，用户审定）**：新增「项目记忆」区（`AXON.md`）+ `project_note` 工具（kind: summary/progress/decision/pitfall/preference/environment/next），面向"后续会话快速理解项目"。会话结束自动沉淀（`memory.auto_summary_on_exit`，默认 true；`/save [补充]` 手动触发；消息数 < `summary_min_messages` 不触发）：以一轮正常 turn 让模型按固定结构（需求进度/交付物/关键决策/踩过的坑/偏好环境/下一步）写 project_note——系统提示词已引导模型**主动记坑**（什么操作 → 什么错误 → 正确做法）。

26. **shell 输出编码修复（v0.2.4，用户实测 traceback 归因）**：`subprocess.run(text=True)` 让 Python 用**系统默认编码**（中文 Windows = GBK）在内部读取线程解码子进程输出；子进程吐 UTF-8 字节（中文文件名/UTF-8 文本）→ `_readerthread` 线程抛 `UnicodeDecodeError` 打到 stderr（用户看到的红框 traceback，**命令其实已成功执行**，属本程序缺陷而非 agent 代码问题）。改：bytes 模式 + `decode_output()` 依次尝试 utf-8 → 系统首选编码 → gbk/cp936 → utf-8(replace)，彻底绕开 text 模式的线程解码；并给子进程注入 `PYTHONIOENCODING=utf-8`/`PYTHONUTF8=1`，使 python/git 等输出统一 UTF-8。

27. **思考/执行反馈与流式去重强化（v0.2.4，用户实测）**：① 转圈不止"思考中"——**工具执行期间同样转圈计时**（`tool()` 打印行动线后启动），并按 ~2s **轮换操作概要**（思考中："正在分析请求…/梳理上下文与进度…/规划下一步…/仔细推敲中…"；执行中："执行 <工具> …/等待执行结果…"），消除"像死机"的观感。② 流式重复第三道防线定为**前缀重放检测**：消息边界后 partial 归零，若累积片仍是"上一条消息文本"的前缀 ⇒ 判定为重放并抑制；一旦分叉则整段补发（含被抑制部分，不丢字）。8 个场景（增量/累积/大 chunk 重放/小 chunk 重放/纯 partial/正当双写/同前缀新消息/正常两轮）全部验证通过。

28. **图片输入与 @引用（v0.2.5，用户需求）**：新建 `axon/attachments.py`。① 附件目录 `<workspace>/tmp/attach/`（信任区内；`env_cleanup` 明确保护不清理）；命名 `图片N.<ext>`；② 剪贴板读取 `PIL.ImageGrab`（截图 → 存 PNG；复制的文件 → 复制进来；同一内容 hash 去重不重复附加），Pillow 缺失时降级 CF_HDROP；③ 输入解析 `parse_input`：引号路径 + 扩展名锚定的裸路径（容忍 Windows 文件名空格）→ 自动收编并替换为 `@图片1.png`，`@` 作为文件引用转义符；④ 发送时附「名称 → 绝对路径」清单（避免超长路径进提示）；⑤ `/attach [路径]` 手动附加。
    打包说明：项目内亦支持直接写路径/`@`引用（模型侧只认清单里的绝对路径）。

29. **assistant 文本落库（v0.2.5，缺陷修复）**：codebuddy 路原先只镜像 `tool_calls`，**agent 的叙述文本从未落库** → 会话历史缺回答、进度日志缺上下文、中断后无从继续。改为 `_pending_text` 缓冲：随下一个工具调用一起落库（保持「叙述→调用」顺序），回合收尾 `_flush_pending_text` 落尾部叙述。

30. **中断续做（v0.2.5，用户实测"请继续"无效）**：根因是中断时没有任何"任务描述"留痕。修：① `AgentLoop.run` 兜住 KeyboardInterrupt（含 SDK 内部抛出的）统一成 `user_interrupt`；② 回合收尾 `_persist_unfinished`：对 user_interrupt/provider_error/stuck_break/max_turns/budget_exhausted 落库 `<interrupted_task>` 观察消息（原始请求 + 未完成状态 + 续做指引）并写 `project_note(kind="next")`；③ `resume.py::extract_task_state` 推断「最近请求 + 是否未完成」（有 interrupted 标记，或最近请求之后无 assistant 文本）；④ `<resume_context>` 新增 `last_user_request` 与 `task_status` 字段。⑤ resume 时 `_replay_history` 把最近历史对话（用户提问 + agent 叙述 + 工具）回显到屏幕——修「恢复后只看到智能体输出」。

31. **插嘴（interjection，v0.2.6，用户需求）**：执行中用户可补充内容，**回合结束后**注入下一轮。① `axon/interject.py`：守护线程轮询键盘（Windows `msvcrt.kbhit`；POSIX `select` 降级），仅 TTY 启用；字符进 buffer（回车提交、Backspace 回退、Esc 清空、Ctrl+C 不接管——瞬断语义不变）；② 回显交给 `StatusLine` 状态行（`⌨ 插嘴: …` 后缀），避免与流式输出抢 `\r`；③ 回合结束 `_settle_interjections`：主线程展示采集内容并**再次确认**（Y/n），确认后包装 `wrap_interjection` → `<user_interjection>` 作为**新一轮**运行（模型据此调整决策或纳入新需求）；④ 系统提示词说明该标签的处理约定。

32. **删除项目 `/delproj`（v0.2.7，用户需求）**：破坏性操作，安全约定优先。① `SessionStore.list_projects()` 按 project_dir 聚合（名称/会话数/消息数/最近时间/最近会话 id 前缀当 project_id）；② `delete_project_data()` 只删**数据库记录**（sessions/messages/compaction_events/audit_log），不碰磁盘文件；③ CLI `/delproj [--list|目标] [--with-files]`：支持按目录/名称/session 前缀匹配（歧义列出候选）；④ **强确认**：必须手输项目名原文；删当前项目额外警告并在删除后退出 REPL；⑤ `--with-files` 把项目目录**移入回收站**（永不直接删），需二次输入 `DELETE`，且盘根/用户根/程序目录/工作空间一律拒绝；⑥ 审计记 `project_delete:data`。

33. **`--delproj` 命令行化（v0.2.8，用户纠正）**：`/delproj` 只能在 REPL 内用，而删除当前项目时正被自身会话占用（"在项目中不好删"）。改为主入口为启动参数：`python -m axon --delproj [序号|名称|目录|session前缀] [--with-files]`——**不加载模型、不需要 API key、不进 REPL**，`run_delproj_mode()` 只依赖 `SessionStore` + `AuditLog`（因此在任意目录、含项目自身内部都能删）。新增按列表**序号**选择（同名项目场景）；斜杠命令 `/delproj` 保留（二者等价，REPL 内删其他项目仍方便）。

34. **状态行残影修复 + 常驻输入行（v0.2.9，用户实测）**：① 残影根因——旧实现用 `\r` 覆盖但**不擦行尾**，操作概要轮换（长行 `执行 execute_shell … (46s)` → 短行 `等待执行结果… (47s)`）会留下 `(46s)` 尾巴。改为 `_write()` 按上一帧长度补空格擦除（免 ANSI，兼容老终端）。② **常驻输入行**：开启 Windows 控制台 VT（`SetConsoleMode | ENABLE_VIRTUAL_TERMINAL_PROCESSING`）后用双行渲染——上行状态（转圈+计时+概要轮换），下行常驻 `axon ➤ 插嘴: <输入>` 占位（光标停在此行），插嘴内容直接显示在提示符后；VT 不可用自动回退单行模式（插嘴挂在状态行尾）。

35. **异常结束的会话补沉淀（v0.2.9，用户问询）**：直接关闭命令行窗口时进程被终止，收尾逻辑（`auto_summary_on_exit`）没有机会执行——**原来不会沉淀**。补：① 会话生命周期标记 `status`（active/ended）：正常退出（`/exit`、Ctrl+D、`/resume` 切走）置 `ended`；② 启动时 `_recover_stale_sessions()` 检查当前项目下残留 `active` 且 >10 分钟的会话（即异常结束）→ 提示"是否补沉淀项目记忆"[Y/n]，拒绝仅归档不重复打扰；消息数 < `summary_min_messages` 直接归档；③ 开关 `memory.recover_prompt`。④ 历史回放对"旧版本无提问文本"的会话给出明确提示，不再让用户困惑。

36. **产物归档到项目目录（v0.3.0，用户要求）**：定位澄清——工作空间（`<project>/.workspace`）是**草稿/执行区**，项目目录才是**用户可见的正式产出区**；原实现把产物一律留在工作空间，用户找不到交付物。实现：① 回合收尾 `_archive_products()` 把本回合产物中位于工作空间内的文件**移动**到项目目录（保留相对路径）；② 排除 `downloads/`、`tmp/` 与隐藏目录（来源素材与临时物留在工作区，避免路径漂移）；③ 目标同名同内容 → 判定已归档并清理工作区副本；同名不同内容 → 加 `-2` 后缀，**绝不覆盖用户文件**；④ products 路径同步更新，页脚以 `[文件→项目]` 标注，进度日志记 `- 归档: …`；⑤ 开关 `tools.move_products_to_project`（默认 true）。配套：系统提示词新增「产物落位」段，要求 agent 一开始就把最终交付物写进项目目录（自动归档只是兜底）。

37. **交互细节修复（v0.3.0，用户实测四项）**：① **转圈中断**——`tool_result(ok)` 曾停转圈，而连续同类工具走合并分支不再启动 → 长任务中途再无动效；改为成功不停转圈（仅失败停并显示 ✗）、合并分支确保 spinner 在跑。② **剪贴板不再自动读取**——原实现每次输入后无条件读剪贴板，导致"躺在剪贴板里的图片被莫名收编"；改为显式触发（`/attach` 或输入含 `@剪贴板`/`@clipboard`），开关 `tools.clipboard_auto_attach`（默认 false）。③ **插嘴不再"消失"**——提交时即时回显 `⌨ 已记录插嘴：…（回合结束后确认）`；未提交的内容在回合收尾也会回显而非丢弃。④ **resume 副作用折叠**——`fold_side_effects()`：压平成单行、截断 96 字符、同类聚合（每类最多 4 条 + "共 N 条"）、总行数上限 24，修"几十条 `[shell] python -c …` 刷屏"。⑤ **last_user_request 被污染**——`extract_task_state`/journal 过滤系统内部消息（`<resume_context>`/`<verification`/`<interrupted_task>`/总结指令），不再把"请为本次会话沉淀项目记忆"当成用户任务；resume 历史无提问文本时，改从项目记忆里捞任务线索展示。

38. **cmd 误判修复 + 查询类放行（v0.3.1，用户实测）**：两条被误拦的命令 `dir /s /b output assets scripts 2>&1` 与 `python scripts\verify_layout.py 2>&1 | findstr /C:"slide"`，根因两条：① `2>&1` 里的 `&` 被原子切分器当命令连接符 → `dir … 2>` 成独立原子，孤立 `>` 触发"写文件"判定；② **cmd 开关被当成路径**——`/s` → `D://s`、`/b` → `D://b`（盘根！）、`/C:"slide"` → `C:slide`（盘根）→ 命中"写信任区外"。修：`strip_void_redirects()` 在切分前剔除无目标重定向（`2>&1`/`>nul`/`2>nul`/`&>nul`/`2>/dev/null`）；`is_switch_token()` 识别开关（cmd 风格命令下 `/`+字母 开头；注意 tokenizer 会把引号转成 `/`，故不能用"只含一个 `/`"判定）；查询类命令补进只读集（more/where/tree/fc/comp/findstr/tasklist/ver/vol/systeminfo/whoami/hostname/ipconfig/netstat/ping/nslookup/chcp）。

39. **盘符根目录/系统目录重点审查（v0.3.1，用户补充要求）**：用户要求"命令行遇到盘根或系统目录必须特别注意审查"——这正是第 38 条 bug 的反向风险（若真把 `/s` 之类误解析当无害，真盘根操作就可能漏判）。实现**四层分级**：① 盘根**下的直接子项**（`D://x.txt`）写/改/删 → **ask + severity=high**（面板标题 `/⚠`、正文加"⚠ 高危操作"警示语）；② 盘根本身（`D://`）写/改/删 → **deny**；③ 系统目录（Windows/Program Files/ProgramData/$Recycle.Bin/Recovery/System Volume Information/PerfLogs/MSOCache/Boot/EFI，各盘 + SystemRoot/ProgramData 环境变量）→ **deny**（补进 deny 表，读写皆拦）；④ 只读命令（`dir D://`、`type`、`findstr`）→ 放行（无害）。`Decision` 新增 `severity` 字段承载高危标记，供审批面板/审计使用。

40. **下载文件名后缀推断修复（v0.4.1，用户实测）**：现象——下载 arXiv 论文得到 `01-swe-agent-aci-2405.15793.pdf.15793`（重复且错误的后缀）。根因两条：① `_ext_of` 把 **URL 路径后缀放在最高优先级且不校验形态**，而 arXiv 链接形如 `/pdf/2405.15793`——`os.path.splitext` 把论文**版本号 `.15793`** 当成扩展名（实测响应头其实给出了正确信息：`Content-Type: application/pdf`、`Content-Disposition: filename="2405.15793v3.pdf"`、魔数 `%PDF-1.5`）；② `_safe_name` 判断"是否已有后缀"用的是"是否等于推断出的 ext"，于是已在 `.pdf` 结尾的名字被再追加一次。修：新增 `_looks_like_ext()`（1–5 位、字母数字、含字母——纯数字后缀直接否决）→ 优先级重排为 URL 后缀（须像扩展名）→ Content-Disposition → Content-Type → **魔数嗅探 `_sniff_ext()`**（pdf/png/jpg/gif/webp/zip 家族细分 docx|xlsx|pptx|apk/旧版 Office/json/html/txt）；`_safe_name` 改为"已有**合理**后缀则尊重、**假**后缀则替换、无后缀则追加"。

41. **工具短名导致回合中止的自愈（v0.4.1，用户实测）**：现象——模型调用 `execute_shell`（短名），SDK 报 `Tool 'execute_shell' not found in the current tool set`，**整个回合直接终止（token 0）**，用户任务半途而废。根因：MCP 工具以全名 `mcp__axon__<name>` 注册，SDK 严格匹配短名失败。修：① 系统提示词新增「工具调用命名」段，明确列出 9 个工具全名与"用短名会被拒绝"；② `CodebuddyRunner._hint_tool_names()`：捕获该错误模式 → 从报错文本解析被误用的短名 → 落库 `<tool_name_hint>` 观察消息（含正确全名与全部可用全名），用户说「继续」时模型即可改正重试（`is_internal_message` 同步把它排除在"用户任务"之外，避免污染 `last_user_request`）。

42. **任务级验收自检（v0.4.2，用户问询）**：用户问"智能体有没有回归验证——检查任务有没有完成、跟任务有什么出入"。核实：原有 V 层（TurnVerifier，6 条确定性规则）只管**技术性**成功（错误悬挂/写入回读/退出码/审批/打转/终答非空），**没有对照用户原始请求的完成度核对**。补：① `acceptance_check()`（零 token 规则式）——数量词 ↔ 产物数（仅单个"可分立"量词严格比对；"页/张"等文件内部量词只提示，避免"5 页 PPT = 1 文件"误报）、产物存在性、未成功操作、产物是否仍在工作空间、请求中显式输出路径是否存在；② 结论进 `TurnResult.acceptance` → 页脚「完成度自检」+ 进度日志 `- 自检:` 行；③ 模型侧提示词新增「收尾自检」段（回答末尾 ≤3 行 `✅ 完成 / ⚠ 偏差`，严禁谎报），并声明"与系统规则式结论不一致时以事实为准"。

43. **文档同步约定（v0.4.2，用户要求）**：功能/行为变更必须同步更新 `README.md` 与 `使用说明.md`（后者面向非技术读者）。本次同步：README（双层验证特性行、下载类型多重推断、工作原理增验收步骤、贡献约定、版本与测试数）；使用说明（界面章新增「完成度自检」节并重排序号、下载文件名规则说明、FAQ 新增"我怎么知道它真的把活干完了"、术语表补两条）。

44. **数据目录迁移到本地不受管位置（v0.4.3，事故根因）**：事故——用户 `--resume` 报 `sqlite3.DatabaseError: database disk image is malformed`。诊断：`.axon/sessions.db` 主库损坏（`sessions` 可读 7 行、`messages` 整表不可读、`audit_log` 544 行可读），而改名时保留的旧库 `.openwp/sessions.db` 完好（integrity ok）。**双重根因**：① 改名迁移用 `shutil.copy2` 复制**处于 WAL 模式的活动库**（当时 `-wal` 有 160KB 未回写数据）→ 主库与 WAL 不匹配；② 数据目录位于个人文档，被我之外的机制**回滚**（沙箱外两次替换都被还原成旧文件大小）——SQLite 由主库 + `-wal` + `-shm` 三文件组成，任何逐文件同步/回滚/DLP 都会造成不一致。修：`program_root()` 改为 `%LOCALAPPDATA%\Axon`（POSIX `~/.local/share/axon`）；`is_cloud_synced()` 检测并警告云同步路径；`legacy_roots()` + `migrate_legacy_root()` 自动把历史目录（`Documents/.axon`、`Documents/.openwp`）迁到新位置（只搬不删）。

45. **数据库自愈：自检 + 一键修复 + 一致性快照迁移（v0.4.3）**：① `axon/repair.py`——`check_integrity()`（quick_check）；`repair_database()` 走**备份坏库三件套 → 用官方 schema 重建 → 逐表逐行搬迁（坏页即停、孤儿行丢弃）→ 校验 → 替换**（本次事故的手工抢救流程内置化，实测从真坏库救回 7 会话 / 533 消息 / 540 审计）；② CLI 新增 `--repair-db`（不需 API key、不进 REPL）；③ `connect()` 首次连接做一次 quick_check，损坏则打印**可操作指引**并 `PRAGMA query_only=ON` **只读降级**（不抛栈、不二次损伤）；④ `migrate_legacy_root()` 改用 **SQLite backup API** 做一致性快照（源库损坏则失败且不留半成品）；⑤ 顺带修 `_cmd_delproj` 边界缺陷：session 前缀恰为纯数字时被误判为列表序号（超范围应回退为前缀匹配）。

46. **路径/程序名归一化（v0.4.4，用户实测两处误询问）**：用户在项目目录内的两条命令仍被要求审批。根因三条：① `/cygdrive/d/Python/AI/Harness/papers`（Cygwin 风格）不被识别 → 解析成 `C:\cygdrive\...` → 判"信任区外"；② 程序名带扩展名（如 cmd + exe）不在 `_CMD_STYLE_PROGRAMS` → `/c` 被当路径解析成 `C:\c`（盘根）；③ 壳命令 `… /c "… & …"` 的**内层命令串**未被解包（`_unwrap_inner` 返回的内层串仍带外层引号 → 递归分析时整串被当引号内容而不切分）→ 判 unknown。修：新增 `normalize_fs_path()`（`/cygdrive/<L>/`、`/mnt/<L>/`、`/<L>/` → `<L>:\`，非 Windows 原样返回）+ `prog_key()`（basename、小写、剥 exe/cmd/bat/com/ps1/py 后缀），并在分类与开关判定处统一使用；`_unwrap_inner()` 返回前剥外层引号。

47. **程序级共享 venv 落地 + 信任区（v0.4.4，用户要求"不存在就创建并安装 python"）**：① `config.venv_dir` / `venv_python` / `runtime_python` 属性；`ensure_venv()` 在 `ensure_dirs()` 中**幂等自动创建**（`sys.executable -m venv --system-site-packages <program_root>/venv`，继承系统已装库 → 新环境立刻可用；失败静默不阻塞启动；开关 `tools.auto_create_venv`）；② `full_zones()`/`trusted_zones()` 纳入 `<program_root>/venv` → 其中 `pip install` 免询问（**只放 venv**，不放整个程序目录，`sessions.db` 仍不被智能体触碰）；③ `analyze()` 识别「该 venv 的解释器 + `-m pip/ensurepip`」（`is_trusted_venv_python()`）→ 直接按信任区内的正常装包处理（此前 `-m` 形式一律 unknown → 反复询问）；④ 提示词重写「Python 环境」段：给出**真实绝对路径**（原为占位文字，且指向 v0.4.3 已迁走的旧目录）、venv 优先、缺包自装、禁止满盘搜索 python（用户实测智能体曾落到 `C:/Windows/py.exe`、试错 5 次以上）。

48. **粘贴监测与 `>引用` 语义（v0.4.5，用户纠正）**：旧实现"回车时比对剪贴板版本号"被用户否定——回车是发送动作，不应顺带探测剪贴板；且剪贴板变化但无图片时会打印"检测到剪贴板变动，但其中没有图片/文件"的噪声。改为 **`ClipboardWatcher` 后台实时监测**（0.35s 轮询版本号；仅当内容是**图片或文件**才处理，纯文本静默忽略）：① 收编规则——**不在项目内** → 复制到 `<项目>/tmp/`；**已在项目内** → 原位不动（用户明确要求）；② 生成引用 `>./tmp/图片1.png`（项目根下文件为 `>harness.md`）并在界面上即时提示 `[已收编]`；③ 发送时引用行前置 + `parse_references()` 解析为**真实绝对路径**清单注入（`>引用 → 绝对路径`）；④ 提示词同步说明 `>` 语义；⑤ `/attach` 复用同一路径。

49. **静默期转圈 + 输出保真 + 沙箱细节（v0.4.5，用户实测三项）**：① **空闲看门狗**——首个文本输出会停转圈，若 SDK 迟迟无事件界面"停着不动"（用户截图）；`StatusLine.start_watchdog()` 在回合内常驻，每 0.4s 检查"距上次活动 > 1.2s 且无转圈" → 以"等待模型响应…"重启转圈；text/tool/thinking 均 `bump()` 刷新活动时间。② **尾部重叠去重（第四道防线）**——SDK 在多轮叙述间会重发前缀，出现"中段整段重复"（前缀重放检测抓不到）；`_dedup_tail_overlap()` 在已发文本尾窗（400 字符）内找 ≥12 字符的最长公共重叠并截掉（短重复不误杀）。③ **命令分析**——`echo/printf/cls/date/time` 补入只读集（用户截图里 `… || echo "EXIT %errorlevel%"` 使整条命令落 unknown → 反复询问）；`_interp_code` 已能跳过 `-u` 等 flag 找到脚本。

50. **图片理解：多模态注入 + 副模型兜底（v0.4.6，用户问询"像 codebuddy/workbuddy 怎么处理"）**：事实核实——我们收到的图片就是「落盘 + 多模态块 + 路径引用」三件套（本对话即实例）；Axon v0.4.5 已具备①②，缺③。实现：① `Message.images` 字段 + `to_openai()` 在 user 消息上生成 `content=[text, image_url(data-URI)]`（OpenAI 路注入点唯一，改一处生效）；② codebuddy 路改传 **async-dict 流**（SDK `query(prompt=AsyncIterable[dict])`，实测 0.3.274 支持），dict 用 Claude-Code 风格 image block（SDK 无 ImageBlock 类型，走原始 dict）；③ `axon/vision.py`：`to_data_url()/to_claude_block()`（PIL 缩放 ≤1568px + JPEG 重编码控 token）、`vision_available()`（`model.vision` auto/on/off + 模型名白名单：gpt-4o/claude-3+/qwen-vl/glm-4v/gemini/step-1v…；实测 `hy3` 不在白名单 → 自动走兜底，正好覆盖"不确定"场景）、`describe_images()`（视觉副模型生成"客观描述 + OCR + 布局"，按 sha256 缓存到 `<数据根>/vision_cache.json`，同一张图只调一次）、`build_vision_note()`（无视觉时的文本注入）、`meta_note()`（末档元数据）；④ **失败自动降级**：codebuddy 原生注入被底层拒绝（error 含 image/content/media_type）→ 进程内标记 `_native_image_blocked`，下回合自动走描述注入并在页脚说明；⑤ 提示词新增「图片理解」三路说明（能看见就别跑 OCR、无视觉时按描述工作、只有元数据时别猜）。

51. **配置查找链与 key 解析（v0.4.7，用户实测非程序目录启动失败）**：现象——`pip install -e .` 后在 `D:\Python\AI\HarnessPPT` 敲 `axon` 报 `CodebuddyUnavailable: 环境变量未设置: ck_g12kknyp`。**双重根因**：① `load_config` 只在 **cwd** 找 `config.yml` → 非程序目录读不到仓库配置 → 落到"纯默认"；② 更糟：`ProviderCfg(env_api_key=os.environ.get("CODEBUDDY_API_KEY"))` 把 **key 的值**（`ck_…`）当成**变量名**存进默认值，后续 `os.environ.get("ck_…")` 必然失败——错误信息因此把 key 值当变量名打出来。修：① `config_search_paths()` 查找链：`--config` > cwd > **`<数据目录>/config.yml`** > 安装自带模板；`seed_user_config()` 在 `ensure_dirs` 首次把模板播种到数据目录 → **任意目录启动读同一份配置**（改一处生效）；② `ProviderCfg` 默认改回**变量名**（`CODEBUDDY_API_KEY`/`CODEBUDDY_INTERNET_ENVIRONMENT`）；③ 新增 `resolve_api_key()`：变量名 → 取值；若值本身像密钥（`ck_`/`sk-`/`pk-` 前缀或 >24 字符）则**兼容**直接当 key 用（老配置不炸）；④ 报错文案改为**可操作指引**（三种设置方式 + `--provider openai` 备选），且不泄露 key 值。

52. **粘贴机制修正：只认粘贴动作（v0.4.8，用户强烈纠正"监听截图非常不合理"）**：v0.4.5 的后台 `ClipboardWatcher` 轮询剪贴板版本号——**截图本身就会更新剪贴板**，于是用户截图（与项目无关）也触发收编；且后台线程直接 `print`，**破坏了 prompt_toolkit 正在渲染的输入行**（收编后无法继续打字，用户实测）。修：① 后台监听**默认关闭**（`tools.clipboard_watch: false`，保留开关）；② 主路径改为 **Ctrl+V 键绑定**（真粘贴动作）→ 读剪贴板 → 收编 → **把 `>引用` 插入输入缓冲区**（用户可接着打字）+ 更新**底部工具栏 toast**（不 print，不打断输入行）；`/attach` 复用同一 `poll_once()`；③ 提示词/横幅同步改为"Ctrl+V 粘贴即收编"。

53. **项目目录默认与输入补全（v0.4.8 用户两项）**：① **项目目录默认值**——未指定 `--project` 且配置未写 `project` → **当前目录**；当前目录是**主程序目录**（禁写区）→ 在 `<用户目录>/axon/` 下新建以时间戳命名的项目 `YYYY-MM-dd_HH-MM-SS`（Windows 文件名不含冒号故用连字符；`mkdir` 后返回）。实测：主程序目录启动自动建目录 ✓。② **输入补全**（prompt_toolkit `Completer`）——`/` 前缀且未含空格 → 补全 10 个斜杠命令；**行首 `>`** → 补全项目内文件（`scan_project_files`：≤15 个、深度 ≤3、排除 `.workspace`/`.git`/`node_modules`/`__pycache__`/隐藏项、目录优先排序；5 秒缓存）。补全菜单由 prompt_toolkit 自动渲染，选中即插入 `>相对路径`。

54. **收编键扩展与目录引用（v0.4.9，用户实测"Ctrl+V 无任何效果"）**：① **根因是终端层**——Windows Terminal 接管 Ctrl+V 与右键粘贴（自行粘贴文本，应用收不到键事件），故 v0.4.8 只注册 `c-v` 在 WT 里**永不触发**（用户截图证实"没有任何效果和变化"）。修：同一 handler 注册**四键** `c-v` / `c-g` / `escape,v`（Alt+V）/ `f2`，后三者是终端不吞的可靠键；启动横幅与 `/help` 明示"图片/文件请按 Ctrl+G（或 Alt+V）"；`/attach` 始终可用。② 横幅旧文案（"截图或复制文件会被自动收编"）下线——v0.4.8 已取消后台监听，文案却在宣传旧行为（用户实测截图抓到）。改为 `/` 与 `>` 的使用说明。③ **`>` 支持目录**：`scan_project_entries()` 同时返回目录（带尾 `/`，目录优先排序，深度 ≤3）与文件；补全菜单可直接选目录并**续拼子路径**（`>docs/` → `>docs/a.md`）；`parse_references()` 接受目录（`is_dir()` 也收编），给模型的清单里标注"（目录）"，模型据此用 execute_shell 遍历。

55. **工作区纳入 `>` 补全 + 引用剥前缀 bug（v0.4.10，用户追问"为什么检测不到 .workspace"）**：① 现象：`>` 补全列不出 `.workspace` 及其子项。根因是 **v0.4.8 的有意排除**——`_SKIP_DIRS` 含 `.workspace` 且"点开头即隐藏"过滤把它挡掉（当初顾虑下载物/草稿太多会淹没项目文件）。但用户需求更合理（下载的 PDF、生成的 PPT、粘贴的图片都在工作区里）。修：`_ALLOW_DOT_DIRS = {".workspace"}` 显式放行，排序权重把工作区内容**排到最后**（项目资产优先），`.git`/其他隐藏项仍排除；实测列表：`docs/`、`docs/guide.md`、`harness.md`、`.workspace/`、`.workspace/downloads/a.pdf`… ② **顺带修一个真 bug**：`parse_references()` 里 `token.lstrip("./\")` 会把 `.workspace/...` **开头的点也剥掉**（变成 `workspace/...`）→ 引用静默失败。改为只剥 `./` / `.\` 前缀（while 循环），并补回归测试。

56. **思考/决策过程可见（v0.4.11，用户诉求"界面大都是执行步骤"）**：现象——CLI 只有 `⚙ execute_shell/web_fetch…` 工具行动线与少量流式文本，模型的**推理过程**完全不可见。根因：SDK 的 `AssistantMessage.content` 里本来就有 `ThinkingBlock`，但 `map_sdk_message` 只提取 `TextBlock`，思考被丢弃。修：① `StreamEvent` 增加 `kind="thinking"`；`map_sdk_message` 提取 `ThinkingBlock`（兼容 `thinking`/`text` 两种字段名），用"已发思考是否以本块结尾"做**整块去重**（不用 12 字符重叠阈值——短思考会被误判为新内容）；② `CodebuddyRunner.run_turn` 新增 `on_thinking` 回调；openai 路把 `delta.reasoning_content`（DeepSeek-R1 等推理模型）也映射为 thinking；③ CLI `StatusLine.thinking_line()`：`💭` 前缀 + ANSI 弱化（dim）样式，穿插在文本与工具行动线之间，>8 行时折叠中间；④ 配置 `agent.show_thinking`（默认 true，可关）。注意：thinking **不并入** `final_text`（避免污染验收与落库的答复正文），只做展示。

57. **思考流式化 + 状态行合并（v0.4.12，用户实测两项）**：① **思考"一下子蹦出来"**——v0.4.11 只从完整 `AssistantMessage` 的 `ThinkingBlock` 提取（整块），而 SDK 的 SSE 流里本来就有增量通道：`{"type":"content_block_delta","delta":{"type":"thinking_delta","thinking":"…"}}`（Anthropic 形态，与 `text_delta` 同路）。修：新增 `_partial_thinking()`，在 `StreamEvent` 分支与文本增量**并行提取**思考增量（累积型/增量型都兼容：前缀对齐取差）；消息边界重置 `thinking_stream`；完整块到达时用"已发是否以本块结尾"抑制（不重复蹦一次）。CLI 侧配套 `StatusLine.thinking_write()`：**行内追加**（逐字不换行、遇 `\n` 才换行并补 `💭` 前缀），`_close_thinking()` 在文本/工具输出前收尾（补 reset+换行）。② **两条轮播合并**——`执行 xxx …` 与 `等待执行结果…` 表达的是同一件事（都在等执行结果），合并为单条 `正在执行 xxx，请稍等...`（转圈计时保留）。

58. **模块化输出 + 长内容折叠 + 中文第一语言（v0.4.13，用户参照 CodeBuddy 样式）**：① **模块标签**——输出按类型分标签：`⚙ 运行 · <工具>`（cyan）、`✓ 完成 · <工具> → <摘要>`（dim，v0.4.13 起成功也反馈一行，不再完全静默）、`✗ 失败 · <工具> — <原因>`（yellow）、`💭 深度思考`（块头 + 内容缩进 4 空格，一个流一个块头）。② **长内容折叠**——终端**无法回删已打印的行**（ANSI 重绘与 rich/spinner 混写易崩），故采用「打印前折叠 + 详细模式开关」：`StatusLine.block()` 在 verbose 关闭且行数 >3 时只保留前 2 行 + `…（共 N 行，Ctrl+O 或 /verbose on 展开）`；`Ctrl+O` 键绑定与 `/verbose [on|off]` 命令切换；附件清单、note 等多行内容均走此折叠；流式内容（思考/答复）不折叠。③ **中文第一语言**——系统提示词明确"始终使用简体中文（第一语言），保留英文技术术语；除非用户明确指定其他语言、或用户用其他语言提问（跟随用户语言）"（原措辞"用户使用中文时"太弱）。

59. **并行能力核实与第一步落地（v0.4.14，用户要求"先核实"）**：**核实方法**——读 SDK 源码 + 对底层 CLI（`codebuddy-headless.exe`，145MB Node SEA）做 strings 分析。**结论：CLI 默认就并发执行同一条消息里的多个 tool_use**，证据链：① 模型请求参数 `modelSettings.parallelToolCalls`（boolean，且"开启但无 tools"会抛错）；② CLI 系统提示词原文："Launch up to 3 Explore agents **IN PARALLEL** (single message, multiple tool calls)"；③ **changelog 白纸黑字**："Concurrent edits to the same file no longer hang: Fixed … **concurrently editing the same file in the same conversation turn** …"——只有并发执行才会产生这个竞态；④ 子智能体并发可调（`CODEBUDDY_CODE_MAX_CONCURRENT_SUBAGENTS`、`..._MAX_SUBAGENTS_PER_SESSION`、`..._SUBAGENT_MODEL`）。**落地**：① 提示词新增「并行与依赖」段：无依赖子任务放同消息多调用（3–5 个）、有依赖的等上游结果、同文件读写改保持串行；② **安全修复**：`TTYPrompter.confirm()` 全程持 `RLock`——并发执行的多个工具会**并发触发审批**，原实现无锁，两个审批面板交错 + 两个 `input()` 互吃输入 → 用户对 A 的 "y" 可能答到 B 上（误批准危险操作）。测试用"两次 input 时间区间不重叠"验证串行化。**因此第二步（自建 `spawn_subagent`）暂不需要**——CLI 已内置并行与子智能体机制。

60. **插嘴改造：独立行回显 + 执行中即时注入（v0.4.15，用户实测两项）**：① **输出拼接**——`on_submit` 直接 `console.print`，若流式输出未换行就拼在同一行。修：回显前 `status.ensure_newline()`，固定独立一行 `💬 [插嘴] 内容`（绿色）。② **回合结束才生效**——根因是 `sdk.query(prompt=str)` 单向流，执行中无法插入消息；但 prompt 支持 **`AsyncIterable[dict]`**（v0.4.6 多模态已在用），这就是 Claude Code stream-json 的标准"执行中继续输入"通道。实现：新增 `InterjectBridge`（键盘线程 `call_soon_threadsafe` → `asyncio.Queue` → async 侧 `get()`），`_prompt_stream()` 在初始消息后进入**插嘴循环**（每 0.4s 等一次，有内容就 yield `{"type":"user","message":{...,"content": wrap_interjection(text)}}`）；SDK 停止消费时 GeneratorExit 自然退出循环。`wrap_interjection` 文案改为“**下一个动作之前**：① 一句话复述确认 ② 重新规划 ③ 不中断进行中的单个工具调用”。`agent.stream_interject`（默认 true）关闭时退回旧路径（回合后弹确认注入）。**未知区**：无 API key 环境无法实测 SDK 是否接受执行中 yield 的 user 消息——若底层拒绝，表现为回合报错，可关 `stream_interject` 回退；待用户实测确认。

61. **插嘴中断重发 + 霸屏治理 + 提示常驻（v0.4.16，用户实测四项）**：① **插嘴"看到但不受影响"**——根因（strings 实证）：CLI 有**消息队列**机制（changelog："accumulated user messages in the queue"、"prevent concurrent message loss **while an Agent is running**"），执行中到达的 user 消息**排队到本回合结束**才处理；v0.4.15 的 prompt 流式注入协议上通（transport 持续写 stdin），但语义是排队，用户一个回合可能 16 轮工具调用 → 体验等于无效。修：**改用 `CodeBuddySDKClient`**（有状态客户端，暴露 `interrupt()`；旧的 `sdk.query()` 是一次性函数拿不到中断句柄）——插嘴到达 → `await client.interrupt()`（CLI 结束当前回合）→ 收到 ResultMessage 后用 `await client.query(wrap_interjection(text))` **同 session** 立即重发（历史连续、无进程重启）；`interrupt` 失败自动降级为队列语义（记 stderr、不崩）。插嘴不再走 prompt 流（避免双重消费）。② **插嘴输入口**：工具栏常驻「💬 执行中可直接输入补充（回车提交）」。③ **霸屏治理**：思考流 ≤4 行 / 正文流 ≤30 行（超出**静默累积** + 首次超限打一行折叠提示"全文已入会话记录"；`verbose` 不受限），思考块按块重置计数。④ `InterjectBridge.get(0)` 走 `get_nowait` 快路径（`wait_for(0)` 有立即超时竞态，实测取不到）。

62. **思考滚动窗口（带框）+ 强去重（v0.4.17，用户两次实测反馈）**：① 用户否定 v0.4.16 的"截断 + 折叠提示"（"还是要全部显示的，而不是显示前面几行，后面就不动了"）→ 改为**固定高度滚动窗口**：`SCROLL_N=6` 行、ANSI 光标重绘（`\x1b[{N+2}A` 上移 → 全区域重写 → 光标回框下）、50ms 节流、`┌─ 💭 深度思考 ─┐` 边框（CJK 双宽对齐）；**1-6 行 → 2-7 行**推进，历史随框滚出屏幕——"全流过 + 不霸屏"的列表式语义；`_close_thinking` 定格最后一屏并把光标移到框下（后续输出落在框外）；非 TTY 退化为直接全显（无 ANSI）。② **整段重放**（图2：正文 + 完成自检整页重来一遍）——根因：v0.4.16 迁移到 `CodeBuddySDKClient` 后启用了 continuous-read API（SDK 注释明示它"can consume cross-turn events"），重放段可能远超 v0.4.5 的 400 字符尾窗 → 去重失效。修：尾窗扩到 2000 字符 + 新增 **rfind 探测**（取新文本开头 80 字符在已发文本中最后一次出现处向前逐字符匹配，连续 ≥40 字符即整体截掉），两段取更优结果。

63. **client 双 connect 卡死修复 + 首条超时护栏（v0.4.18，用户实测"159 秒无输出一直转圈"）**：根因——v0.4.16 迁移写成 `async with CodeBuddySDKClient(...) as client: await client.connect(prompt=...)`，而 **`__aenter__` 内部已经 `connect()`**（无 prompt，延迟 initialize）；再次 connect 会**重建 SubprocessTransport → 启动第二个 CLI 子进程**并遗弃第一个（其 _Query 未关闭）→ 两进程互抢 stdio **deadlock**，表现为无任何输出、无限转圈。修：`async with` 后改用 **`query()`**（SDK 设计意图：首个动作是 query → `initialize(has_prompt=True)` → CLI 不回放历史；若先 receive 则 has_prompt=False 会走 resume-read-history 路径）。另加**首条消息超时护栏**：`receive_messages()` 改为显式迭代 + `asyncio.wait_for`（首条用 `agent.model_timeout_sec`，后续 600s 宽限）——连接层卡死时在超时内报"等待模型响应超时"，不再无声转圈。**教训**：client 模式的连接语义与 `query()` 一次性函数不同（__aenter__ 已连接、initialize 延迟、has_prompt 由首个动作决定），迁移时必须按新语义改写全部调用点，不能只替换入口。

64. **审批键盘竞争修复 + 思考框改 rich Live（v0.4.19，用户实测两项）**：① **沙箱确认卡死**——`InterjectReader`（msvcrt 后台线程）**全局抢按键**，审批的 `input()` 从 stdin 读但按键先被采集器吃掉 → 用户的 y/n 永远到不了审批 → 卡死（截图 91s 无响应，状态行还显示"等待模型响应"）。修：审批 `_confirm_locked` 期间 `kb.pause()` + `status.stop_watchdog()`，finally 里 `kb.resume()` + `start_watchdog()`（采集器加 `_paused` Event，`_loop` 暂停时连 `kbhit` 都不调用）。② **思考框 20+ 重复**——v0.4.17 手写 ANSI 光标上移重绘在真实终端错位（框宽触边折行 → 物理行数与 `\x1b[{n}A` 不符 → 每次重绘向下叠一个新框）。修：**改用 rich Live**（`transient=False`、`refresh_per_second=8`、`height=SCROLL_N+2` 固定高度 Panel，宽度由 rich 按终端管理不乱折），close 时定格保留；无 rich console / 非 TTY 退化为直接全显。教训：**ANSI 手写重绘在 Windows 终端不可靠，终端交互一律走 rich 的能力**。

65. **公开发布：PyPI + GitHub 上线（v0.4.19 起）**：包已发布到 PyPI（**`pip install ai-agent-axon`**，已验证在线且版本与本地一致），源码开源在 GitHub（**https://github.com/gufengaoyue/axon**）。README 与《使用说明.md》新增：pip 直接安装（推荐普通用户）/ 源码安装（开发）两条路径、项目地址与 PyPI 链接、升级命令（`pip install -U ai-agent-axon`）；徽章区补 GitHub/PyPI 徽章并修正滞后版本号（0.4.1→0.4.19）与测试数（241→407）。**用户偏好**：用户手册**只维护 Markdown 单格式**，不再生成 `使用说明.html`（用户已删除并要求不再产出）。

66. **SDK 依赖补进分发包 + Live 独占终端守卫（v0.4.20，用户实测）**：① **PyPI 0.4.19 的 requires_dist 缺 `codebuddy-agent-sdk`**（已用 PyPI JSON API 核实）——pip 用户装完跑 codebuddy 路会报"SDK 未安装"。修：`pyproject.toml` dependencies 补 `codebuddy-agent-sdk>=0.3.274`（本地文件此前已有一行但未随 0.4.19 发布；去重后单条），升版本 0.4.20 并**构建 wheel 验证 metadata 含该依赖**。② **刷屏 `[2Kaxon ❯ …`**（用户实测一屏字面 ANSI 序列）——根因：rich Live（思考滚动框）活动期间，spinner 线程的裸重绘（`\x1b[1A\r\x1b[2K` + 状态行/输入行）与 Live 抢终端渲染，ESC 序列被打成字面并逐帧累积。修：`StatusLine._live_active` 标志（`_thinking_begin` 置位 / `_close_thinking` 清除），`_start()` 与 `_render_once()` 双守卫——**Live 独占终端期间不启动转圈、不写任何裸序列**；Live 降级路径（无 rich）不置位，转圈照常。顺带统一执行中回显提示符（`axon ➤` → `axon ❯`，与 PromptSession 一致）。

67. **系统级长期记忆 + 会话时长 + 思考重放去重（v0.4.21，用户需求）**：① **系统级长期记忆**——用户要求"任何项目通用"的记忆层（系统环境/用户信息/偏好/工具链路径…），文件 `<个人文档>/.axon/longtermmemory.md`（用户可见可编辑；`memory.long_term_file` 可改位置），与"项目记忆"（AXON.md）严格分工。实现：`axon/memory/global_store.py`（`GlobalMemoryStore`：播种模板 / 分小节行级追加去重 / 预算内注入 / 行数上限）；**载入顺序「长期记忆 > 项目记忆」**（两路 provider 在 `mem = memory_store.render_block()` 前注入 `<global_memory>` 块）；新工具 `global_memory_write(section, text)`（注册于 builtin；提示词给出判定标准"**换一个项目这条还成立吗**"）；PathPolicy 把长期记忆文件列为**单文件信任区**（仅该文件，不含父目录）。注意与既有 `memory_write(scope=global)` 的区别：后者是项目文件内的条目标记，本模块是**真正跨项目共享的独立文件**。首次使用自动播种模板并初始化真实环境事实。② **会话时长**：`main()` 包裹 `repl()` 计时，退出时打印 `── 本次会话时长：X 小时 Y 分 Z 秒`。③ **思考重放去重**：SSE 在回合/中断边界会重放同一段思考流（用户实测两个完全相同思考框）——partial thinking 分支的局部累积（`thinking_stream`）在消息边界被重置抓不到，改为与**已发全文**（`thinking_emitted`）做 `_dedup_tail_overlap`（尾窗+rfind 强探测）。冒烟教训：首版 `append` 按字符区间拼接会在小节末尾与下节标题**粘连**并引发后续小节重复创建——重写为**行级操作**（`lines.insert` + 结构重建）。

68. **每回合用时（v0.4.22，用户纠正）**：v0.4.21 首版把时长放在**退出时**显示（`── 本次会话时长`），用户纠正为"**每次智能体完成任务后**显示、每次完成任务时计算"——即**回合级**。改：`_run_turn` 记 `t_turn`，页脚状态行追加 `· 用时 X`（`_print_turn_result(..., elapsed=)`，`_fmt_duration` 秒/分/时自适应）；**移除**退出时的会话总时长（`main()` 恢复直接 `return repl(...)`）。测试覆盖格式函数（8s/65s/3725s）、页脚含"用时"、旧调用向后兼容、退出显示已移除。

69. **插嘴"插不进"三项修复（v0.4.23，用户实测）**：① **根因一：用户不知道要按回车**——输入后文本停留在缓冲态，输入行只显示 `axon ➤ 插嘴: xxx`（无任何"待发送"提示），用户以为已发送。修：缓冲态显示 `axon ❯ 插嘴: xxx  ← 按回车发送`。② **根因二：回显被思考框吞**——rich Live（滚动窗）独占终端期间 `console.print` 的 `💬 [插嘴] 已提交` 会被吞/破坏布局（用户截图里只有缓冲行、没有提交回显）。修：Live 期间回显**排队 + 立即刷新面板**（`_render_panel` 底部显示最近一条回显），`_close_thinking` 时补打完整回显。③ **根因三：送达失败静默丢弃**——`bridge.submit()` 返回 False（回合刚结束/未绑定）时无提示。修：显式警告"未能送达（任务可能刚结束）——请作为下一条消息发送"。回显文案统一为 `💬 [插嘴] 已提交（将在下一个动作前生效）：xxx`。
**机制澄清**：`使用说明.md` 受安全中心专门规则保护——脚本（bash/python）读写该文件会被拦截，Edit/Write 工具可正常修改；此前误判为"沙箱回滚"，实为该规则。

70. **三层记忆沉淀（v0.4.24，用户讨论确认方案）**：用户提问"第 1 回合踩坑解决，第 2 回合会直接用正确方法吗"——实证（demo）：旧版 journal 只记「踩坑: web_fetch 502」而**不记"后来换 web_search 成功"**（执行行只列工具名、无因果），project_note（含正确做法的完整教训）**只在退出时写** → 会话内会重复踩坑。用户方案"每回合沉淀"经评估改为**三层**（成本/收益平衡）：**L0 规则式教训**（零 token）——`journal.extract_lessons()` 从工具事件流提取「失败→成功」配对（同工具重试 / 换工具成功两种模式，每回合最多 2 条），写入 journal「- 教训:」行，**下一回合随 render_block 注入即消费**；**L1 触发式沉淀**（有信号才调模型）——`cli._note_signals()` 检测四类信号（lesson/stuck_break/插嘴纠正 corrected/自检⚠），非空才跑 TURN_NOTE_PROMPT 轻量回合写 project_note(pitfall/decision)，`agent.auto_note: trigger(默认)/always/off`；**L2 退出总结**（现状保留）。提示词新增「教训消费」指令：进度日志的「教训」行是已验证路径，相似问题**直接采用、禁止重复已失败路径**（除非环境明显变化）。经济性：L0 覆盖最高频教训（零成本），L1 约每 3-5 回合一次小调用，同等效果成本约为"每回合 always"的 1/3~1/5。

71. **中断后"一直转圈"修复（v0.4.25，用户实测）**：现象——Ctrl+C 中断后界面**持续转圈/重绘**不恢复。根因：**rich Live（思考滚动框）从不被中断路径关闭**——`idle()` 只停 spinner（`_stop()`），而思考期间 Live 已打开（`_live_active=True`，spinner 本就退避）；Ctrl+C 异常穿过 `loop.run()` 后 `_run_turn` 的 finally 与 repl 的 `except KeyboardInterrupt` 都**没有调用 `_close_thinking()`** → Live 对象与其 **~8Hz 自动刷新线程**继续存活，在终端里无休止重绘思考面板，并与随后 prompt_toolkit 的输入行互相覆盖（视觉上就是"一直在转"）。同缺陷也影响**正常路径**：一个"只思考、无正文/工具输出"的回合结束后 Live 不关，页脚被 Live 覆盖。修复（三处 + 一竞态）：① `StatusLine.idle()` 先 `_close_thinking()`（幂等；覆盖 repl 两个清理点与审批前的 idle）；② `_run_turn` 的 **finally 任何出口**（正常/异常/Ctrl+C）`st._close_thinking()`，且位于"未提交插嘴提示"打印之前；③ repl 的 `except KeyboardInterrupt` 先 `status.idle()` 再打印"(回合被中断)"，内层 `except KeyboardInterrupt: status.idle()` 防**二次 Ctrl+C 打断清理**；④ `stop_watchdog()` 改为 **join 看门狗线程**（timeout 0.5s）——消除"stop 之后看门狗仍调用 thinking() 重启转圈且无人再停"的竞态。回归测试 24 项（Live 开/idle 关/幂等、KeyboardInterrupt 穿 finally 后 `_live_active=False`、看门狗 join 后线程消亡、接线检查）。

72. **生图模型接入口（v0.4.25，用户需求）**：用户要求"文生图/图生图独立模型入口：有生图任务才调用；调用时特别提醒用户；未配置或失败 → 放弃生图任务并告知，**不要试图用无生图能力的模型生成图片**"。实现：① **配置段 `image_gen`**（`ImageGenConfig`：`enabled/base_url/api_key_env(默认 IMAGE_GEN_API_KEY)/api_key/model/size/timeout_sec/out_dir`；`configured` = model + base_url 均非空——key 可缺省以兼容本地免鉴权服务；`resolve_key()` 复用 `resolve_api_key()` 的"环境变量名 / 直接值"双语义）。② **客户端 `axon/image_gen.py`**（OpenAI 兼容 images API：文生图 `POST /images/generations` JSON；图生图 `POST /images/edits` multipart——覆盖 OpenAI DALL·E / 火山方舟 Seedream / SiliconFlow / 本地兼容服务；响应兼容 `b64_json` 与 `url`（回源下载）两种形态；落盘 `<workspace>/images/`，文件名 `模式_时间戳_提示词slug.ext` 防重名；扩展名按魔数嗅探）。③ **工具 `image_generate(prompt, image_path?, size?)`**（builtin 第 9 个；`parallel_safe=False` 防按次计费的并发刷量；`risk=NETWORK` 走网络策略）。④ **放弃语义（用户红线）**：未配置/`enabled=false`/调用失败/基准图不存在 → `tool_result_err` + 明确话术"请放弃生图任务并告知用户…**严禁**用无生图能力的模型'生成'图片：不要输出 SVG/HTML/ASCII 艺术或绘图代码冒充图片，不要把提示词当作图片交付，也不要用 execute_shell 装 Pillow 画图替代"。⑤ **prompt 规则双保险**：系统提示词新增「图片生成」节——只有**显式**生图任务才调用；调用时系统会特别提醒；失败即放弃并给出配置方法；工具全名清单与 `_hint_tool_names` 同步补 `image_generate`。⑥ **CLI 特别提醒**：`StatusLine.tool()` 对 `image_generate` 特判——打印 `🖼 正在调用生图模型 <model> 生成图片（文生图/图生图）…`（品红）并以该事项转圈；`main()` 把已配置的生图模型名挂到 `status.image_model`。注意：**用户侧要生效需自行在 config.yml 填 `image_gen` 段**（数据目录里已播种的旧配置不含该段，走默认值=未配置→放弃话术，行为安全）。

73. **openai 路连接错误：根因是 base_url 不展开变量名 + 异常裸奔（v0.4.26，用户实测）**：现象——`axon --provider openai --model qwen3.7-flash` 报 `raise APIConnectionError(request=request) from err / openai.APIConnectionError: Connection error.`（**裸 traceback 泼屏**）。**双重根因**：① **配置层**——用户把 `env_api_key` 的「填环境变量名」惯例套用到 base_url（config.yml 写 `base_url: OPENAI_BASE_URL`，期望 `set OPENAI_BASE_URL=<端点>` 生效），但旧实现把**字面量当主机名** → DNS 解析 "OPENAI_BASE_URL" 失败 → 连接错误（该编辑被 v0.4.25 的 `git add -A` 顺带提交进仓库）；且 qwen3.7-flash 本就不属于 api.openai.com。② **循环层**——openai 路的 `provider.chat` 异常在 `AgentLoop._run` **无人捕获**（只捕 KeyboardInterrupt），`main()` 也无顶层兜底 → SDK 异常一路裸奔成 traceback。修复：① `resolve_base_url()`——含 `://` 或 `.`（URL 特征）原样使用；形如变量名（全大写+数字/下划线）取环境变量的值，**未设置则 fail-fast**（`ValueError` 带两种修法：设变量 / 直接写 URL）；config.yml 注释与数据目录已播种配置同步说明两种写法。② `_run` 的 chat 消费处 `except Exception` → `provider_error` TurnResult（保留已产出的部分文本；KeyboardInterrupt 是 BaseException 不受影响，仍走 user_interrupt → 中断语义不被破坏）。③ `_friendly_provider_error()` 按异常类型给可操作中文话术：**APIConnectionError** 显示当前 base_url + 模型名，「端点/模型不匹配」检测（非 OpenAI 模型名特征表命中 api.openai.com 端点 → 指明 DashScope 兼容端点）、**代理环境变量陷阱**检测（httpx 默认读 HTTP(S)_PROXY，残留失效本地代理时提示清空）；401/404/429/400/超时分型。④ 新增 `--base-url` 临时覆盖端点（`_override_provider_field()` 统一处理 model/base_url 覆盖，顺带修复「只写 active_provider、无 providers 段」时 `--model` 覆盖 KeyError 的潜在 bug）。**实证**：DashScope 端点 + 虚拟 key → 真实到达阿里云并返回 401（话术正确分型）；变量未设置 → 启动即报可操作错误。教训：**配置里「变量名间接」一旦对某个字段成立（env_api_key），用户就会推广到其他字段**——要么全字段一致支持并在注释写明，要么显式拒绝；以及 CLI 覆盖写入 raw 时必须处理「段不存在」（setdefault 链）。

74. **自证死循环 + 中断后沉淀卡死（v0.4.27，用户实测）**：现象——① 模型起初答对，随后在总结阶段陷入**自证死循环**：每轮结束都被 `<verification_failed>` 观察消息推着重写同样的答案（用户截图里模型自己诊断"这是需要重启会话才能彻底消除的系统干扰"——诊断正确但无法脱身）；② 用户 Ctrl+C 后出现"正在沉淀本回合教训（信号：lesson/corrected）"，沉淀回合又陷进同样的思考出不来。**根因链（四环相扣）**：① `stuck_fired` **粘滞**——打转触发后用户按 y 继续、模型换方法成功，终答仍被规则 5（no_stuck）判负 → 每轮追加 verification_failed → 无限自证（跑满 max_turns=50 才停）；② 验证规则 1/3/4 扫**全量历史**（无回合 scope）——往回合遗留的 transient/resource 失败永久污染后续所有回合；③ L1 沉淀不看中断——Ctrl+C 被 `AgentLoop.run` 吞成 `user_interrupt` 返回（repl 层看不到异常），旧实现照常触发 `_maybe_note_turn` → **又开一个完整沉淀回合**，继承毒上下文（满历史 verification_failed 观察消息）再次陷循环；④ `_note_signals` 传**全量消息**——corrected 信号从第二回合起恒真（历史 user 累积）、lesson 可能来自历史配对。**修复**：① stuck 用户选继续后 `stuck_fired = False` 解滞（连同 StuckDetector 重置——每轮重新检测，不粘滞）；② **verification_failed 熔断**：连续 2 次未通过即 `return TurnResult(..., "verification_failed", note=规则名+advice)`，不再追加观察消息；观察消息措辞升级为"系统自动规则校验、非用户报错；若认为已完成/误报，说明理由即可结题，**不要反复重写已经正确的答案**"；③ 规则 1/3/4 按**回合 scope** 过滤（`_in_scope`；openai loop 路按 `turn_start` 构集，codebuddy runner 路由 `_mirror_tool_results` 把 tool 消息 turn 纳入 scope_turns）；规则 2 改用**最后轮次**口径（`{max(scope_turns)}`——本回合早期被拒、模型已换方法成功的场景不判负，否则"拒绝→恢复→验证失败"又成死循环源，回归测试 test_loop_denied_action_blocks 锁住）；④ repl 层 `stop_reason == "user_interrupt"` → **跳过 L1 沉淀**；⑤ repl 记录 `n_msgs_before`，L1 信号源改**本回合增量消息**；⑥ TURN_NOTE_PROMPT 显式隔离历史观察消息（"忽略其内容，不要回应、不要重做其中要求的修正"）。**实证**：同参数 read_file 连续失败 3 次 → 继续 → 换方法成功 → **verified**（旧实现死循环到 max_turns）；_AlwaysFailVerifier 下 2 轮熔断、观察消息仅 1 条；repl 集成测试验证中断零沉淀/正常回合照常沉淀。测试：test_v0427.py 13 项；全量 525 passed。**教训**：① "重试反馈"类观察消息必须配**熔断**——否则模型的自尊心（不承认完成）会把回合变成无限自证；② 会话级粘滞标志（stuck_fired）在"用户显式选择继续"后必须清除，否则恢复路径被自己的验证器锁死；③ 吞异常转返回值的设计（AgentLoop.run 吞 KI）会让上层"看不到"中断——所有下游触发器（沉淀/总结）都必须显式检查中断语义。

75. **双行状态行残影 + 思考全文回顾（v0.4.28，用户实测 + 用户讨论定案）**：① **执行结束后状态行残留**——用户截图：两行重复的"正在执行 execute_shell, 请稍等... (0s)"停住不消失，而操作早已完成（`✓ 完成` 都打出来了）。根因：v0.2.9 引入的**双行状态行**（状态行 + 常驻输入行）用 `"\n"` 占位 + `\x1b[1A` 上移重绘，该机制与流式输出（半行文本未换行）、思考框（Live 改光标）、看门狗线程（并发重启 spinner）互相干扰——占位写在不可控时机 → 光标行偏移 → 每帧清错行 → 旧帧永久残留（(0s) 不增长即错位后无人更新的残行）。修复：**双行默认关闭**（`agent.two_line_status: false`）——其原始目的"提示符一直存在"已被 v0.4.8 起的 prompt_toolkit 底部工具栏覆盖；改回 v0.2.2–0.2.8 长期稳定的**单行 `\r` 同行重写**（无占位、无上移，光标管理简单一个数量级）；`_start()` 加 `_two_line_primed` 守卫（开回双行时每次启动恰好一次占位，运行中重复 `_start()` 是 no-op）；单行插嘴尾缀补回 v0.4.23 的"← 按回车发送"提示（不因关双行丢失）。`tool_result` 成功不停圈的有意设计（v0.3.0）不变——修复残行不该改这个语义（回归测试锁住）。② **思考全文回顾（用户讨论定案）**——用户要求"保留滚动窗显示方式，同时能回顾思考的所有内容（收起/展开）"。**选型**：终端内交互展开（Ctrl+T 放大 Live）在 Windows 终端不可靠（v0.4.17 手写 ANSI 重绘错位、v0.4.19 改 Live 的教训）且与插嘴键盘采集（msvcrt）冲突 → 采用**落盘 + 命令回顾**：`thinking_write` 始终累积全文，`_close_thinking` 时追加到 `<program_root>/thinking/<会话8位>.log`（段带序号/时间/字数；单文件超 2MB 保留后半防无限增长；非 TTY 不落盘——那场景思考本就全显；每回合提示一次"💭 思考全文 N 字已存档 → /think 可回顾"降噪）；新命令 `/think [n|all]`（默认最近 1 段，n=最近 n 段，all=全部；走 `status.block()` 折叠，Ctrl+O 全显）。**语义**：收起=默认滚动窗（执行中），展开=/think 读档（任何时候）——终端显示方式完全不变，回顾走命令。测试：test_v0428.py 13 项（单行无 ANSI 序列、双行 primed 语义、存档落盘与提示一次、/think 分段读取、tool_result 语义回归）；全量 538 passed。

76. **全面审计 + 40 项修复（v0.4.29，用户要求"修复所有 bug"）**：四路并行审计（sandbox/session、agent/providers、tools/memory、cli/config）+ 逐项动态复现，发现 5 P0 / 15 P1 / ~20 P2。**P0**：① `verify.py` 空 scope `max()` 崩（codebuddy 路纯问答每问必崩）；② `codebuddy.py` `_vision` cellvar 未绑定先用（带图必崩）；③ `memory/store.py` `_rewrite` 只重建记忆区（删除条目抹掉项目记忆+进度日志）；④ 解释器 `file_write` 不提取代码内路径（`python -c open('C:/Windows/x','w')` 放行）；⑤ `%VAR%`/`$env:` 不识别（`type %USERPROFILE%\.ssh\id_rsa` 绕过 deny）。**P1**：image size 字符串化 "None"、插嘴 bind 在同步线程必失败（执行中注入整体失效）、复合删除命令误删（`rm x && gcc y`）、中断回合产物恒空（_persist 顺序）、query_only 降级启动崩、memory 插入点/预算投影、授权缓存 flag 粒度、file_edit 非 UTF-8 破坏、resume_context 被补沉淀偷走、compaction turn 重排与 scope 错位、arguments 非 dict、web_fetch save_as/重定向、image_path 外泄、压缩后二次 assemble。**P2**：tool_result 粘连、CJK 列宽、StatusLine 加锁、MCP/pre_store/clipwatch 生命周期、YAMLError、config 启发式、死代码清理、prompts 矛盾、NullStatus 补齐、journal hint 噪声、global_store 解码、env_cleanup downloads、repair 防护、attachments 缓存、vision cache 上限、tokenizer images。测试 28 项；全量 567 passed。

77. **第二轮复审（对 v0.4.29 修复的回归审）+ 20 项修复（v0.4.30）**：四路复审发现**修复本身引入/遗漏**的问题，再次证明"修完必须再审"。**P0（v0.4.29 引入）**：① `shell.py` 缺 `from pathlib import Path` → 所有纯删除命令 NameError（回收站链路整条失效）；② `file_edit` 字节域与 Windows CRLF 冲突（write 落 CRLF、read 显示 LF、edit 用 LF 匹配 → "自写→自读→自改"闭环断裂），补 LF/CRLF 互换回退；③ `image.py`/`memory.py` 调不存在的 `gw.check`（GatewayHandle 只有 confirm/note_subaction）→ 图生图与 global_memory_write 必失败——image 改为"工作空间/项目目录内 + 扩展名 + 魔数"三重结构性约束（不依赖网关句柄），memory 删除冗余二次判定。**P1**：④ verify 规则 2 口径（`max(scope_turns)` 恒等于最后消息 turn，而 audit `_turn` 是发起工具调用的 assistant turn → deny 记录从不被发现）；改为"同类被拒 ≥2 次才判负"（拒绝后结题=正常交互，反复重试被拒动作才危险）；⑤ `<user_interjection>` 注入后"最后 user 消息"定位失准 → `_collect_products`/journal/resume 统一加内部观察消息过滤（6 种标记）；⑥ 压缩旧 summary 的 turn=max+1 被当"最新消息"永久保留 → 摘要叠加（实测连续 2 次压缩 2 份）——旧 summary 强制进 old_part 且生成新摘要后从输出中移除；⑦ `_pair_across_boundary` 逐个 insert(0) 打乱时序 → 按 turn 有序插入；⑧ `fut.result(timeout)` 假超时（`with ThreadPoolExecutor` 退出仍 `shutdown(wait=True)`）+ 孤儿线程副作用 → 改显式 `shutdown(wait=False, cancel_futures=True)`，超时提示"后台可能仍在执行、勿盲目重试"；⑨ db `PRAGMA` 在坏库上先抛（v0.4.3 修复指引永不触发）→ 包 try；`SessionStore` 的 OperationalError 只读态跳过、非只读重抛；⑩ 插嘴 settle 非流式分支丢文本 + `reset_queue` 丢 asyncio 队列残留 → 统一 pending_texts + 排水到 fallback；⑪ 解释器 `shutil.copy/move`、`os.rename`、`mkdir` 的目标路径不提取 → 补正则 + `saw_write and not literals` 兜底 dynamic；⑫ `%VAR:~0,2%`/`${HOME}`/`!VAR!` 绕过 → 正则放宽；⑬ `open('x')`（无 mode）误判为写 → 写 mode 必须显式；⑭ 字面量正则 `(.+?)` 二次回溯（23KB 570ms）→ 有界字符类 `[^'"\n]{0,4096}`（17ms）；⑮ `destructive` 漏单个 `&` 与换行；⑯ `memory.add_entry` 在无 MEMORY 区的畸形文件上把条目插到区外 → 写前补区头；⑰ `-p`/`/resume` 的 resume_context 登记、`--resume __pick__` 重复提示、`handle_slash` 无异常兜底；⑱ 双行 `_print` 落点、`_render_paused` 深度计数、`_run_turn` 页脚前停转圈、思考不并入 assistant 正文（防历史重复叙述）。测试 18 项（test_v0430.py）+ 两套灾难场景脚本（删除链路/CRLF 闭环/规则2/插嘴定位/摘要叠加/config null/熔断/去重）；全量 585 passed。**教训**：① 给工具加"结构性安全约束"时不要假设网关句柄有判定接口（check 是网关自身的方法，不是 handle）；② 字节域修复必须连带换行域（Windows CRLF vs universal newlines）；③ 任何"按最后一条消息定位"的启发式都会被系统注入消息（插嘴/验证/中断提示）打破——要么按 turn 区间，要么统一维护内部标记表；④ 压缩产物（summary）不能再参与下一次压缩的"最近 N 回合"分区。
