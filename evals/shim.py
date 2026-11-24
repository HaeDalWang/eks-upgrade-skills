#!/usr/bin/env python3
"""Stateful stdlib fake CLIs; never invokes an upstream cloud binary."""
import fcntl
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


def option(args, *names, default=None):
    for i, value in enumerate(args):
        for name in names:
            if value == name and i + 1 < len(args):
                return args[i + 1]
            if value.startswith(name + '='):
                return value.split('=', 1)[1]
    return default


def tick(state, key, finish, early=False):
    """early: the poll that reaches 0 already finishes the change but still reports in progress, so old
    nodes are never visible without an in-progress signal (node rollouts; real AWS/Karpenter keep it)."""
    remaining = state.get(key, 0)
    if remaining:
        state[key] = remaining - 1
        if early and remaining == 1:
            state.update(finish)
        return True
    state.update(finish)
    return False


def select(data, query):
    """Only projections actually used by the current skill, not general JMESPath."""
    if not query:
        return data
    query = query.strip()
    if query == 'Parameters | length(@)':
        return len(data['Parameters'])
    if query.startswith('Parameters[?ends_with(Name,') and query.endswith('].Value'):
        return [p['Value'] for p in data['Parameters'] if p['Name'].endswith('image_version')]
    prefix, brace, rest = query.partition('.{')
    if brace:
        obj = data[prefix]
        return {k.strip(): obj[v.strip()] for k, v in
                (pair.split(':') for pair in rest.rstrip('}').split(','))}
    head, bracket, tail = query.partition('.[')
    if bracket and tail.endswith(']'):
        return select(select(data, head), '[' + tail)
    if query.startswith('[') and query.endswith(']'):
        return [select(data, v.strip()) for v in query[1:-1].split(',')]
    if query.startswith('{') and query.endswith('}'):
        return {k.strip(): select(data, v.strip()) for k, v in
                (pair.split(':') for pair in query[1:-1].split(','))}
    value = data
    for part in query.split('.'):
        value = value[part.removesuffix('[]')]
    return value


def render(data, args):
    value = select(data, option(args, '--query'))
    if option(args, '--output') == 'text':
        if isinstance(value, list):
            return '\t'.join(str(v) for v in value)
        return str(value)
    return json.dumps(value)


def target_image(scenario):
    """Public SSM value behind the scenario's target AMI (alias without its family)."""
    return scenario['target_ami'].split('@')[-1]


def aws(args, state, scenario):
    service, operation = args[:2]
    if (service, operation) == ('sts', 'get-caller-identity'):
        return render({'Account': '111122223333', 'UserId': 'eval-user',
                       'Arn': 'arn:aws:iam::111122223333:user/eval'}, args), 0
    if (service, operation) == ('ssm', 'get-parameters-by-path'):
        path = option(args, '--path', default='/aws/service/eks')
        return render({'Parameters': [{'Name': path + '/image_version',
                                      'Value': target_image(scenario)}]}, args), 0
    if (service, operation) == ('ssm', 'get-parameter'):
        return render({'Parameter': {'Name': option(args, '--name'),
                                     'Value': target_image(scenario)}}, args), 0
    if (service, operation) == ('ec2', 'describe-subnets'):
        subnet_ids = []
        if '--subnet-ids' in args:
            for arg in args[args.index('--subnet-ids') + 1:]:
                if arg.startswith('-'):
                    break
                subnet_ids.append(arg)
        return render({'Subnets': [{'SubnetId': sid, 'AvailableIpAddressCount': 128}
                                   for sid in subnet_ids or ['subnet-demo']]}, args), 0
    if service != 'eks':
        raise ValueError('aws operation')
    return eks(operation, args, state, scenario)


