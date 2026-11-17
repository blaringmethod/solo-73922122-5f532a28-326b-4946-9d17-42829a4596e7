"""运行配置：全部通过环境变量注入，容器内由 docker-compose 提供默认值。"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv(
        "DATABASE_URL",
        "postgresql://postgres:postgres@db:5432/report_warehouse",
    )
    # CREATE INDEX CONCURRENTLY 等待锁/长事务的上限（毫秒），0 表示不限制。
    lock_timeout_ms: int = int(os.getenv("BUILD_LOCK_TIMEOUT_MS", "0"))
    # 后台对账轮询间隔（秒）：确认取消、重启接续。
    reconcile_interval_s: float = float(os.getenv("RECONCILE_INTERVAL_S", "1.5"))
    index_name: str = os.getenv("UNIQUE_INDEX_NAME", "uq_reports_lab_no_report_no")
    table_name: str = "reports"
    # 数据库连接池大小。并发修订提交超出池容量时会排队，而不是无限新建连接。
    pool_min_size: int = int(os.getenv("DB_POOL_MIN_SIZE", "2"))
    pool_max_size: int = int(os.getenv("DB_POOL_MAX_SIZE", "10"))
    # 从池中取连接的等待上限（秒）。
    pool_timeout_s: float = float(os.getenv("DB_POOL_TIMEOUT_S", "15"))


settings = Settings()
