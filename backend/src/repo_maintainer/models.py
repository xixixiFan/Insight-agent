# Bug 复盘 / 自动修复 Agent 系统的 领域模型定义文件
# from xx import xx 导入特定函数

#  annotations的作用是允许在类型提示中使用前向引用，即在类型提示中引用尚未定义的类或类型。这在定义类时非常有用，尤其是当类之间存在相互引用的情况。通过使用annotations，可以避免因类定义顺序导致的类型错误。
from __future__ import annotations
# dataclasses模块提供了一个装饰器和函数，用于简化类的定义，使其更容易创建不可变的数据对象。它自动生成一些特殊方法，如__init__()、__repr__()、__eq__()等，从而减少了样板代码的编写。
from dataclasses import dataclass, field
# datetime模块提供了处理日期和时间的类和函数。datetime.now(timezone.utc)返回当前的UTC时间，isoformat()将其转换为ISO 8601格式的字符串表示。
from datetime import datetime, timezone
from enum import Enum
# typing模块提供了对类型提示的支持。Any表示可以是任意类型，list[str]表示字符串列表，dict[str, Any]表示键为字符串、值为任意类型的字典。
from typing import Any


# 定义获取当前UTC时间的函数，返回ISO 8601格式的字符串表示
def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# 定义类（面向对象：属性+方法）
# Bug categories,继承自str和Enum类，表示Bug的分类
class BugCategory(str, Enum):
    """Bug 分类（对齐 Bug 复盘知识库场景）"""
    # react_closure: React 组件闭包问题
    REACT_CLOSURE = "react_closure"
    # sse_stream: Server-Sent Events 流问题
    SSE_STREAM = "sse_stream"
    # race_condition: 竞态条件问题
    RACE_CONDITION = "race_condition"
    # ws_reconnect: WebSocket 重连问题
    WS_RECONNECT = "ws_reconnect"
    # ci_pollution: CI 污染问题
    CI_POLLUTION = "ci_pollution"
    # dependency_conflict: 依赖冲突问题
    DEPENDENCY_CONFLICT = "dependency_conflict"
    # test_regression: 测试回归问题
    TEST_REGRESSION = "test_regression"
    # cross_module_defect: 跨模块缺陷
    CROSS_MODULE_DEFECT = "cross_module_defect"
    # unknown: 未知问题
    UNKNOWN = "unknown"


# Legacy enum for backward compatibility
class IncidentType(str, Enum):
    # ci_failure: CI 失败问题
    CI_FAILURE = "ci_failure"
    # dependency_conflict: 依赖冲突问题
    DEPENDENCY_CONFLICT = "dependency_conflict"
    # test_regression: 测试回归问题
    TEST_REGRESSION = "test_regression"
    # cross_module_defect: 跨模块缺陷
    CROSS_MODULE_DEFECT = "cross_module_defect"
    # unknown: 未知问题
    UNKNOWN = "unknown"


# 定义Agent角色的枚举类，继承自str和Enum类
class AgentRole(str, Enum):
    """Agent role definitions"""
    # generalist: 通用型 Agent
    GENERALIST = "generalist"
    # event_classifier: 事件分类器 Agent
    EVENT_CLASSIFIER = "event_classifier"
    # info_extractor: 信息抽取器 Agent
    INFO_EXTRACTOR = "info_extractor"
    # context_enricher: 上下文增强器 Agent
    CONTEXT_ENRICHER = "context_enricher"
    # review_agent: 审查 Agent
    REVIEW_AGENT = "review_agent"
    # pipeline_orchestrator: 流水线编排器 Agent
    PIPELINE_ORCHESTRATOR = "pipeline_orchestrator"
    # quality_scorer: 质量评分器 Agent
    QUALITY_SCORER = "quality_scorer"
    # diagnose: 诊断 Agent
    DIAGNOSE = "diagnose"
    # fix: 修复 Agent
    FIX = "fix"
    # validate: 验证 Agent
    VALIDATE = "validate"
    # critic: 批评 Agent
    CRITIC = "critic"
    # rebuttal: 反驳 Agent
    REBUTTAL = "rebuttal"
    # reflect: 反思 Agent
    REFLECT = "reflect"