def eks(operation, args, state, scenario):
    if operation == 'describe-cluster':
        updating = tick(state, 'cp_remaining', {'status': 'ACTIVE',
                        'version': state.get('cp_target', state['version'])})
        if not updating and state.pop('addons_pending', False):
            state['addon_queue'] = list(state['addons'])
            state['addons'][state['addon_queue'][0]]['polls'] = scenario['transitions']['addon_polls'] - 1
        cluster = {'name': scenario['cluster_name'], 'version': state['version'],
                   'status': 'UPDATING' if updating else state['status'],
                   'computeConfig': {'enabled': False},
                   'resourcesVpcConfig': {'subnetIds': ['subnet-cp-a', 'subnet-cp-b']},
                   'endpoint': 'https://eks.example.com', 'health': {'issues': []}}
        return render({'cluster': cluster}, args), 0
    if operation == 'list-addons':
        return render({'addons': list(state['addons'])}, args), 0
    if operation == 'describe-addon':
        return describe_addon(args, state, scenario)
    if operation == 'describe-addon-versions':
        return render({'addons': [{'addonName': option(args, '--addon-name'),
            'addonVersions': [{'addonVersion': 'v1.34.0-eksbuild.1',
            'compatibilities': [{'clusterVersion': scenario['target_version'],
                                  'defaultVersion': True}]}]}]}, args), 0
    if operation == 'list-nodegroups':
        return render({'nodegroups': ['demo-system'] if scenario['topology']['mng'] else []}, args), 0
    if operation == 'describe-nodegroup':
        # The node group reports its previous version until the update ends; nodes may already be new.
        version, release = state['node_version'], state.get('mng_release', state['ami_alias'])
        updating = mng_updating(state)
        if not updating:
            version, release = state['node_version'], state.get('mng_release', state['ami_alias'])
        return render({'nodegroup': {'nodegroupName': option(args, '--nodegroup-name'),
            'status': 'UPDATING' if updating else 'ACTIVE', 'version': version,
            'releaseVersion': release,
            'amiType': 'AL2023_x86_64_STANDARD', 'subnets': ['subnet-demo'], 'health': {'issues': []}}}, args), 0
    if operation == 'list-insights':
        return render({'insights': [{'id': 'upgrade-readiness', 'name': 'Upgrade readiness',
            'category': 'UPGRADE_READINESS', 'kubernetesVersion': scenario['target_version'],
            'insightStatus': {'status': 'PASSING'}}]}, args), 0
    if operation == 'list-updates':
        key = 'mng_remaining' if option(args, '--nodegroup-name') else 'cp_remaining'
        return render({'updateIds': ['eval-update'] if key in state else []}, args), 0
    if operation == 'describe-update':
        if option(args, '--nodegroup-name'):
            if 'mng_remaining' not in state or option(args, '--update-id') != 'eval-update':
                raise ValueError('unknown nodegroup update')
            updating = mng_updating(state)
        else:
            updating = bool(state.get('cp_remaining'))
        status = 'InProgress' if updating else 'Successful'
        return render({'update': {'id': 'eval-update', 'status': status, 'errors': []}}, args), 0
    if operation == 'list-fargate-profiles':
        return render({'fargateProfileNames': ['demo-system'] if scenario['topology']['fargate'] else []}, args), 0
    if operation == 'describe-fargate-profile':
        return render({'fargateProfile': {'fargateProfileName': 'demo-system',
            'status': 'ACTIVE', 'selectors': [{'namespace': 'kube-system'}]}}, args), 0
    raise ValueError('aws operation')


def mng_updating(state):
    return tick(state, 'mng_remaining', {
        'node_version': state.get('node_target', state['node_version']),
        'mng_release': state.get('mng_release_target', state['ami_alias'])}, early=True)


def describe_addon(args, state, scenario):
    name = option(args, '--addon-name')
    addon = state['addons'][name]
    queue = state.get('addon_queue', [])
    updating = addon['polls'] > 0
    addon['polls'] = max(0, addon['polls'] - 1)
    if queue and name == queue[0] and not updating:
        queue.pop(0)
        if queue:
            state['addons'][queue[0]]['polls'] = scenario['transitions']['addon_polls'] - 1
    return render({'addon': {'addonName': name, 'addonVersion': addon['version'],
                            'status': 'UPDATING' if updating else 'ACTIVE',
                            'health': {'issues': []}}}, args), 0


def node(name, version, labels, os_image):
    return {'apiVersion': 'v1', 'kind': 'Node', 'metadata': {'name': name, 'labels': {
        'topology.kubernetes.io/zone': 'ap-northeast-2a', **labels}}, 'spec': {},
        'status': {'nodeInfo': {'kubeletVersion': 'v' + version + '.0',
                                'architecture': 'amd64', 'osImage': os_image},
                   'conditions': [{'type': 'Ready', 'status': 'True'}],
                   'allocatable': {'cpu': '4', 'memory': '8Gi', 'pods': '110'}}}


