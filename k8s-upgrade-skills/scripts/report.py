#!/usr/bin/env python3
"""Write the upgrade report's tables from audit.log; the agent fills only the narrative slots.

Usage:
  python3 scripts/report.py --audit-log audit.log --plan-doc upgrade-plan.json [--output PATH]

Scope: records after the latest PLAN-APPROVED for this plan JSON (path + sha256), up to the next
approval of another plan. Phase 0 is the latest gate_check block for the plan's cluster/target.
Works for completed and failed runs. Writes one REPORT audit line.

Exit codes: 0 = report written, 1 = NEXT: STOP (missing/unapproved inputs, write failure).
"""
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lib import GateArgumentParser, audit_append
from upgrade_plan import detail_fields, fields, load, records

PHASE = re.compile(r"PHASE([0-9]+)")
PREFLIGHT = re.compile(r"(COM|WLS|CAP|INF)-")
RESULTS = ("PASS", "WARN", "FAIL")
SLOT = "<!-- NARRATIVE:{} — {} -->"

TEXT = {
    "ko": dict(
        title="EKS 업그레이드 보고서", cluster="클러스터", version="버전", plan="계획서",
        result="결과", generated="생성", success="✅ 성공", warn="⚠️ 경고 포함 완료 (Phase 7 WARN — 사용자 확인 필요)",
        failed="❌ Phase {}에서 실패", stopped="⏸ 미완료 — 마지막 Phase {}", open="⏸ 미완료 — Phase {} 진행 중 중단",
        none="⏸ 미완료 — 실행된 Phase 없음",
        phases="Phase 타임라인 (UTC)", phase_cols="| Phase | 시작 | 종료 | 소요 | 결과 | 실행 횟수 |",
        steps="계획 단계", step_cols="| ID | 유형 | 상태 | 시각 (UTC) | 비고 |",
        done="완료", skipped="건너뜀", not_reached="미도달",
        applies="Apply 가드 기록", apply_cols="| 시각 (UTC) | 단계 | planfile | 결과 | 상세 |",
        events="감시 이벤트 (DRAIN-*/SVC-* WARN/FAIL)", event_cols="| 시각 (UTC) | 모니터 | 결과 | 상세 |",
        deviations="이탈·사용자 결정", deviation_cols="| 시각 (UTC) | 종류 | 상세 |", planned="계획 단계",
        phase0="Phase 0 사전 검증 요약", phase0_cols="| 결과 | CRITICAL | HIGH | MEDIUM/INFO | PASS | SKIP |",
        phase0_none="Phase 0 기록 없음", empty="없음",
        summary="요약", troubleshooting="트러블슈팅 내역", state="현재 클러스터 상태", next="다음 단계",
        hint_summary="에이전트: 결과를 2~3문장으로 서술",
        hint_trouble="에이전트: 발생한 문제와 취한 조치를 시간순으로 서술 (없으면 '없음')",
        hint_state="에이전트: CP 버전, 노드 버전, 비정상 Pod 수를 조회해 표로 작성",
        hint_next="에이전트: 실패·경고 시 후속 조치, 성공 시 '없음'"),
    "en": dict(
        title="EKS Upgrade Report", cluster="Cluster", version="Version", plan="Plan",
        result="Result", generated="Generated", success="✅ Success", warn="⚠️ Completed with warnings (Phase 7 WARN — user confirmation required)",
        failed="❌ Failed at Phase {}", stopped="⏸ Incomplete — last phase {}", open="⏸ Incomplete — stopped during Phase {}",
        none="⏸ Incomplete — no phase ran",
        phases="Phase timeline (UTC)", phase_cols="| Phase | Start | End | Duration | Result | Runs |",
        steps="Plan steps", step_cols="| ID | Type | Status | Time (UTC) | Note |",
        done="done", skipped="skipped", not_reached="not reached",
        applies="Apply guard records", apply_cols="| Time (UTC) | Step | planfile | Result | Detail |",
        events="Monitor events (DRAIN-*/SVC-* WARN/FAIL)", event_cols="| Time (UTC) | Monitor | Result | Detail |",
        deviations="Deviations and user decisions", deviation_cols="| Time (UTC) | Kind | Detail |", planned="planned",
        phase0="Phase 0 pre-flight summary", phase0_cols="| Result | CRITICAL | HIGH | MEDIUM/INFO | PASS | SKIP |",
        phase0_none="no Phase 0 record", empty="None",
        summary="Summary", troubleshooting="Troubleshooting", state="Current cluster state", next="Next steps",
        hint_summary="agent: describe the outcome in 2-3 sentences",
        hint_trouble="agent: problems met and actions taken, in time order ('None' if none)",
        hint_state="agent: query CP version, node versions, unhealthy Pod count; write a table",
        hint_next="agent: follow-ups for a failure or warning; 'None' on success"),
}


