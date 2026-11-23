#!/usr/bin/env python3
"""Read-only discovery of an EKS + Terraform project (exit 0/1/2/127).

Finds the Terraform root that calls terraform-aws-modules/eks, runs a trial
``terraform plan -lock=false -refresh=true -out`` into a temp dir plus
``show -json``, and reads cluster facts from aws/kubectl and the target AMI
from public SSM. Writes discovery.json (overwritten every run) and one
DISCOVERY audit line. Nothing is applied, locked or initialized.

Anything that cannot be decided is listed in ``unknowns`` with a reason and is
never guessed: exit 2 ``NEXT: ASK_USER``. Unsupported projects (no EKS module,
plain aws_eks_cluster, several EKS modules) exit 1 ``NEXT: STOP``.
"""

import hashlib
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
from pathlib import Path

from lib import SYSTEM_NS, GateArgumentParser, audit_append, kubectl_json, run_cmd
from plan_check import _ami_values, _manifest, _manifest_kind
from upgrade_plan import detail_fields

PLAN_TIMEOUT = 900
EKS_SOURCE = re.compile(r"^(registry\.terraform\.io/)?terraform-aws-modules/eks/aws$"
                        r"|terraform-aws-eks(\.git)?(\?ref=[^/]*)?$")
MODULE_SOURCE = re.compile(r'module\s+"[^"]+"\s*\{[^{}]*?\bsource\s*=\s*"([^"]+)"', re.S)
PLAIN_CLUSTER = re.compile(r'resource\s+"aws_eks_cluster"')
AUTH_ERROR = re.compile(
    r"ExpiredToken|InvalidClientTokenId|NoCredentialProviders|No valid credential sources"
    r"|security token included in the request|UnrecognizedClientException|SSO session"
    r"|Unable to locate credentials|failed to refresh cached credentials|AccessDenied", re.I)
VAR_ERROR = re.compile(r"No value for required variable|Failed to read variables file"
                       r"|undeclared variable|Invalid value for input variable", re.I)
INIT_ERROR = re.compile(r"terraform init|Module not installed|Inconsistent dependency lock file"
                        r"|Required plugins are not installed", re.I)
VERSION_ARGUMENTS = {21: ("kubernetes_version",), 20: ("cluster_version",)}
MANIFEST_TYPES = ("kubectl_manifest", "kubernetes_manifest")
# amiType -> public SSM parameter holding the EKS MNG release_version for {v}.
MNG_SSM = {
    "AL2023_x86_64_STANDARD": "/aws/service/eks/optimized-ami/{v}/amazon-linux-2023/x86_64/standard/recommended/release_version",
    "AL2023_ARM_64_STANDARD": "/aws/service/eks/optimized-ami/{v}/amazon-linux-2023/arm64/standard/recommended/release_version",
    "BOTTLEROCKET_x86_64": "/aws/service/bottlerocket/aws-k8s-{v}/x86_64/latest/image_version",
    "BOTTLEROCKET_ARM_64": "/aws/service/bottlerocket/aws-k8s-{v}/arm64/latest/image_version",
}
BOTTLEROCKET_SSM = "/aws/service/bottlerocket/aws-k8s-{v}/{arch}/latest/image_version"
NODE_ARCH = {"amd64": "x86_64", "arm64": "arm64"}


class Halt(Exception):
    """Discovery cannot continue: status is 'stop' or 'ask_user'."""

    def __init__(self, status, field, reason):
        super().__init__(reason)
        self.status, self.field, self.reason = status, field, reason


# ── Terraform root ────────────────────────────────────────────

def is_eks_source(source):
    return bool(EKS_SOURCE.search(source)) and "//modules/" not in source


