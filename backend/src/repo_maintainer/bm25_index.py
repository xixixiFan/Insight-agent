"""
BM25 关键词倒排索引模块（RAG 知识增强层）

基于 BM25 算法实现关键词召回，与向量语义检索互补：
- 向量检索：擅长语义相似（但可能召回语义相近但关键词不同的文档）
- BM25 检索：擅长关键词精确匹配（如特定 API 名称、错误码、函数名）
- 混合召回：两者结合后 rerank，兼顾语义和词汇精度

不依赖外部库，纯 Python 实现 BM25F 算法。
"""
# 让所有类型注解变成字符串，延迟解析。
from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
# @dataclass的作用是自动实现 __init__ 方法，无需手动编写。
# 这里使用它来定义 BM25Document 类，用于存储和管理 BM25 索引文档。
# 结合@property，可以方便地定义只读属性。
class BM25Document:
    """
    doc_id: 文档唯一标识符
    doc_type: 文档类型，knowledge_entry / bug_report / trace
    title: 文档标题
    body: 文档正文
    keywords: 文档关键词列表
    metadata: 其他元数据，如创建时间、更新时间等

    field(default_factory=list) 初始化时创建空列表，避免 None 错误。
    """
    doc_id: str
    doc_type: str           # knowledge_entry / bug_report / trace
    title: str
    body: str
    keywords: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property # @property的作用是将方法转换为只读属性，无需在实例化时调用。例如: text = doc.full_text,而不是doc.full_text()
    def full_text(self) -> str:
        """
        定义full_text属性，用于合并title、keywords、body字段用于索引
        @ Args:
            self: 当前文档实例
        @ Returns:
            str: 合并后的文档文本，包含标题、关键词和正文
        """
        parts = [self.title]
        # 把keywords列表中的每个元素逐个添加到parts列表末尾
        parts.extend(self.keywords) # extend() 会将传入的可迭代对象中的每个元素逐一添加到列表的末尾。如果传入的是非可迭代对象，则会报错。 
        if self.body:
            # 把body作为整体添加到parts列表末尾
            parts.append(self.body) # append() 会将传入的参数作为一个整体添加到列表的末尾。无论参数是单个元素还是一个可迭代对象，它都被视为一个单一元素。
        return " ".join(filter(None, parts))


@dataclass
class BM25SearchResult:
    """
    doc_id: 文档唯一标识符
    doc_type: 文档类型，knowledge_entry / bug_report / trace
    score: 文档与查询的 BM25 分数
    title: 文档标题
    metadata: 其他元数据，如创建时间、更新时间等
    matched_terms: 查询中匹配的关键词列表
    """
    doc_id: str
    doc_type: str
    score: float
    title: str
    metadata: dict[str, Any] = field(default_factory=dict) # 字典
    matched_terms: list[str] = field(default_factory=list) # 列表


