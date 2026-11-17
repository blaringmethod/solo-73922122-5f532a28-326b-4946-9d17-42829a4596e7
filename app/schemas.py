"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ReportIn(BaseModel):
    lab_no: str = Field(..., min_length=1, description="实验室号（非空）")
    report_no: str = Field(..., min_length=1, description="实验室内报告号（非空）")
    measured: int = Field(..., ge=-2_147_483_648, le=2_147_483_647,
                          description="整数测值（PostgreSQL INTEGER 范围）")


class ReportOut(ReportIn):
    id: int


class BatchReportIn(BaseModel):
    items: list[ReportIn] = Field(..., min_length=1)


PG_INT_MAX = 2_147_483_647


class RevisionIn(BaseModel):
    revision_no: int = Field(..., ge=1, le=PG_INT_MAX, description="修订号，从 1 起递增")
    measured: int = Field(..., ge=-PG_INT_MAX - 1, le=PG_INT_MAX,
                          description="该修订的整数测值（PostgreSQL INTEGER 范围）")


class RevisionOut(BaseModel):
    report_id: int
    revision_no: int
    measured: int
    outcome: str = Field(
        ...,
        description="applied=本次使其生效；pending=已保存等待前序；duplicate=同号同值重发未重复应用",
    )
    newly_recorded: bool
    applied_now: list[int] = Field(default_factory=list, description="本次连续应用到的修订号")
    effective_revision: int = Field(..., description="当前已连续生效到的修订号（0 表示尚无）")
    effective_measured: int = Field(..., description="当前生效测值")
    next_required_revision: int = Field(..., description="下一个待补修订号")


class RevisionRecordOut(BaseModel):
    revision_no: int
    measured: int
    applied: bool
    created_at: datetime
    applied_at: datetime | None = None


class RevisionListOut(BaseModel):
    report_id: int
    lab_no: str
    report_no: str
    effective_revision: int
    effective_measured: int
    next_required_revision: int
    revisions: list[RevisionRecordOut]
