#!/usr/bin/env python3
"""Generate an approved upgrade plan; audit.log alone records execution progress."""
import hashlib
import json
import re
import shlex
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from lib import GateArgumentParser, audit_append, parse_audit_line

APPROVALS = {"ko": ("승인", "확인", "시작"), "en": ("approve", "confirm", "start")}
POOLS = ("MNG", "Karpenter", "Fargate")


def detail_fields(**fields):
    return " ".join("{}={}".format(key, shlex.quote(str(value))) for key, value in fields.items())


def fields(detail):
    return dict(token.split("=", 1) for token in shlex.split(detail) if "=" in token)


def records(path):
    source = Path(path)
    return [row for line in source.read_text(encoding="utf-8").splitlines()
            for row in [parse_audit_line(line)] if row] if source.exists() else []


def default_order(pools):
    return ["cp"] + (["nodes"] if "MNG" in pools else []) + (
        ["karpenter"] if "Karpenter" in pools else []) + ["full"]


def nonempty(value):
    return isinstance(value, str) and bool(value.strip()) and "\n" not in value and "\r" not in value


def validate(doc):
    from plan_check import STEPS
    if not isinstance(doc, dict) or doc.get("schema_version") != 1:
        raise ValueError("unsupported upgrade plan schema_version")
    for key in ("cluster", "current_version", "target_version", "tf_dir", "output_language"):
        if not nonempty(doc.get(key)):
            raise ValueError("missing/invalid plan " + key)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", doc["cluster"]):
        raise ValueError("invalid cluster name")
    if doc["output_language"] not in APPROVALS:
        raise ValueError("output_language must be ko or en")
    if any(not re.fullmatch(r"\d+\.\d+", doc[k]) for k in ("current_version", "target_version")):
        raise ValueError("versions must be X.Y")
    pools, steps, deviations = doc.get("node_pools"), doc.get("steps"), doc.get("deviations")
    if not isinstance(pools, list) or any(p not in POOLS for p in pools):
        raise ValueError("invalid node_pools")
    if not isinstance(steps, list) or not steps or not isinstance(deviations, list):
        raise ValueError("steps and deviations must be lists; steps cannot be empty")
    ids = set()
    for step in steps:
        if not isinstance(step, dict) or not nonempty(step.get("id")) or step["id"] in ids:
            raise ValueError("steps require unique nonempty ids")
        ids.add(step["id"])
        if step.get("type") not in STEPS:
            raise ValueError("invalid step type")
        if not isinstance(step.get("targets"), list) or any(not nonempty(t) for t in step["targets"]):
            raise ValueError("targets must be a list of addresses")
        expect = step.get("expect")
        if not isinstance(expect, dict) or any(not nonempty(k) or not (
                nonempty(v) or (k in ("alias", "release_version") and isinstance(v, list)
                                and v and all(nonempty(item) for item in v))) for k, v in expect.items()):
            raise ValueError("expect must map names to values (resolved AMIs may be lists)")
    order = [step["type"] for step in steps]
    if not set(default_order(pools)) <= set(order):
        raise ValueError("plan omits a required pool/cp/full step; use skip at execution time")
    if any(not isinstance(d, dict) or not nonempty(d.get("reason")) for d in deviations):
        raise ValueError("deviations require a reason")
    if order != default_order(pools) and not deviations:
        raise ValueError("custom order requires --reason")
    return doc


def load(path):
    path = Path(path).expanduser().resolve()
    data = path.read_bytes()
    return path, validate(json.loads(data)), hashlib.sha256(data).hexdigest()


