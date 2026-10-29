"""
幂等键管理模块（任务编排层）

确保相同的 PR/Commit 不会被重复处理，避免：
1. Webhook 重放导致的重复抽取
2. 网络抖动导致的重复提交
3. 并发触发导致的重复执行

幂等键格式：{repo}:{pr_id}:{sha}
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 存储层：SQLite读写、建表、索引、清理
class IdempotencyStore:
    """
    幂等键存储（SQLite 实现）

    提供幂等键的注册、查询和过期清理功能。

    表格字段：
    - key: 幂等键（唯一标识）
    - trace_id: 关联的 trace_id（可选）
    - status: 处理状态（processing/completed/failed）
    - payload: 关联的上下文（JSON 字符串）
    - created_at: 创建时间（UTC ISO 格式）
    - expires_at: 过期时间（UTC ISO 格式，可选），支持 TTL 机制
    TTL机制：在创建幂等键时，可以指定一个过期时间（TTL，Time To Live），当幂等键超过这个时间后，会被认为是过期的，可以重新处理。这样可以避免长时间占用资源，同时也允许在一定时间内重复处理相同的请求。
    - completed_at: 完成时间（UTC ISO 格式，可选）
    """

    _CREATE_TABLE = """
    CREATE TABLE IF NOT EXISTS idempotency_keys (
        key TEXT PRIMARY KEY,
        trace_id TEXT,
        status TEXT NOT NULL DEFAULT 'processing',
        payload TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        expires_at TEXT,
        completed_at TEXT
    )
    """

    def __init__(self, db_path: str | Path) -> None:
        """ 
        args:
            db_path: SQLite 数据库文件路径
        """
        self.db_path = Path(db_path) #Path方法的作用是将传入的db_path参数转换为Path对象，以便后续进行路径操作和文件系统操作。Path对象提供了更方便的方式来处理文件路径，而不仅仅是字符串。
        # parents=True：如果父目录不存在，则创建所有必要的父目录。
        # exist_ok=True：如果目录已经存在，则不会引发异常。
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # threading.local() 创建一个线程本地存储对象，每个线程都有自己的独立副本，避免多线程环境下的共享数据冲突。
        # SQLite不能跨线程共享，每个线程都需要独立的连接对象，因此使用线程本地存储来管理每个线程的数据库连接。
        self._local = threading.local() 
        self._init_db() 

    def _conn(self) -> sqlite3.Connection:
        """获取当前线程的 SQLite 连接"""
        conn = getattr(self._local, "conn", None)
        # getattr() 函数用于获取对象的属性值，如果属性不存在，则返回默认值 None。
        if conn is None:
            # 如果conn为None，说明当前线程还没有创建数据库连接，则创建一个新的连接，并将其存储在线程本地存储对象中，以便后续使用。
            # check_same_thread=False 参数允许在不同线程中使用同一个连接对象，避免了 SQLite 默认的线程限制。
            conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            # sqlite3.Row 是一个特殊的行工厂，作用：让查询结果可以按"列名"访问，而不是只能按索引
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    def _init_db(self) -> None:
        """ 
        with=上下文管理器，自动管理资源
        对于 SQLite 连接：
            -进入 with 时：拿到连接
            -退出 with 时：自动提交或回滚事务
        !关键：with 对 SQLite 连接是事务管理，对文件是关闭。不同对象行为不同
        """
        with self._conn() as conn:
            conn.execute(self._CREATE_TABLE) #执行SQL语句，创建幂等键表格
            """ 建立索引：
              - idx_idempotency_status → 加速 WHERE status = ?
              - idx_idempotency_expires→ 加速 WHERE expires_at < ?（清理时用）
            """
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_idempotency_status ON idempotency_keys (status)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_idempotency_expires ON idempotency_keys (expires_at)"
            )

    @staticmethod
    # @staticmethod 装饰器用于将类中的方法定义为静态方法，静态方法不依赖于类的实例，可以直接通过类名调用，而不需要创建对象实例。静态方法通常用于与类相关的功能，但不需要访问类的属性或实例的状态。
    # 与@classmethod 不同，静态方法不接收类本身作为第一个参数（通常命名为 cls），而是像普通函数一样定义。它们通常用于实现与类相关的工具函数或辅助功能，而不需要访问类的实例或类属性。
    def make_key(repo: str, pr_id: str, sha: str) -> str:
        """
        构造幂等键
        args：
            -repo：不同仓库 PR 号可能相同；
            -pr_id：同仓库不同 PR；
            -sha：同 PR 多次 commit。
        三者组合唯一标识一次“待处理事件”
        格式：{repo}:{pr_id}:{sha}
        示例：octocat/hello-world:42:abc1234
        """
        raw = f"{repo}:{pr_id}:{sha}"
        return raw

    @staticmethod
    def make_key_hash(repo: str, pr_id: str, sha: str) -> str:
        """
        构造哈希幂等键（用于长字符串场景）
       
        返回 SHA1 哈希前缀 + 可读前缀
        """
        raw = f"{repo}:{pr_id}:{sha}"
        """ 
        作用： 把长字符串（repo + pr_id + sha）压缩成固定长度的短标识。
          -raw.encode("utf-8") 字符串 → 字节（哈希只能处理字节）
          -hashlib.sha1() 函数用于创建一个 SHA-1 哈希对象，它可以对输入数据进行哈希计算，生成一个固定长度的哈希值。SHA-1 是一种常用的哈希算法，输出为 160 位（20 字节）的哈希值。
          -hexdigest() 转成十六进制字符串
        """
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
        # 保留可读前缀用于调试
        repo_slug = repo.replace("/", "-")[:20]
        return f"{repo_slug}:{pr_id[:8]}:{digest}"

    def try_acquire(
        self,
        key: str,
        trace_id: str | None = None,
        ttl_hours: int = 24,
        payload: dict[str, Any] | None = None,
    ) -> tuple[bool, dict[str, Any] | None]:
        """
        尝试 INSERT
        ├── 成功 → 返回 (True, None)  ← 首次，拿到锁
        └── 主键冲突 → 查询现有记录
            ├── 已过期 → 删除，递归重试  ← TTL 机制
            └── 未过期 → 返回 (False, existing)  ← 已存在
        tuple表示返回值是一个元组，包含两个元素:布尔值和字典或None。
        Returns:
            (acquired, existing_record)
            - (True, None)：首次处理，成功获取锁
            - (False, record)：已有记录，返回现有处理状态

        不需要显式加锁，数据库主键冲突本身就是原子的，没有竟态
        """
        now = _utc_now()
        expires_at = _utc_add_hours(now, ttl_hours)
        # json.dumps() 函数用于将 Python 对象（如字典、列表等）转换为 JSON 字符串。ensure_ascii=False 参数用于确保生成的 JSON 字符串中可以包含非 ASCII 字符（例如中文），而不是将其转义为 Unicode 转义序列。
        payload_str = json.dumps(payload or {}, ensure_ascii=False)

        try:
            with self._conn() as conn:
                conn.execute(
                    """
                    INSERT INTO idempotency_keys (key, trace_id, status, payload, created_at, expires_at)
                    VALUES (?, ?, 'processing', ?, ?, ?)
                    """,
                    (key, trace_id, payload_str, now, expires_at),
                )
            return True, None
        # sqlite3.IntegrityError 异常表示在尝试插入数据时违反了数据库的完整性约束（例如主键冲突）。在这种情况下，说明幂等键已经存在，因此需要查询现有记录。
        except sqlite3.IntegrityError:
            # 键已存在，查询现有记录
            existing = self._get(key)
            if existing:
                # 检查是否已过期（过期的可以重新处理）
                exp = existing.get("expires_at")
                if exp and exp < now:
                    # 已过期，删除并重新创建
                    with self._conn() as conn:
                        conn.execute("DELETE FROM idempotency_keys WHERE key = ?", (key,))
                    return self.try_acquire(key, trace_id, ttl_hours, payload)
            return False, existing

    def complete(self, key: str, trace_id: str, success: bool = True) -> None:
        """标记幂等键处理完成"""
        now = _utc_now()
        # 如果 success 为 True，则 status 为 "completed"，否则为 "failed"。这个状态将用于表示幂等键的处理结果。
        status = "completed" if success else "failed"
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE idempotency_keys
                SET status = ?, trace_id = ?, completed_at = ?
                WHERE key = ?
                """,
                (status, trace_id, now, key),
            )

    def get_status(self, key: str) -> dict[str, Any] | None:
        """查询幂等键状态"""
        return self._get(key)

    def cleanup_expired(self) -> int:
        """清理已过期的幂等键，返回清理数量"""
        now = _utc_now()
        with self._conn() as conn:
            cursor = conn.execute(
                "DELETE FROM idempotency_keys WHERE expires_at < ? AND status IN ('completed', 'failed')",
                (now,),
            )
            return cursor.rowcount # 返回上一条 SQL 影响的行数

    def summary(self) -> dict[str, int]:
        """统计各状态的幂等键数量"""
        rows = self._conn().execute(
            "SELECT status, COUNT(*) as cnt FROM idempotency_keys GROUP BY status"
        ).fetchall() # 取出所有行
        return {row["status"]: row["cnt"] for row in rows}

    def _get(self, key: str) -> dict[str, Any] | None:
        """查询幂等键记录"""
        row = self._conn().execute(
            # (key,) 逗号是必须的，表示这是一个单元素元组.SQL参数必须是一个序列（如元组或列表），即使只有一个参数，也需要加逗号。
            "SELECT * FROM idempotency_keys WHERE key = ?", (key,)
        ).fetchone() # 取出一行
        if row is None:
            return None
        return dict(row) # 将 sqlite3.Row 对象转换为字典，以便更方便地访问字段值。