def find_roots(base):
    """Return (roots calling the EKS module, dirs with a plain aws_eks_cluster)."""
    calls, plain = {}, set()
    for tf in sorted(base.rglob("*.tf")):
        if any(part.startswith(".") for part in tf.relative_to(base).parts[:-1]):
            continue
        text = tf.read_text(encoding="utf-8", errors="replace")
        calls.setdefault(tf.parent, []).extend(MODULE_SOURCE.findall(text))
        if PLAIN_CLUSTER.search(text):
            plain.add(tf.parent)
    callers = {}
    for directory, sources in calls.items():
        for source in sources:
            if source.startswith(("./", "../")):
                callers.setdefault((directory / source).resolve(), set()).add(directory)
    pending = [d for d, sources in calls.items() if any(is_eks_source(s) for s in sources)]
    roots, seen = set(), set()
    while pending:  # walk local wrapper modules up to the directory nobody calls
        directory = pending.pop()
        if directory in seen:
            continue
        seen.add(directory)
        ups = callers.get(directory.resolve())
        if ups:
            pending.extend(ups)
        else:
            roots.add(directory)
    return sorted(roots), sorted(plain)


def resolve_tf_dir(args):
    if args.tf_dir:
        return Path(args.tf_dir).expanduser().resolve()
    roots, plain = find_roots(Path(args.root).expanduser().resolve())
    if len(roots) > 1:
        raise Halt("ask_user", "tf_dir", "several Terraform roots call the EKS module: "
                   + ", ".join(str(r) for r in roots))
    if roots:
        return roots[0]
    if plain:
        raise Halt("stop", "tf_dir", "plain aws_eks_cluster without terraform-aws-modules/eks is "
                   "not supported: " + ", ".join(str(p) for p in plain))
    raise Halt("stop", "tf_dir", "no Terraform root calls terraform-aws-modules/eks under "
               + str(args.root))


# ── Trial plan ────────────────────────────────────────────────

def classify_failure(output):
    if AUTH_ERROR.search(output):
        return Halt("ask_user", "auth_prefix", "AWS credentials failed; give auth_prefix "
                    "(e.g. 'aws-vault exec <profile> --'): " + last_line(output))
    if VAR_ERROR.search(output):
        return Halt("ask_user", "tf_var_file", "Terraform variables missing/unreadable; give "
                    "tf_var_file: " + last_line(output))
    if INIT_ERROR.search(output):
        return Halt("ask_user", "terraform_init", "Terraform root is not initialized; run "
                    "terraform init there (discovery never initializes): " + last_line(output))
    return Halt("stop", "terraform_plan", "trial plan failed: " + last_line(output))


def last_line(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1][:300] if lines else "no output"


def trial_plan(tf_dir, prefix, var_file):
    chdir = "-chdir=" + str(tf_dir)
    with tempfile.TemporaryDirectory(prefix="discover-") as tmp:
        planfile = str(Path(tmp) / "discover.tfplan")
        command = prefix + ["terraform", chdir, "plan", "-input=false", "-lock=false",
                            "-refresh=true", "-no-color", "-out=" + planfile]
        if var_file:
            command.append("-var-file=" + var_file)
        result = run_cmd(command, timeout=PLAN_TIMEOUT)
        if result.returncode != 0:
            raise classify_failure(result.stdout + "\n" + result.stderr)
        shown = run_cmd(prefix + ["terraform", chdir, "show", "-json", planfile], timeout=120)
    if shown.returncode != 0:
        raise classify_failure(shown.stdout + "\n" + shown.stderr)
    try:
        plan = json.loads(shown.stdout)
    except json.JSONDecodeError as error:
        raise Halt("stop", "terraform_plan", "terraform show -json is not JSON: {}".format(error))
    if not isinstance(plan, dict) or not isinstance(plan.get("configuration"), dict):
        raise Halt("stop", "terraform_plan", "terraform show -json has no configuration block")
    return plan


# ── Configuration ─────────────────────────────────────────────

def eks_calls(module, address="", chain=()):
    """Yield (address, [outer..eks call]) for every EKS module call, at any depth."""
    for name, call in sorted((module.get("module_calls") or {}).items()):
        here = "{}module.{}".format(address + "." if address else "", name)
        if is_eks_source(call.get("source", "")):
            yield here, list(chain) + [call]
        yield from eks_calls(call.get("module") or {}, here, tuple(chain) + (call,))