def context(path, audit_log, require_approval=True):
    path, doc, digest = load(path)
    rows = records(audit_log)
    approvals = [i for i, row in enumerate(rows) if row["rule_id"] == "PLAN-APPROVED"
                 and row["result"] == "PASS" and fields(row["detail"]).get("plan_doc") == str(path)]
    approved = approvals and fields(rows[approvals[-1]]["detail"]).get("sha256") == digest
    if require_approval and not approved:
        raise ValueError("no PLAN-APPROVED for this path and JSON sha256")
    scoped = []
    for row in rows[approvals[-1] + 1:] if approved else []:
        meta = fields(row["detail"])
        if meta.get("plan_doc") == str(path) and meta.get("plan_sha256") == digest:
            scoped.append(row)
    cursor = 0
    boundary = 0
    for i, row in enumerate(scoped):
        if row["result"] not in ("PASS", "SKIP") or row["rule_id"] not in ("STEP-DONE", "DEVIATION"):
            continue
        if (row["rule_id"], row["result"]) not in (("DEVIATION", "SKIP"), ("STEP-DONE", "PASS")):
            continue
        meta = fields(row["detail"])
        if cursor >= len(doc["steps"]) or (meta.get("step_id") != doc["steps"][cursor]["id"]
                or meta.get("step") != doc["steps"][cursor]["type"]):
            raise ValueError("out-of-order completion in audit.log")
        cursor += 1
        boundary = i + 1
    step = doc["steps"][cursor] if cursor < len(doc["steps"]) else None
    return doc, step, dict(plan_doc=str(path), plan_sha256=digest), scoped, boundary


def current(path, audit_log):
    doc, step, meta, rows, boundary = context(path, audit_log)
    if step is None:
        raise ValueError("all approved steps are complete")
    return doc, step, meta, rows, boundary


def exact_answer(doc, answer):
    return (answer.lower() if doc["output_language"] == "en" else answer) in APPROVALS[doc["output_language"]]


def render(doc):
    ko = doc["output_language"] == "ko"
    lines = ["# " + ("EKS 업그레이드 계획서" if ko else "EKS Upgrade Plan"), "",
             "{}: {}".format("클러스터" if ko else "Cluster", doc["cluster"]),
             "{} → {}".format(doc["current_version"], doc["target_version"]),
             "Terraform: " + doc["tf_dir"], "", "| ID | Type | Targets | Expect |",
             "|---|---|---|---|"]
    for step in doc["steps"]:
        cells = [step["id"], step["type"], ", ".join(step["targets"]), json.dumps(step["expect"], ensure_ascii=False)]
        lines.append("| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |")
    lines.extend(["", "## " + ("모범사례 이탈" if ko else "Deviations"), ""])
    lines.extend(["- " + d["reason"] for d in doc["deviations"]] or ["없음" if ko else "None"])
    lines.extend(["", " / ".join(APPROVALS[doc["output_language"]]), ""])
    return "\n".join(lines)


def create(args):
    pools = list(dict.fromkeys(args.node_pool))
    order = args.order.split(",") if args.order else default_order(pools)
    if len(order) != len(set(order)):
        raise ValueError("CLI order cannot repeat step types")
    steps = [{"id": kind, "type": kind, "targets": [], "expect": {}} for kind in order]
    by_id = {step["id"]: step for step in steps}
    for item in args.target:
        kind, sep, address = item.partition("=")
        if not sep or kind not in by_id or not nonempty(address):
            raise ValueError("--target requires STEP=ADDRESS for an included step")
        by_id[kind]["targets"].append(address)
    for item in args.expect:
        kind, sep, pair = item.partition(":")
        name, equal, value = pair.partition("=")
        if not sep or not equal or kind not in by_id or not nonempty(name) or not nonempty(value):
            raise ValueError("--expect requires STEP:NAME=VALUE for an included step")
        expected = by_id[kind]["expect"]
        if name in ("alias", "release_version"):
            expected.setdefault(name, []).append(value)
        elif name in expected and expected[name] != value:
            raise ValueError("conflicting expected variable values")
        else:
            expected[name] = value
    doc = validate(dict(schema_version=1, cluster=args.cluster_name, current_version=args.current_version,
                        target_version=args.target_version, tf_dir=str(Path(args.tf_dir).expanduser().resolve()),
                        output_language=args.output_language, node_pools=pools, steps=steps,
                        deviations=[{"reason": args.reason}] if args.reason else []))
    path = Path(args.plan_doc).expanduser().resolve()
    zone = timezone(timedelta(hours=9)) if args.output_language == "ko" else timezone.utc
    md = path.parent / "upgrade-plan-{}-{}.md".format(doc["cluster"], datetime.now(zone).strftime("%Y%m%d"))
    write_outputs(path, doc, md)
    print(str(path) + "\n" + str(md))