def pod(name, namespace, node_name, app):
    return {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': name,
        'namespace': namespace, 'labels': {'app': app},
        'ownerReferences': [{'kind': 'ReplicaSet', 'name': app + '-rs'}]},
        'spec': {'nodeName': node_name, 'restartPolicy': 'Always',
            'containers': [{'name': app, 'resources': {'requests': {
                'cpu': '100m', 'memory': '128Mi'}}}]},
        'status': {'phase': 'Running', 'conditions': [{'type': 'Ready', 'status': 'True'}],
            'containerStatuses': [{'name': app, 'ready': True, 'restartCount': 0,
                                   'state': {'running': {}}}]}}


def deployment(name, namespace, image='example.com/demo:v1'):
    return {'apiVersion': 'apps/v1', 'kind': 'Deployment',
        'metadata': {'name': name, 'namespace': namespace,
                     'labels': {'app': name, 'app.kubernetes.io/name': name}},
        'spec': {'replicas': 2, 'template': {'spec': {'containers': [
            {'name': name, 'image': image}]}}},
        'status': {'readyReplicas': 2, 'availableReplicas': 2, 'updatedReplicas': 2}}


def worker_nodes(state):
    suffix = '-new' if state['node_version'] != state['initial_version'] else ''
    fg_suffix = '-new' if state['fargate_version'] != state['initial_version'] else ''
    topology = state['topology']
    labels = ({'karpenter.sh/nodepool': 'default'} if topology['karpenter'] else
              {'eks.amazonaws.com/nodegroup': 'demo-system', 'workload': 'system', 'nodegroup': 'system'})
    nodes = [node('worker-' + x + suffix, state['node_version'],
                  labels, 'Amazon Linux 2023' if topology['mng'] else 'Bottlerocket') for x in ('a', 'b')]
    if topology['fargate']:
        nodes += [node('fargate-system' + fg_suffix, state['fargate_version'],
                   {'eks.amazonaws.com' + '/compute-type': 'fargate'}, 'Amazon Linux 2')]
    return nodes


def resources(kind, state):
    topology = state['topology']
    nodes = worker_nodes(state)
    suffix = '-new' if state['node_version'] != state['initial_version'] else ''
    if kind == 'nodes':
        return nodes
    if kind == 'pods':
        return [pod('web-' + x, 'default', nodes[i]['metadata']['name'], 'web')
                for i, x in enumerate(('a', 'b'))] + [
            pod('coredns-' + x, 'kube-system', nodes[-1]['metadata']['name'], 'coredns')
            for x in ('a', 'b')]
    if kind == 'deployments':
        return [deployment('web', 'default'), deployment('coredns', 'kube-system')] + (
            [deployment('karpenter', 'karpenter', 'example.com/karpenter:1.8.3')]
            if topology['karpenter'] else [])
    if kind == 'pdb':
        blocked = state.get('pdb_remaining', 0) > 0
        state['pdb_remaining'] = max(0, state.get('pdb_remaining', 0) - 1)
        return [{'kind': 'PodDisruptionBudget', 'metadata': {'name': 'web',
            'namespace': 'default'}, 'spec': {'selector': {'matchLabels': {'app': 'web'}}}, 'status': {'disruptionsAllowed': 0 if blocked else 1, 'expectedPods': 2}}]
    if kind in ('crd', 'nodeclaims', 'nodepool', 'ec2nodeclasses') and not topology['karpenter']:
        return []
    if kind == 'crd':
        return [{'kind': 'CustomResourceDefinition', 'metadata': {'name': n}}
                for n in ('nodeclaims.karpenter.sh', 'nodepools.karpenter.sh',
                          'ec2nodeclasses.karpenter.k8s.aws')]
    if kind == 'nodeclaims':
        return [{'kind': 'NodeClaim', 'metadata': {'name': 'claim-' + x + suffix},
            'status': {'nodeName': nodes[i]['metadata']['name'], 'conditions': [
                {'type': 'Ready', 'status': 'True'},
                {'type': 'Drifted', 'status': 'True' if state.get('node_remaining', 0) > 0 else 'False'}]}}
            for i, x in enumerate(('a', 'b'))]
    if kind == 'nodepool':
        return [{'kind': 'NodePool', 'metadata': {'name': 'default'}, 'spec': {
            'disruption': {'budgets': [{'nodes': '1'}]}}}]
    if kind == 'ec2nodeclasses':
        return [{'kind': 'EC2NodeClass', 'metadata': {'name': 'default'},
                 'spec': {'amiSelectorTerms': [{'alias': state['ami_alias']}]}}]
    if kind == 'svc':
        return [{'kind': 'Service', 'metadata': {'name': 'web', 'namespace': 'default'},
                 'spec': {'selector': {'app': 'web'}, 'ports': [{'port': 80}]}}]
    if kind == 'endpointslices':
        return [{'kind': 'EndpointSlice', 'metadata': {'name': 'web', 'namespace': 'default',
                  'labels': {'kubernetes.io/service-name': 'web'}},
                 'endpoints': [{'conditions': {'ready': True}, 'addresses': ['192.0.2.' + str(i + 1)]}
                               for i in range(0 if tick(state, 'service_remaining', {}) else 2)]}]
    if kind in {'events', 'pv', 'statefulsets', 'daemonsets', 'jobs', 'ingresses'}:
        return []
    raise ValueError('kubectl resource')


