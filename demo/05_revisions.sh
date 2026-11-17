#!/usr/bin/env bash
# 演示 5：已入仓报告的测值更正（乱序、幂等重发、同号异值拒绝、补齐后连续生效）。
#
# 使用方式：
#   bash demo/05_revisions.sh                      # 用 API_BASE / PSQL_URL 默认值
#   API_BASE=http://127.0.0.1:8000 \
#   PSQL_URL=postgresql://postgres:postgres@127.0.0.1:5432/report_warehouse \
#     bash demo/05_revisions.sh
set -euo pipefail
cd "$(dirname "$0")"
source ./common.sh

echo "== 1. 清理并插入一条已入仓报告（初始测值 100） =="
psql_exec -c "DROP INDEX IF EXISTS uq_reports_lab_no_report_no;" \
          -c "TRUNCATE reports RESTART IDENTITY CASCADE;" \
          -c "DELETE FROM index_build_rounds;" >/dev/null
c -X POST "$API_BASE/reports" -d '{"lab_no":"LAB-001","report_no":"R-1001","measured":100}' >/dev/null
RID="$(last_report_id)"
echo "report_id = $RID"

show() { python3 -m json.tool; }
expect_visible() {
  # expect_visible <期望测值>
  local got
  got="$(c "$API_BASE/reports?limit=500" | python3 -c '
import sys, json
rid = '"$RID"'
items = json.load(sys.stdin)["items"]
print(next(i["measured"] for i in items if i["id"] == rid))')"
  [[ "$got" == "$1" ]] || { echo "EXPECTED visible=$1, GOT $got" >&2; exit 1; }
  echo "OK: /reports 可见测值 = $got"
}

echo "== 2. 网络重试导致乱序：先到修订 3，再到修订 2 =="
c -X POST "$API_BASE/reports/$RID/revisions" -d '{"revision_no":3,"measured":330}' | show
c -X POST "$API_BASE/reports/$RID/revisions" -d '{"revision_no":2,"measured":220}' | show
echo "-- 前序（修订 1）缺失：两条都已保存，但当前值仍是 100，待补号仍为 1"
expect_visible 100

echo "== 3. 补齐修订 1：1、2、3 在同一次提交内连续生效，可见值一次到 330 =="
c -X POST "$API_BASE/reports/$RID/revisions" -d '{"revision_no":1,"measured":110}' | show
expect_visible 330

echo "== 4. 同号同值重发（网络重试）：outcome=duplicate，不重复应用 =="
c -X POST "$API_BASE/reports/$RID/revisions" -d '{"revision_no":2,"measured":220}' | show
REVISION_ROWS="$(psql_exec -tA -c "SELECT count(*) FROM report_revisions WHERE report_id=$RID")"
echo "report_revisions 行数 = $REVISION_ROWS（应为 3，重发未新增）"
[[ "$REVISION_ROWS" == "3" ]] || { echo "DUPLICATE APPLIED!"; exit 1; }

echo "== 5. 同号异值：拒绝（HTTP 409），已有数据保持不变 =="
HTTP_CODE="$(curl -sS -o /tmp/demo5_conflict.json -w '%{http_code}' \
  -H 'Content-Type: application/json' \
  -X POST "$API_BASE/reports/$RID/revisions" -d '{"revision_no":2,"measured":999}')"
echo "HTTP $HTTP_CODE"; python3 -m json.tool < /tmp/demo5_conflict.json
[[ "$HTTP_CODE" == "409" ]] || { echo "EXPECTED 409!"; exit 1; }
expect_visible 330

echo "== 6. 修订记录与生效号核对（缺口后的 4、5 尚未提交，待补号为 4） =="
c "$API_BASE/reports/$RID/revisions" | python3 -m json.tool
psql_exec -c "SELECT id, measured, effective_revision FROM reports WHERE id=$RID;"

echo "DEMO-5 OK"
