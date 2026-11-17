-- 测值修订表：按报告主键 (reports.id) 提交、从 1 起递增的修订号。
-- 幂等脚本，随 01_schema.sql 一同在容器首次启动时执行。
--
-- 一致性不变量（由 API 在单个事务内维护）：
--   * reports.effective_revision 始终等于“已连续生效到的修订号”，0 表示尚无修订生效；
--   * 修订号 <= effective_revision 的记录 applied 必为 TRUE；
--   * 修订号 >  effective_revision 的记录 applied 必为 FALSE（缺口后的待补修订）。

BEGIN;

-- 为已有库补列（幂等）：当前已连续应用到的修订号。
ALTER TABLE reports
    ADD COLUMN IF NOT EXISTS effective_revision INTEGER NOT NULL DEFAULT 0;

COMMENT ON COLUMN reports.effective_revision
    IS '当前连续生效到的修订号，0 表示尚无修订生效';

CREATE TABLE IF NOT EXISTS report_revisions (
    report_id    BIGINT NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    revision_no  INTEGER NOT NULL CHECK (revision_no >= 1),
    measured     INTEGER NOT NULL,
    applied      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    applied_at   TIMESTAMPTZ,
    PRIMARY KEY (report_id, revision_no)
);

COMMENT ON TABLE  report_revisions              IS '按报告主键提交的测值修订记录';
COMMENT ON COLUMN report_revisions.revision_no  IS '修订号，从 1 起递增';
COMMENT ON COLUMN report_revisions.measured     IS '该修订提交的整数测值';
COMMENT ON COLUMN report_revisions.applied      IS '是否已按顺序生效；false 表示等待前序修订补齐';

-- 仅用于快速读取一份报告的待补修订（主键已覆盖大部分访问路径）。
CREATE INDEX IF NOT EXISTS idx_report_revisions_pending
    ON report_revisions (report_id, revision_no)
    WHERE applied = FALSE;

COMMIT;