class BM25Index:
    """
    BM25 关键词倒排索引
    实现 BM25+ 算法（BM25 的改进版本，避免词频权重为 0 的问题）：
    公式：score(q, d) = Σ IDF(t) * (tf(t,d) * (k1+1)) / (tf(t,d) + k1*(1-b+b*|d|/avgdl)) + δ
    参数：
    - IDF(t)：逆文档频率，词越稀有值越大；
    - k1: 控制词频饱和度（通常 1.2-2.0），词频权重，防止过拟合。
    - b: 控制文档长度归一化（0-1），用于调整长文档对查询的影响。
    - delta: BM25+ 改进项，保证即使低频词也有基础贡献。
    - avgdl: 平均文档长度，用于归一化文档长度。
    """

    # bm25_documents 存储文档原文（title/body/keywords/metadata/created_at）
    _CREATE_TABLE = """
    CREATE TABLE IF NOT EXISTS bm25_documents (
        doc_id TEXT NOT NULL,
        doc_type TEXT NOT NULL,
        title TEXT NOT NULL DEFAULT '',
        body TEXT NOT NULL DEFAULT '',
        keywords TEXT NOT NULL DEFAULT '[]',
        metadata TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        PRIMARY KEY (doc_id, doc_type)
    )
    """
    # bm25_inverted_index 倒排表 
    _CREATE_INDEX_TABLE = """
    CREATE TABLE IF NOT EXISTS bm25_inverted_index (
        term TEXT NOT NULL, 
        doc_id TEXT NOT NULL,
        doc_type TEXT NOT NULL,
        tf INTEGER NOT NULL DEFAULT 1,
        doc_len INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (term, doc_id, doc_type)
    )
    """
    # bm25_stats 缓存key-value对，用于存储文档总数n_docs和平均文档长度avg_doc_len，避免每次搜索都全表聚合
    _CREATE_STATS_TABLE = """
    CREATE TABLE IF NOT EXISTS bm25_stats (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """

    def __init__(
        self,
        db_path: str | Path,
        k1: float = 1.5, 
        b: float = 0.75,
        delta: float = 0.5,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.k1 = k1
        self.b = b
        self.delta = delta
        self._local = threading.local() # threading.local()会创建一个“线程隔离的数据容器”。SQLite 连接不是线程安全的，每个线程要独立连接。这就是 Python 里常见的"线程本地连接池"模式。
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        """获取线程本地的数据库连接"""
        conn = getattr(self._local, "conn", None) # getattr() 会返回指定属性的值，如果属性不存在，则返回 None。
        if conn is None:
            conn = sqlite3.connect(str(self.db_path), check_same_thread=False) # check_same_thread=False 会允许在多个线程中使用同一个连接。
            conn.row_factory = sqlite3.Row # sqlite3.Row是一个类，它创建的实例可以像字典一样使用。例如，conn.execute("SELECT 1").fetchone() 会返回一个sqlite3.Row类的实例，用于表示数据库中的行数据。
            conn.execute("SELECT 1") # 执行一个简单的查询，确保连接成功。
            self._local.conn = conn # 把连接绑定到当前线程名下
        return conn

    def _init_db(self) -> None:
        """
        初始化数据库
        1、创建数据库表（bm25_documents, bm25_inverted_index, bm25_stats）
        2、创建索引（idx_bm25_term, idx_bm25_doc）
        """
        with self._conn() as conn:
            conn.execute(self._CREATE_TABLE)
            conn.execute(self._CREATE_INDEX_TABLE)
            conn.execute(self._CREATE_STATS_TABLE)
            # 1、根据 bm25_inverted_index.term 字段创建索引，用于快速查询包含该词的文档。
            # 2、根据 bm25_inverted_index.doc_id, bm25_inverted_index.doc_type 字段创建索引，用于快速查询该文档的所有词频。
            conn.execute("CREATE INDEX IF NOT EXISTS idx_bm25_term ON bm25_inverted_index (term)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_bm25_doc ON bm25_inverted_index (doc_id, doc_type)")

    def index_document(self, doc: BM25Document) -> None:
        """倒排索引单个文档"""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()

        with self._conn() as conn:
            # 先删除旧索引，使用占位符 ? 防止 SQL注入。
            conn.execute(
                "DELETE FROM bm25_inverted_index WHERE doc_id = ? AND doc_type = ?",
                (doc.doc_id, doc.doc_type),
            )
            conn.execute(
                "INSERT OR REPLACE INTO bm25_documents VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    doc.doc_id,
                    doc.doc_type,
                    doc.title,
                    doc.body,
                    json.dumps(doc.keywords, ensure_ascii=False),
                    json.dumps(doc.metadata, ensure_ascii=False),
                    now,
                ),
            )

            # 给文档全文分词并建立倒排索引
            tokens = self._tokenize(doc.full_text)
            doc_len = len(tokens)
            term_freq: dict[str, int] = {} 
            # 统计每个词的词频（出现次数）
            for token in tokens:
                term_freq[token] = term_freq.get(token, 0) + 1
            for term, tf in term_freq.items(): #items方法返回一个迭代器，每个元素是一个二组，每个元组包含键（term）和对应的值（tf）。   
                # 每个元组的键是词，值是词频。
                conn.execute(
                    "INSERT OR REPLACE INTO bm25_inverted_index VALUES (?, ?, ?, ?, ?)",
                    (term, doc.doc_id, doc.doc_type, tf, doc_len),
                )

        # 更新统计
        self._update_stats()

    def index_batch(self, docs: list[BM25Document]) -> None:
        """批量倒排索引文档"""
        for doc in docs:
            self.index_document(doc)

    def remove_document(self, doc_id: str, doc_type: str) -> None:
        # 删除文档：1.从 bm25_documents 表删除文档；2.从 bm25_inverted_index 表删除该文档的所有索引。
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM bm25_documents WHERE doc_id = ? AND doc_type = ?",
                (doc_id, doc_type),
            )
            conn.execute(
                "DELETE FROM bm25_inverted_index WHERE doc_id = ? AND doc_type = ?",
                (doc_id, doc_type),
            )
        self._update_stats()

    def search(
        self,
        query: str,
        doc_types: list[str] | None = None,
        limit: int = 10,
    ) -> list[BM25SearchResult]:
        """
        BM25 关键词搜索
        Args:
            query: 搜索查询
            doc_types: 过滤文档类型（None=全部）
            limit: 返回结果数量
        Returns:
            按 BM25 分数排序的结果列表
        """
        query_terms = self._tokenize(query)
        if not query_terms:
            return []

        # 获取统计信息
        stats = self._get_stats()
        n_docs = stats.get("n_docs", 0)
        avg_doc_len = stats.get("avg_doc_len", 100.0)
        if n_docs == 0:
            return []

        # 计算每个文档的 BM25 分数
        doc_scores: dict[tuple[str, str], float] = {} # doc_scores是文档BM25分数，tuple是元组，用于存储文档ID和文档类型，BM25分数为值
        doc_matched_terms: dict[tuple[str, str], list[str]] = {} # doc_matched_terms是文档匹配词，tuple是元组，用于存储文档ID和文档类型，匹配的词为值

        for term in set(query_terms):  # 去重 term
            # 获取包含该 term 的文档列表
            rows = self._get_term_docs(term, doc_types)
            df = len(rows)  # 文档频率
            if df == 0:
                continue

            # IDF（BM25+）
            idf = math.log((n_docs - df + 0.5) / (df + 0.5) + 1)

            for row in rows:
                tf = row["tf"]
                doc_len = row["doc_len"]
                doc_key = (row["doc_id"], row["doc_type"])

                # BM25+ 分数
                tf_normalized = (tf * (self.k1 + 1)) / (
                    tf + self.k1 * (1 - self.b + self.b * doc_len / max(avg_doc_len, 1))
                ) + self.delta

                score = idf * tf_normalized
                doc_scores[doc_key] = doc_scores.get(doc_key, 0.0) + score

                if doc_key not in doc_matched_terms:
                    doc_matched_terms[doc_key] = []
                if term not in doc_matched_terms[doc_key]:
                    doc_matched_terms[doc_key].append(term)

        if not doc_scores:
            return []

        # 按分数排序，取 top-K
        sorted_docs = sorted(doc_scores.items(), key=lambda x: x[1], reverse=True)[:limit]

        # 获取文档元数据
        results: list[BM25SearchResult] = []
        for (doc_id, doc_type), score in sorted_docs:
            doc_row = self._get_document(doc_id, doc_type)
            if doc_row is None:
                continue
            try:
                metadata = json.loads(doc_row["metadata"]) if doc_row["metadata"] else {}
            except Exception:  # noqa: BLE001
                metadata = {}
            results.append(BM25SearchResult(
                doc_id=doc_id,
                doc_type=doc_type,
                score=round(score, 4),
                title=doc_row["title"],
                metadata=metadata,
                matched_terms=doc_matched_terms.get((doc_id, doc_type), []),
            ))

        return results

    def get_document_count(self) -> int:
        """获取索引文档总数"""
        row = self._conn().execute("SELECT COUNT(*) as cnt FROM bm25_documents").fetchone()
        return int(row["cnt"]) if row else 0

    def _tokenize(self, text: str) -> list[str]:
        """
        分词（中英文混合）

        中文：按字/词分割（简单 2-gram）
        英文：空格分割 + 小写化 + 去停用词
        """
        if not text:
            return []

        tokens: list[str] = []

        # 英文分词（支持驼峰拆分）
        english_text = re.sub(r"[^\u0000-\u007F]+", " ", text)
        # 驼峰拆分
        english_text = re.sub(r"([a-z])([A-Z])", r"\1 \2", english_text)
        english_words = re.findall(r"[a-zA-Z][a-zA-Z0-9]*", english_text)
        english_tokens = [w.lower() for w in english_words if len(w) >= 2]
        english_tokens = [w for w in english_tokens if w not in _ENGLISH_STOPWORDS]
        tokens.extend(english_tokens)

        # 中文分词（bigram）
        chinese_chars = re.findall(r"[\u4e00-\u9fff]", text)
        if len(chinese_chars) >= 2:
            for i in range(len(chinese_chars) - 1):
                tokens.append(chinese_chars[i] + chinese_chars[i + 1])
        elif chinese_chars:
            tokens.extend(chinese_chars)

        # 数字（版本号等）
        numbers = re.findall(r"\d+(?:\.\d+)+", text)
        tokens.extend(numbers)

        return tokens

    def _get_term_docs(
        self,
        term: str,
        doc_types: list[str] | None,
    ) -> list[sqlite3.Row]:
        """获取包含该 term 的文档列表"""
        if doc_types:
            placeholders = ",".join("?" for _ in doc_types)
            return self._conn().execute(
                f"SELECT * FROM bm25_inverted_index WHERE term = ? AND doc_type IN ({placeholders})",
                [term, *doc_types],
            ).fetchall()
        return self._conn().execute(
            "SELECT * FROM bm25_inverted_index WHERE term = ?",
            (term,),
        ).fetchall()

    def _get_document(self, doc_id: str, doc_type: str) -> sqlite3.Row | None:
        return self._conn().execute(
            "SELECT * FROM bm25_documents WHERE doc_id = ? AND doc_type = ?",
            (doc_id, doc_type),
        ).fetchone()

    def _update_stats(self) -> None:
        """更新索引统计信息"""
        row = self._conn().execute(
            "SELECT COUNT(*) as n_docs, AVG(doc_len) as avg_len "
            "FROM (SELECT DISTINCT doc_id, doc_type, doc_len FROM bm25_inverted_index)"
        ).fetchone()
        n_docs = int(row["n_docs"]) if row and row["n_docs"] else 0
        avg_len = float(row["avg_len"]) if row and row["avg_len"] else 0.0

        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO bm25_stats VALUES ('n_docs', ?)",
                (str(n_docs),),
            )
            conn.execute(
                "INSERT OR REPLACE INTO bm25_stats VALUES ('avg_doc_len', ?)",
                (str(avg_len),),
            )

    def _get_stats(self) -> dict[str, float]:
        """获取索引统计信息(文档总数n_docs和平均文档长度avg_doc_len)并转为字典返回"""
        rows = self._conn().execute("SELECT key, value FROM bm25_stats").fetchall() # fetchall()方法返回所有行，每个行是一个元组，包含键和值
        stats: dict[str, float] = {}
        for key, value in rows:
            try:
                stats[key] = float(value)
            except (ValueError, TypeError):
                pass
        return stats


