"""
LLM 执行器（重构版）

新增能力：
1. Bug 复盘场景：generate_bug_review() 方法，输出符合 BugReviewDocument Schema 的 JSON
2. JSON Schema 约束：通过 schema_mode 参数控制输出格式
3. 指数退避重试：内置重试机制（最多 3 次，1/2/4s 间隔）
4. 日志裁剪前置：集成 LogCompressor 对超长日志进行预处理
"""
from __future__ import annotations

import json
import random
import re
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .bug_schema import BUG_REVIEW_JSON_SCHEMA, BugReviewDocument, parse_llm_bug_review
from .config import OpenAIExecutorConfig, SandboxConfig
from .log_compressor import LogCompressor
from .models import BugReport, PatchEdit, PatchOperation, PatchPlan, PreparedIncident, StructuredReview

# 继承自 RuntimeError 的自定义异常类，用于表示 LLM 执行器相关的错误。
class PatchExecutorError(RuntimeError):
    pass
# 继承自 RuntimeError 的自定义异常类，用于表示 Bug 复盘抽取相关的错误。
class BugExtractionError(RuntimeError):
    """Bug 复盘抽取失败"""
    pass


class OpenAIPatchExecutor:
    """
    LLM 执行器（支持代码修复 + Bug 复盘抽取双模式）
    设计模式：适配器（Adapter） —— 一套接口，适配三家 LLM。

    代码修复模式（generate_patch）：
    - 原有功能，生成 PatchPlan（文件编辑列表）

    Bug 复盘抽取模式（generate_bug_review）：
    - 新增功能，从 BugReport 原始内容抽取结构化 BugReviewDocument
    - 支持 JSON Schema 约束，防止 LLM 输出格式错乱
    - 内置指数退避重试（最多 3 次）
    - 日志裁剪前置（通过 LogCompressor）
    """

    #re.compile() 方法用于预编译正则表达式模式，返回一个正则表达式对象，可以使用该对象进行匹配操作。
    FILE_REFERENCE = re.compile(
        # FILE_REFERENCE 用于匹配文件路径引用，支持多种文件扩展名和格式。
        r"(?:(?:File\s+\"(?P<quoted>[^\"]+\.(?:py|js|jsx|ts|tsx|json|toml|ya?ml|txt|md))\")|"
        r"(?P<plain>[A-Za-z0-9_./\\-]+\.(?:py|js|jsx|ts|tsx|json|toml|ya?ml|txt|md))"
        r"(?::\d+|::[A-Za-z_][A-Za-z0-9_]*|\s|$))",
        re.IGNORECASE, # IGNORECASE 标志表示匹配时忽略大小写。
    )
    SYMBOL_REFERENCE = re.compile(
        # SYMBOL_REFERENCE 用于匹配符号引用，支持函数调用和模块对象引用。
        r"(?:where\s+.*?=\s+|where\s+False\s+=\s+|E\s+\+\s+where\s+.*?=\s+)"
        r"(?P<symbol>[A-Za-z_][A-Za-z0-9_]*)\s*\(",
        re.IGNORECASE,
    )
    MODULE_OBJECT_REFERENCE = re.compile(r"<(?P<module>[A-Za-z_][A-Za-z0-9_.]*)\.(?P<symbol>[A-Za-z_][A-Za-z0-9_]*)\s+object")
    # MODULE_OBJECT_REFERENCE 用于匹配模块对象引用，支持 <module.symbol object> 格式。

    def __init__(self, config: OpenAIExecutorConfig, sandbox_config: SandboxConfig) -> None:
        self.config = config
        self.sandbox_config = sandbox_config

    def is_configured(self) -> bool:
        return bool(self.config.api_key)

    # ===== Bug 复盘抽取 API（新增）=====

    def generate_bug_review(
        self,
        bug_report: BugReport | PreparedIncident, # 联合类型
        source_text: str = "",
        max_retries: int = 3,
        initial_retry_delay: float = 1.0,
        max_retry_delay: float = 10.0,
        knowledge_context: str = "",
    ) -> BugReviewDocument:
        """
        从 BugReport 原始内容抽取结构化 Bug 复盘文档

        Args:
            bug_report: BugReport 对象或 PreparedIncident
            source_text: 额外的原始文本（如 PR 讨论、完整日志）
            max_retries: 最大重试次数（指数退避 + Full Jitter）
            initial_retry_delay: 初始重试延迟秒数
            max_retry_delay: 最大重试重试延迟秒数（上限延迟秒数）
            knowledge_context: 由 KnowledgeMatchSkill 生成的历史案例上下文

        Returns:
            BugReviewDocument 结构化文档

        Raises:
            BugExtractionError: 抽取失败（超过重试次数）
        """
        if not self.is_configured():
            # raise方法用于抛出异常，终止当前函数的执行，并将异常传递给调用者。
            raise BugExtractionError(
                f"Missing API key in environment variable `{self.config.api_key_env}`."
            )

        # 获取实际的 BugReport：如果传入的是 PreparedIncident，则取其 incident 属性；否则直接使用 BugReport。
        if isinstance(bug_report, PreparedIncident):
            report = bug_report.incident
        else:
            report = bug_report

        # 日志裁剪前置（防止超出 LLM的Context Window）
        log_compressor = LogCompressor(max_chars=5000)
        compressed_logs = log_compressor.compress(report.logs).text if report.logs else ""

        # 构造原始文本（来源绑定）
        if not source_text:
            source_text = "\n".join(filter(None, [ # filter(None, ...) 用于过滤掉空字符串或 None 值，确保只保留非空的文本片段。
                report.title,
                report.description,
                compressed_logs,
            ]))

        system_prompt = self._build_bug_review_system_prompt(knowledge_context)
        user_payload = self._build_bug_review_user_payload(report, source_text, compressed_logs)

        last_error: Exception | None = None
        # range(1, max_retries + 1) 生成从 1 到 max_retries 的整数序列，用于控制重试次数。
        # 重试循环：
        # 1. 尝试发送请求，解析 JSON 输出
        # 2. 如果解析失败，根据重试次数和延迟时间，判断是否需要重试
        # 3. 如果重试次数超过最大重试次数，抛出异常
        for attempt in range(1, max_retries + 1):
            try:
                response = self._dispatch_request(
                    system_prompt=system_prompt,
                    user_payload=user_payload,
                )
                raw_text = self._extract_output_text(response)
                doc, errors = parse_llm_bug_review(raw_text)
                # 条件1：doc 不为空，说明解析成功
                if doc is not None:
                    doc.bug_id = report.id
                    return doc
                raise BugExtractionError(f"Schema 校验失败: {'; '.join(errors)}")
            except BugExtractionError as exc:
                last_error = exc
                if attempt < max_retries:
                    # 指数退避：Full Jitter + 上限 10 秒，每次重试延迟翻倍，最大延迟 10 秒
                    base_delay = initial_retry_delay * (2 ** (attempt - 1))
                    capped_delay=min(base_delay, max_retry_delay)
                    delay = random.uniform(0, capped_delay) # Full jitter：[0, capped_delay]区间均匀随机
                    # time.sleep(delay) 用于引入一个时间延迟，防止对 API 过度请求。
                    time.sleep(delay)
                    continue
            except PatchExecutorError as exc:
                last_error = exc
                if attempt < max_retries:
                    # 指数退避：每次重试延迟翻倍，最大延迟 10 秒
                   self._sleep_retry_delay(initial_retry_delay,max_retry_delay,attempt-1)
                continue

        raise BugExtractionError(
            f"Bug 复盘抽取失败（已重试 {max_retries} 次）: {last_error}"
        )

    def _build_bug_review_system_prompt(self, knowledge_context: str = "") -> str:
        """
        构建 Bug 复盘抽取抽取系统的提示提示词。
        """
        prompt = (
            "你是专业的 Bug 复盘信息抽取专家。\n"
            "你的任务是从原始 Bug 报告（PR 描述/日志/commit message）中抽取结构化的复盘文档。\n"
            "规则：\n"
            "1. root_cause.source.snippet 必须引用原始文本中的真实片段，严禁虚构\n"
            "2. impact.severity 根据受影响用户量和功能重要性判断（P0=全站崩溃/P1=核心功能/P2=次要功能/P3=体验问题）\n"
            "3. keywords 用于知识库检索，需提取技术关键词（如 React/Hook/useEffect/闭包等）\n"
            "4. confidence 反映你对本次抽取结果的把握程度（0-1）\n"
            "5. 只输出 JSON，不要任何解释文字\n"
            f"6. 严格遵循以下 JSON Schema：{json.dumps(BUG_REVIEW_JSON_SCHEMA, ensure_ascii=False)}"
        )
        # dumps() 方法将 JSON 数据转换为字符串，确保_ascii=False 用于保留中文字符
        if knowledge_context:
            prompt += "\n\n" + knowledge_context
        return prompt

    def _build_bug_review_user_payload(
        self,
        report: BugReport,
        source_text: str,
        compressed_logs: str,
    ) -> dict[str, Any]:
        """
        构建 Bug 复盘抽取抽取系统的用户输入。
        """
        return {
            "task": "从以下原始 Bug 报告中抽取结构化复盘文档",
            "bug_report": {
                "id": report.id,
                "title": report.title,
                "description": report.description[:3000],
                "source_type": report.source_type,
                "pr_id": report.pr_id,
                "commit_sha": report.commit_sha,
                "changed_files": report.changed_files[:10],
                "logs": compressed_logs[:3000] if compressed_logs else "",  # 截断到 3000 字个字符，防止超出 API 最大输入长度
            },
            "source_text_for_snippet_binding": source_text[:4000],  # 截断到 4000 字个字符，防止超出 API 最大输入长度
            "output_format": "严格遵循 JSON Schema，输出单个 JSON 对象",
        }

    # ===== 原有代码修复 API =====

    def generate_patch(
        self,
        prepared: PreparedIncident,
        repo_root: str | Path,
        validation_commands: list[str],
        previous_failure: str = "",
        attempt: int = 1,
        max_retries: int = 2,
        initial_retry_delay: float = 1.0,
    ) -> PatchPlan:
        """
        生成补丁。
        Args:
            prepared: 已准备的 Bug 报告
            repo_root: 仓库根目录
            validation_commands: 验证命令列表
            previous_failure: 上一次失败的补丁（可选）
            attempt: 当前重试次数（默认 1）
            max_retries: 最大重试次数（默认 2）
            initial_retry_delay: 初始重试延迟（秒）
        Returns:
            生成的补丁计划（PatchPlan）
        """
        if not self.is_configured():
            raise PatchExecutorError(
                f"Missing API key in environment variable `{self.config.api_key_env}`. "
                f"Set it before running the repair pipeline for provider `{self.config.provider}`."
            )

        repo_path = Path(repo_root).resolve()  # 转换为Path对象，确保路径绝对化
        file_context = self._collect_file_context(repo_path, prepared)  # 收集代码上下文文件
        system_prompt, user_payload = self._build_prompt_parts(  # 构建提示词词和用户输入
            prepared=prepared,
            file_context=file_context,
            validation_commands=validation_commands,
            previous_failure=previous_failure,
            attempt=attempt,
        )

        last_error: Exception | None = None
        for retry in range(max_retries + 1):
            try:
                # 发送请求
                response = self._dispatch_request(system_prompt=system_prompt, user_payload=user_payload)
                # 解取响应中的文本内容
                raw_text = self._extract_output_text(response)
                # 解析补丁计划
                return self._parse_patch_plan(raw_text, response)
            except (PatchExecutorError, Exception) as exc:  # noqa: BLE001
                last_error = exc
                # 仅对网络/限流类错误重试，解析错误不重试
                if retry < max_retries and self._is_retriable_error(exc):
                    self._sleep_retry_delay(initial_retry_delay, max_retry_delay, retry)
                    time.sleep(delay)
                    continue
                raise

        raise PatchExecutorError(f"生成补丁失败（已重试 {max_retries} 次）: {last_error}")

    @staticmethod
    def _is_retriable_error(exc: Exception) -> bool:
        """
        判断错误是否可重试（网络超时/限流）。
        把异常信息转小写，看有没有包含可重试的关键词
        429 为限流错误，503 为服务不可用错误，connection 为连接错误，temporarily 为临时错误
        """
        msg = str(exc).lower()
        retriable_keywords = ["timeout", "rate limit", "429", "503", "connection", "temporarily"]
        return any(kw in msg for kw in retriable_keywords)

    @staticmethod
    def _sleep_retry_delay(
        initial_retry_delay: float,
        max_retry_delay: float,
        retry_index: int,
    ) -> None:
        """
        指数退避 + Full Jitter 睡眠。

        - 指数部分：base = initial_retry_delay * (2 ** retry_index)
        - 上限部分：capped = min(base, max_retry_delay)
        - Full Jitter：delay = random.uniform(0, capped)

        Full Jitter 是 AWS 推荐策略（参见《Exponential Backoff With Jitter》）：
        退避区间为 [0, capped]，最大等待时间等于纯指数退避，
        不会因加 jitter 而拉长整体节奏，
        但能避免多请求同时撞到同一 Rate Limit（惊群效应）。

        Args:
            initial_retry_delay: 初始重试延迟秒数
            max_retry_delay: 单次重试延迟上限（秒）
            retry_index: 当前重试轮次（0 表示第一次失败后第一次重试）
        """
        base_delay = initial_retry_delay * (2 ** retry_index)
        capped_delay = min(base_delay, max_retry_delay)
        delay = random.uniform(0, capped_delay)
        time.sleep(delay)

    def _collect_file_context(self, repo_path: Path, prepared: PreparedIncident) -> list[dict[str, str]]:
        """
        收集代码上下文文件。
        Args:
            repo_path: 仓库根目录
            prepared: 已准备的 Bug 报告
        Returns:
            代码上下文文件列表（字典列表）
        """
        # dict.fromkeys去重保序，set无序
        # file_candidates来自 relevant_files 和 changed_files去重后的列表
        file_candidates = list(dict.fromkeys(prepared.relevant_files + prepared.incident.changed_files))
        # extend 方法用于在列表末尾添加多个元素
        # 1、添加日志上下文文件
        file_candidates.extend(path for path in self._log_context_candidates(prepared) if path not in file_candidates)
        # 2、添加依赖上下文文件
        file_candidates.extend(path for path in self._dependency_neighbors(prepared, file_candidates) if path not in file_candidates)
        # 3、添加测试上下文文件
        if self._has_test_candidate(file_candidates):
            file_candidates.extend(path for path in self.test if path not in file_candidates)
        if not file_candidates:
            file_candidates.extend(self._small_repo_source_fallback(prepared))
        selected_files = file_candidates[: self.sandbox_config.max_context_files]
        contexts: list[dict[str, str]] = []
        for relative_path in selected_files:
            path = repo_path / relative_path # repo_path 是 Path 对象，relative_path 是字符串，/ 运算符用于拼接路径组件
            # path.exists() 检查文件是否存在，path.is_file() 检查是否为文件（不是目录）
            # 过滤掉不存在的文件或目录
            if not path.exists() or not path.is_file():
                continue
            try:
                # path.read_text() 读取文件内容，指定编码为utf-8
                # 如果文件编码为utf-8-sig，会自动跳过BOM头  
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                try:
                    content = path.read_text(encoding="utf-8-sig")
                except UnicodeDecodeError:
                    continue
            contexts.append(
                {
                    "path": relative_path.replace("\\", "/"),
                    "content": content[: self.sandbox_config.max_file_chars],
                }
            )
        return contexts

    def _log_context_candidates(self, prepared: PreparedIncident) -> list[str]:
        """
        收集日志上下文文件。
        Args:
            prepared: 已准备的 Bug 报告
        Returns:
            日志上下文文件列表（路径列表）
        """
        text = "\n".join(
            [
                prepared.incident.title,
                prepared.incident.description,
                prepared.incident.logs,
                " ".join(prepared.compressed_error.key_stack_frames),
            ]
        )
        referenced_paths = self._extract_referenced_paths(text)  # 提取引用的文件路径
        referenced_symbols = self._extract_referenced_symbols(text) # 提取引用的符号（变量、函数、类等），返回小写集合
        referenced_modules = self._extract_referenced_modules(text) # 提取引用的模块名，返回集合。
        candidates: list[str] = []
        for file_path in prepared.snapshot.files:
            normalized = file_path.replace("\\", "/").lower().lstrip("./")
            basename = Path(normalized).name
            stem = Path(normalized).stem.replace("-", "_")
            if normalized in referenced_paths or basename in referenced_paths or any(normalized.endswith(f"/{item}") for item in referenced_paths):
                candidates.append(file_path)
                continue
            if stem in referenced_modules:
                candidates.append(file_path)
                continue
            if any(symbol.file_path == file_path and symbol.name.lower() in referenced_symbols for symbol in prepared.snapshot.symbols):
                candidates.append(file_path)
        return list(dict.fromkeys(candidates))

    def _dependency_neighbors(self, prepared: PreparedIncident, seeds: list[str]) -> list[str]:
        """
        收集依赖上下文文件。
        Args:
            prepared: 已准备的 Bug 报告
            seeds: 依赖种子文件列表（路径列表）
        Returns:
            依赖上下文文件列表（路径列表）
        """
        seed_set = set(seeds)
        if not seed_set:
            return []
        files_by_stem: dict[str, list[str]] = {}
        for file_path in prepared.snapshot.files:
            # setdefault() 方法用于在字典中设置默认值，如果键不存在则创建并设置默认值
            # 这里用于将文件路径按模块名分组，模块名是小写且替换短横线为下划线
            # stem 是文件名（不包含扩展名），name 是文件名（包含扩展名）,suffix 是文件扩展名（小写）
            files_by_stem.setdefault(Path(file_path).stem.lower().replace("-", "_"), []).append(file_path)
        neighbors: list[str] = []
        for source, target in prepared.snapshot.dependency_edges:
            target_stem = str(target).split(".")[-1].lower().replace("-", "_")
            if source in seed_set:
                neighbors.extend(files_by_stem.get(target_stem, []))
            if target_stem in {Path(path).stem.lower().replace("-", "_") for path in seed_set}:
                neighbors.append(source)
        return list(dict.fromkeys(path for path in neighbors if path not in seed_set))

    def _small_repo_source_fallback(self, prepared: PreparedIncident) -> list[str]:
        """
        收集小仓库源代码上下文文件。
        Args:
            prepared: 已准备的 Bug 报告
        Returns:
            小仓库源代码上下文文件列表（路径列表）
        """
        code_suffixes = {".py", ".js", ".jsx", ".ts", ".tsx"}
        code_files = [path for path in prepared.snapshot.files if Path(path).suffix.lower() in code_suffixes]
        if len(code_files) > 20:
            return []
        source_files = [path for path in code_files if not self._is_test_file(path)]
        test_files = [path for path in code_files if self._is_test_file(path)]
        return source_files + test_files

    def _has_test_candidate(self, paths: list[str]) -> bool:
        """
        检查路径列表中是否包含测试文件。
        Args:
            paths: 文件路径列表（路径列表）
        Returns:
            是否包含测试文件（布尔值）
        """
        return any(self._is_test_file(path) for path in paths)

    def _is_test_file(self, file_path: str) -> bool:
        """检查文件路径是否为测试文件。"""
        normalized = file_path.replace("\\", "/").lower()
        name = Path(normalized).name
        return name.startswith("test_") or name.endswith("_test.py") or "/tests/" in normalized

    def _extract_referenced_paths(self, text: str) -> set[str]:
        """提取文本中引用的文件路径。"""
        paths: set[str] = set()
        for match in self.FILE_REFERENCE.finditer(text):
            value = match.group("quoted") or match.group("plain") or ""
            normalized = value.replace("\\", "/").strip().lower().lstrip("./")
            if normalized:
                paths.add(normalized)
                paths.add(Path(normalized).name)
        return paths

    def _extract_referenced_symbols(self, text: str) -> set[str]:
        """提取引用的符号（变量、函数、类等），返回小写集合。"""
        symbols = {match.group("symbol").lower() for match in self.SYMBOL_REFERENCE.finditer(text) if match.group("symbol")}
        for match in self.MODULE_OBJECT_REFERENCE.finditer(text):
            symbols.add(match.group("symbol").lower())
        return symbols

    def _extract_referenced_modules(self, text: str) -> set[str]:
        """提取引用的模块名，返回集合。"""
        modules: set[str] = set()
        for match in self.MODULE_OBJECT_REFERENCE.finditer(text):
            module = match.group("module").split(".")[0].strip().lower().replace("-", "_")
            if module:
                modules.add(module)
        return modules

    def _build_prompt_parts(
        self,
        prepared: PreparedIncident,
        file_context: list[dict[str, str]],
        validation_commands: list[str],
        previous_failure: str,
        attempt: int,
    ) -> tuple[str, dict[str, Any]]:
        system_prompt = (
            "You are an autonomous repository repair agent. "
            "Return only a JSON object. "
            "Your task is to repair the repository by proposing concrete file edits. "
            "You may only modify files that are already present in the repository unless a new test file is essential. "
            "Prefer the smallest safe fix. "
            "Never include markdown fences."
        )
        user_payload = {
            "task": "Repair the repository incident in a disposable git sandbox.",
            "attempt": attempt,
            "incident": {
                "title": prepared.incident.title,
                "description": prepared.incident.description,
                "changed_files": prepared.incident.changed_files,
                "suspected_modules": prepared.incident.suspected_modules,
                "logs": prepared.incident.logs[-6000:],
            },
            "compressed_error": {
                "error_type": prepared.compressed_error.error_type.value,
                "error_name": prepared.compressed_error.error_name,
                "semantic_summary": prepared.compressed_error.semantic_summary,
                "root_cause_cluster": prepared.compressed_error.root_cause_cluster,
                "keywords": prepared.compressed_error.keywords,
            },
            "repair_hints": [
                {
                    "title": step.title,
                    "description": step.description,
                    "target_files": step.target_files,
                }
                for step in prepared.repair_steps[:6]
            ],
            "validation_commands": validation_commands,
            "previous_validation_failure": previous_failure[-6000:],
            "files": file_context,
            "response_schema": {
                "analysis": "string",
                "summary": "string",
                "commit_message": "string",
                "validation_commands": ["string"],
                "edits": [
                    {
                        "path": "relative/path.py",
                        "operation": "rewrite|create|delete",
                        "reason": "short reason",
                        "content": "full file content for rewrite/create; empty for delete",
                    }
                ],
            },
        }
        return system_prompt, user_payload

    """
    ===== LLM 请求分发与响应处理 =====
    设计模式：策略（Strategy） —— 根据 compatibility_mode 调用不同的请求方法
    1. openai_responses：调用 OpenAI Responses API
    2. openai_compatible_chat：调用 OpenAI 兼容的 Chat Completions API
    3. google_gemini：调用 Google Gemini API

    args:
        system_prompt: prompt str
        user_payload: 用户输入的 payload dict
    returns:
        response: LLM 返回的 JSON dict
    """
    def _dispatch_request(self, system_prompt: str, user_payload: dict[str, Any]) -> dict[str, Any]:
        """根据兼容模式分发请求到对应的 API 方法。"""
        mode = self.config.compatibility_mode.strip().lower() # compatibility_mode 是配置文件中的兼容模式，用于指定使用哪个 LLM 模型。openai_responses、openai_compatible_chat、google_gemini
        if mode == "openai_responses":
            # openai_responses：input 数组 + text.format
            return self._request_openai_responses(system_prompt, user_payload)
        if mode == "openai_compatible_chat":
            # openai_compatible_chat：messages + response_format
            return self._request_openai_compatible_chat(system_prompt, user_payload)
        if mode == "google_gemini":
            # google_gemini：systemInstruction + contents + generationConfig
            return self._request_google_gemini(system_prompt, user_payload)
        raise PatchExecutorError(f"Unsupported LLM compatibility mode: {self.config.compatibility_mode}")

    def _request_openai_responses(self, system_prompt: str, user_payload: dict[str, Any]) -> dict[str, Any]:
        """调用 OpenAI Responses API 进行请求。"""
        payload = {
            "model": self.config.model,
            # input 数组包含系统消息和用户消息，系统消息用于设置模型的行为，用户消息包含实际的请求内容。
            "input": [
                {
                    "role": "system",
                    "content": [{"type": "input_text", "text": system_prompt}],
                },
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": json.dumps(user_payload, ensure_ascii=False)}],
                    # json.dumps() 方法将 Python 对象转换为 JSON 字符串，ensure_ascii=False 参数确保非 ASCII 字符（如中文）不会被转义。
                },
            ],
            "text": {"format": {"type": "json_object"}}, # json_object 格式要求模型生成的文本必须是 JSON 格式的对象
            "max_output_tokens": self.config.max_output_tokens, # max_output_tokens 参数限制模型生成的最大输出令牌数，防止输出过长。
        }
        # endpoint是拼接后的完整 URL，用于发送请求到 OpenAI Responses API。
        endpoint = self.config.base_url.rstrip("/") + "/responses" # rstrip() 方法用于移除 base_url 末尾的斜杠，确保 URL 拼接正确。
        return self._post_json(endpoint, payload, self._auth_headers())

    def _request_openai_compatible_chat(self, system_prompt: str, user_payload: dict[str, Any]) -> dict[str, Any]:
        """ 调用 OpenAI 兼容的 Chat Completions API """
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.1, # temperature 参数控制生成文本的随机性，值越低表示输出越确定，值越高表示输出越多样化。
            "max_tokens": self.config.max_output_tokens,
        }
        endpoint = self.config.base_url.rstrip("/") + "/chat/completions"
        return self._post_json(endpoint, payload, self._auth_headers())

    """
    调用 Google Gemini API
    args:
        system_prompt: prompt str
        user_payload: 用户输入的 payload dict
    returns:
        response: LLM 返回的 JSON dict
    """
    def _request_google_gemini(self, system_prompt: str, user_payload: dict[str, Any]) -> dict[str, Any]:
        if not self.config.api_key:
            raise PatchExecutorError("Google Gemini provider requires an API key.")
        query = urlencode({"key": self.config.api_key}) # urlencode() 方法将字典转换为 URL 编码的查询字符串
        endpoint = f"{self.config.base_url.rstrip('/')}/models/{self.config.model}:generateContent?{query}"
        payload = {
            "systemInstruction": {
                "parts": [{"text": system_prompt}],
            },
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": json.dumps(user_payload, ensure_ascii=False)}],
                }
            ],
            "generationConfig": {
                "responseMimeType": "application/json",
                "maxOutputTokens": self.config.max_output_tokens,
                "temperature": 0.1,
            },
        }
        headers = {"Content-Type": "application/json", **self.config.extra_headers}
        return self._post_json(endpoint, payload, headers)

    """
    ===== HTTP 请求与响应处理 =====
    1. _post_json：发送 POST 请求并解析 JSON 响应
    2. _auth_headers：生成认证请求头（Authorization + Content-Type）
    3. _extract_output_text：从 LLM 响应中提取文本输出，支持多种兼容模式
    4. _extract_openai_responses_text：专门处理 OpenAI Responses API
    5. _parse_patch_plan：解析 LLM 输出的 JSON，生成 PatchPlan 对象
    6. _usage_payload：提取 LLM 响应中的使用量信息
    """

    # args:
    #     endpoint: 完整的 API URL
    #     payload: 请求的 JSON 数据
    #     headers: 请求头（包含认证信息）
    def _post_json(self, endpoint: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        # Request()构造函数用于创建 HTTP 请求对象，包含 URL、数据、方法和请求头等信息。
        # 这里 data=json.dumps(payload).encode("utf-8") 将 JSON 数据编码为 UTF-8 字节序列，确保与 API 接口要求一致
        request = Request( 
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers=headers,
        )
        try:
            # urlopen() 方法用于发送 HTTP 请求并获取响应，timeout 参数指定请求超时时间。
            with urlopen(request, timeout=self.config.timeout_seconds) as response:
                # json.loads() 方法将响应的 JSON 字符串解析为 Python 字典对象，response.read() 读取响应内容，decode("utf-8") 将字节数据解码为字符串。
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            # error="ignore" 参数用于忽略解码错误，防止因响应内容包含非 UTF-8 字符而导致异常。
            detail = exc.read().decode("utf-8", errors="ignore")
            # raise xx from exc 语法用于在抛出新异常时保留原始异常的上下文信息，便于调试和追踪错误来源。
            raise PatchExecutorError(
                f"Provider `{self.config.provider}` returned HTTP {exc.code}: {detail}"
            ) from exc
        except URLError as exc:
            raise PatchExecutorError(f"Failed to reach provider `{self.config.provider}`: {exc}") from exc

    def _auth_headers(self) -> dict[str, str]:
        """创建认证请求头，包含 Content-Type 和 Authorization（如果有 API Key）。"""
        headers = {
            "Content-Type": "application/json",
            **self.config.extra_headers, # ** 运算符用于将 extra_headers 字典中的键值对合并到 headers 字典中
        }
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers

    def _extract_output_text(self, response: dict[str, Any]) -> str:
        """ 
        从 LLM 响应中提取文本输出，支持多种兼容模式：
        1. openai_responses：从 Responses API 的 output_text 或 output 数组
        2. openai_compatible_chat：从 Chat Completions API 的 choices[0].message.content
        3. google_gemini：从 Gemini API 的 candidates[0].content.parts
        """
        mode = self.config.compatibility_mode.strip().lower()
        if mode == "openai_responses":
            return self._extract_openai_responses_text(response)
        if mode == "openai_compatible_chat":
            choices = response.get("choices", [])
            if choices:
                message = choices[0].get("message", {})
                content = message.get("content", "")
                if isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict) and isinstance(item.get("text"), str):
                            return item["text"] # dict包括键值对，item["text"]=item.get("text") ，是获取字典中键为 "text" 的值，如果是字符串则返回。
                if isinstance(content, str) and content.strip(): #strip() 方法用于去除字符串两端的空白字符，如果 content 是非空字符串，则直接返回。
                    return content
            raise PatchExecutorError("Chat completions API returned no text output.")
        if mode == "google_gemini":
            candidates = response.get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                text_parts = [str(part.get("text", "")) for part in parts if isinstance(part, dict) and part.get("text")]
                if text_parts:
                    return "\n".join(text_parts)
            raise PatchExecutorError("Google Gemini API returned no text output.")
        raise PatchExecutorError(f"Unsupported LLM compatibility mode: {self.config.compatibility_mode}")

    def _extract_openai_responses_text(self, response: dict[str, Any]) -> str:
        """从 OpenAI Responses API 响应中提取文本输出。"""
        direct_text = response.get("output_text")
        if isinstance(direct_text, str) and direct_text.strip():
            return direct_text

        for item in response.get("output", []):
            # type 字段用于区分不同类型的输出项，只有 type 为 "message" 的项才包含文本内容。
            if item.get("type") != "message": 
                continue
            for content in item.get("content", []):
                if isinstance(content, dict):
                    text = content.get("text")
                    if isinstance(text, str) and text.strip():
                        return text
                    nested_text = content.get("output_text")
                    if isinstance(nested_text, str) and nested_text.strip():
                        return nested_text
        raise PatchExecutorError("Responses API returned no text output.")

    def _parse_patch_plan(self, raw_text: str, response: dict[str, Any]) -> PatchPlan:
        """解析成 PatchPlan 对象。
        Args:
            raw_text: 从 LLM 响应中提取的文本内容
            response: 原始 LLM 响应（包含元数据）
        Returns:
            解析后的 PatchPlan 对象
        """
        payload = self._extract_json_object(raw_text)  # 从文本中提取 JSON 对象
        if not payload:
            raise PatchExecutorError("Model returned no JSON output.")
        # 从 JSON 对象中提取补丁编辑操作
        edits: list[PatchEdit] = [] 
        for item in payload.get("edits", []): 
            # 检查每个编辑项是否为字典类型，如果不是则跳过
            if not isinstance(item, dict):
                continue
            operation_value = str(item.get("operation", "rewrite")).lower()
            # 检查操作值是否为支持的类型，如果不是则抛出异常
            if operation_value not in {member.value for member in PatchOperation}:
                raise PatchExecutorError(f"Unsupported patch operation: {operation_value}")
            # 将路径中的反斜杠替换为正斜杠，确保路径格式正确，例如 "C:\\Users\\Admin\\Desktop\\test\\test.md" 替换为 "C:/Users/Admin/Desktop/test/test.md"
            path = str(item.get("path", "")).replace("\\", "/").strip()
            self._validate_relative_path(path) # 验证路径是否为相对路径，如果不是则抛出异常
            edits.append(
                PatchEdit(
                    path=path,
                    operation=PatchOperation(operation_value),
                    content=str(item.get("content", "")),
                    reason=str(item.get("reason", "")),
                )
            )
        if not edits:
            raise PatchExecutorError("Model returned no file edits.")

        usage_payload = self._usage_payload(response)  # 从 LLM 响应中提取使用量信息
        validation_commands = payload.get("validation_commands", [])  # 从 JSON 对象中提取验证命令
        if not isinstance(validation_commands, list):
            validation_commands = []
        return PatchPlan(
            analysis=str(payload.get("analysis", "")),  # 分析结果
            summary=str(payload.get("summary", "")),  # 摘要
            commit_message=str(payload.get("commit_message", "fix: apply autonomous repository repair")).strip()
            or "fix: apply autonomous repository repair",
            validation_commands=[str(command) for command in validation_commands if str(command).strip()],
            edits=edits,
            model=str(response.get("model", self.config.model)),
            raw_response=raw_text,
            usage=usage_payload,
        )

    def _usage_payload(self, response: dict[str, Any]) -> dict[str, int]:
        """从 LLM 响应中提取使用量信息。"""
        usage = response.get("usage", {}) if isinstance(response.get("usage"), dict) else {}
        mode = self.config.compatibility_mode.strip().lower()
        if mode == "google_gemini":
            metadata = response.get("usageMetadata", {})
            usage = {
                "input_tokens": int(metadata.get("promptTokenCount", 0)),
                "output_tokens": int(metadata.get("candidatesTokenCount", 0)),
                "total_tokens": int(metadata.get("totalTokenCount", 0)),
            }
        elif mode == "openai_compatible_chat":
            usage = {
                "input_tokens": int(usage.get("prompt_tokens", 0)),
                "output_tokens": int(usage.get("completion_tokens", 0)),
                "total_tokens": int(usage.get("total_tokens", 0)),
            }
        else:
            usage = {
                "input_tokens": int(usage.get("input_tokens", 0)),
                "output_tokens": int(usage.get("output_tokens", 0)),
                "total_tokens": int(usage.get("total_tokens", 0)),
            }
        return usage


    def _extract_json_object(self, raw_text: str) -> dict[str, Any]:
        """提取 JSON 对象，返回字典。"""
        try:
            payload = json.loads(raw_text) #json.loads() 用于将 JSON 字符串转换为 Python 对象。
        except json.JSONDecodeError:
            start = raw_text.find("{") #find() 方法用于查找子字符串在字符串中的第一个出现位置，返回其索引位置。
            end = raw_text.rfind("}") #rfind() 方法用于查找子字符串在字符串中的最后一个出现位置，返回其索引位置。
            if start < 0 or end <= start:
                raise PatchExecutorError("Model did not return valid JSON.")
            payload = json.loads(raw_text[start : end + 1])
        if not isinstance(payload, dict):
            raise PatchExecutorError("Model response JSON must be an object.")
        return payload

    def _validate_relative_path(self, path: str) -> None:
        """验证相对路径是否安全。"""
        normalized = Path(path) #Path() 类用于表示文件系统路径。
        # 防止 LLM 返回恶意路径（如 /etc/passwd 或 ../../etc/passwd）, 检查路径是否绝对路径/包含 .. 组件/为空
        if normalized.is_absolute() or ".." in normalized.parts or not path:
            raise PatchExecutorError(f"Unsafe patch path returned by model: {path}")
