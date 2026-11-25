#!/usr/bin/env python3
"""Validate a saved Terraform plan before apply (exit 0/1/2/127).

Fixed step policy: cp=cluster version, addon updates, time_sleep replacement,
node-group version-only updates alongside a CP version update or to the target; addon=addon
updates; nodes=node-group version/release_version updates; karpenter=EC2NodeClass
manifest updates. full permits non-destructive reconciliation; only time_sleep
may be replaced; full also permits deleting deposed objects. Full updates with
tag drift and unlisted changes require ASK_USER (exit 2), while version/AMI
mismatch, changed/unknown full AMIs without expectations and unreadable input
are STOP (exit 1).

AMI expectations name Terraform variables. ``release_version=value`` and
``alias=value`` additionally name resolved resource values when these differ
from the source variable. Every changed node AMI must match a resolved expected
value; null and unknown AMIs never count as matches.
"""

import hashlib
import json
import re
import shlex
import sys
from pathlib import Path

from lib import GateArgumentParser, audit_append, run_cmd
from upgrade_plan import current, detail_fields, fields, load, records

STEPS = ("cp", "addon", "nodes", "karpenter", "full")
NODE_TYPES = frozenset({"aws_eks_node_group", "aws_launch_template",
                        "aws_autoscaling_group", "aws_eks_fargate_profile"})
ALLOWED_FIELDS = {"cp": {"version"}, "nodes": {"version", "release_version"}}
# Terraform providers recompute these output-only attributes on version changes.
# Optional/computed inputs (notably release_version) deliberately stay checked.
COMPUTED_FIELDS = {
    "aws_eks_cluster": {"arn", "id", "certificate_authority", "endpoint", "identity",
                        "platform_version", "status", "created_at"},
    "aws_eks_node_group": {"arn", "id", "resources", "status"},
}
ACTIONS = {("no-op",), ("read",), ("create",), ("update",),
           ("delete",), ("delete", "create"), ("create", "delete")}


def _manifest(change, side="after"):
    values = change.get(side) or {}
    manifest = values.get("manifest", values.get("yaml_body", {}))
    if isinstance(manifest, str):
        try:
            return json.loads(manifest)
        except json.JSONDecodeError:
            return manifest
    return manifest


def _manifest_kind(manifest):
    if isinstance(manifest, dict):
        return manifest.get("kind")
    if isinstance(manifest, str):
        match = re.search(r"(?m)^\s*kind:\s*['\"]?([^\s'\"#]+)", manifest)
        return match.group(1) if match else None
    return None


def _is_node_class(change):
    return any(_manifest_kind(_manifest(change, side)) == "EC2NodeClass"
               for side in ("before", "after"))


def _unknown_manifest_identity(resource):
    if resource["type"] not in ("kubernetes_manifest", "kubectl_manifest"):
        return False
    change = resource["change"]
    unknown = change.get("after_unknown") or {}
    if not any(_has_unknown(unknown.get(field)) for field in ("manifest", "yaml_body")):
        return False
    manifest_unknown = unknown.get("manifest")
    kind_unknown = (manifest_unknown.get("kind") if isinstance(manifest_unknown, dict)
                    else manifest_unknown)
    return (not _is_node_class(change) and
            (not _manifest_kind(_manifest(change)) or _has_unknown(kind_unknown)))


def _ami_values(change, side="after"):
    """Read resolved node AMIs without treating unknown values as defaults."""
    after = change.get(side) or {}
    unknown = (change.get("after_unknown") or {}) if side == "after" else {}
    if "release_version" in after or "release_version" in unknown:
        return [None if unknown.get("release_version") else after.get("release_version")]
    if any(_has_unknown(unknown.get(field)) for field in ("manifest", "yaml_body")):
        return [None]
    manifest = _manifest(change, side)
    if isinstance(manifest, str):
        return re.findall(r"(?m)^\s*-?\s*alias:\s*['\"]?([^\s'\"#]+)", manifest)
    if isinstance(manifest, dict):
        terms = manifest.get("spec", {}).get("amiSelectorTerms", [])
        return [term.get("alias") for term in terms if isinstance(term, dict)]
    return []


