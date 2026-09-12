# Stage 1 커널 최적화 실행 가이드

이 문서는 Gemma-4-12B 커널 최적화 대회의 Stage 1을 처음 실행하는 팀원을 위한
한국어 빠른 시작 가이드다. 개발 환경 설치, 기준 성능 측정, schedule 분석, 최적화
검증, 최종 제출까지 한 흐름으로 설명한다.

팀 실험을 ID별로 남기고 baseline/candidate schedule, 정적 검토, Arena 결과,
KEEP/REJECT와 재현 patch를 한 사이클로 관리하려면
[커널 최적화 실험 파이프라인](../pipeline/README.md)을 함께 사용한다.

## 1. Stage 1에서 최적화하는 것

Stage 1 평가 대상은 `src/ops.rs`에 선언된 다음 세 커널이다.

| 커널 | 주요 연산 |
|---|---|
| `ops::sliding_project_qkv` | RMSNorm, Q/K/V projection, Q/K RMSNorm, RoPE, K/V cache write |
| `ops::sliding_attention_output` | head broadcast, O projection, RMSNorm, residual add |
| `ops::decoder_feedforward` | RMSNorm, NVFP4 GeGLU MLP, RMSNorm, residual add, layer gate |

정확도는 통과 조건이다. 출력 하나라도 허용 오차를 벗어나면 cycle이 줄어도 성능
점수를 받을 수 없다. 최종 점수는 세 커널의 baseline 대비 speedup을 기하평균한
값이다.

수정 내용 중 채점에 반영되는 범위는 다음과 같다.

```text
src/ops.rs
src/device/**
```

다음 계약은 유지해야 한다.

- `#[device]` 함수 이름, 인자, 타입, 반환 타입을 변경하지 않는다.
- `ops.rs`, `ops_vision.rs`, `ops_audio.rs`의 모듈 경로를 옮기지 않는다.
- `src/axes.rs`, `tests/`, fixture를 바꿔 평가 shape나 허용 오차를 변경하지 않는다.
- `Cargo.toml` 변경이나 새 외부 의존성에 기대지 않는다. 해당 파일은 제출되지 않는다.
- shared 코드를 수정하면 세 평가 커널을 모두 다시 검사한다.

## 2. 개발 환경 설치

권장 호스트 환경은 x86_64 Ubuntu 22.04 이상, GLIBC 2.34 이상이다.

```bash
sudo apt install build-essential libclang-dev
sudo apt install gcc-aarch64-linux-gnu
```

Rust가 없다면 공식 rustup으로 설치한다.

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source ~/.cargo/env
```

레포가 고정한 nightly와 개발 도구를 설치한다.

```bash
rustup toolchain install nightly-2026-05-01
cargo +nightly-2026-05-01 install cargo-binstall
cargo +nightly-2026-05-01 binstall cargo-furiosa-opt@0.6.0
cargo install furiosa-schedule-viewer
cargo binstall furiosa-arena-cli
cargo binstall moa-submitter-cli
```

설치를 확인한다.

```bash
rustc --version
cargo --version
cargo furiosa-opt --help
furiosa-schedule-viewer --help
furiosa-arena --version
moa-submitter --help
```

## 3. Reference fixture 생성

Fixture 생성에는 Python, NumPy, PyTorch, safetensors가 필요하다. 가상환경은 레포
밖에 만들면 작업 트리에 불필요한 파일이 생기지 않는다.

```bash
python3 -m venv ~/.venvs/furiosa-gemma4
source ~/.venvs/furiosa-gemma4/bin/activate
python -m pip install --upgrade pip
python -m pip install numpy torch safetensors
```

레포 루트에서 fixture를 한 번 생성한다.

```bash
python3 scripts/generate_references.py
ls -lh ref/fixtures.safetensors
```

Fixture는 원래 수치 의미를 고정하는 기준이다. 커널 구현을 바꿀 때마다 fixture를
수정하거나 기대 출력을 다시 정의하지 않는다. `ref/`는 Git과 최종 제출에서
제외된다.

## 4. Baseline schedule 생성

코드를 변경하기 전에 세 커널의 정적 schedule을 보관한다.

```bash
mkdir -p target/schedules

cargo furiosa-opt compile ops::sliding_project_qkv --exact \
  --dump-schedule target/schedules/sliding_project_qkv.baseline.json

cargo furiosa-opt compile ops::sliding_attention_output --exact \
  --dump-schedule target/schedules/sliding_attention_output.baseline.json