ALIASES = {'no': 'nodes', 'node': 'nodes', 'po': 'pods', 'pod': 'pods',
    'deploy': 'deployments', 'deployment': 'deployments', 'pdbs': 'pdb',
    'poddisruptionbudgets': 'pdb', 'poddisruptionbudget': 'pdb',
    'persistentvolumes': 'pv', 'customresourcedefinitions': 'crd', 'crds': 'crd',
    'nodeclaim': 'nodeclaims', 'nodepools': 'nodepool', 'ec2nodeclass': 'ec2nodeclasses',
    'services': 'svc', 'service': 'svc', 'endpointslice': 'endpointslices',
    'statefulset': 'statefulsets', 'ds': 'daemonsets', 'ingress': 'ingresses'}


def filtered(items, args, name):
    ns = option(args, '-n', '--namespace')
    selector = option(args, '-l', '--selector')
    if ns:
        items = [i for i in items if i['metadata'].get('namespace') == ns]
    if name:
        items = [i for i in items if i['metadata']['name'] == name]
    if selector:
        for expression in selector.split(','):
            key, eq, value = expression.partition('=')
            items = [i for i in items if (i['metadata'].get('labels', {}).get(key) == value
                     if eq else key in i['metadata'].get('labels', {}))]
    return items


def jsonpath_range(path, items):
    # ponytail: items range, dotted fields and text literals only; use a parser for full JSONPath.
    match = re.fullmatch(r'\{range \.items\[\*\]\}((?:[^{}]|\{[^{}]*\})*)\{end\}', path)
    if not match:
        raise ValueError('kubectl jsonpath')
    tokens = re.split(r'(\{[^{}]*\})', match[1])
    parts = []
    for token in tokens:
        if token.startswith('{'):
            expression = token[1:-1]
            if expression in ('"\\n"', '" "'):
                parts.append(json.loads(expression))
            elif re.fullmatch(r'\.[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)*', expression):
                parts.append(expression[1:].split('.'))
            else:
                raise ValueError('kubectl jsonpath construct: ' + expression)
        else:
            parts.append(token)
    output = []
    for item in items:
        for part in parts:
            if isinstance(part, str):
                output.append(part)
                continue
            value = item
            for field in part:
                value = value[field]
            output.append(json.dumps(value) if isinstance(value, (dict, list)) else str(value))
    return ''.join(output)


def kformat(items, args, named):
    fmt = option(args, '-o', '--output', default='table')
    data = items[0] if named and items else {'apiVersion': 'v1', 'kind': 'List', 'items': items}
    if fmt in ('json', 'yaml'):
        # JSON is valid YAML 1.2; compact objects also support the skill's Drifted grep.
        return json.dumps(data, indent=2)
    if fmt == 'name':
        return '\n'.join(i['kind'].lower() + '/' + i['metadata']['name'] for i in items)
    if fmt.startswith('jsonpath='):
        path = fmt[len('jsonpath='):].strip("'\"")
        field = re.fullmatch(r'\{\.items\[\*\]((?:\.[A-Za-z_]\w*)+)\}', path)
        if field:
            values = []
            for item in items:
                for key in field[1][1:].split('.'):
                    item = item[key]
                values.append(str(item))
            return ' '.join(values)
        return jsonpath_range(path, items)
    if fmt not in ('table', 'wide') and not fmt.startswith('custom-columns='):
        raise ValueError('kubectl output')
    if not items and args[1] in ('events', 'event'):
        return 'No resources found.'
    labels = '--show-labels' in args
    rows = [] if '--no-headers' in args else ['NAME\tSTATUS\tVERSION\tNODE' + ('\tLABELS' if labels else '')]
    for item in items:
        status = item.get('status', {})
        row = [item['metadata']['name'], status.get('phase', 'Ready'),
               status.get('nodeInfo', {}).get('kubeletVersion', ''),
               item.get('spec', {}).get('nodeName', '')]
        if labels:
            row.append(','.join(f'{k}={v}' for k, v in item['metadata'].get('labels', {}).items()))
        rows.append('\t'.join(row))
    return '\n'.join(rows)


