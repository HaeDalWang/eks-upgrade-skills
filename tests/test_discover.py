"""discover.py: read-only Terraform/EKS discovery with faked terraform/aws/kubectl."""
import json
import subprocess

import pytest

import discover
import lib
from lib import parse_audit_line

AL2023 = "/aws/service/eks/optimized-ami/1.34/amazon-linux-2023/x86_64/standard/recommended/release_version"
BR_X86 = "/aws/service/bottlerocket/aws-k8s-1.34/x86_64/latest/image_version"
EKS_TF = 'module "eks" {\n  source  = "terraform-aws-modules/eks/aws"\n  version = "21.10.1"\n}\n'


def ref(*names):
    return {"references": list(names)}


def make_plan(major=21, mng=True, karpenter=False, fargate=False, version_ref="var.eks_cluster_version",
              mng_release="1.33.0-20261101", alias="bottlerocket@1.46.0", variables=None):
    version_arg = "kubernetes_version" if major == 21 else "cluster_version"
    expressions = {version_arg: ref(version_ref) if version_ref else {"constant_value": "1.33"}}
    if fargate:
        expressions["fargate_profiles"] = {"constant_value": {"kube-system": {}}}
    state = [{"address": "module.eks.aws_eks_cluster.this[0]", "type": "aws_eks_cluster",
              "values": {"name": "demo-eks", "version": "1.33"}}]
    values = {"eks_cluster_version": "1.33"}
    if mng:
        state.append({"address": 'module.eks.module.eks_managed_node_group["system"].aws_eks_node_group.this[0]',
                      "type": "aws_eks_node_group",
                      "values": {"release_version": mng_release, "ami_type": "AL2023_x86_64_STANDARD"}})
        values["eks_node_ami_alias_al2023"] = "1.33.0-20261101"
    if karpenter:
        body = json.dumps({"kind": "EC2NodeClass", "spec": {"amiSelectorTerms": [{"alias": alias}]}})
        state.append({"address": "kubectl_manifest.node_class", "type": "kubectl_manifest",
                      "values": {"yaml_body": body}})
        values["node_alias"] = "bottlerocket@1.46.0"
    values.update(variables or {})
    return {"configuration": {"root_module": {"variables": {k: {"default": v} for k, v in values.items()},
                                              "module_calls": {"eks": {
                "source": "terraform-aws-modules/eks/aws",
                "version_constraint": "~> {}.0".format(major), "expressions": expressions}}}},
            "prior_state": {"values": {"root_module": {"resources": state[:1], "child_modules": [
                {"resources": state[1:]}]}}},
            "variables": {k: {"value": v} for k, v in values.items()}, "resource_changes": []}


def node(labels, arch="amd64"):
    return {"metadata": {"labels": labels}, "status": {"nodeInfo": {"architecture": arch}}}


