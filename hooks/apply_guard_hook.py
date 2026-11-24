#!/usr/bin/env python3
"""PreToolUse saved-plan check. Only deny emits output; never execute shell input."""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sys

ROOT = Path(os.environ.get('CLAUDE_PLUGIN_ROOT', Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(ROOT / 'k8s-upgrade-skills/scripts'))


def apply_commands(command, cwd):
    """Recognize literal shell segments and shell -c; reject uncertain apply syntax."""
    lexer = shlex.shlex(command.replace('\\\n', '').replace('\n', ';'), posix=True, punctuation_chars=';&|()<>')
    lexer.whitespace_split = True
    tokens = list(lexer)
    segments, segment = [], []
    for token in tokens + [';']:
        if token and all(c in ';&|()<>' for c in token):
            if segment:
                segments.append((segment, token))
                segment = []
        else:
            segment.append(token)
    found = []
    for segment_index, (words, separator) in enumerate(segments):
        if 'cd' in words and (segment_index != 0 or words[0] != 'cd'):
            raise ValueError('only a plain leading cd DIR && is supported')
        if words[0] == 'cd':
            if len(words) != 2 or separator != '&&':
                raise ValueError('ambiguous cd before apply')
            cwd = (cwd / words[1]).resolve()
        for i, word in enumerate(words):
            if Path(word).name in ('sh', 'bash', 'zsh'):
                j = next((j for j in range(i + 1, len(words) - 1)
                          if re.fullmatch(r'-[a-zA-Z]*c[a-zA-Z]*', words[j])), None)
                if j is not None:
                    found.extend(apply_commands(words[j + 1], cwd))
            if Path(word).name != 'terraform':
                continue
            args, directory = words[i + 1:], cwd
            if args and args[0].startswith('-chdir='):
                directory = (directory / args[0].split('=', 1)[1]).resolve()
                args = args[1:]
            if args and args[0] == 'apply':
                found.append((directory, args[1:]))
    return found


def check_plan(directory, args, rows, fields, approval):
    # Only saved-plan apply: reject planning flags and all unknown options conservatively.
    options = {'-input=false', '-input=true', '-no-color', '-compact-warnings'}
    files = [a for a in args if not a.startswith('-')]
    if len(files) != 1 or any(a.startswith('-') and a not in options for a in args):
        raise ValueError('saved planfile required; apply planning/auto-approve flags forbidden')
    planfile = (directory / files[0]).expanduser().resolve()
    guards = [r for r in rows if r['rule_id'] == 'APPLY-GUARD'
              and fields(r['detail']).get('planfile') == str(planfile)]
    if not guards or guards[-1]['result'] != 'PASS':
        raise ValueError('missing last APPLY-GUARD PASS for ' + str(planfile))
    metadata = fields(guards[-1]['detail'])
    if any(approval.get(k) and metadata.get(g) != approval[k] for k, g in (
            ('plan_doc', 'plan_doc'), ('sha256', 'plan_sha256'),
            ('target_version', 'target_version'))):
        raise ValueError('APPLY-GUARD belongs to another approved plan/target')
    if metadata.get('sha256') != hashlib.sha256(planfile.read_bytes()).hexdigest():
        raise ValueError('APPLY-GUARD planfile sha256 changed: ' + str(planfile))
    return planfile


def deny(error, planfile, approval):
    command = 'python3 scripts/apply_guard.py --planfile ' + shlex.quote(str(planfile))
    for option, key in (('--plan-doc', 'plan_doc'), ('--cluster-name', 'cluster'),
                        ('--target-version', 'target_version')):
        command += ' ' + option + ' ' + shlex.quote(approval.get(key, 'unknown'))
    command += ' --audit-log audit.log'
    reason = 'APPLY-GUARD required: {}. NEXT: {}'.format(str(error).replace('\n', ' '), command)
    print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse',
          'permissionDecision': 'deny', 'permissionDecisionReason': reason}}))