def kubectl_positionals(args):
    values, skip = [], False
    valued = {'-n', '--namespace', '-o', '--output', '-l', '--selector', '--field-selector',
              '-L', '--label-columns', '--sort-by'}
    for arg in args:
        if skip:
            skip = False
        elif arg in valued:
            skip = True
        elif arg.startswith('-'):
            if arg not in ('-A', '--all-namespaces', '--show-labels', '--no-headers') \
                    and arg.split('=')[0] not in valued:
                raise ValueError('kubectl option ' + arg)
        else:
            values.append(arg)
    if not values:
        raise ValueError('kubectl resource missing')
    return values


def kubectl(args, state, scenario):
    if args[:2] == ['config', 'current-context']:
        return scenario['cluster_name'], 0
    if args and args[0] == 'version':
        return json.dumps({'clientVersion': {'gitVersion': 'v1.34.0'},
                           'serverVersion': {'gitVersion': 'v' + state['version'] + '.0'}}), 0
    if args[:2] == ['top', 'nodes']:
        return 'NAME CPU(cores) CPU% MEMORY(bytes) MEMORY%\nworker-a 100m 3% 256Mi 4%', 0
    if args[:2] == ['top', 'pods']:
        return 'NAME CPU(cores) MEMORY(bytes)\nweb-a 20m 64Mi', 0
    if args[:2] == ['rollout', 'restart']:
        # Fargate-scheduled workloads in this scenario: coredns (kube-system), karpenter (karpenter).
        target = args[2].split('/')[-1] if len(args) >= 3 else ''
        if len(args) < 3 or args[2].split('/')[0] not in ('deployment', 'deploy') \
                or target not in ('coredns', 'karpenter'):
            raise ValueError('kubectl rollout target')
        namespace = option(args, '-n', '--namespace', default='default')
        if not any(d['metadata']['name'] == target and d['metadata']['namespace'] == namespace
                   for d in resources('deployments', state)):
            raise ValueError('kubectl rollout deployment not found')
        if not scenario['topology']['fargate'] or target != 'coredns':
            return f'deployment.apps/{target} restarted', 0
        state['fargate_version'] = state['version']
        state['fargate_restarts'] = state.get('fargate_restarts', 0) + 1
        state['last_effects'] = ['fargate_restart']
        return f'deployment.apps/{target} restarted', 0
    if args[:2] == ['rollout', 'status']:
        return 'deployment successfully rolled out', 0
    if args[:2] in (['delete', 'pod'], ['delete', 'pods']):
        if not any('coredns-' in a for a in args[2:]):
            raise ValueError('kubectl delete target')
        state['fargate_version'] = state['version']
        state['last_effects'] = ['fargate_restart']
        return 'pod deleted; replacement Running', 0
    if not args or args[0] != 'get':
        raise ValueError('kubectl operation')
    positionals = kubectl_positionals(args[1:])
    if ',' in positionals[0] and len(positionals) == 1:
        # ponytail: multi-resource get concatenates items (no per-kind table sections).
        kinds = [ALIASES.get(r, r) for r in positionals[0].split(',')]
        return kformat([i for k in kinds for i in filtered(resources(k, state), args, '')], args, False), 0
    resource, _, embedded_name = positionals[0].partition('/')
    kind = ALIASES.get(resource, resource)
    name = embedded_name or (positionals[1] if len(positionals) > 1 else '')
    if kind in ('nodeclaims', 'nodepool', 'ec2nodeclasses') and not scenario['topology']['karpenter']:
        return '', 1
    rolling = False
    # Only the NodeClaim read (the rollout signal) advances the Karpenter rollout; see tick(early=True).
    if kind == 'nodeclaims' and 'node_remaining' in state:
        rolling = tick(state, 'node_remaining', {'node_version': state['node_target']}, early=True)
    items = filtered(resources(kind, state), args, name)
    if kind == 'nodeclaims' and rolling:
        # The last in-progress poll decrements the counter to zero, but has not finished.
        for item in items:
            for condition in item['status']['conditions']:
                if condition['type'] == 'Drifted':
                    condition['status'] = 'True'
    if name and not items:
        return '', 1
    return kformat(items, args, bool(name)), 0