class Fake:
    """Minimal fake CLI: records argv, answers from attributes."""

    def __init__(self, plan, nodes=None, plan_error=None, cluster_error=None, ssm=None):
        self.plan, self.plan_error, self.cluster_error = plan, plan_error, cluster_error
        self.nodes = nodes if nodes is not None else [node({"eks.amazonaws.com/nodegroup": "system"})]
        self.ssm = {AL2023: "1.34.0-20261121", BR_X86: "1.47.0"} if ssm is None else ssm
        self.calls = []

    def __call__(self, args, timeout=60):
        self.calls.append(args)
        tool = next(a for a in args if a in ("terraform", "aws", "kubectl"))
        rest = args[args.index(tool) + 1:]
        out, code = getattr(self, tool)(rest)
        return subprocess.CompletedProcess(args, code, out if code == 0 else "", "" if code == 0 else out)

    def terraform(self, rest):
        if "plan" in rest:
            return (self.plan_error, 1) if self.plan_error else ("Plan: 0 to add", 0)
        return json.dumps(self.plan), 0

    def aws(self, rest):
        if rest[:2] == ["eks", "describe-cluster"]:
            if self.cluster_error:
                return self.cluster_error, 254
            return json.dumps({"cluster": {"version": "1.33", "status": "ACTIVE"}}), 0
        if rest[:2] == ["eks", "list-nodegroups"]:
            return json.dumps({"nodegroups": ["system"] if any(
                "eks.amazonaws.com/nodegroup" in n["metadata"]["labels"] for n in self.nodes) else []}), 0
        if rest[:2] == ["eks", "describe-nodegroup"]:
            return json.dumps({"nodegroup": {"amiType": "AL2023_x86_64_STANDARD"}}), 0
        if rest[:2] == ["ssm", "get-parameter"]:
            name = rest[rest.index("--name") + 1]
            return (self.ssm[name], 0) if name in self.ssm else ("ParameterNotFound", 254)
        return "unsupported", 1

    def kubectl(self, rest):
        kind = rest[1] if rest[1] != "-A" else rest[2]
        items = {"nodes": self.nodes,
                 "services": [{"metadata": {"name": "web", "namespace": "shop"},
                               "spec": {"selector": {"app": "web"}, "type": "ClusterIP"}},
                              {"metadata": {"name": "kube-dns", "namespace": "kube-system"},
                               "spec": {"selector": {"k8s-app": "kube-dns"}}}],
                 "pods": [{"metadata": {"namespace": "shop", "labels": {"app": "web"}},
                           "status": {"phase": "Running"}} for _ in range(3)],
                 "poddisruptionbudgets": [{"metadata": {"name": "web-pdb", "namespace": "shop"},
                                           "spec": {"selector": {"matchLabels": {"app": "web"}}}}],
                 "ingresses": [{"metadata": {"namespace": "shop"}, "spec": {"rules": [{"http": {
                     "paths": [{"backend": {"service": {"name": "web"}}}]}}]}}]}[kind]
        return json.dumps({"items": items}), 0


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "infra"
    root.mkdir()
    (root / "eks.tf").write_text(EKS_TF)
    (root / "terraform.tfvars").write_text('eks_cluster_version = "1.33"\n')
    monkeypatch.setattr(discover.shutil, "which", lambda tool: "/usr/bin/" + tool)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def run(monkeypatch, fake, *extra):
    monkeypatch.setattr(discover, "run_cmd", fake)
    monkeypatch.setattr(lib, "run_cmd", fake)
    code = discover.main(["--target-version", "1.34", "--root", "."] + list(extra))
    doc = json.loads(open("discovery.json", encoding="utf-8").read())
    audit = [parse_audit_line(line) for line in open("audit.log", encoding="utf-8")]
    return code, doc, audit


def test_v21_mng_discovers_variables_pools_and_target_ami(project, monkeypatch, capsys):
    code, doc, audit = run(monkeypatch, Fake(make_plan()))
    assert code == 0, capsys.readouterr().out
    assert doc["tf_dir"] == str((project / "infra").resolve())
    assert doc["module"] == {"address": "module.eks", "source": "terraform-aws-modules/eks/aws",
                             "version_constraint": "~> 21.0", "major": 21}
    assert doc["cluster_version"] == {"argument": "kubernetes_version", "variable": "eks_cluster_version",
                                      "value": "1.33", "set_in": str(project / "infra" / "terraform.tfvars")}
    assert doc["node_pools"] == ["MNG"] and doc["cluster_name"] == "demo-eks"
    assert doc["mng"]["ami_variable"] == "eks_node_ami_alias_al2023"
    assert doc["mng"]["target_release"] == "1.34.0-20261121"
    assert doc["service_candidates"] == [{"name": "web", "namespace": "shop", "via": "ingress",
                                          "ready_pods": 3, "pdb": "web-pdb", "min_endpoints": 2}]
    assert [(r["rule_id"], r["result"]) for r in audit] == [("DISCOVERY", "PASS")]


def test_trial_plan_is_lock_free_refreshing_and_never_applies(project, monkeypatch):
    fake = Fake(make_plan())
    run(monkeypatch, fake, "--auth-prefix", "aws-vault exec dev --", "--tf-var-file", "dev.tfvars")
    terraform = [c for c in fake.calls if "terraform" in c]
    assert all(c[:4] == ["aws-vault", "exec", "dev", "--"] for c in terraform)
    plan = next(c for c in terraform if "plan" in c)
    assert {"-lock=false", "-refresh=true", "-input=false", "-var-file=dev.tfvars"} <= set(plan)
    assert any(a.startswith("-out=") for a in plan)
    assert not any(word in c for c in terraform for word in ("apply", "init", "import", "state"))
    assert all(c[0] == "kubectl" for c in fake.calls if "kubectl" in c)