class IdempotencyManager:
    """
    幂等管理器（高级封装）
        Store 是通用的（任何幂等场景）
        Manager 是业务专用的（针对 BugReport）
        分层解耦：以后换存储（Redis），只改 Store
    提供基于 BugReport 的幂等检查便利方法。
    """

    def __init__(self, store: IdempotencyStore) -> None:
        self.store = store

    def check_bug_report(
        self,
        repo: str,
        pr_id: str,
        sha: str,
        trace_id: str | None = None,
        ttl_hours: int = 24,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        """
        检查 Bug 报告是否已经处理过

        Returns:
            (is_new, idempotency_key, existing_record)
            - is_new=True：首次处理
            - is_new=False：已有记录（existing_record 包含状态信息）
        """
        key = self.store.make_key(repo, pr_id, sha)
        is_new, existing = self.store.try_acquire(
            key,
            trace_id=trace_id,
            ttl_hours=ttl_hours,
            payload={
                "repo": repo,
                "pr_id": pr_id,
                "sha": sha,
            },
        )
        return is_new, key, existing

    def mark_completed(self, key: str, trace_id: str, success: bool = True) -> None:
        """标记处理完成"""
        self.store.complete(key, trace_id, success)

    def cleanup(self) -> int:
        """清理过期记录"""
        return self.store.cleanup_expired()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc_add_hours(base: str, hours: int) -> str:
    from datetime import timedelta
    dt = datetime.fromisoformat(base)
    return (dt + timedelta(hours=hours)).isoformat()