cargo furiosa-opt compile ops::decoder_feedforward --exact \
  --dump-schedule target/schedules/decoder_feedforward.baseline.json
```

`--exact`은 비슷한 이름의 다른 커널까지 선택되는 것을 방지한다.

Schedule viewer를 실행하고 생성한 JSON을 연다.

```bash
furiosa-schedule-viewer
```

각 커널에서 다음 항목을 기록한다.

1. 전체 makespan
2. `MainContext`, `SubContext`, `DmaEngine`별 점유 시간
3. critical path에서 가장 긴 node와 Rust source line
4. DMA node의 utilization
5. 긴 idle 구간의 dependency 또는 resource conflict

Schedule은 정적 실행 계획이다. 최종 성능 판단에는 실제 RNGD cycle이 필요하다.

## 5. Arena에서 baseline 실행

Arena는 개발 중인 커널을 원격 RNGD에서 시험하는 서비스다. 최종 대회 채점
제출에 사용하는 `moa-submitter`와는 별개다.

최초 한 번 로그인한다.

```bash
furiosa-arena login
```

레포 루트에서 테스트를 실행한다.

```bash
./scripts/rngd_test.sh | tee target/baseline-rngd.log
```

스크립트는 다음 작업을 자동 수행한다.

1. 세 평가 커널을 포함한 `test_kernels` 바이너리를 빌드한다.
2. 바이너리와 fixture, 원격 entrypoint를 임시 디렉터리에 준비한다.
3. Arena에 실행 제한 70초로 제출한다.
4. Job이 끝날 때까지 상태를 확인한다.
5. 정확도와 실제 RNGD cycle을 출력한다.

서버의 Job 실행 제한은 코드에서 70초로 고정되어 있다. 큐를 기다리는 로컬
시간은 별도로 기본 1,800초이며 필요하면 변경할 수 있다.

```bash
RNGD_WAIT_TIMEOUT=3600 ./scripts/rngd_test.sh
```

자주 사용하는 실행 옵션은 다음과 같다.

```bash
# 기존 바이너리를 재사용
./scripts/rngd_test.sh --no-build

# Job ID만 받고 별도로 확인
./scripts/rngd_test.sh --no-build --no-wait

# polling 주기를 10초로 변경
RNGD_POLL_SECONDS=10 ./scripts/rngd_test.sh --no-build

# 알아보기 쉬운 Job 이름 사용
RNGD_JOB_NAME=team-baseline ./scripts/rngd_test.sh --no-build
```

로컬 머신에 RNGD와 필요한 SDK가 준비돼 있다면 다음 경로도 사용할 수 있다.

```bash
./scripts/local_test.sh
```

## 6. Arena Job 확인

Job 이름과 Job ID는 다르다. 예를 들어 `rngd_test_7187`의 `7187`은 스크립트가
이름을 만들 때 붙인 임의 숫자이며 Job ID가 아니다.

정상 제출되면 다음과 같이 실제 숫자 ID가 출력된다.

```text
submitted job 18961
==> waiting on job 18961
```

Job 목록과 상태, 로그는 다음 명령으로 확인한다.

```bash
furiosa-arena list
furiosa-arena status 18961
furiosa-arena logs 18961 --follow
```

완료된 baseline 로그를 보관한다.

```bash
furiosa-arena logs 18961 > target/baseline-rngd-job-18961.log
```

## 7. 결과 읽는 법

정상 실행 예시는 다음과 같다.

```text
[sliding_project_qkv q] max|Δ|=0.03125 (1.45% of expected)
    mean|Δ|=0.000996 within tol=100.00% -> PASS
    cycles=250288