def test_v20_uses_cluster_version_argument(project, monkeypatch):
    code, doc, _ = run(monkeypatch, Fake(make_plan(major=20)))
    assert code == 0
    assert doc["cluster_version"]["argument"] == "cluster_version"
    assert doc["cluster_version"]["variable"] == "eks_cluster_version"


def test_karpenter_and_fargate_combo_gives_alias_variable_and_bottlerocket_target(project, monkeypatch):
    nodes = [node({"karpenter.sh/nodepool": "default"}),
             node({"eks.amazonaws.com/compute-type": "fargate"})]
    code, doc, _ = run(monkeypatch, Fake(make_plan(mng=False, karpenter=True, fargate=True), nodes=nodes))
    assert code == 0, doc["unknowns"]
    assert doc["node_pools"] == ["Karpenter", "Fargate"]
    assert doc["karpenter"]["node_classes"] == ["kubectl_manifest.node_class"]
    assert doc["karpenter"]["alias_variable"] == "node_alias"
    assert doc["karpenter"]["target_alias"] == "bottlerocket@1.47.0"


def test_mng_karpenter_fargate_all_present(project, monkeypatch):
    nodes = [node({"eks.amazonaws.com/nodegroup": "system"}), node({"karpenter.sh/nodepool": "default"}),
             node({"eks.amazonaws.com/compute-type": "fargate"})]
    code, doc, _ = run(monkeypatch, Fake(make_plan(karpenter=True, fargate=True), nodes=nodes))
    assert code == 0, doc["unknowns"]
    assert doc["node_pools"] == ["MNG", "Karpenter", "Fargate"]
    assert set(doc["cluster"]["running_pools"]) == {"MNG", "Karpenter", "Fargate"}


def test_plain_aws_eks_cluster_stops(project, monkeypatch, capsys):
    (project / "infra" / "eks.tf").write_text('resource "aws_eks_cluster" "this" {\n  name = "x"\n}\n')
    fake = Fake(make_plan())
    code, doc, audit = run(monkeypatch, fake)
    assert code == 1 and doc["status"] == "stop"
    assert "plain aws_eks_cluster" in capsys.readouterr().out
    assert fake.calls == [] and audit[0]["result"] == "FAIL"


def test_no_eks_module_stops_and_karpenter_submodule_alone_is_not_eks(project, monkeypatch):
    (project / "infra" / "eks.tf").write_text(
        'module "karpenter" {\n  source = "terraform-aws-modules/eks/aws//modules/karpenter"\n}\n')
    code, doc, _ = run(monkeypatch, Fake(make_plan()))
    assert code == 1 and "no Terraform root" in doc["unknowns"][0]["reason"]


def test_wrapper_module_is_walked_up_to_the_calling_root(project, monkeypatch):
    infra = project / "infra"
    (infra / "modules" / "cluster").mkdir(parents=True)
    (infra / "modules" / "cluster" / "main.tf").write_text(EKS_TF)
    (infra / "eks.tf").write_text('module "cluster" {\n  source = "./modules/cluster"\n}\n')
    (project / ".terraform").mkdir()
    (project / ".terraform" / "cached.tf").write_text(EKS_TF)
    assert discover.find_roots(project.resolve())[0] == [infra.resolve()]


def test_wrapper_variables_are_traced_to_the_root_variable():
    chain = [{"expressions": {"k8s": ref("var.cluster_k8s")}},
             {"expressions": {"kubernetes_version": ref("var.k8s")}}]
    assert discover.trace_variable(chain, "kubernetes_version") == ("cluster_k8s", "")
    chain[0]["expressions"]["k8s"] = ref("local.version")
    assert discover.trace_variable(chain, "kubernetes_version")[0] is None