def approved_context(rows, fields):
    approvals = [i for i, row in enumerate(rows) if row['rule_id'] == 'PLAN-APPROVED']
    if not approvals:
        return None, []
    boundary = approvals[-1]
    approval = fields(rows[boundary]['detail'])
    if approval.get('plan_doc'):
        from upgrade_plan import load
        _, doc, digest = load(approval['plan_doc'])
        if approval.get('sha256') != digest:
            raise ValueError('approved plan JSON changed; restore/reapprove before APPLY-GUARD')
        approval = dict(approval, cluster=doc['cluster'], target_version=doc['target_version'])
    scoped = rows[boundary + 1:]
    boundaries = [i for i, row in enumerate(scoped)
                  if row['rule_id'] in ('STEP-DONE', 'DEVIATION')
                  and row['result'] in ('PASS', 'SKIP')]
    return approval, scoped[boundaries[-1] + 1:] if boundaries else scoped


def checked_applies(command, cwd):
    candidate = bool(re.search(r'terraform\b[\s\S]*\bapply\b', command))
    try:
        applies = apply_commands(command, cwd)
    except ValueError:
        if candidate or 'apply' in command:
            raise
        return []
    if not applies and not candidate:
        return []
    if re.search(r'\b(if|then|elif|else|for|while|until|case|function|pushd|popd|eval|source)\b', command):
        raise ValueError('ambiguous shell control flow around terraform apply')
    if re.search(r'(?:^|\s)(?:--chdir(?:=|\s)|-C(?:\s|$)|-D(?:\s|$))', command):
        raise ValueError('ambiguous wrapper working directory around terraform apply')
    if re.search(r'\bcd\b', command) and re.search(r';|\|', command):
        raise ValueError('ambiguous conditional cwd around terraform apply')
    if any(c in command for c in ('$', '`', '<', '>', '(', ')', '*', '?', '[', '{', '~')):
        raise ValueError('write the apply with literal paths, no $VAR or ( ); '
                         'shell expansion/redirection is ambiguous')
    raw_applies = re.findall(r'\bterraform\b(?:(?!\bterraform\b)[\s\S])*?\bapply\b', command)
    if not applies or len(raw_applies) > len(applies):
        raise ValueError('cannot resolve every terraform apply command')
    return applies


def audit_path(cwd):
    """Find the active run even after Bash persists a cd into Terraform's directory."""
    home = Path.home().resolve()
    for directory in (cwd, *cwd.parents):
        candidate = directory / 'audit.log'
        if candidate.exists():
            return candidate
        if directory == home:
            break
    return None


def main():
    cwd, error, applies = Path.cwd(), None, []
    try:
        payload = json.load(sys.stdin)
        cwd = Path(payload.get('cwd', str(cwd))).resolve()
        if payload.get('tool_name') != 'Bash':
            return
        command = payload['tool_input']['command']
        if not isinstance(command, str):
            raise ValueError('tool_input.command must be a string')
        applies = checked_applies(command, cwd)
        if not applies:
            return
    except Exception as exc:
        error = exc
    active, approval, planfile = False, {}, 'unknown'
    try:
        from lib import parse_audit_line
        from upgrade_plan import fields
        audit = audit_path(cwd)
        if audit is None:
            return
        active = True
        rows = [r for line in audit.read_text().splitlines() for r in [parse_audit_line(line)] if r]
        if not any(r['rule_id'] == 'PLAN-APPROVED' for r in rows):
            active = False
            return
        if error:
            raise error
        approval, scoped = approved_context(rows, fields)
        for directory, args in applies:
            files = [a for a in args if not a.startswith('-')]
            planfile = (directory / files[-1]).resolve() if files else 'unknown'
            check_plan(directory, args, scoped, fields, approval)
    except Exception as exc:
        if active:
            deny(exc, planfile, approval)


if __name__ == '__main__':
    main()