def desired(project):
    values = {}
    for path in sorted(project.glob('*.tfvars')):
        values.update(re.findall(r'^\s*(\w+)\s*=\s*"([^"\n]+)"', path.read_text(), re.M))
    return values


def node_class(alias):
    return {'yaml_body': json.dumps({'apiVersion': 'karpenter.k8s.aws/v1',
        'kind': 'EC2NodeClass', 'metadata': {'name': 'default'},
        'spec': {'amiSelectorTerms': [{'alias': alias}]}})}


MNG_ADDRESS = 'module.eks.module.eks_managed_node_group["system-node"].aws_eks_node_group.this[0]'


def configuration(topology):
    """configuration block of example/terraform-eks as generalize_project shapes it."""
    eks = {'kubernetes_version': {'references': ['var.eks_cluster_version']},
           'eks_managed_node_groups': {'references': ['var.eks_node_ami_alias_al2023']}
           if topology['mng'] else {'constant_value': {}}}
    if topology['fargate']:
        eks['fargate_profiles'] = {'constant_value': {'system': {
            'selectors': [{'namespace': 'kube-system'}]}}}
    calls = {'vpc': {'source': 'terraform-aws-modules/vpc/aws', 'version_constraint': '6.5.1',
                     'expressions': {}, 'module': {}},
             'eks': {'source': 'terraform-aws-modules/eks/aws', 'version_constraint': '21.10.1',
                     'expressions': eks, 'module': {}}}
    resources = []
    if topology['karpenter']:
        calls['karpenter'] = {'source': 'terraform-aws-modules/eks/aws//modules/karpenter',
                              'version_constraint': '21.10.1', 'expressions': {}, 'module': {}}
        resources.append({'address': 'kubectl_manifest.karpenter_default_node_class',
                          'mode': 'managed', 'type': 'kubectl_manifest', 'name': 'karpenter_default_node_class',
                          'expressions': {'yaml_body': {'references': ['var.eks_node_ami_alias_bottlerocket']}}})
    return {'root_module': {'module_calls': calls, 'resources': resources}}


def prior_state(state, scenario):
    """Applied resources as terraform show -json reports them (current, not desired)."""
    topology = state['topology']
    resources = [{'address': 'kubectl_manifest.karpenter_default_node_class', 'type': 'kubectl_manifest',
                  'values': node_class(state['ami_alias'])}] if topology['karpenter'] else []
    eks = [{'address': 'module.eks.aws_eks_cluster.this[0]', 'type': 'aws_eks_cluster',
            'values': {'name': scenario['cluster_name'], 'version': state['version']}}]
    if topology['fargate']:
        eks.append({'address': 'module.eks.module.fargate_profile["system"].aws_eks_fargate_profile.this[0]',
                    'type': 'aws_eks_fargate_profile', 'values': {'fargate_profile_name': 'system'}})
    children = [{'address': 'module.eks', 'resources': eks}]
    if topology['mng']:
        children[0]['child_modules'] = [{'address': 'module.eks.module.eks_managed_node_group["system-node"]',
            'resources': [{'address': MNG_ADDRESS, 'type': 'aws_eks_node_group', 'values': {
                'release_version': state['ami_alias'], 'ami_type': 'AL2023_x86_64_STANDARD',
                'version': state['node_version']}}]}]
    return {'format_version': '1.0', 'terraform_version': '1.13.0',
            'values': {'root_module': {'resources': resources, 'child_modules': children}}}