def write_outputs(path, doc, md):
    """Keep existing outputs; remove only files created by a failed invocation."""
    outputs = [(path, json.dumps(doc, ensure_ascii=False, indent=2) + "\n"), (md, render(doc))]
    created = []
    try:
        for target, content in outputs:
            with target.open("x", encoding="utf-8") as stream:
                created.append(target)
                stream.write(content)
    except OSError:
        for target in reversed(created):
            target.unlink()
        raise


def approve(args):
    path, doc, digest = load(args.plan_doc)
    if not exact_answer(doc, args.answer):
        return 2
    rows = records(args.audit_log)
    approvals = [fields(row["detail"]) for row in rows if row["rule_id"] == "PLAN-APPROVED"
                 and row["result"] == "PASS" and fields(row["detail"]).get("plan_doc") == str(path)]
    if not approvals or approvals[-1].get("sha256") != digest:
        # Mid-run edits must go through skip/ASK_USER (DEVIATION), never a fresh approval.
        for row in rows:
            meta = fields(row["detail"])
            if ((row["rule_id"], row["result"]) in (("APPLY-GUARD", "PASS"), ("STEP-DONE", "PASS"),
                                                    ("DEVIATION", "SKIP"))
                    and meta.get("cluster_name") == doc["cluster"]
                    and meta.get("target_version") == doc["target_version"]):
                raise ValueError("execution already started for this cluster/target; "
                                 "use skip/ASK_USER instead of re-approving an edited plan")
        audit_append(args.audit_log, "PLAN-APPROVED", "PASS",
                     detail_fields(plan_doc=str(path), sha256=digest, answer=args.answer))
    print("PLAN-APPROVED PASS " + str(path))
    return 0


def advance(args):
    doc, step, meta, rows, boundary = current(args.plan_doc, args.audit_log)
    if args.command == "skip" and not exact_answer(doc, args.answer):
        return 2
    if args.step_id != step["id"]:
        raise ValueError("only the current step may be finished/skipped")
    meta.update(step_id=step["id"], step=step["type"], cluster_name=doc["cluster"],
                target_version=doc["target_version"])
    if args.command == "skip":
        if not nonempty(args.reason):
            raise ValueError("skip requires a nonempty reason")
        audit_append(args.audit_log, "DEVIATION", "SKIP",
                     detail_fields(**meta, reason=args.reason, answer=args.answer))
    else:
        if not any(row["rule_id"] == "APPLY-GUARD" and row["result"] == "PASS"
                   and fields(row["detail"]).get("step_id") == step["id"]
                   and fields(row["detail"]).get("step") == step["type"] for row in rows[boundary:]):
            raise ValueError("current step needs APPLY-GUARD PASS after previous completion")
        audit_append(args.audit_log, "STEP-DONE", "PASS", detail_fields(**meta))
    return 0


def build_parser():
    parser = GateArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("create", "approve", "skip", "done", "next"):
        sub = commands.add_parser(name)
        sub.add_argument("--plan-doc", default="upgrade-plan.json")
        if name != "create":
            sub.add_argument("--audit-log", default="audit.log")
        if name in ("approve", "skip"):
            sub.add_argument("--answer", required=True)
        if name in ("skip", "done"):
            sub.add_argument("--step-id", required=True)
        if name == "skip":
            sub.add_argument("--reason", required=True)
        if name == "create":
            for flag in ("cluster-name", "current-version", "target-version", "tf-dir"):
                sub.add_argument("--" + flag, required=True)
            sub.add_argument("--output-language", choices=APPROVALS, required=True)
            sub.add_argument("--node-pool", choices=POOLS, action="append", default=[])
            sub.add_argument("--target", action="append", default=[])
            sub.add_argument("--expect", action="append", default=[])
            sub.add_argument("--order")
            sub.add_argument("--reason")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.command == "create":
            create(args)
            return 0
        if args.command == "approve":
            code = approve(args)
        elif args.command in ("skip", "done"):
            code = advance(args)
        else:
            _, step, _, _, _ = context(args.plan_doc, args.audit_log, require_approval=False)
            print(json.dumps(step, ensure_ascii=False))
            return 0
        if code == 2:
            print("NEXT: ASK_USER exact approval word required")
        return code
    except (OSError, ValueError, TypeError, AttributeError) as error:
        print("NEXT: STOP {}".format(error))
        return 1


if __name__ == "__main__":
    sys.exit(main())