# 定义补丁操作的枚举类，继承自str和Enum类
class PatchOperation(str, Enum):
    REWRITE = "rewrite"
    CREATE = "create"
    DELETE = "delete"

# 定义审批范围的枚举类，继承自str和Enum类
class ApprovalScope(str, Enum):
    # local_command: 本地命令执行范围
    LOCAL_COMMAND = "local_command"
    # github_write: GitHub 写入范围
    GITHUB_WRITE = "github_write"
    # ci_write: CI 写入范围
    CI_WRITE = "ci_write"
    # remote_execution: 远程执行范围
    REMOTE_EXECUTION = "remote_execution"


""" 
Bug report (primary event record)
@dataclass 一个装饰器，帮你自动生成样板代码（__init__、__repr__、__eq__）
slots=True 表示使用 __slots__ 来优化内存使用，避免为每个实例创建 __dict__，从而节省内存空间。
__slots__原理：Python 对象默认用一个字典 __dict__ 存属性，灵活但吃内存，用 __slots__ 改成固定槽位
"""
@dataclass(slots=True)
class BugReport:
    """
    Bug 复盘原始报告（对齐上游愿景：来源扩展至 PR/提交/日志/群聊）
    类型注释，不会影响代码执行，但可以帮助开发者理解变量的类型，并在静态类型检查工具中使用。
    与Java默认属性为null不同，Python没有默认值，必须显式赋值，否则属性不存在
    """
    id: str
    title: str
    description: str
    logs: str
    """ 
    注解 + 赋值，变量存在且为 list[str] 类型，默认值为一个空列表
    不能直接写[],因为Python 的默认参数在函数定义时只求值一次，会导致所有实例共享同一个列表对象
    """
    changed_files: list[str] = field(default_factory=list)
    suspected_modules: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    reported_at: str = field(default_factory=iso_now)
    # 新增字段：来源类型（PR / 日志 / 群聊 / 手动）
    source_type: str = "manual"
    # 新增字段：关联 PR/Commit 信息
    # 注解 + 赋值，变量存在且为 None 或 str 类型，默认值为 None
    pr_id: str | None = None
    commit_sha: str | None = None
    repo_url: str | None = None

    """ 
    声明BugReport类的实例方法to_dict，用于将实例转换为字典表示。返回类型为dict[str, Any]，表示键为字符串，值可以是任意类型的字典。
    自动生成 to_dict: from pydantic import BaseModel, class BugReport(BaseModel): id: str; title: str; description: str; logs: str; changed_files: list[str] = []; suspected_modules: list[str] = []; metadata: dict[str, Any] = {}; reported_at: str = iso_now(); source_type: str = "manual"; pr_id: str | None = None; commit_sha: str | None = None; repo_url: str | None = None
    """
    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "logs": self.logs,
            # list(...) 和 dict(...)是防御性拷贝，确保返回的字典中的列表和字典是 BugReport 实例的副本，而不是原始对象的引用。这样可以避免外部修改返回的字典影响到 BugReport 实例的内部状态。
            "changed_files": list(self.changed_files),
            "suspected_modules": list(self.suspected_modules),
            "metadata": dict(self.metadata),
            "reported_at": self.reported_at,
            "source_type": self.source_type,
            "pr_id": self.pr_id,
            "commit_sha": self.commit_sha,
            "repo_url": self.repo_url,
        }


    # classmethod 装饰器将 from_dict 方法定义为类方法，可以通过类本身调用，而不需要实例化对象。
    # cls 参数表示当前类（BugReport）;self 参数表示当前实例（对象）。在类方法中，cls 可以用于创建类的实例或访问类的属性和方法。
    # -> "BugReport" 表示该方法的返回类型是 BugReport 类的实例。前向引用（"BugReport"）用于在类型提示中引用尚未定义的类。
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BugReport:
        return cls(
            # data.get("title","默认")类比map.getOrDefault("title", "默认")
            id=data.get("id", data.get("title", "bug").lower().replace(" ", "-")),
            title=data.get("title", "Untitled Bug Report"),
            description=data.get("description", ""),
            logs=data.get("logs", ""),
            # list() 是浅拷贝,要深拷贝得用 copy.deepcopy()
            changed_files=list(data.get("changed_files", [])),
            suspected_modules=list(data.get("suspected_modules", [])),
            metadata=dict(data.get("metadata", {})),
            reported_at=data.get("reported_at", iso_now()),
            source_type=data.get("source_type", "manual"),
            pr_id=data.get("pr_id"),
            commit_sha=data.get("commit_sha"),
            # or的短路特性：a 为真返回 a，否则返回 b
            repo_url=data.get("repo_url") or data.get("metadata", {}).get("repo_url"),
        )