```

- `max|Δ|`: 모든 출력 원소 중 가장 큰 절대 오차
- `% of expected`: 최대 절대 오차가 발생한 위치에서 기준값 대비 상대 크기
- `mean|Δ|`: 모든 출력 원소의 평균 절대 오차
- `within tol`: 허용 오차를 통과한 원소의 비율
- `PASS`: 모든 원소가 허용 오차 이내라는 뜻
- `cycles`: 해당 커널 호출에서 수집한 실제 RNGD cycle 수. 낮을수록 좋다.

판정식은 다음과 같다.

```text
abs(expected - actual) <= atol + rtol * abs(expected)
```

공개 baseline 실행 Job 18961에서는 다음 결과를 얻었다. 장비와 evaluator 변경
여부를 확인하기 위해 팀별 첫 실행 결과를 별도로 보관한다.

| 커널 | Cycle | 결과 |
|---|---:|---|
| `sliding_project_qkv` | 250,288 | PASS |
| `sliding_attention_output` | 409,907 | PASS |
| `decoder_feedforward` | 3,704,175 | PASS |

원격 Job의 `duration_sec`는 프로세스 전체 실행 시간이다. 커널 성능 비교에는
`duration_sec`가 아니라 커널별 `cycles`를 사용한다.

## 8. 최적화 반복 절차

한 번에 커널 하나와 가설 하나만 변경한다.

```text
baseline 확보
→ 병목 가설 1개 선택
→ 구현 변경
→ 변경 커널 compile 및 candidate schedule 저장
→ 세 커널 정확도 검사
→ 실제 RNGD cycle 비교
→ 개선 시 유지, 아니면 원복
```

예를 들어 `sliding_attention_output`의 첫 candidate를 만들었다면 다음처럼
검증한다.

```bash
cargo fmt --check

cargo furiosa-opt compile ops::sliding_attention_output --exact \
  --dump-schedule target/schedules/sliding_attention_output.candidate01.json

RNGD_JOB_NAME=attention-output-candidate01 \
  ./scripts/rngd_test.sh | tee target/attention-output-candidate01.log
```

Candidate 판정표는 다음과 같다.

| 항목 | 조건 |
|---|---|
| 빌드 | 변경 커널 compile 성공 |
| 정확도 | 세 커널 모두 `within tol=100%`, `PASS` |
| 정적 성능 | candidate makespan이 baseline보다 감소 |
| 실제 성능 | Arena에서 대상 커널 cycle이 감소 |
| 회귀 | 변경하지 않은 커널의 cycle과 정확도가 악화되지 않음 |

Schedule이 빨라졌어도 실제 RNGD cycle이 줄지 않으면 최종 candidate로 채택하지
않는다. 가능하면 같은 candidate를 여러 번 실행해 결과가 재현되는지 확인한다.

## 9. 변경 전후 기록 양식

팀에서 다음 형식으로 실험을 기록하면 candidate 비교가 쉽다.

```text
candidate: attention-output-candidate01
git commit: <commit SHA>
hypothesis: FP8 decode intermediate를 제거하면 DM write/read가 감소한다.

baseline schedule:  <cycles>
candidate schedule: <cycles>

baseline RNGD:  409907
candidate RNGD: <cycles>
speedup:        409907 / <candidate cycles>

