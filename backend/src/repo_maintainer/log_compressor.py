"""
日志智能裁剪模块（LLM 抽取层）

对超长日志进行智能压缩，保留关键错误信息，去除重复/噪声行，
为后续 LLM 抽取节省 Token。

压缩策略：
1. 优先保留错误类行（ERROR/FATAL/Exception/Traceback）
2. 保留时间窗口（错误前后 N 行）
3. 去重相似行（重复日志折叠）
4. 截断过长行
5. 预算控制（不超过 max_chars）
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from collections import deque


@dataclass
class LogLine:
    """
    单条日志行
    index 日志行索引
    raw 原始日志行文本
    level 日志级别
    is_error 是否为错误类行
    is_traceback 是否为 Traceback 行
    is_stackframe 是否为堆栈帧行
    is_noise 是否为高频重复/无关行
    importance 日志行重要性（默认 0.5）
    """
    index: int
    raw: str
    level: str = "INFO"         # DEBUG/INFO/WARN/ERROR/FATAL
    is_error: bool = False
    is_traceback: bool = False
    is_stackframe: bool = False
    is_noise: bool = False      # 高频重复/无关行
    importance: float = 0.5


@dataclass
class CompressedLog:
    """
    压缩后的日志结果
    text 压缩后的日志文本
    original_line_count 原始日志行数
    retained_line_count 保留的日志行数
    compression_ratio 压缩比
    error_line_count 错误类行数
    truncated 是否截断
    metadata 元数据（可选）
    """
    text: str
    original_line_count: int
    retained_line_count: int
    compression_ratio: float
    error_line_count: int
    truncated: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


class LogCompressor:
    """
    日志智能裁剪器
    将原始日志压缩为 Bug 复盘所需的关键信息，
    适合传入 LLM 进行结构化抽取。
    """

    # 日志级别正则
    _LEVEL_PATTERN = re.compile(
        r"\b(DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL|FATAL|EXCEPTION)\b",
        re.IGNORECASE,
    )

    # Traceback 头部正则
    _TRACEBACK_HEAD = re.compile(
        r"^(Traceback \(most recent call last\)|Exception:|Error:|FATAL:|CRITICAL:)",
        re.IGNORECASE,
    )

    # Python 堆栈帧正则
    _STACKFRAME = re.compile(
        r'^\s+File "(.+)", line (\d+), in (.+)'
    )

    # 时间戳正则（常见格式）
    _TIMESTAMP = re.compile(
        r"^\d{4}[-/]\d{2}[-/]\d{2}[T ]\d{2}:\d{2}:\d{2}"
    )

    # 噪声行正则（可过滤）
    _NOISE_PATTERNS = [
        re.compile(r"^\s*$"),                           # 空行
        re.compile(r"^\s*#"),                           # 注释行
        re.compile(r"INFO.*health.*check", re.I),       # 健康检查
        re.compile(r"GET /health.*200", re.I),          # HTTP 健康探针
        re.compile(r"INFO.*heartbeat", re.I),           # 心跳日志
        re.compile(r"DEBUG.*polling", re.I),            # 轮询日志
    ]

    def __init__(
        self,
        max_chars: int = 8000,
        context_window: int = 5,        # 错误行前后保留 N 行
        max_similar_lines: int = 3,     # 相似行最多保留 N 条
        min_line_importance: float = 0.3,
    ) -> None:
        self.max_chars = max_chars
        self.context_window = context_window
        self.max_similar_lines = max_similar_lines
        self.min_line_importance = min_line_importance

    def compress(self, raw_log: str) -> CompressedLog:
        """
        压缩日志：·
        1. 解析每行
        2. 标记噪声行
        3. 标记重要行（错误上下文）
        4. 去重相似行
        5. 排序并截断
        6. 预算控制（不超过 max_chars）
        7. 返回压缩后的日志文本
        Args:
            raw_log: 原始日志文本
        Returns:
            CompressedLog 包含压缩后的文本和统计信息
        """
        if not raw_log or not raw_log.strip():
            return CompressedLog(
                text="",
                original_line_count=0,
                retained_line_count=0,
                compression_ratio=1.0,
                error_line_count=0,
            )

        # 0. 按行分隔日志行
        lines = raw_log.splitlines() # splitlines方法按照行('\r', '\r\n', \n')分隔，返回一个包含各行作为元素的列表
        original_count = len(lines)
       
        # 1. 每行日志被解析成 LogLine 对象:level, is_error, is_traceback, is_stackframe, is_noise, importance, raw
        # enumerate()函数用于同时获取索引和元素
        parsed = [self._parse_line(i, line) for i, line in enumerate(lines)]
        
        # 2. 标记噪声行（判断并设置is_noise属性）
        parsed = self._mark_noise(parsed)

        # 3. 标记重要行，保留前后 context_window 行（条件：is_error、is_traceback、importance >= 0.8）
        important_indices = self._find_important_indices(parsed)

        # 4. 去重相似行
        retained = self._deduplicate(parsed, important_indices)

        # 5. 提取保留行的原始文本，按预算截断
        retained_lines = [p.raw for p in retained]
        error_count = sum(1 for p in parsed if p.is_error)

        # 6. 预算控制（不超过 max_chars）
        # 如果截断，添加截断提示（... [已截断 N 行] ...）
        # 其他情况直接返回保留行的原始文本
        text, truncated = self._apply_budget(retained_lines)

        retained_count = len(retained_lines)
        compression_ratio = retained_count / max(original_count, 1) # 压缩比 = 保留行数 / 原始行数

        return CompressedLog(
            text=text, # 压缩后的日志文本
            original_line_count=original_count, # 原始日志行数
            retained_line_count=retained_count, # 保留行数
            compression_ratio=round(compression_ratio, 3), # 压缩比
            error_line_count=error_count, # 错误行数
            truncated=truncated, # 是否截断标志
            # 其他元数据
            metadata={
                "max_chars": self.max_chars,
                "context_window": self.context_window,
            },
        )

    def extract_error_summary(self, raw_log: str) -> dict[str, Any]:
        """
        从日志中抽取错误摘要（不依赖 LLM）
        Args:
            raw_log: 原始日志文本
        Returns:
            包含 error_type、error_message、error_line_count、traceback_frames、has_traceback 的字典
            error_type: 错误类型（ExceptionClass）
            error_message: 错误消息（最多 200 个字符）
            error_line_count: 错误行数
            traceback_frames: 堆栈帧列表，每个元素为 {"file": ..., "line": ..., "function": ...}
            has_traceback: 是否有 Traceback 行
        """  
        lines = raw_log.splitlines() # 按行分隔日志行
       
        parsed = [self._parse_line(i, line) for i, line in enumerate(lines)] # 解析每行日志

        error_lines = [p for p in parsed if p.is_error or p.is_traceback] # 收集错误行和 Traceback 行
        traceback_frames = [p for p in parsed if p.is_stackframe] # 收集堆栈帧行

        # 提取错误类型
        error_type = "unknown"
        error_message = ""
        for p in error_lines:
            # Python 异常格式：ExceptionClass: message
            # p.raw 是原始日志行文本，包含异常类型和消息，例如：
            # Exception: An error occurred
            match = re.search(r"(\w+(?:Error|Exception|Warning|Failure))\s*:\s*(.+)", p.raw)
            if match:
                error_type = match.group(1) # 提取异常类型
                error_message = match.group(2)[:200] # 提取异常消息，最多 200 个字符
                break

        # 提取关键堆栈帧
        key_frames = []
        for p in traceback_frames[:10]: # 只取前 10 行
            # p.raw 是原始日志行文本，包含堆栈帧信息,例如：
            # File "/path/to/file.py", line 1456, in function_name
            #   raise Exception("An error occurred")
            # self._STACKFRAME 是预编译的正则表达式，用于匹配堆栈帧格式
            frame_match = self._STACKFRAME.match(p.raw)
            if frame_match:
                key_frames.append({
                    "file": frame_match.group(1), # 提取文件名
                    "line": int(frame_match.group(2)), # 提取行号
                    "function": frame_match.group(3), # 提取函数名
                })

        return {
            "error_type": error_type,
            "error_message": error_message,
            "error_line_count": len(error_lines), # 错误行数
            "traceback_frames": key_frames[:5], # 只取前 5 行堆栈帧信息
            "has_traceback": len(traceback_frames) > 0,
        }

    def _parse_line(self, index: int, raw: str) -> LogLine:
        """
        每行日志被解析成 LogLine，带 5 个标记：
        - level：DEBUG / INFO / WARN / ERROR / FATAL；
        - is_error: 是否为错误日志
        - is_traceback: 是否为 Traceback 行
        - is_stackframe: 是否为堆栈帧行
        - importance: 日志重要性分数，用于压缩
        Args:
            index: 日志行索引
            raw: 原始日志行文本
        Returns:
            LogLine 包含解析后的日志行信息
        """
        line = LogLine(index=index, raw=raw) # 创建日志行对象

        # 预编译正则表达式检测日志级别
        level_match = self._LEVEL_PATTERN.search(raw) 
        if level_match:
            line.level = level_match.group(0).upper() # 将匹配到的日志级别转换为大写作为日志级别
            line.is_error = line.level in ("ERROR", "CRITICAL", "FATAL", "EXCEPTION")
        # 预编译正则表达式检测 Traceback 行头
        if self._TRACEBACK_HEAD.match(raw.strip()):
            line.is_traceback = True
            line.is_error = True
        # 预编译正则表达式检测是否为堆栈帧行
        if self._STACKFRAME.match(raw):
            line.is_stackframe = True

        # 计算重要性分数
        importance = 0.3
        if line.is_error:
            importance = 0.95
        elif line.is_traceback:
            importance = 0.9
        elif line.is_stackframe:
            importance = 0.75
        elif level_match and line.level == "WARN":
            importance = 0.5
        elif "Exception" in raw or "Error" in raw:
            importance = 0.7

        line.importance = importance
        return line

    def _mark_noise(self, parsed: list[LogLine]) -> list[LogLine]:
        """
        标记噪声行（不包含错误日志、Traceback 行、堆栈帧行）
        Args:
            parsed: 解析后的日志行列表
        Returns:
            包含噪声标记的日志行列表
        """
        for p in parsed:
            # 判断是否为噪声行：包含空行、注释行、健康检查、HTTP健康探针、心跳日志、轮询日志
            if any(pattern.search(p.raw) for pattern in self._NOISE_PATTERNS):
                p.is_noise = True
        return parsed

    def _find_important_indices(self, parsed: list[LogLine]) -> set[int]:
        """找到需要保留的行索引（错误行 + 上下文）"""
        important: set[int] = set()
        n = len(parsed)

        for p in parsed:
            # 判断条件：
            # 1. 错误日志、Traceback 行、堆栈帧行
            # 2. 重要性分数 >= 0.8 的行
            # 3. 重要性分数 >= 0.8 的行
            if p.is_error or p.is_traceback or p.importance >= 0.8:
                # 保留前后 context_window 行
                start = max(0, p.index - self.context_window) 
                end = min(n, p.index + self.context_window + 1)
                for i in range(start, end):
                    important.add(i)

        return important

    def _deduplicate(
        self,
        parsed: list[LogLine],
        important_indices: set[int],
    ) -> list[LogLine]:
        """
        去重相似行（按行内容前缀分组）
        重要行（错误上下文）始终保留，
        噪声行跳过，不进行去重。
        其他行按前 60 字符分组去重，最多保留 max_similar_lines 个字符分组。
        超过 max_similar_lines 个字符分组的相似行，添加折叠提示。
        Args:
            parsed: 解析后的日志行列表
            important_indices: 重要行索引集合
        Returns:
            包含去重后的日志行列表
        """
        retained: list[LogLine] = [] # 保留的日志行列表
        prefix_count: dict[str, int] = {} # 前缀计数字典，记录每个前缀的出现次数

        for p in parsed:
            # 重要行始终保留
            if p.index in important_indices:
                retained.append(p)
                continue

            # 跳过噪声行
            if p.is_noise:
                continue

            # 按前 60 字符分组去重，并记录出现次数
            prefix = p.raw.strip()[:60]
            count = prefix_count.get(prefix, 0)

            if count < self.max_similar_lines:
                retained.append(p)
                prefix_count[prefix] = count + 1
            elif count == self.max_similar_lines:
                # 添加折叠提示
                retained.append(LogLine(
                    index=p.index,
                    raw=f"... [相似行已折叠]",
                    importance=0.1,
                ))
                prefix_count[prefix] = count + 1

        return retained

    def _apply_budget(self, lines: list[str]) -> tuple[str, bool]:
        """
        应用 Token 预算，截断超长日志
        Args:
            lines: 原始日志行列表
        Returns:
            包含截断后的日志行文本和是否截断标志的元组
            str: 截断后的日志行文本
            bool: 是否截断标志
        """
        truncated = False  # 是否截断标志
        result_lines: list[str] = []  # 截断后的日志行列表
        total_chars = 0 # 总字符数，包括换行符

        # 优先保留开头（可能有配置信息）和结尾（最新日志）
        head_chars = self.max_chars // 4
        tail_chars = self.max_chars * 3 // 4

        head_lines: list[str] = []
        # deque是一种双端队列，支持在两端高效插入和删除元素
        tail_lines: deque[str] = deque()

        # 从头部收集：
        # 如果添加当前行后，总字符数不超过 head_chars，
        # 则添加当前行到 head_lines，更新总字符数，继续遍历下一行
        # 否则，跳出循环
        for line in lines:
            if total_chars + len(line) + 1 <= head_chars:
                head_lines.append(line)
                total_chars += len(line) + 1
            else:
                break
        
        # 从尾部收集：
        # 把除 head_lines 外的行，从后往前遍历
        # 如果添加当前行后，总字符数不超过 tail_chars，
        # 则添加当前行到 tail_lines，更新总字符数，继续遍历上一行
        # 否则，截断标志设为 True，跳出循环
        remaining_budget = self.max_chars - total_chars
        tail_chars_used = 0
        for line in reversed(lines[len(head_lines):]):
            if tail_chars_used + len(line) + 1 <= remaining_budget:
                tail_lines.appendleft(line) # 从后往前添加，保持最新行在最后
                tail_chars_used += len(line) + 1
            else:
                truncated = True
                break

        result_lines = head_lines 
        if truncated:
            result_lines.append(
                f"... [已截断 {len(lines) - len(head_lines) - len(tail_lines)} 行] ..."
            )
        result_lines.extend(tail_lines)

        return "\n".join(result_lines), truncated

    def get_stats(self, raw_log: str) -> dict[str, Any]:
        """获取日志统计信息（不压缩）"""
        lines = raw_log.splitlines()
        parsed = [self._parse_line(i, line) for i, line in enumerate(lines)]
        return {
            "total_lines": len(lines),
            "error_lines": sum(1 for p in parsed if p.is_error),
            "traceback_lines": sum(1 for p in parsed if p.is_traceback),
            "stackframe_lines": sum(1 for p in parsed if p.is_stackframe),
            "total_chars": len(raw_log),
            "needs_compression": len(raw_log) > self.max_chars,
        }


def compress_log_for_llm(
    raw_log: str,
    max_chars: int = 6000,
) -> str:
    """
    便捷函数：压缩日志供 LLM 使用
    Args:
        raw_log: 原始日志文本
        max_chars: 最大字符数，默认 6000
    Returns:
        压缩后的日志文本
    """
    compressor = LogCompressor(max_chars=max_chars)
    result = compressor.compress(raw_log)
    return result.text


def extract_key_frames(raw_log: str) -> list[dict[str, Any]]:
    """
    便捷函数：提取关键堆栈帧
    Returns:
        堆栈帧列表 [{"file": ..., "line": ..., "function": ...}]
    """
    compressor = LogCompressor() # 创建日志压缩器实例
    summary = compressor.extract_error_summary(raw_log) # 提取错误摘要
    return summary.get("traceback_frames", []) # 返回堆栈帧信息