def scope(rows, path, digest):
    """Rows of the current approved execution; earlier runs and later other plans are excluded."""
    approvals = [i for i, row in enumerate(rows) if row["rule_id"] == "PLAN-APPROVED"
                 and row["result"] == "PASS"]
    mine = [i for i in approvals if fields(rows[i]["detail"]).get("plan_doc") == str(path)]
    if not mine or fields(rows[mine[-1]]["detail"]).get("sha256") != digest:
        raise ValueError("no PLAN-APPROVED for this plan path and JSON sha256 in audit.log")
    start = mine[-1]
    # Any later approval belongs to another plan path: its records are not this run's.
    end = next((i for i in approvals if i > start), len(rows))
    return start, end


def phase0(rows, end, doc):
    """Latest gate_check block for this cluster/target before the end of scope."""
    for i in range(end - 1, -1, -1):
        row = rows[i]
        if row["rule_id"] != "PHASE0" or row["result"] not in RESULTS:
            continue
        meta = fields(row["detail"])
        if (meta.get("cluster_name") == doc["cluster"]
                and meta.get("target_version") == doc["target_version"]):
            enter = next((j for j in range(i - 1, -1, -1) if rows[j]["rule_id"] == "PHASE0"
                          and rows[j]["result"] == "ENTER"), i)
            rules = [r for r in rows[enter:i] if PREFLIGHT.match(r["rule_id"])]
            return dict(row=row, start=rows[enter]["timestamp"], counts=preflight_counts(rules))
    return None


def preflight_counts(rules):
    """Same severity buckets as lib.audit_flush."""
    def count(test):
        return sum(1 for r in rules if test(r["result"], r["severity"]))
    return [count(lambda res, sev: res == "FAIL" and sev in ("CRITICAL", "-")),
            count(lambda res, sev: res == "WARN" or (res == "FAIL" and sev == "HIGH")),
            count(lambda res, sev: res == "FAIL" and sev in ("MEDIUM", "LOW")),
            count(lambda res, sev: res == "PASS"),
            count(lambda res, sev: res == "SKIP")]


def timeline(rows):
    """phase number -> first ENTER, last result, runs. Phase 7 sub-gate copies are ignored."""
    phases, current = {}, None
    for index, row in enumerate(rows):
        match = PHASE.fullmatch(row["rule_id"])
        if not match:
            continue
        number = int(match.group(1))
        if current == 7 and number != 7:
            continue
        item = phases.setdefault(number, dict(start=None, end=None, result=None, runs=0, last=index))
        item["last"] = index
        if row["result"] == "ENTER":
            item["start"] = item["start"] or row["timestamp"]
            item["runs"] += 1
            item["result"] = None
            current = number
        elif row["result"] in RESULTS:
            item["end"], item["result"] = row["timestamp"], row["result"]
            current = None
    return phases


def overall(phases, text):
    """(label, result key, phase) from the phase boundary lines only."""
    final = phases.get(7, {}).get("result")
    if final == "PASS":
        return text["success"], "success", "7"
    if final == "WARN":
        return text["warn"], "warn", "7"
    failed = [n for n, p in phases.items() if p["result"] == "FAIL"]
    if failed:
        number = max(failed, key=lambda n: phases[n]["last"])
        return text["failed"].format(number), "failed", str(number)
    unfinished = [n for n, p in phases.items() if p["result"] is None]
    if unfinished:
        return text["open"].format(unfinished[0]), "incomplete", str(unfinished[0])
    if phases:
        number = max(phases, key=lambda n: phases[n]["last"])
        return text["stopped"].format(number), "incomplete", str(number)
    return text["none"], "incomplete", "none"