# 定义IncidentReport类，继承自BugReport类，用于表示事件报告。该类主要用于向后兼容，提供了一个from_dict类方法，用于从字典数据创建IncidentReport实例。
class IncidentReport(BugReport):
    """Type alias for backward compatibility."""
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "IncidentReport":
        return cls(
            id=data.get("id", data.get("title", "incident").lower().replace(" ", "-")),
            title=data.get("title", "Untitled incident"),
            description=data.get("description", ""),
            logs=data.get("logs", ""),
            changed_files=list(data.get("changed_files", [])),
            suspected_modules=list(data.get("suspected_modules", [])),
            metadata=dict(data.get("metadata", {})),
            reported_at=data.get("reported_at", iso_now()),
            source_type=data.get("source_type", "manual"),
            pr_id=data.get("pr_id"),
            commit_sha=data.get("commit_sha"),
            repo_url=data.get("repo_url"),
        )


# 定义CompressedError类，用于表示压缩后的错误信息。该类使用了dataclass装饰器，并启用了slots以优化内存使用。
@dataclass(slots=True)
class CompressedError:
    error_type: IncidentType
    error_name: str
    keywords: list[str]
    key_stack_frames: list[str]
    root_cause_cluster: str
    semantic_summary: str
    confidence: float


# 定义KnowledgeEntry类，用于表示踩坑知识条目。该类使用了dataclass装饰器，并启用了slots以优化内存使用。
@dataclass(slots=True)
class KnowledgeEntry:
    """踩坑知识条目（对齐上游愿景：团队踩坑知识库）"""
    id: str
    name: str
    description: str
    triggers: list[str]
    error_types: list[str]
    action_template: str
    version: int = 1
    usage_count: int = 0
    success_count: int = 0
    last_used_at: str = field(default_factory=iso_now)
    active: bool = True
    source_trace_ids: list[str] = field(default_factory=list)
    embedding: list[float] = field(default_factory=list)
    # 新增字段：关联的 Bug 复盘文档
    related_bug_ids: list[str] = field(default_factory=list)
    # 新增字段：关键词标签（用于语义搜索）
    keywords: list[str] = field(default_factory=list)
    # 新增字段：适用场景（前端/后端/全栈等）
    applicable_context: str = ""

    # property 装饰器将 success_rate 方法定义为属性，可以通过实例.属性名 的方式访问，而不需要调用方法。
    @property
    def success_rate(self) -> float:
        if self.usage_count == 0:
            return 0.0
        return self.success_count / self.usage_count

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "KnowledgeEntry":
        return cls(
            id=data["id"],
            name=data["name"],
            description=data["description"],
            triggers=list(data.get("triggers", [])),
            error_types=list(data.get("error_types", [])),
            action_template=data.get("action_template", ""),
            version=int(data.get("version", 1)),
            usage_count=int(data.get("usage_count", 0)),
            success_count=int(data.get("success_count", 0)),
            last_used_at=data.get("last_used_at", iso_now()),
            active=bool(data.get("active", True)),
            source_trace_ids=list(data.get("source_trace_ids", [])),
            embedding=list(data.get("embedding", [])),
            related_bug_ids=list(data.get("related_bug_ids", [])),
            keywords=list(data.get("keywords", [])),
            applicable_context=data.get("applicable_context", ""),
        )



class Skill(KnowledgeEntry):
    """Type alias for compatibility."""
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Skill":
        return cls(
            id=data["id"],
            name=data["name"],
            description=data["description"],
            triggers=list(data.get("triggers", [])),
            error_types=list(data.get("error_types", [])),
            action_template=data.get("action_template", ""),
            version=int(data.get("version", 1)),
            usage_count=int(data.get("usage_count", 0)),
            success_count=int(data.get("success_count", 0)),
            last_used_at=data.get("last_used_at", iso_now()),
            active=bool(data.get("active", True)),
            source_trace_ids=list(data.get("source_trace_ids", [])),
            embedding=list(data.get("embedding", [])),
            related_bug_ids=list(data.get("related_bug_ids", [])),
            keywords=list(data.get("keywords", [])),
            applicable_context=data.get("applicable_context", ""),
        )