accuracy: PASS/FAIL
arena job: <job ID>
decision: keep/revert
```

실험 전후 변경 범위도 확인한다.

```bash
git status --short
git diff --check
git diff -- src/ops.rs src/device
```

## 10. 최종 채점 제출

Arena 테스트를 통과한 candidate만 최종 채점 서버에 제출한다.

```bash
moa-submitter login
moa-submitter submit
```

결과를 확인한다.

```bash
moa-submitter status
moa-submitter status <SUBMISSION_ID>
moa-submitter log <SUBMISSION_ID>
```

`moa-submitter status <SUBMISSION_ID>`는 커널별 cycle, 실패 원인, 최종 score를
보여준다. 최종 점수는 다음 기하평균이다.

```text
cuberoot(
    baseline_qkv  / candidate_qkv
  * baseline_out  / candidate_out
  * baseline_ffn  / candidate_ffn
)
```

한 커널의 큰 개선만큼 다른 커널의 회귀도 점수에 반영되므로 제출 전 세 커널을
항상 함께 실행한다.

## 11. 리더보드 등록과 확인

리더보드는 Arena 테스트 작업과 별개다. `furiosa-arena submit`은 원격 RNGD에서
정확도와 cycle을 확인하는 개발용 테스트이며, 이것만 실행해서는 최종 채점이나
리더보드 등록이 이루어지지 않는다.

공개된 `moa-submitter` CLI에는 별도의 `leaderboard register` 명령이 없다. 먼저
GitHub 계정으로 로그인한 뒤 최종 제출을 실행한다.

```bash
moa-submitter login
moa-submitter submit
```

제출 상태와 채점 결과는 다음 명령으로 확인한다.

```bash
moa-submitter status
moa-submitter status <SUBMISSION_ID>
moa-submitter log <SUBMISSION_ID>
```

공개 CLI 문서와 현재 명령 구조를 기준으로 보면, 채점에 성공해 최종 score가
생성된 제출이 리더보드에 자동 반영되는 방식으로 이해하면 된다. 즉, 별도 등록
절차보다 다음 조건을 만족하는 제출을 만드는 것이 중요하다.

- 세 커널이 모두 정확도 검사를 통과해야 한다.
- 각 커널의 cycle과 최종 score가 정상적으로 계산되어야 한다.
- 실패하거나 score가 생성되지 않은 제출은 유효한 리더보드 기록이 되지 않는다.
- 팀에서는 여러 번 제출할 수 있지만 리더보드에는 팀의 최고 점수만 표시된다.

따라서 제출 후에는 `moa-submitter status <SUBMISSION_ID>`에서 `SUCCEEDED` 여부와
최종 score를 먼저 확인하고, 대회 리더보드에서 팀 점수가 반영되었는지 확인한다.
공개 CLI 문서에는 리더보드 웹 주소나 팀 표시 이름을 설정하는 별도 명령이 안내되어
있지 않다. 성공한 제출의 점수가 보이지 않거나 팀 이름이 잘못 표시되면 제출 ID와
함께 운영진에게 문의한다.

> 참고: 여기서 “자동 반영”은 공개된 CLI 문서와 별도 등록 명령이 없는 현재 CLI
> 동작을 바탕으로 한 해석이다. 대회 운영 공지가 별도로 제공되면 해당 공지를
> 우선한다.

## 12. 문제 해결

### `furiosa-arena list`에 아무 Job도 보이지 않는다

`submitting rngd_test_XXXX`에서 종료되고 실제 `submitted job <ID>`가 나오지
않았다면 서버가 Job을 생성하지 않은 것이다. 현재 스크립트는 submit 실패 원문을
그대로 출력하므로 화면의 오류를 확인한다.

로그인을 다시 확인한다.

```bash
furiosa-arena login
furiosa-arena list
```

### `timeout_sec ... exceeds server maximum 70`

이전 스크립트가 서버 실행 제한보다 큰 1,800초를 전달할 때 발생하던 문제다.
현재 스크립트는 실행 제한을 70초로 고정하므로 최신 코드를 사용한다.

### `status`에서 소유권 오류가 발생한다

Job 이름의 접미사를 Job ID로 잘못 사용한 경우가 많다. `furiosa-arena list`에서
자신이 소유한 실제 숫자 ID를 확인한다.

### 원격 작업이 로컬 대기 시간을 넘긴다

Job은 계속 실행 중일 수 있다. 대기 시간을 늘리거나 `--no-wait`로 제출한다.

```bash
RNGD_WAIT_TIMEOUT=3600 ./scripts/rngd_test.sh --no-build

# 또는
./scripts/rngd_test.sh --no-build --no-wait
furiosa-arena list
```

### Fixture가 없다는 오류가 발생한다

Python 가상환경을 활성화하고 fixture를 생성한다.

```bash
source ~/.venvs/furiosa-gemma4/bin/activate
python3 scripts/generate_references.py
```

## 13. 팀원용 최소 체크리스트

- [ ] Rust nightly와 `cargo-furiosa-opt` 설치
- [ ] `furiosa-arena`, `moa-submitter`, schedule viewer 설치
- [ ] `ref/fixtures.safetensors` 생성
- [ ] 세 baseline schedule JSON 저장
- [ ] Arena baseline 실행 및 Job ID 기록
- [ ] 세 커널 모두 정확도 PASS 확인
- [ ] 커널별 baseline cycle 기록
- [ ] 한 candidate에서 한 가지 가설만 변경
- [ ] candidate schedule과 실제 RNGD cycle 모두 비교
- [ ] 세 커널 PASS 확인 후 `moa-submitter submit`
- [ ] 제출 상태와 최종 score 확인
- [ ] 대회 리더보드에서 팀 최고 점수 반영 확인

## 참고 자료

- [커널 최적화 실험 파이프라인](../pipeline/README.md)
- [Stage 1 최적화 절차](../OPTIMIZATION.md)
- [레포 구조](../ARCHITECTURE.md)
- [Furiosa Optimizer 공식 문서](https://developer.furiosa.ai/furiosa-opt/book/)
- [furiosa-arena-cli](https://github.com/kreatinj/furiosa-arena-cli)
- [moa-submitter-cli](https://github.com/micro2026-moa/moa-submitter-cli)