def _has_unknown(value):
    if isinstance(value, dict):
        return any(_has_unknown(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_unknown(item) for item in value)
    return value is True


def _changed_fields(change, kind):
    before, after = change.get("before") or {}, change.get("after") or {}
    unknown = change.get("after_unknown") or {}
    return {key for key in before.keys() | after.keys() | unknown.keys()
            if key not in COMPUTED_FIELDS.get(kind, set())
            and (before.get(key) != after.get(key) or _has_unknown(unknown.get(key)))}


def _allowed(resource, step, cp_update, target_version):
    kind, change = resource["type"], resource["change"]
    actions = change["actions"]
    if actions in (["no-op"], ["read"]):
        return True
    if actions == ["delete"] and "deposed" in resource and step == "full":
        return True
    if "delete" in actions:
        return (kind == "time_sleep" and step in ("cp", "full")
                and "create" in actions)
    if step == "full":
        return actions != ["update"] or not (_changed_fields(change, kind) & {"tags", "tags_all"})
    if kind == "aws_eks_addon":
        return step in ("cp", "addon") and actions == ["update"]
    if step == "cp" and kind == "time_sleep":
        return actions in (["create"], ["update"])
    if step == "cp" and kind == "aws_eks_cluster":
        return actions == ["update"] and _changed_fields(change, kind) <= {"version"}
    if kind == "aws_eks_node_group" and step in ALLOWED_FIELDS:
        return (actions == ["update"] and _changed_fields(change, kind) <= ALLOWED_FIELDS[step]
                and (step != "cp" or cp_update
                     or (change.get("after") or {}).get("version") == target_version))
    return (step == "karpenter" and kind in ("kubernetes_manifest", "kubectl_manifest")
            and actions == ["update"] and _is_node_class(change))


def _validate_changes(plan):
    if not isinstance(plan, dict):
        raise ValueError("terraform show must return a JSON object")
    resources = plan.get("resource_changes", [])
    if not isinstance(resources, list):
        raise ValueError("resource_changes must be a list")
    for resource in resources:
        if not isinstance(resource, dict) or not isinstance(resource.get("type"), str):
            raise ValueError("resource change missing type")
        change = resource.get("change")
        if not isinstance(change, dict) or not isinstance(change.get("actions"), list):
            raise ValueError("resource change missing actions")
        if tuple(change["actions"]) not in ACTIONS:
            raise ValueError("unrecognized resource actions")
        for key in ("before", "after", "after_unknown"):
            if change.get(key) is not None and not isinstance(change[key], dict):
                raise ValueError("resource change {} must be an object".format(key))
    return resources


def classify_plan(plan, step, target_version, expected):
    """Return (exit code, detail, node_changes) for a validated show JSON."""
    resources = _validate_changes(plan)
    changing = [r for r in resources if r["change"]["actions"] not in (["no-op"], ["read"])]
    node_changes = any(r["type"] in NODE_TYPES or _is_node_class(r["change"]) for r in changing)
    if any(_unknown_manifest_identity(resource) for resource in changing):
        return 1, "manifest kind unknown; cannot establish node-change safety", node_changes
    cp_update = _cp_update(changing)
    for resource in resources:
        if resource["type"] == "aws_eks_cluster" and resource["change"]["actions"] != ["delete"]:
            change = resource["change"]
            if ((change.get("after_unknown") or {}).get("version")
                    or (change.get("after") or {}).get("version") != target_version):
                return 1, "cluster version does not match target_version", node_changes
    if expected:
        variables = plan.get("variables", {})
        if not isinstance(variables, dict):
            raise ValueError("variables must be an object")
        for name, value in expected.items():
            if name in ("release_version", "alias"):
                continue
            actual = variables.get(name)
            if not isinstance(actual, dict) or actual.get("value") != value:
                return 1, "AMI variable {} missing or differs from expectation".format(name), node_changes
    ami_code, ami_message = _check_amis(changing, step, expected)
    if ami_code:
        return ami_code, ami_message, node_changes
    unexpected = [r.get("address", r["type"]) for r in changing if not _allowed(r, step, cp_update, target_version)]
    if unexpected:
        return 2, "outside {} allow-list: {}".format(step, ", ".join(unexpected)) + (
            " (tag drift)" if step == "full" and any(
                r["change"]["actions"] == ["update"] and
                _changed_fields(r["change"], r["type"]) & {"tags", "tags_all"}
                for r in changing) else ""), node_changes
    return 0, "saved plan matches step allow-list", node_changes


def _cp_update(resources):
    return any(r["type"] == "aws_eks_cluster" and r["change"]["actions"] == ["update"]
               and "version" in _changed_fields(r["change"], r["type"]) for r in resources)


def _expected_values(expected, kind):
    if kind in expected:
        value = expected[kind]
        return {value} if isinstance(value, str) else set(value)
    return {value for name, value in expected.items() if name not in ("release_version", "alias")}


def _check_amis(resources, step, expected):
    for resource in resources:
        change = resource["change"]
        if change["actions"] == ["delete"]:
            continue
        if resource["type"] != "aws_eks_node_group" and not _is_node_class(change):
            continue
        kind = "release_version" if resource["type"] == "aws_eks_node_group" else "alias"
        values = _ami_values(change)
        allowed = _expected_values(expected, kind)
        ami_changed = values != _ami_values(change, "before") or None in values
        if step == "full" and ami_changed and not allowed:
            return 1, "changed/unknown node AMI requires approved plan expectations for " + kind
        if step == "nodes" or expected:
            if not values or any(not value or value not in allowed for value in values):
                return 1, "node AMI missing/unknown or differs from approved plan expectations"
    return 0, ""


def _print_node_table(resources, expected):
    """Show approved AMIs beside resolved plan values without changing gate policy."""
    rows = []
    for resource in resources:
        change = resource["change"]
        if change["actions"] in (["no-op"], ["read"]):
            continue
        is_mng = resource["type"] == "aws_eks_node_group"
        if not is_mng and not _is_node_class(change):
            continue
        kind = "release_version" if is_mng else "alias"
        allowed = _expected_values(expected, kind)
        values = _ami_values(change)
        matched = bool(values) and all(value and value in allowed for value in values)
        before, after = change.get("before") or {}, change.get("after") or {}
        unknown = change.get("after_unknown") or {}
        transitions = []
        if is_mng and any("version" in side for side in (before, after, unknown)):
            version = None if _has_unknown(unknown.get("version")) else after.get("version")
            transitions.append("version: {} → {}".format(before.get("version") or "unknown", version or "unknown"))
        transitions.append("{}: {} → {}".format(kind,
            ", ".join(str(value) if value else "unknown" for value in _ami_values(change, "before")) or "unknown",
            ", ".join(str(value) if value else "unknown" for value in values) or "unknown"))
        rows.append("{} | {} | {} | {}".format(resource.get("address", resource["type"]),
            ", ".join(sorted(allowed)) or "none", "; ".join(transitions), "yes" if matched else "no"))
    if rows:
        print("address | expected | before → after | match")
        print("\n".join(rows))


def build_parser():
    parser = GateArgumentParser(description=__doc__)
    parser.add_argument("--planfile", required=True)
    parser.add_argument("--plan-doc", required=True)
    parser.add_argument("--target-version", required=True)
    parser.add_argument("--tf-dir", required=True)
    parser.add_argument("--auth-prefix", default="")
    parser.add_argument("--audit-log", required=True)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    planfile = Path(args.planfile).expanduser().resolve()
    metadata = dict(planfile=str(planfile), sha256="unknown", step="unknown", step_id="unknown",
                    target_version=args.target_version, node_changes="false")
    code, message = 1, "plan check failed"
    try:
        if not re.fullmatch(r"\d+\.\d+", args.target_version):
            raise ValueError("--target-version must be X.Y")
        doc, step, binding, rows, _ = current(args.plan_doc, args.audit_log)
        metadata.update(binding, step=step["type"], step_id=step["id"])
        if args.target_version != doc["target_version"] or Path(args.tf_dir).expanduser().resolve() != Path(doc["tf_dir"]):
            raise ValueError("target-version/tf-dir differs from approved plan")
        expected = step["expect"]
        # A saved plan cannot be relabelled after a skip or completion either.
        # Scoped to this approved plan: reused file names in other hops/plans are unrelated.
        for row in records(args.audit_log):
            prior = fields(row["detail"])
            if (row["rule_id"] == "PLAN-CHECK" and prior.get("planfile") == str(planfile)
                    and prior.get("plan_doc") == binding["plan_doc"]
                    and prior.get("plan_sha256") == binding["plan_sha256"]
                    and prior.get("step_id") not in (None, "unknown")
                    and (prior.get("step_id") != step["id"] or prior.get("step") != step["type"])):
                raise ValueError("saved planfile already checked for a different step_id")
        metadata["sha256"] = hashlib.sha256(planfile.read_bytes()).hexdigest()
        command = shlex.split(args.auth_prefix) + ["terraform", "-chdir=" + args.tf_dir,
                                                  "show", "-json", str(planfile)]
        result = run_cmd(command)
        if result.returncode != 0:
            raise ValueError("terraform show failed: " + result.stderr.strip())
        plan = json.loads(result.stdout)
        code, message, node_changes = classify_plan(plan, step["type"], doc["target_version"], expected)
        resources = _validate_changes(plan)
        _print_node_table(resources, expected)
        metadata["cp_update"] = str(_cp_update(resources)).lower()
        metadata["node_changes"] = str(node_changes).lower()
        if load(args.plan_doc)[2] != binding["plan_sha256"]:
            raise ValueError("approved JSON changed while being checked")
        if hashlib.sha256(planfile.read_bytes()).hexdigest() != metadata["sha256"]:
            raise ValueError("planfile changed while being checked")
    except (OSError, ValueError, TypeError, AttributeError) as error:
        code, message = 1, str(error)
    result = {0: "PASS", 1: "FAIL", 2: "WARN"}[code]
    severity = {0: "-", 1: "CRITICAL", 2: "HIGH"}[code]
    try:
        audit_append(args.audit_log, "PLAN-CHECK", result,
                     detail_fields(**metadata, detail=message), severity=severity)
    except OSError as error:
        print("NEXT: STOP cannot write audit log: {}".format(error))
        return 1
    print("PLAN-CHECK {}: {}".format(result, message))
    if code:
        print("NEXT: {} {}".format("ASK_USER" if code == 2 else "STOP", message))
    return code


if __name__ == "__main__":
    sys.exit(main())