@pytest.mark.parametrize("error,field", [
    ("Error: ExpiredToken: The security token included in the request is expired", "auth_prefix"),
    ("Error: No value for required variable\n  on variables.tf line 3", "tf_var_file"),
    ("Error: Module not installed\nRun terraform init", "terraform_init"),
])
def test_plan_failures_ask_the_right_question(project, monkeypatch, capsys, error, field):
    code, doc, audit = run(monkeypatch, Fake(make_plan(), plan_error=error))
    assert code == 2 and doc["status"] == "ask_user"
    assert doc["unknowns"][0]["field"] == field
    assert capsys.readouterr().out.strip().splitlines()[-1] == "NEXT: ASK_USER " + field
    assert audit[0]["result"] == "WARN"


def test_aws_auth_failure_on_describe_cluster_asks_for_auth_prefix(project, monkeypatch):
    fake = Fake(make_plan(), cluster_error="An error occurred (UnrecognizedClientException)")
    code, doc, _ = run(monkeypatch, fake)
    assert code == 2 and doc["unknowns"][-1]["field"] == "auth_prefix"


def test_other_plan_failure_stops(project, monkeypatch, capsys):
    code, _, _ = run(monkeypatch, Fake(make_plan(), plan_error="Error: Invalid reference"))
    assert code == 1 and capsys.readouterr().out.strip().endswith("Error: Invalid reference")


def test_undecidable_facts_become_unknowns_not_guesses(project, monkeypatch, capsys):
    nodes = [node({"eks.amazonaws.com/nodegroup": "system"}), node({"karpenter.sh/nodepool": "x"}, "arm64")]
    plan = make_plan(version_ref=None, mng_release=None)  # constant version, unpinned AMI
    code, doc, _ = run(monkeypatch, Fake(plan, nodes=nodes, ssm={}))
    fields = {u["field"] for u in doc["unknowns"]}
    assert code == 2
    assert {"cluster_version.variable", "mng.ami_variable", "mng.target_release", "node_pools"} <= fields
    assert doc["cluster_version"]["variable"] == "unknown" and doc["mng"]["ami_variable"] is None
    assert doc["mng"]["target_release"] is None
    assert capsys.readouterr().out.strip().splitlines()[-1].startswith("NEXT: ASK_USER ")


def test_al2023_karpenter_alias_target_is_unknown(project, monkeypatch):
    plan = make_plan(mng=False, karpenter=True, alias="al2023@v20250101",
                     variables={"node_alias": "al2023@v20250101"})
    code, doc, _ = run(monkeypatch, Fake(plan, nodes=[node({"karpenter.sh/nodepool": "default"})]))
    assert code == 2 and doc["karpenter"]["alias_variable"] == "node_alias"
    assert [u["field"] for u in doc["unknowns"]] == ["karpenter.target_alias"]


def test_two_variables_with_the_same_ami_value_are_ambiguous(project, monkeypatch):
    plan = make_plan(variables={"other_ami": "1.33.0-20261101"})
    code, doc, _ = run(monkeypatch, Fake(plan))
    assert code == 2 and doc["mng"]["ami_variable"] is None
    assert "2 variables" in doc["unknowns"][0]["reason"]


def test_several_eks_roots_ask_which_one(project, monkeypatch):
    (project / "other").mkdir()
    (project / "other" / "eks.tf").write_text(EKS_TF)
    code, doc, _ = run(monkeypatch, Fake(make_plan()))
    assert code == 2 and doc["unknowns"][0]["field"] == "tf_dir"


def test_target_defaults_to_live_cp_plus_one_and_ami_var_files_are_recorded(project, monkeypatch):
    fake = Fake(make_plan())
    monkeypatch.setattr(discover, "run_cmd", fake)
    monkeypatch.setattr(lib, "run_cmd", fake)
    assert discover.main(["--root", "."]) == 0
    doc = json.loads(open("discovery.json", encoding="utf-8").read())
    assert doc["target_version"] == "1.34" and doc["mng"]["target_release"] == "1.34.0-20261121"
    assert doc["mng"]["ami_variable_set_in"] == "default"


def test_missing_cli_exits_127(project, monkeypatch, capsys):
    monkeypatch.setattr(discover.shutil, "which", lambda tool: None if tool == "terraform" else "/bin/x")
    assert discover.main(["--target-version", "1.34"]) == 127
    assert "NEXT: STOP terraform" in capsys.readouterr().out