@dataclass(slots=True)
class SymbolDefinition:
    symbol_id: str
    name: str
    kind: str
    file_path: str
    line: int
    language: str
    signature: str = ""


@dataclass(slots=True)
class CodeEdge:
    source: str
    target: str
    edge_type: str
    weight: float = 1.0


@dataclass(slots=True)
class CodeGraphMetadata:
    graph_nodes: int
    graph_edges: int
    strongly_connected_components: int
    tree_sitter_enabled: bool
    languages_with_tree_sitter: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RepoSnapshot:
    root_dir: str
    files: list[str]
    file_summaries: dict[str, str]
    dependency_edges: list[tuple[str, str]]
    call_edges: list[tuple[str, str]]
    language_breakdown: dict[str, int]
    symbols: list[SymbolDefinition] = field(default_factory=list)
    graph_edges: list[CodeEdge] = field(default_factory=list)
    graph_metadata: CodeGraphMetadata | None = None


@dataclass(slots=True)
class ContextBundle:
    working_context: dict[str, Any]
    short_term_memories: list[dict[str, Any]]
    long_term_memories: list[dict[str, Any]]
    matched_skills: list[KnowledgeEntry]
    relevant_files: list[str]
    budget_allocations: dict[str, int]
    semantic_focus: list[str]


@dataclass(slots=True)
class AgentTask:
    id: str
    role: AgentRole
    objective: str
    focus_files: list[str]
    budget: int
    allowed_tools: list[str] = field(default_factory=list)
    sandbox_policy: dict[str, Any] = field(default_factory=dict)
    negotiation_round: int = 1


## Extraction execution step
@dataclass(slots=True)
class ExtractionStep:
    """LLM 抽取步骤（对齐 Bug 复盘场景）"""
    title: str
    description: str
    target_files: list[str]
    confidence: float
    source_skill_id: str | None = None
    # 新增字段：字段抽取结果（用于结构化输出）
    extracted_fields: dict[str, Any] = field(default_factory=dict)


# RepairStep 是 ExtractionStep 的别名，用于表示修复步骤。它们具有相同的结构和属性，但在语义上可能用于不同的上下文。
RepairStep = ExtractionStep


@dataclass(slots=True)
class AgentExecution:
    task_id: str
    role: AgentRole
    reasoning_summary: str
    repair_steps: list[ExtractionStep]
    token_estimate: int
    success_likelihood: float
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SchedulerDecision:
    mode: str
    complexity_score: int
    reasons: list[str]
    tasks: list[AgentTask]
    budget_breakdown: dict[str, int]


@dataclass(slots=True)
class PreparedIncident:
    incident: BugReport
    snapshot: RepoSnapshot
    compressed_error: CompressedError
    matched_skills: list[KnowledgeEntry]
    relevant_files: list[str]
    context: ContextBundle
    decision: SchedulerDecision
    executions: list[AgentExecution]
    repair_steps: list[ExtractionStep]
    retrieval_matches: list["RetrievalMatch"] = field(default_factory=list)
    github_run_id: int | None = None


@dataclass(slots=True)
# 补丁编辑操作
class PatchEdit:
    path: str # 文件路径
    operation: PatchOperation # 操作类型
    content: str = "" # 操作内容
    reason: str = "" # 操作原因


## Structured review output
@dataclass(slots=True)
class StructuredReview:
    """LLM extraction output"""
    analysis: str # 分析结果
    summary: str # 摘要
    commit_message: str # 提交消息
    validation_commands: list[str] # 验证命令
    edits: list[PatchEdit] # 补丁编辑操作
    model: str # 模型名称
    raw_response: str = "" # 原始 LLM 响应
    usage: dict[str, int] = field(default_factory=dict) # 使用量信息
    # 新增字段：抽取质量指标
    extraction_confidence: float = 0.0
    # 新增字段：Bug 复盘核心字段
    root_cause: str = "" # 根原因
    impact_scope: str = "" # 影响范围
    fix_solution: str = "" # 修复方案
    prevention: str = "" # 预防措施
    related_modules: list[str] = field(default_factory=list) # 相关模块
    keywords: list[str] = field(default_factory=list) # 关键词