# 英文停用词（轻量版）
_ENGLISH_STOPWORDS = frozenset([
    "the", "a", "an", "in", "on", "at", "to", "for", "of", "and", "or",
    "but", "is", "are", "was", "were", "be", "been", "being", "have",
    "has", "had", "do", "does", "did", "will", "would", "could", "should",
    "may", "might", "it", "its", "this", "that", "these", "those", "with",
    "from", "by", "as", "not", "no", "so", "if", "then", "when", "where",
    "how", "what", "which", "who", "all", "each", "some", "any", "more",
    "one", "two", "can", "get", "use", "used", "new", "also", "than",
])


def create_knowledge_entry_document(
    entry_id: str,
    name: str,
    description: str,
    keywords: list[str],
    triggers: list[str],
    action_template: str = "",
    applicable_context: str = "",
) -> BM25Document:
    """便捷函数：将知识条目转为 BM25Document"""
    all_keywords = list(dict.fromkeys(keywords + triggers))
    body = "\n".join(filter(None, [description, action_template, applicable_context]))
    return BM25Document(
        doc_id=entry_id,
        doc_type="knowledge_entry",
        title=name,
        body=body,
        keywords=all_keywords,
    )


def create_bug_report_document(
    report_id: str,
    title: str,
    description: str,
    keywords: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> BM25Document:
    """便捷函数：将 Bug 报告转为 BM25Document"""
    return BM25Document(
        doc_id=report_id,
        doc_type="bug_report",
        title=title,
        body=description,
        keywords=keywords or [],
        metadata=metadata or {},
    )