def duration(start, end):
    if not start or not end:
        return "-"
    parse = lambda ts: datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
    seconds = int((parse(end) - parse(start)).total_seconds())
    return "{}m {:02d}s".format(seconds // 60, seconds % 60)


def cell(value):
    return str(value).replace("|", "\\|")


def table(header, rows, text):
    if not rows:
        return [text["empty"]]
    width = header.count("|") - 1
    return [header, "|" + "---|" * width] + ["| " + " | ".join(cell(c) for c in r) + " |" for r in rows]


def step_rows(doc, scoped, digest, text):
    finished = {}
    for row in scoped:
        meta = fields(row["detail"])
        if meta.get("plan_sha256") != digest:
            continue
        if (row["rule_id"], row["result"]) == ("STEP-DONE", "PASS"):
            finished[meta.get("step_id")] = (text["done"], row["timestamp"], "")
        elif (row["rule_id"], row["result"]) == ("DEVIATION", "SKIP"):
            finished[meta.get("step_id")] = (text["skipped"], row["timestamp"], meta.get("reason", ""))
    return [[s["id"], s["type"], *finished.get(s["id"], (text["not_reached"], "-", ""))]
            for s in doc["steps"]]


def record_rows(scoped, doc, text):
    applies = [[r["timestamp"], fields(r["detail"]).get("step_id", "unknown"),
                Path(fields(r["detail"]).get("planfile", "unknown")).name, r["result"],
                fields(r["detail"]).get("detail", "")]
               for r in scoped if r["rule_id"] == "APPLY-GUARD"]
    events = [[r["timestamp"], r["rule_id"], r["result"], r["detail"]] for r in scoped
              if r["rule_id"].startswith(("DRAIN-", "SVC-")) and r["result"] in ("WARN", "FAIL")]
    deviations = [["-", text["planned"], d["reason"]] for d in doc["deviations"]]
    for r in scoped:
        meta = fields(r["detail"]) if r["rule_id"] == "DEVIATION" else {}
        if r["rule_id"] == "DEVIATION":
            deviations.append([r["timestamp"], "DEVIATION " + r["result"], "step_id={} reason={} answer={}".format(
                meta.get("step_id", "unknown"), meta.get("reason", ""), meta.get("answer", ""))])
        elif r["rule_id"] == "ASK_USER":
            deviations.append([r["timestamp"], "ASK_USER", r["detail"]])
    return applies, events, deviations


def render(doc, path, scoped, rows, end, digest):
    text = TEXT[doc["output_language"]]
    phases = timeline(scoped)
    label, key, number = overall(phases, text)
    pre = phase0(rows, end, doc)
    if pre:
        phases.setdefault(0, dict(start=pre["start"], end=pre["row"]["timestamp"],
                                  result=pre["row"]["result"], runs=1, last=-1))
    phase_rows = [["Phase {}".format(n), p["start"] or "-", p["end"] or "-",
                   duration(p["start"], p["end"]), p["result"] or "-", p["runs"]]
                  for n, p in sorted(phases.items())]
    applies, events, deviations = record_rows(scoped, doc, text)
    zone = timezone(timedelta(hours=9)) if doc["output_language"] == "ko" else timezone.utc
    lines = ["# " + text["title"], "",
             "| | |", "|---|---|",
             "| {} | {} |".format(text["cluster"], doc["cluster"]),
             "| {} | {} → {} |".format(text["version"], doc["current_version"], doc["target_version"]),
             "| {} | {} |".format(text["plan"], cell(path)),
             "| {} | {} |".format(text["result"], label),
             "| {} | {} |".format(text["generated"], datetime.now(zone).strftime("%Y-%m-%d %H:%M %Z")), "",
             "## " + text["summary"], "", SLOT.format("summary", text["hint_summary"]), "",
             "## " + text["phase0"], ""]
    lines += table(text["phase0_cols"], [[pre["row"]["result"], *pre["counts"]]], text) if pre \
        else [text["phase0_none"]]
    for title, header, body in (("phases", "phase_cols", phase_rows), ("steps", "step_cols",
                                step_rows(doc, scoped, digest, text)), ("applies", "apply_cols", applies),
                                ("events", "event_cols", events), ("deviations", "deviation_cols", deviations)):
        lines += ["", "## " + text[title], ""] + table(text[header], body, text)
    for title, slot, hint in (("troubleshooting", "troubleshooting", "hint_trouble"),
                              ("state", "cluster-state", "hint_state"), ("next", "next-steps", "hint_next")):
        lines += ["", "## " + text[title], "", SLOT.format(slot, text[hint])]
    return "\n".join(lines) + "\n", key, number, zone


def main(argv=None):
    parser = GateArgumentParser(description="Write the upgrade report tables from audit.log")
    parser.add_argument("--audit-log", default="audit.log")
    parser.add_argument("--plan-doc", default="upgrade-plan.json")
    parser.add_argument("--output", help="default: ./upgrade-report-<cluster>-<YYYYMMDD>.md")
    args = parser.parse_args(argv)
    try:
        path, doc, digest = load(args.plan_doc)
        rows = records(args.audit_log)
        start, end = scope(rows, path, digest)
        content, key, number, zone = render(doc, path, rows[start + 1:end], rows, end, digest)
        output = Path(args.output or "upgrade-report-{}-{}.md".format(
            doc["cluster"], datetime.now(zone).strftime("%Y%m%d"))).expanduser().resolve()
        output.write_text(content, encoding="utf-8")
        audit_append(args.audit_log, "REPORT", "PASS", detail_fields(
            plan_doc=str(path), plan_sha256=digest, cluster_name=doc["cluster"],
            target_version=doc["target_version"], output=str(output), result=key, phase=number))
    except (OSError, ValueError, TypeError, KeyError) as error:
        print("NEXT: STOP {}".format(error))
        return 1
    print("REPORT PASS {} result={} phase={}".format(output, key, number))
    return 0


if __name__ == "__main__":
    sys.exit(main())
