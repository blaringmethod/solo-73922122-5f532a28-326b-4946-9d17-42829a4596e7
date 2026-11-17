#!/usr/bin/env bash
# 演示 6：并发补交。多份/同份报告的乱序修订同时到达，最终状态必须一致：
#   * 修订记录序号连续、全部 applied；
#   * reports.effective_revision = 最大连续修订号；
#   * reports.measured = 该修订的测值；
#   * /reports 可见值与之一致。
set -euo pipefail
cd "$(dirname "$0")"
source ./common.sh

TOTAL="${TOTAL:-30}"        # 每份报告的修订条数
WORKERS="${WORKERS:-10}"    # 并发线程数
REPORTS="${REPORTS:-3}"     # 并发报告份数

echo "== 1. 清理并准备 $REPORTS 份报告 =="
psql_exec -c "DROP INDEX IF EXISTS uq_reports_lab_no_report_no;" \
          -c "TRUNCATE reports RESTART IDENTITY CASCADE;" \
          -c "DELETE FROM index_build_rounds;" >/dev/null
for i in $(seq 1 "$REPORTS"); do
  c -X POST "$API_BASE/reports" -d "{\"lab_no\":\"CONC\",\"report_no\":\"R-$i\",\"measured\":0}" >/dev/null
done
RIDS="$(psql_exec -tA -c "SELECT string_agg(id::text, ' ') FROM reports")"
echo "report_ids = $RIDS"

echo "== 2. 以 $WORKERS 并发、乱序提交每份报告的修订 $TOTAL 条 =="
API_BASE="$API_BASE" RIDS="$RIDS" TOTAL="$TOTAL" WORKERS="$WORKERS" python3 - <<'PY'
import concurrent.futures as cf
import json
import os
import random
import urllib.request

base = os.environ["API_BASE"]
rids = [int(x) for x in os.environ["RIDS"].split()]
total = int(os.environ["TOTAL"])
workers = int(os.environ["WORKERS"])
rng = random.Random(20261117)
tasks = []
for rid in rids:
    order = list(range(1, total + 1))
    rng.shuffle(order)
    # 每个号提交两次，模拟网络重试（同号同值幂等）。
    tasks += [(rid, n, n * 10) for n in order for _ in range(2)]
rng.shuffle(tasks)

def submit(task):
    rid, n, value = task
    req = urllib.request.Request(
        f"{base}/reports/{rid}/revisions",
        data=json.dumps({"revision_no": n, "measured": value}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.load(resp)
        assert body["outcome"] in ("applied", "pending", "duplicate"), body
        return resp.status

with cf.ThreadPoolExecutor(max_workers=workers) as ex:
    codes = list(ex.map(submit, tasks))
print(f"提交 {len(codes)} 次，全部 HTTP 200；其中重试 "
      f"{len(tasks) - len(rids) * total} 次")
PY

echo "== 3. 核对：数据库不变量 =="
psql_exec -v ON_ERROR_STOP=1 -c "
DO \$\$
DECLARE r RECORD; bad INT := 0;
BEGIN
  FOR r IN SELECT id, measured, effective_revision FROM reports LOOP
    IF r.effective_revision <> $TOTAL THEN
      RAISE WARNING 'report % effective_revision=% (want $TOTAL)', r.id, r.effective_revision;
      bad := bad + 1;
    END IF;
    IF r.measured <> r.effective_revision * 10 THEN
      RAISE WARNING 'report % measured=% effective=%', r.id, r.measured, r.effective_revision;
      bad := bad + 1;
    END IF;
    IF EXISTS (SELECT 1 FROM report_revisions
                WHERE report_id = r.id AND applied IS DISTINCT FROM (revision_no <= r.effective_revision)) THEN
      RAISE WARNING 'report % revision applied flags inconsistent', r.id;
      bad := bad + 1;
    END IF;
    IF (SELECT count(*) FROM report_revisions WHERE report_id = r.id) <> $TOTAL THEN
      RAISE WARNING 'report % revision count mismatch', r.id;
      bad := bad + 1;
    END IF;
  END LOOP;
  IF bad > 0 THEN RAISE EXCEPTION '% consistency violation(s)', bad; END IF;
  RAISE NOTICE 'all reports consistent: effective_revision=$TOTAL, measured=$((TOTAL))0';
END \$\$;"

echo "== 4. 核对：API 可见值与生效号 =="
for rid in $RIDS; do
  c "$API_BASE/reports/$rid/revisions" | python3 -c '
import sys, json
s = json.load(sys.stdin)
assert s["effective_revision"] == '"$TOTAL"', s["effective_revision"]
assert s["effective_measured"] == '"$TOTAL"' * 10, s["effective_measured"]
assert s["next_required_revision"] == '"$TOTAL"' + 1
assert all(r["applied"] for r in s["revisions"]), "some revision not applied"
print("report {rid}: effective={e} measured={m} next={n}".format(
    rid=s["report_id"], e=s["effective_revision"],
    m=s["effective_measured"], n=s["next_required_revision"]))'
done
c "$API_BASE/reports?limit=500" | python3 -c '
import sys, json
items = json.load(sys.stdin)["items"]
assert all(i["measured"] == '"$TOTAL"' * 10 for i in items), items
print("/reports 可见测值全部一致")'

echo "== 5. 同一号不同值并发（各号仅一个赢家，其余 409） =="
RID="$(echo "$RIDS" | cut -d' ' -f1)"
API_BASE="$API_BASE" RID="$RID" TOTAL="$TOTAL" python3 - <<'PY'
import concurrent.futures as cf
import json
import os
import urllib.error
import urllib.request

base = os.environ["API_BASE"]
rid = os.environ["RID"]
next_no = int(os.environ["TOTAL"]) + 1

def submit(value):
    req = urllib.request.Request(
        f"{base}/reports/{rid}/revisions",
        data=json.dumps({"revision_no": next_no, "measured": value}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code

with cf.ThreadPoolExecutor(max_workers=8) as ex:
    codes = list(ex.map(submit, range(1, 9)))
print("HTTP 状态：", sorted(codes))
assert codes.count(200) == 1 and codes.count(409) == 7, codes
PY

echo "DEMO-6 OK"