def module_major(call):
    match = re.search(r"(\d+)\.", call.get("version_constraint") or "") or \
        re.search(r"ref=v?(\d+)", call.get("source", ""))
    return int(match.group(1)) if match else None


def trace_variable(chain, argument):
    """Follow an EKS module argument up through wrapper calls to one root variable."""
    expression = (chain[-1].get("expressions") or {}).get(argument)
    for depth in range(len(chain) - 1, -1, -1):
        if expression is None:
            return None, "argument {} is not set".format(argument)
        if "constant_value" in expression:
            return None, "hard-coded constant {!r}, not a variable".format(expression["constant_value"])
        names = sorted({ref.split(".")[1] for ref in expression.get("references", [])
                        if ref.startswith("var.")})
        if len(names) != 1:
            return None, "expression references {} (not exactly one variable)".format(
                expression.get("references", []))
        if depth == 0:
            return names[0], ""
        expression = (chain[depth - 1].get("expressions") or {}).get(names[0])
    return None, "unreachable"


def assigns(path, name):
    """True/False if the tfvars file assigns the variable; None if the file cannot be read."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        if path.name.endswith(".json"):
            data = json.loads(text)
            return isinstance(data, dict) and name in data
    except (OSError, ValueError):
        return None
    return bool(re.search(r"(?m)^\s*{}\s*=".format(re.escape(name)), text))


def set_in(plan, tf_dir, name, var_file):
    """(source, reason): where the variable gets its value, never guessed.

    Terraform precedence, highest first (developer.hashicorp.com/terraform/language/values/
    variables#variable-definition-precedence): -var/-var-file (our -var-file), *.auto.tfvars and
    *.auto.tfvars.json in lexical order, terraform.tfvars.json, terraform.tfvars, TF_VAR_<name>,
    then the variable default. "default" only if the plan value equals the declared default and
    nothing above sets it.
    """
    auto = sorted(list(tf_dir.glob("*.auto.tfvars")) + list(tf_dir.glob("*.auto.tfvars.json")),
                  key=lambda p: p.name, reverse=True)
    files = ([tf_dir / var_file] if var_file else []) + auto + [
        tf_dir / "terraform.tfvars.json", tf_dir / "terraform.tfvars"]
    # TF_CLI_ARGS* is inserted right after the subcommand, so its -var outranks every file except
    # our own -var-file, which discovery appends after it.
    cli_var = any("-var" in os.environ.get(key, "") for key in ("TF_CLI_ARGS", "TF_CLI_ARGS_plan"))
    if cli_var and not (var_file and assigns(tf_dir / var_file, name)):
        return "unknown", "TF_CLI_ARGS carries -var/-var-file that discovery cannot read"
    for path in files:
        if not path.is_file():
            continue
        found = assigns(path, name)
        if found is None:
            return "unknown", "cannot parse {} to see whether it sets {}".format(path, name)
        if found:
            return str(path), ""
    env = "TF_VAR_" + name
    if env in os.environ:
        return "env:" + env, ""
    declared = ((plan.get("configuration") or {}).get("root_module") or {}).get("variables") or {}
    if name in declared and "default" in (declared[name] or {}):
        if declared[name]["default"] == variable_value(plan, name):
            return "default", ""
        return "unknown", "plan value {!r} differs from the declared default but no tfvars file or " \
            "TF_VAR sets it".format(variable_value(plan, name))
    return "unknown", "no tfvars file or TF_VAR sets {} and the plan shows no default".format(name)


def source_of(plan, tf_dir, name, var_file, field, unknowns):
    source, reason = set_in(plan, tf_dir, name, var_file)
    if source == "unknown":
        unknowns.append(unknown(field, reason))
    return source


def variable_value(plan, name):
    entry = (plan.get("variables") or {}).get(name)
    return entry.get("value") if isinstance(entry, dict) else None


def cluster_version(plan, chain, major, tf_dir, var_file, unknowns):
    arguments = VERSION_ARGUMENTS.get(major) or tuple(
        a for a in ("kubernetes_version", "cluster_version") if a in (chain[-1].get("expressions") or {}))
    if len(arguments) != 1:
        unknowns.append(unknown("cluster_version.argument", "module major {} has no single known "
                                "version argument".format(major)))
        return {"argument": "unknown", "variable": "unknown", "value": "unknown", "set_in": "unknown"}
    variable, reason = trace_variable(chain, arguments[0])
    if variable is None:
        unknowns.append(unknown("cluster_version.variable", reason))
        return {"argument": arguments[0], "variable": "unknown", "value": "unknown", "set_in": "unknown"}
    return {"argument": arguments[0], "variable": variable,
            "value": variable_value(plan, variable), "set_in": source_of(plan, tf_dir, variable, var_file,
                                "cluster_version.set_in", unknowns)}


def unknown(field, reason):
    return {"field": field, "reason": reason}


# ── Resources ─────────────────────────────────────────────────

def state_resources(module):
    for resource in module.get("resources") or []:
        yield resource
    for child in module.get("child_modules") or []:
        yield from state_resources(child)


def resources(plan):
    """address -> {type, before, after}: prior_state values and planned values."""
    found = {}
    prior = ((plan.get("prior_state") or {}).get("values") or {}).get("root_module") or {}
    for resource in state_resources(prior):
        found[resource.get("address")] = {"type": resource.get("type"),
                                          "before": resource.get("values") or {}, "after": None}
    for change in plan.get("resource_changes") or []:
        entry = found.setdefault(change.get("address"), {"type": change.get("type"), "before": {}})
        entry["after"] = (change.get("change") or {}).get("after")
        if not entry["before"]:
            entry["before"] = (change.get("change") or {}).get("before") or {}
    return found


def matching_variable(plan, values, field, label):
    """The single variable whose value equals a current/planned resource value."""
    values = {v for v in values if isinstance(v, str) and v}
    if not values:
        return None, "{} is not set on any resource; AMI is not pinned by Terraform".format(field)
    names = sorted(name for name, entry in (plan.get("variables") or {}).items()
                   if isinstance(entry, dict) and isinstance(entry.get("value"), str)
                   and entry["value"] in values)
    if len(names) != 1:
        return None, "{} {} is fed by {} variables {} (need exactly one)".format(
            label, sorted(values), len(names), names)
    return names[0], ""


def under(address, eks_address):
    """True if address is inside the EKS module, including count/for_each instances."""
    return bool(re.match(r'{}(\[(?:[^\]"]|"[^"]*")+\])?\.'.format(re.escape(eks_address)), address or ""))


def mng_facts(plan, found, eks_address, unknowns):
    groups = {a: r for a, r in found.items() if r["type"] == "aws_eks_node_group"
              and under(a, eks_address)}
    facts = {"present": bool(groups), "node_groups": sorted(groups), "ami_variable": None,
             "ami_types": sorted({r["before"].get("ami_type") for r in groups.values()
                                  if r["before"].get("ami_type")}),
             "current_release": None, "target_release": None}
    if not groups:
        return facts
    current = {r["before"].get("release_version") for r in groups.values()}
    planned = {(r["after"] or {}).get("release_version") for r in groups.values()}
    facts["current_release"] = sorted(v for v in current if v)[0] if any(current) else None
    variable, reason = matching_variable(plan, current | planned, "release_version", "MNG release_version")
    facts["ami_variable"] = variable
    if variable is None:
        unknowns.append(unknown("mng.ami_variable", reason))
    return facts


def karpenter_facts(plan, found, unknowns):
    classes = {a: r for a, r in found.items() if r["type"] in MANIFEST_TYPES and any(
        _manifest_kind(_manifest({"after": r[side]})) == "EC2NodeClass" for side in ("before", "after"))}
    facts = {"present": bool(classes), "node_classes": sorted(classes), "alias_variable": None,
             "current_alias": None, "target_alias": None}
    if not classes:
        return facts
    current = {v for r in classes.values() for v in _ami_values({"after": r["before"]})}
    planned = {v for r in classes.values() if r["after"] for v in _ami_values({"after": r["after"]})}
    if len({v for v in current if v}) == 1:
        facts["current_alias"] = next(v for v in current if v)
    else:
        unknowns.append(unknown("karpenter.current_alias", "EC2NodeClass aliases {} (need exactly "
                                "one)".format(sorted(v for v in current if v))))
    variable, reason = matching_variable(plan, current | planned, "alias", "EC2NodeClass alias")
    facts["alias_variable"] = variable
    if variable is None:
        unknowns.append(unknown("karpenter.alias_variable", reason))
    return facts


def fargate_present(found, chain):
    if any(r["type"] == "aws_eks_fargate_profile" for r in found.values()):
        return True
    expression = (chain[-1].get("expressions") or {}).get("fargate_profiles") or {}
    return bool(expression.get("constant_value"))


def cluster_name_from(found, eks_address):
    for address, resource in sorted(found.items()):
        if resource["type"] == "aws_eks_cluster" and under(address, eks_address):
            return resource["before"].get("name") or (resource["after"] or {}).get("name")
    return None


# ── Live cluster ──────────────────────────────────────────────

def aws_json(prefix, args, field):
    result = run_cmd(prefix + ["aws"] + args + ["--output", "json"])
    if result.returncode != 0:
        output = result.stdout + "\n" + result.stderr
        if AUTH_ERROR.search(output):
            raise Halt("ask_user", "auth_prefix", "aws credentials failed for {}: {}".format(
                field, last_line(output)))
        return None, last_line(output)
    try:
        return json.loads(result.stdout), ""
    except json.JSONDecodeError:
        return None, "aws {} returned non-JSON".format(" ".join(args[:2]))


def node_pool(labels):
    if labels.get("eks.amazonaws.com/compute-type") == "fargate":
        return "Fargate"
    if "karpenter.sh/nodepool" in labels:
        return "Karpenter"
    if "eks.amazonaws.com/nodegroup" in labels:
        return "MNG"
    return "unmanaged"


def cluster_facts(prefix, name, unknowns):
    facts = {"version": None, "status": None, "running_pools": {}, "nodegroup_ami_types": []}
    described, reason = aws_json(prefix, ["eks", "describe-cluster", "--name", name], "describe-cluster")
    if described is None:
        unknowns.append(unknown("cluster.version", "describe-cluster failed: " + reason))
    else:
        cluster = described.get("cluster") or {}
        facts.update(version=cluster.get("version"), status=cluster.get("status"))
    groups, _ = aws_json(prefix, ["eks", "list-nodegroups", "--cluster-name", name], "list-nodegroups")
    for group in (groups or {}).get("nodegroups") or []:
        detail, _ = aws_json(prefix, ["eks", "describe-nodegroup", "--cluster-name", name,
                                      "--nodegroup-name", group], "describe-nodegroup")
        ami_type = ((detail or {}).get("nodegroup") or {}).get("amiType")
        if ami_type and ami_type not in facts["nodegroup_ami_types"]:
            facts["nodegroup_ami_types"].append(ami_type)
    nodes = kubectl_json("nodes", all_ns=False)
    if nodes is None:
        unknowns.append(unknown("cluster.running_pools", "kubectl get nodes failed"))
        return facts
    for node in nodes.get("items") or []:
        pool = node_pool((node.get("metadata") or {}).get("labels") or {})
        arch = ((node.get("status") or {}).get("nodeInfo") or {}).get("architecture", "unknown")
        entry = facts["running_pools"].setdefault(pool, {"nodes": 0, "architectures": []})
        entry["nodes"] += 1
        if arch not in entry["architectures"]:
            entry["architectures"].append(arch)
    return facts


def selected(selector, labels):
    return bool(selector) and all(labels.get(k) == v for k, v in selector.items())


def ingress_backs(ingress, namespace, name):
    if (ingress.get("metadata") or {}).get("namespace") != namespace:
        return False
    spec = ingress.get("spec") or {}
    backends = [spec.get("defaultBackend") or {}] + [
        path.get("backend") or {} for rule in spec.get("rules") or []
        for path in (rule.get("http") or {}).get("paths") or []]
    return any((b.get("service") or {}).get("name") == name for b in backends)


def service_candidates(unknowns):
    """Non-system Services with running backends: service_watch candidates for the interview."""
    services, pods = kubectl_json("services"), kubectl_json("pods")
    if services is None or pods is None:
        unknowns.append(unknown("service_candidates", "kubectl get services/pods failed"))
        return []
    pdbs = (kubectl_json("poddisruptionbudgets") or {}).get("items") or []
    ingresses = (kubectl_json("ingresses") or {}).get("items") or []
    candidates = []
    for service in services.get("items") or []:
        meta, spec = service.get("metadata") or {}, service.get("spec") or {}
        namespace, selector = meta.get("namespace", "default"), spec.get("selector") or {}
        if namespace in SYSTEM_NS or not selector:
            continue
        backends = [p for p in pods.get("items") or []
                    if (p.get("metadata") or {}).get("namespace") == namespace
                    and selected(selector, (p.get("metadata") or {}).get("labels") or {})
                    and (p.get("status") or {}).get("phase") == "Running"]
        if not backends:
            continue
        labels = (backends[0].get("metadata") or {}).get("labels") or {}
        pdb = [(b.get("metadata") or {}).get("name") for b in pdbs
               if (b.get("metadata") or {}).get("namespace") == namespace and selected(
                   ((b.get("spec") or {}).get("selector") or {}).get("matchLabels") or {}, labels)]
        if any(ingress_backs(i, namespace, meta.get("name")) for i in ingresses):
            via = "ingress"
        else:
            via = "loadbalancer" if spec.get("type") == "LoadBalancer" else "clusterip"
        candidates.append({"name": meta.get("name"), "namespace": namespace, "via": via,
                           "ready_pods": len(backends), "pdb": pdb[0] if pdb else None,
                           "min_endpoints": max(1, len(backends) - 1)})
    return candidates


# ── Target AMI ────────────────────────────────────────────────

def ssm_value(prefix, path):
    result = run_cmd(prefix + ["aws", "ssm", "get-parameter", "--name", path,
                               "--query", "Parameter.Value", "--output", "text"])
    value = result.stdout.strip()
    if result.returncode != 0 or not value or value == "None":
        return None
    return value


def single_target(values, field, unknowns):
    if not values or None in values or len(set(values)) != 1:
        unknowns.append(unknown(field, "public SSM gave {} (need one value)".format(values)))
        return None
    return values[0]


def mng_target(prefix, ami_types, target, unknowns):
    unsupported = [t for t in ami_types if t not in MNG_SSM]
    if not ami_types or unsupported:
        unknowns.append(unknown("mng.target_release", "amiTypes {} have no known public SSM "
                                "release_version".format(unsupported or "none found")))
        return None
    return single_target([ssm_value(prefix, MNG_SSM[t].format(v=target)) for t in ami_types],
                         "mng.target_release", unknowns)


def karpenter_target(prefix, current_alias, architectures, target, unknowns):
    family, _, version = (current_alias or "").partition("@")
    if family != "bottlerocket":
        unknowns.append(unknown("karpenter.target_alias", "alias family {!r} has no pinned public "
                                "SSM version rule; give the target alias".format(family or None)))
        return None
    archs = [NODE_ARCH.get(a) for a in architectures]
    if not archs or None in archs:
        unknowns.append(unknown("karpenter.target_alias", "running Karpenter node architectures {} "
                                "unknown".format(architectures)))
        return None
    values = [ssm_value(prefix, BOTTLEROCKET_SSM.format(v=target, arch=a)) for a in archs]
    image = single_target(values, "karpenter.target_alias", unknowns)
    if image is None:
        return None
    lead = "v" if version.startswith("v") and not image.startswith("v") else ""
    return "bottlerocket@" + lead + image


# ── Assembly ──────────────────────────────────────────────────

def eks_module(plan):
    root = (plan.get("configuration") or {}).get("root_module") or {}
    calls = list(eks_calls(root))
    if len(calls) > 1:
        raise Halt("stop", "module", "several EKS module calls in one root: "
                   + ", ".join(a for a, _ in calls) + " (one cluster per run)")
    if not calls:
        plain = any(r.get("type") == "aws_eks_cluster" for r in root.get("resources") or [])
        raise Halt("stop", "module", "plain aws_eks_cluster is not supported" if plain
                   else "configuration has no terraform-aws-modules/eks call")
    return calls[0]


def analyze_terraform(args, doc, plan, tf_dir):
    unknowns = doc["unknowns"]
    address, chain = eks_module(plan)
    call = chain[-1]
    major = module_major(call)
    doc["module"] = {"address": address, "source": call.get("source"),
                     "version_constraint": call.get("version_constraint"), "major": major}
    if major is None:
        unknowns.append(unknown("module.major", "no version constraint or git ref on the EKS module"))
    doc["cluster_version"] = cluster_version(plan, chain, major, tf_dir, args.tf_var_file, unknowns)
    found = resources(plan)
    doc["mng"] = mng_facts(plan, found, address, unknowns)
    doc["karpenter"] = karpenter_facts(plan, found, unknowns)
    for facts, key, label in ((doc["mng"], "ami_variable", "mng"), (doc["karpenter"], "alias_variable", "karpenter")):
        if facts[key]:
            facts[key + "_set_in"] = source_of(plan, tf_dir, facts[key], args.tf_var_file,
                                               "{}.{}_set_in".format(label, key), unknowns)
    doc["fargate"] = {"present": fargate_present(found, chain)}
    doc["node_pools"] = [pool for pool, key in (("MNG", "mng"), ("Karpenter", "karpenter"),
                                                ("Fargate", "fargate")) if doc[key]["present"]]
    return found


def next_minor(version):
    match = re.fullmatch(r"(\d+)\.(\d+)", version or "")
    return "{}.{}".format(match.group(1), int(match.group(2)) + 1) if match else None


def analyze_cluster(args, doc, prefix):
    unknowns = doc["unknowns"]
    live = cluster_facts(prefix, doc["cluster_name"], unknowns)
    doc["cluster"] = live
    if not args.target_version:
        args.target_version = doc["target_version"] = next_minor(live["version"])
    if not args.target_version:
        raise Halt("ask_user", "target_version", "live Control Plane version unknown; give target_version")
    doc["current_version"] = live["version"] or doc["cluster_version"].get("value")
    value = doc["cluster_version"].get("value")
    if live["version"] and value not in (live["version"], args.target_version, "unknown"):
        unknowns.append(unknown("cluster_version.value", "Terraform {} = {!r} matches neither the live "
                                "CP {} nor the target {}".format(doc["cluster_version"]["variable"], value,
                                                                live["version"], args.target_version)))
    for pool in sorted(set(live["running_pools"]) - set(doc["node_pools"])):
        unknowns.append(unknown("node_pools", "running {} nodes are not managed by this Terraform "
                                "root".format(pool)))
    doc["service_candidates"] = service_candidates(unknowns)
    if doc["mng"]["present"]:
        types = live["nodegroup_ami_types"] or doc["mng"]["ami_types"]
        doc["mng"]["ami_types"] = types
        doc["mng"]["target_release"] = mng_target(prefix, types, args.target_version, unknowns)
    if doc["karpenter"]["present"]:
        archs = (live["running_pools"].get("Karpenter") or {}).get("architectures", [])
        doc["karpenter"]["target_alias"] = karpenter_target(
            prefix, doc["karpenter"]["current_alias"], archs, args.target_version, unknowns)


def discover(args, doc):
    prefix = shlex.split(args.auth_prefix)
    tf_dir = resolve_tf_dir(args)
    doc["tf_dir"] = str(tf_dir)
    plan = trial_plan(tf_dir, prefix, args.tf_var_file)
    found = analyze_terraform(args, doc, plan, tf_dir)
    doc["cluster_name"] = args.cluster_name or cluster_name_from(found, doc["module"]["address"])
    if not doc["cluster_name"]:
        raise Halt("ask_user", "cluster_name", "aws_eks_cluster name is not in Terraform state; "
                   "give cluster_name")
    analyze_cluster(args, doc, prefix)


def build_parser():
    parser = GateArgumentParser(description=__doc__)
    parser.add_argument("--target-version", help="default: live Control Plane minor + 1")
    parser.add_argument("--root", default=".", help="directory searched for the Terraform root")
    parser.add_argument("--tf-dir", help="skip the search and use this Terraform root")
    parser.add_argument("--cluster-name", help="default: aws_eks_cluster name from Terraform state")
    parser.add_argument("--auth-prefix", default="")
    parser.add_argument("--tf-var-file", default="", help="relative to the Terraform root")
    parser.add_argument("--output", default="discovery.json")
    parser.add_argument("--audit-log", default="audit.log")
    return parser


def finish(args, doc):
    doc["status"] = doc["status"] or ("ask_user" if doc["unknowns"] else "complete")
    fields = sorted({u["field"] for u in doc["unknowns"]})
    result = {"complete": "PASS", "ask_user": "WARN"}.get(doc["status"], "FAIL")
    content = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
    Path(args.output).write_text(content, encoding="utf-8")
    audit_append(args.audit_log, "DISCOVERY", result, detail_fields(
        status=doc["status"], output=str(Path(args.output).resolve()),
        sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        cluster_name=doc["cluster_name"] or "unknown", tf_dir=doc["tf_dir"] or "unknown",
        module=(doc["module"] or {}).get("address", "unknown"), pools=",".join(doc["node_pools"]) or "none",
        unknowns=",".join(fields) or "none"), severity="-" if result != "FAIL" else "CRITICAL")
    print("DISCOVERY {}: {}".format(result, args.output))
    for item in doc["unknowns"]:
        print("  unknown {}: {}".format(item["field"], item["reason"]))
    if doc["status"] == "complete":
        return 0
    if doc["status"] == "ask_user":
        print("NEXT: ASK_USER " + ", ".join(fields))
        return 2
    print("NEXT: STOP " + "; ".join(u["reason"] for u in doc["unknowns"] if u.get("stop")))
    return 1


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.target_version and not re.fullmatch(r"\d+\.\d+", args.target_version):
        print("NEXT: STOP --target-version must be X.Y")
        return 1
    missing = [tool for tool in ("terraform", "aws", "kubectl") if shutil.which(tool) is None]
    if missing:
        print("NEXT: STOP {} not found in PATH".format(", ".join(missing)))
        return 127
    doc = {"schema_version": 1, "status": None, "tf_dir": None, "cluster_name": args.cluster_name,
           "current_version": None, "target_version": args.target_version, "module": None,
           "cluster_version": None, "node_pools": [], "mng": None, "karpenter": None, "fargate": None,
           "cluster": None, "service_candidates": [], "unknowns": []}
    try:
        discover(args, doc)
    except Halt as halt:
        doc["status"] = halt.status
        doc["unknowns"].append(dict(unknown(halt.field, halt.reason), stop=halt.status == "stop"))
    try:
        return finish(args, doc)
    except OSError as error:
        print("NEXT: STOP cannot write discovery output: {}".format(error))
        return 1


if __name__ == "__main__":
    sys.exit(main())