def mng_set_in(project, monkeypatch, plan=None):
    code, doc, _ = run(monkeypatch, Fake(plan or make_plan()))
    return code, doc


def test_f2_discovery_audit_line_records_sha256_and_cluster(project, monkeypatch):
    import hashlib
    _, doc, audit = run(monkeypatch, Fake(make_plan()))
    sha = hashlib.sha256((project / "discovery.json").read_bytes()).hexdigest()
    assert f"sha256={sha}" in audit[0]["detail"] and "cluster_name=demo-eks" in audit[0]["detail"]


@pytest.mark.parametrize("filename,content", [
    ("terraform.tfvars.json", '{"eks_node_ami_alias_al2023": "1.33.0-20261101"}'),
    ("prod.auto.tfvars.json", '{"eks_node_ami_alias_al2023": "1.33.0-20261101"}'),
    ("zz.auto.tfvars", 'eks_node_ami_alias_al2023="1.33.0-20261101"\n')])
def test_f3_json_and_compact_assignments_are_found_not_called_default(project, monkeypatch, filename, content):
    (project / "infra" / filename).write_text(content)
    code, doc = mng_set_in(project, monkeypatch)
    assert code == 0 and doc["mng"]["ami_variable_set_in"] == str(project / "infra" / filename)


def test_f3_auto_tfvars_json_beats_terraform_tfvars_json(project, monkeypatch):
    for name in ("terraform.tfvars.json", "a.auto.tfvars.json"):
        (project / "infra" / name).write_text('{"eks_node_ami_alias_al2023": "x"}')
    _, doc = mng_set_in(project, monkeypatch)
    assert doc["mng"]["ami_variable_set_in"].endswith("a.auto.tfvars.json")


def test_f3_tf_var_env_is_a_source(project, monkeypatch):
    monkeypatch.setenv("TF_VAR_eks_node_ami_alias_al2023", "1.33.0-20261101")
    _, doc = mng_set_in(project, monkeypatch)
    assert doc["mng"]["ami_variable_set_in"] == "env:TF_VAR_eks_node_ami_alias_al2023"


def test_f3_unlocatable_source_is_unknown_with_reason_never_default(project, monkeypatch):
    plan = make_plan()
    plan["configuration"]["root_module"]["variables"]["eks_node_ami_alias_al2023"]["default"] = "other"
    code, doc = mng_set_in(project, monkeypatch, plan)
    assert code == 2 and doc["mng"]["ami_variable_set_in"] == "unknown"
    assert any(u["field"] == "mng.ami_variable_set_in" and "differs from the declared default" in u["reason"]
               for u in doc["unknowns"])
    plan["configuration"]["root_module"]["variables"].pop("eks_node_ami_alias_al2023")
    _, doc = mng_set_in(project, monkeypatch, plan)
    assert doc["mng"]["ami_variable_set_in"] == "unknown"


def test_f3_unparseable_json_tfvars_is_unknown(project, monkeypatch):
    (project / "infra" / "terraform.tfvars.json").write_text("{broken")
    _, doc = mng_set_in(project, monkeypatch)
    assert doc["mng"]["ami_variable_set_in"] == "unknown"


@pytest.mark.parametrize("replacement", ["module.eks[0].", 'module.eks["main"].'])
def test_f4_counted_or_for_each_eks_module_resources_are_matched(project, monkeypatch, replacement):
    plan = json.loads(json.dumps(make_plan()).replace("module.eks.", json.dumps(replacement)[1:-1]))
    code, doc, _ = run(monkeypatch, Fake(plan))
    assert code == 0 and doc["node_pools"] == ["MNG"] and doc["cluster_name"] == "demo-eks"
    assert doc["mng"]["present"] and doc["mng"]["ami_variable"] == "eks_node_ami_alias_al2023"


def test_f4_a_module_with_a_longer_name_is_not_the_eks_module():
    assert discover.under("module.eks[0].aws_eks_cluster.this[0]", "module.eks")
    assert not discover.under("module.eks_other.aws_eks_cluster.this", "module.eks")