PatchPlan = StructuredReview


@dataclass(slots=True)
class ValidationResult:
    commands: list[str]
    success: bool
    command_results: list[dict[str, Any]]
    combined_output: str
    duration_seconds: float


## Review result
@dataclass(slots=True)
class ReviewResult:
    """Review outcome"""
    trace: "ExecutionTrace"
    success: bool
    sandbox_path: str
    baseline_validation: ValidationResult
    final_validation: ValidationResult
    attempts: int
    rollback_performed: bool
    commit_hash: str | None
    diff_summary: str
    patch_plan: StructuredReview | None
    report_path: str
    delivery: dict[str, Any] = field(default_factory=dict)
    # 新增字段：审核状态（draft/pending/approved/rejected）
    review_status: str = "draft"
    # 新增字段：审核备注
    reviewer_notes: str = ""
    # 新增字段：发布到 Wiki 的结果
    wiki_publish_result: dict[str, Any] = field(default_factory=dict)



RepairOutcome = ReviewResult


@dataclass(slots=True)
class GitHubWorkflowRun:
    run_id: int
    workflow_id: int | None
    name: str
    status: str
    conclusion: str | None
    html_url: str | None
    logs_url: str | None
    rerun_url: str | None
    head_branch: str | None
    head_sha: str | None
    event: str | None
    created_at: str | None
    updated_at: str | None


@dataclass(slots=True)
class GitHubSyncResult:
    owner: str
    repo: str
    runs: list[GitHubWorkflowRun]
    incidents: list[BugReport]
    # last_logs_excerpt 字段用于存储最近的日志摘要，类型为字符串，默认值为空字符串。这个字段可以帮助用户快速了解最近的日志内容，而无需查看完整的日志。
    latest_logs_excerpt: str = ""


@dataclass(slots=True)
class CiWorkflowRun:
    provider: str
    run_id: str
    name: str
    status: str
    conclusion: str | None
    web_url: str | None
    logs_url: str | None
    rerun_target: str | None
    ref: str | None
    revision: str | None
    created_at: str | None
    updated_at: str | None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CiSyncResult:
    provider: str
    runs: list[CiWorkflowRun]
    incidents: list[BugReport]
    latest_logs_excerpt: str = ""


@dataclass(slots=True)
class RetrievalMatch:
    item_id: str
    item_type: str
    score: float
    lexical_score: float
    semantic_score: float
    payload: dict[str, Any]
    reranker_score: float = 0.0


@dataclass(slots=True)
class ApprovalRecord:
    scope: ApprovalScope
    subject: str
    actor: str
    approved_at: str
    expires_at: str
    reason: str = ""


@dataclass(slots=True)
class AuditEvent:
    event_type: str
    status: str
    subject: str
    created_at: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ExecutionTrace:
    id: str
    incident: BugReport
    compressed_error: CompressedError
    decision: SchedulerDecision
    executions: list[AgentExecution]
    matched_skill_ids: list[str]
    success: bool
    repair_summary: str
    token_usage: int
    context_digest: dict[str, Any]
    execution_mode: str = "planning"
    validation_passed: bool | None = None
    validation_commands: list[str] = field(default_factory=list)
    sandbox_path: str | None = None
    commit_hash: str | None = None
    rollback_performed: bool = False
    diff_summary: str | None = None
    attempt_count: int = 0
    llm_model: str | None = None
    github_run_id: int | None = None
    retrieval_matches: list[dict[str, Any]] = field(default_factory=list)
    approvals_used: list[dict[str, Any]] = field(default_factory=list)
    agent_runtime_backend: str | None = None
    collaboration_blackboard: dict[str, Any] = field(default_factory=dict)
    delivery: dict[str, Any] = field(default_factory=dict)
    auto_learning_status: str = "not_requested"
    auto_learning_report: dict[str, Any] = field(default_factory=dict)
    # 新增字段：审核状态（draft/pending/approved/rejected）
    review_status: str = "draft"
    # 新增字段：审核备注
    reviewer_notes: str = ""
    created_at: str = field(default_factory=iso_now)
