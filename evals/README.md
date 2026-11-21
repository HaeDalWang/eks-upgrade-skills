# 현재 skill regression baseline (M1)

현재 skill은 수정하지 않는다. 모든 시나리오는 `demo-eks`, 1.33 → 1.34이며,
stdlib shim과 JSON 시나리오를 사용한다.

| Scenario | Topology | 검증 대상 (현재 aws/terraform-eks/SKILL.md) |
|---|---|---|
| normal-karpenter-fargate | Karpenter + Fargate | 기존 정상 baseline |
| cp-409 | Karpenter | 2-3-a: accepted 409 → ACTIVE → gate 전 sync apply 1회 |
| addon-sequential | Karpenter | 3-1 / 3-2: 순차 UPDATING, nonzero gate 후 30초 대기·재실행 |
| mng-rolling | MNG | 4-0 … 4-6: DRAIN-P4 선행, AMI targeted rollout, Phase 5 skip |

각 JSON의 `topology`가 Terraform 복사본과 shim 구성을 결정하고, `assertions`가 채점 항목을
선택한다. `assertion_references`에 각 항목의 skill 근거를 둔다. Fargate 없는 시나리오에는
`fargate_restarted`를 적용하지 않는다. 승인 답변은 `승인`이며 FAIL gate 우회 답변은 없다.

```sh
python3 -m pytest tests/ evals/ -q                         # 빠른 검증, 모델 호출 없음
K8S_EVAL=1 python3 -m pytest evals -m eval_live -k cp-409 -q -s  # 유료, 선택한 시나리오 1회
# 또는 결과 경로를 직접 지정 (존재하지 않는 경로):
K8S_EVAL=1 python3 -m evals.runner --model claude-opus-5-5 --run-dir evals/runs/m1-01
```

`pytest`와 기존 test dependencies가 있는 Python을 사용한다(예: `PATH=<repo>/.venv/bin:$PATH`).
실행 전 `claude`가 PATH에 있어야 하며, OAuth token을
`~/.config/k8s-upgrade-eval/oauth-token`에 준비해야 한다. 토큰은 출력하거나
결과 디렉토리에 저장하지 않는다. 실패한 shim gap을 고친 뒤의 재실행까지 **최대 3회**다.

각 run은 임시 HOME, 일반화한 example Terraform 복사본, shim bin을 갖는다.
`install.sh --tool claude`로 현재 skill을 설치한다. 자식 환경은 HOME/PATH/TERM/LANG/
CLAUDE_CODE_OAUTH_TOKEN만 전달하고, 매 turn 전에 CLI 경로와 환경을 검사한다.
이는 PATH/환경 격리이며 OS 수준의 filesystem/network sandbox는 아니다.
Fake user는 tools/MCP를 끈 Haiku로 agent 메시지 끝 2500자에서 답안 ID만 선택한다.
`NONE`은 한 번 재선택하며 두 시도를 `user.jsonl`에 기록한다(첫 시도는 `RETRY`).
재시도 후 `NONE`/미등록 ID는 정해진 중단 안내문으로 기록한다. agent의 마지막 줄 `EVAL_DONE`, max_turns,
wall_seconds로 멈춘다. 모델 오류도 결과와 중단 사유를 남긴다.
API timeout·접속 실패·5xx·overloaded는 `infra_error`로 중단한다. grader는 기존 `turns/`도
검사하며, infra run의 `grade.json`은 skill assertion 대신 최상위 `stop_reason`,
`excluded_from_pass_rate: true`, `evidence`를 담는다. 집계는 이 run을 분모에서 제외해야 한다.
live pytest는 infra run을 skip한다.

`evals/runs/` 아래 결과: `calls.jsonl`, `state.json`, `turns/NN.jsonl`,
`fake-user/NN.jsonl`, `user.jsonl`, `audit.log` (프로젝트 원본의 복사),
`grade.json`, `summary.json`. 비용/토큰은 agent와 Haiku 양쪽의 stream result를 합산한다.
응답을 받지 못한 timeout 호출의 비용은 모름이며 합산되지 않는다.

채점은 CLI 호출, audit 기록, 실제 전송한 승인 답변만 사용한다. `calls.jsonl`의
`cp_status`/`cp_version`은 호출 전 상태다. Python wrapper는 실제 phase gate의 exit,
시작/종료 시각, audit 시작/종료 길이를 기록한다(원본 gate는 그대로 실행). 호출의 `audit_lines`는
호출 직전에 관측한 audit 길이로, 초 단위 audit timestamp의 동률을 피한다.
`effects`는 실제 apply가 바꾼 리소스 종류다. Phase 7 재검사로 앞 Phase 누락을
숨기지 않는다. drain 검사는 audit의 started/finished로 판단하며 프로세스 생존 자체의
증명은 아니다. FAIL은 skill 동작을 먼저 확인하고, shim unsupported가 있으면
harness gap을 수정한다. `fixtures/passing/`은 채점용 합성 기록이며 실제 측정값이 아니다.

모델 한계: 단일 CP/worker pool, poll 횟수 기반 전이, Fargate CoreDNS 재시작.
`cp-409`의 `cp_polls`와 순차 add-on의 `addon_polls`는 완료 ACTIVE 응답을 포함한 호출 수다.
정상 baseline은 기존대로 N회 UPDATING 후 다음 호출에서 ACTIVE이며, MNG `node_polls`는
N회 UPDATING 후 다음 describe-nodegroup에서 노드 교체와 ACTIVE를 반환한다.
MNG 복사본은 example의 AL2023 amiType을 유지하고 release_version에 시나리오 AMI 변수를 연결한다.
Phase 5는 CRD가 없으면 `phase_gate.py:gate_phase5`의 현재 동작대로 audit에 **PASS + SKIP detail**을 쓴다.
`fixtures/passing/expected-grade.json`은 확장 전 grader 결과이며 정상 fixture의 결과·evidence를 비교한다.
Terraform provider/state 엔진이나 Kubernetes scheduler 전체를 모사하지 않는다.
특히 현재 skill에서 NodeClass apply 전에 Phase 5 gate를 실행하면 이전 버전 노드를
관측하여 FAIL할 수 있다. harness는 이를 자동 보정하지 않는다.
실전 archive는 rule/result 형태만 참조했고 식별 정보는 복사하지 않았다.