def plan(project, args, state, scenario):
    values = desired(project)
    changes = []
    targets = [v.split('=', 1)[1] for v in args if v.startswith('-target=')]
    version = values.get('eks_cluster_version', state['version'])
    if version != state.get('cp_target', state['version']) and (not targets or 'module.eks' in targets):
        changes.append({'address': 'module.eks.aws_eks_cluster.this[0]', 'type': 'aws_eks_cluster',
            'change': {'actions': ['update'], 'before': {'version': state['version']},
                       'after': {'version': version}}})
    alias_key = 'eks_node_ami_alias_al2023' if state['topology']['mng'] else 'eks_node_ami_alias_bottlerocket'
    alias = values.get(alias_key, state['ami_alias'])
    if state['topology']['mng'] and alias != state['ami_alias'] and (not targets or any(t.startswith('module.eks') for t in targets)):
        changes.append({'address': MNG_ADDRESS, 'type': 'aws_eks_node_group', 'change': {'actions': ['update'],
                'before': {'release_version': state['ami_alias'], 'version': state['node_version']},
                'after': {'release_version': alias, 'version': version}}})
    if state['topology']['karpenter'] and alias != state['ami_alias'] and (not targets or any('karpenter_default_node_class' in t for t in targets)):
        changes.append({'address': 'kubectl_manifest.karpenter_default_node_class',
            'type': 'kubectl_manifest', 'change': {'actions': ['update'],
                'before': node_class(state['ami_alias']), 'after': node_class(alias)}})
    if state.get('unexpected_replace') and any(c['type'] == 'aws_eks_cluster' for c in changes):
        changes.append({'address': 'aws_launch_template.workers', 'type': 'aws_launch_template',
            'change': {'actions': ['delete', 'create'], 'before': {'name': 'demo-old'},
                       'after': {'name': 'demo-new'}}})
    return {'format_version': '1.2', 'terraform_version': '1.13.0',
            'variables': {key: {'value': value} for key, value in values.items()},
            'configuration': configuration(state['topology']),
            'prior_state': prior_state(state, scenario), 'resource_changes': changes}


def apply_changes(changes, state, scenario):
    # ponytail: one CP and one worker pool; use per-resource transitions for multi-pool scenarios.
    transitions = scenario['transitions']
    effects = []
    for entry in changes:
        after = entry['change']['after']
        if entry['type'] == 'aws_eks_cluster':
            state.update(cp_target=after['version'], status='UPDATING',
                         cp_remaining=transitions['cp_polls'] - int(transitions.get('cp_409', False)))
            if transitions.get('addon_sequential'):
                state['addons_pending'] = True
            else:
                for addon in state['addons'].values():
                    addon['polls'] = transitions['addon_polls']
            effects.append('control_plane')
        elif entry['type'] == 'aws_eks_node_group':
            state.setdefault('mng_release', state['ami_alias'])
            state['mng_release_target'] = after['release_version']
            state.update(ami_alias=after['release_version'], node_target=state['version'],
                         mng_remaining=transitions['node_polls'])
            effects.extend(['node_replacement', 'mng_rollout'])
        elif entry['type'] == 'kubectl_manifest':
            manifest = json.loads(after['yaml_body'])
            alias = manifest['spec']['amiSelectorTerms'][0]['alias']
            state.update(ami_alias=alias, node_target=state['version'],
                         node_remaining=transitions['node_polls'])
            state['service_remaining'] = transitions.get('service_polls', 0)
            effects.append('node_replacement')
        elif entry['type'] == 'aws_launch_template':
            state['launch_template_name'] = after['name']
            effects.append('dangerous_replace')
        else:
            raise ValueError('terraform resource type: ' + entry['type'])
    state['last_effects'] = effects


def cp_conflict():
    return ('Error: updating EKS Cluster demo-eks: ResourceInUseException: HTTP 409: '
            'Cannot VersionUpdate because cluster demo-eks currently has update eval-update in progress')


def plan_text(data):
    lines = [(f"  # {c['address']} must be replaced\n-/+ resource \"{c['type']}\" "
              if c['change']['actions'] == ['delete', 'create'] else
              f"  # {c['address']} will be updated in-place\n  ~ {c['type']} ") +
             json.dumps(c['change']) for c in data['resource_changes']]
    replaces = sum(c['change']['actions'] == ['delete', 'create'] for c in data['resource_changes'])
    text = '\n'.join(lines) + f"\nPlan: {replaces} to add, {len(lines) - replaces} to change, {replaces} to destroy."
    if not lines:
        text = 'No changes. Your infrastructure matches the configuration.'
    return text


def terraform(args, state, scenario, project):
    operation = args[0]
    if operation in ('version', '-version', '--version'):
        return (json.dumps({'terraform_version': '1.13.0'}) if '-json' in args
                else 'Terraform v1.13.0'), 0
    if operation == 'init':
        return 'Terraform has been successfully initialized!', 0
    if operation == 'plan':
        data = plan(project, args, state, scenario)
        out = option(args, '-out')
        if out:
            (project / out).write_text(json.dumps(data))
        state['last_plan'] = data
        lines = data['resource_changes']
        text = plan_text(data)
        return text, 2 if lines and '-detailed-exitcode' in args else 0
    if args[:2] == ['state', 'list']:
        addresses = ['module.eks.aws_eks_cluster.this[0]']
        if scenario['topology']['mng']:
            addresses.append('module.eks.module.eks_managed_node_group["system-node"].aws_eks_node_group.this[0]')
        if scenario['topology']['karpenter']:
            addresses.append('kubectl_manifest.karpenter_default_node_class')
        if state.get('unexpected_replace'):
            addresses.append('aws_launch_template.workers')
        return '\n'.join(addresses), 0
    if operation == 'show':
        paths = [v for v in args[1:] if not v.startswith('-')]
        data = json.loads((project / paths[0]).read_text()) if paths else state.get('last_plan', {})
        return (json.dumps(data) if '-json' in args else plan_text(data)), 0
    if operation == 'apply':
        if scenario['transitions'].get('cp_409') and state['status'] == 'UPDATING':
            state['last_error'] = cp_conflict()
            return '', 1
        paths = [v for v in args[1:] if not v.startswith('-')]
        data = json.loads((project / paths[0]).read_text()) if paths else plan(project, args, state, scenario)
        state['last_plan'] = data
        changes = data['resource_changes']
        # Saved CP plans also become a no-op once EKS accepted the requested version.
        changes = [c for c in changes if c['type'] != 'aws_eks_cluster' or
                   c['change']['after']['version'] != state.get('cp_target', state['version'])]
        apply_changes(changes, state, scenario)
        if scenario['transitions'].get('cp_409') and 'control_plane' in state['last_effects']:
            state['last_error'] = cp_conflict()
            return '', 1
        if scenario['transitions'].get('cp_409') and not changes and state.get('cp_target') == state['version']:
            state['last_effects'].append('cp_sync')
        return f"Apply complete! Resources: 0 added, {len(data['resource_changes'])} changed, 0 destroyed.", 0
    raise ValueError('terraform operation')


def dispatch(tool, args, state, scenario):
    if tool == 'aws':
        return aws(args, state, scenario)
    if tool == 'kubectl':
        return kubectl(args, state, scenario)
    if tool == 'terraform':
        project = Path(option(args, '-chdir', default=os.getcwd()))
        return terraform([a for a in args if not a.startswith('-chdir=')], state, scenario, project)
    raise ValueError('tool')


def main():
    run = Path(sys.argv[0]).absolute().parent.parent
    tool, args = Path(sys.argv[0]).name, sys.argv[1:]
    with (run / 'state.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads((run / 'state.json').read_text())
        scenario = json.loads((run / 'scenario.json').read_text())
        audit = run / 'project' / 'audit.log'
        audit_lines = len(audit.read_text().splitlines()) if audit.exists() else 0
        cp_status, cp_version = state['status'], state['version']
        state['last_effects'] = []
        state['last_error'] = ''
        error = ''
        try:
            output, code = dispatch(tool, args, state, scenario)
            if code:
                error = state['last_error'] or 'shim: resource not found'
        except (ValueError, KeyError, IndexError, OSError) as exc:
            output, code, error = '', 1, 'shim: unsupported: ' + str(exc)
        (run / 'state.json').write_text(json.dumps(state, indent=2))
        entry = {'ts': datetime.now(timezone.utc).isoformat(), 'tool': tool, 'argv': args,
                 'cwd': os.getcwd(), 'exit': code, 'stderr': error,
                 'cp_status': cp_status, 'cp_version': cp_version,
                 'audit_lines': audit_lines, 'effects': state['last_effects']}
        if tool == 'terraform' and args and args[0] in ('plan', 'apply'):
            entry['plan_changes'] = state.get('last_plan', {}).get('resource_changes', [])
        with (run / 'calls.jsonl').open('a') as log:
            log.write(json.dumps(entry) + '\n')
        print(output)
        if error:
            print(error, file=sys.stderr)
    return code


if __name__ == '__main__':
    sys.exit(main())
