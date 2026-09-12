# 커널 최적화 실험 파이프라인

이 파이프라인은 Stage 1 실험을 `baseline → candidate → 정적 검토 → RNGD 검증 →
KEEP/REJECT → 공유 record` 순서로 추적한다. 자동 최적화기가 아니라, 한 가설의
근거와 결과를 잃지 않도록 실행 순서와 상태 전이를 강제하는 표준 라이브러리 기반
Python CLI다.

## 시작 전 준비

레포 루트에서 실행하며, 기존 Stage 1 도구와 fixture가 준비되어 있어야 한다.

```bash
cargo furiosa-opt --help
furiosa-schedule-viewer --help
furiosa-arena --version
test -f ref/fixtures.safetensors
```

Arena를 처음 사용할 때만 로그인한다.

```bash
furiosa-arena login
```

명령 목록은 다음과 같이 확인한다.

```bash
python3 pipeline/optimize.py --help
```

## 한 번의 실험 흐름

### 1. 실험 생성

한 실험에는 한 가지 candidate 가설만 기록한다.

```bash
python3 pipeline/optimize.py create \
  --kernel decoder_feedforward \
  --name down-project-copy \
  --hypothesis "project_down 중간 복사를 줄이면 TDMA cycle이 감소한다"
```

출력된 `<EXPERIMENT_ID>`를 이후 명령에 사용한다. 생성 시 Git HEAD, 초기 Git
상태와 제출 범위(`src/ops.rs`, `src/device/**`) fingerprint를 기록한다.

### 2. Baseline schedule 생성

코드를 수정하기 전에 실행한다.

```bash
python3 pipeline/optimize.py baseline <EXPERIMENT_ID>
```

성공하면 baseline source snapshot, base commit 대비 `baseline.patch`, compile 로그와
schedule이 보존되고 상태가 `BASELINE_READY`가 된다.

### 3. 한 가지 가설 구현 후 candidate 생성

`src/ops.rs` 또는 `src/device/**`를 수정한 뒤 실행한다.

```bash
python3 pipeline/optimize.py candidate <EXPERIMENT_ID>
```

성공하면 candidate snapshot, baseline 대비 `candidate.patch`, compile 로그와
schedule이 보존되고 상태가 `CANDIDATE_READY`가 된다. 성공한 candidate는 같은
실험에서 덮어쓰지 않는다. 추가 수정은 새 실험으로 기록한다.

### 4. Schedule을 사람이 검토

```bash
python3 pipeline/optimize.py analyze <EXPERIMENT_ID>
```

생성된 `schedule/analysis.md`를 schedule viewer와 함께 채운다. 다음 항목을 baseline과
candidate에서 같은 cycle 범위로 비교한다.

- makespan과 긴 직렬 구간
- Main/Sub/VE/Core 점유와 idle 구간
- compute와 TDMA/PDMA overlap
- source hotspot과 반복 패턴
- SRAM/VRF 압력 및 불필요한 copy
- 가설을 지지하거나 반박하는 관찰

`analyze`는 자동으로 개선을 주장하지 않는다. 사람이 수정한 파일도 덮어쓰지 않는다.
새 템플릿만 터미널에서 보려면 `--print`를 사용한다.

```bash
python3 pipeline/optimize.py analyze <EXPERIMENT_ID> --print
```

### 5. 정적 근거 승인

```bash
python3 pipeline/optimize.py approve-static <EXPERIMENT_ID> \
  --note "makespan과 TDMA 구간을 확인해 Arena 검증을 진행한다"
```

두 schedule hash, analysis 존재 여부, candidate snapshot과 현재 제출 소스 fingerprint가
모두 일치해야 `READY_FOR_ARENA`로 전환된다.

### 6. 명시적으로 Arena 실행

이 명령만 원격 Job을 새로 제출한다.

```bash
python3 pipeline/optimize.py arena <EXPERIMENT_ID>
```

내부적으로 `RNGD_JOB_NAME=<EXPERIMENT_ID>-a001 ./scripts/rngd_test.sh`를 실행한다.
Arena 실행 제한은 스크립트에서 70초로 고정되어 있다. Job 성공뿐 아니라 세 커널의
정확도 PASS와 세 cycle이 모두 파싱되어야 상태가 `ARENA_PASSED`가 된다.

Job ID를 얻은 뒤 로컬 대기가 끊겼다면 새 Job을 제출하지 말고 동기화한다.

```bash
python3 pipeline/optimize.py sync-arena <EXPERIMENT_ID>
```

terminal attempt를 다시 측정할 때만 다음을 사용한다.

```bash
python3 pipeline/optimize.py arena <EXPERIMENT_ID> --new-attempt
```

### 7. KEEP 또는 REJECT 결정

KEEP은 유효한 PASS attempt가 있을 때만 가능하다.

```bash
python3 pipeline/optimize.py decide <EXPERIMENT_ID> \
  --keep \
  --reason "세 커널 정확도 PASS, decoder_feedforward cycle 감소"
```

Arena 실패 또는 성능 회귀는 REJECT로 남긴다.

```bash
python3 pipeline/optimize.py decide <EXPERIMENT_ID> \
  --reject \
  --reason "정적 makespan은 감소했지만 RNGD cycle이 증가했다"
```

결정 명령은 소스와 Git 상태를 바꾸지 않는다.

### 8. 팀 공유 record 생성

```bash
python3 pipeline/optimize.py export <EXPERIMENT_ID>
```

최종 상태의 실험만 `pipeline/records/<EXPERIMENT_ID>/`로 export된다. 공유 record에는
다음 다섯 파일만 포함한다.

```text
README.md
manifest.json
baseline.patch
candidate.patch
REPRODUCE.md
```

raw schedule, 전체 source snapshot, compile/Arena 로그, fixture, binary, 환경변수와 절대
홈 경로는 포함하지 않는다. 기존 record와 byte 단위로 같을 때만 재실행을 허용하고,
팀원이 수정한 record는 덮어쓰지 않는다.

## 조회와 상태

```bash
python3 pipeline/optimize.py list
python3 pipeline/optimize.py show <EXPERIMENT_ID>
```

| 상태 | 의미 | 일반적인 다음 명령 |
|---|---|---|
| `CREATED` | 실험만 생성됨 | `baseline` |
| `BASELINE_READY` | baseline snapshot/schedule 검증됨 | 소스 수정 후 `candidate` |
| `CANDIDATE_READY` | candidate snapshot/schedule/patch 검증됨 | `analyze` |
| `READY_FOR_ARENA` | 정적 검토와 hash gate 통과 | `arena` |
| `ARENA_RUNNING` | Job ID를 확보했고 원격 결과 대기 중 | `sync-arena` |
| `ARENA_PASSED` | 세 커널 정확도와 cycle 모두 확인됨 | `decide` |
| `ARENA_FAILED` | terminal 실패 또는 정확도 실패 | `decide --reject` 또는 재측정 |
| `KEPT` / `REJECTED` | 최종 결정됨 | `export` |

실패한 compile과 Arena attempt는 지우지 않는다. 원인을 고친 뒤 같은 단계의 명령을
다시 실행하면 `attempt-002`처럼 새 기록이 생긴다. 성공한 candidate 뒤 소스가 다시
바뀌면 기존 실험을 재사용하지 말고 새 실험을 만든다.

## 저장 위치와 공유 경계

```text
target/pipeline/<KERNEL>/<EXPERIMENT_ID>/  # 로컬 원본, Git 제외
pipeline/records/<EXPERIMENT_ID>/         # 요약/patch/재현 문서, Git 공유
```

`manifest.json`은 현재 상태의 기계 판독 원본이고 `events.jsonl`은 append-only 감사
기록이다. 실험 `README.md`는 이 둘에서 다시 만들 수 있는 사람용 인덱스다.

## Arena와 리더보드 제출은 다르다

`furiosa-arena`는 개발 중 정확도와 실제 RNGD cycle을 확인한다. 최종 채점과
리더보드 반영은 별도 `moa-submitter`를 사용한다. 파이프라인은 안전상
`moa-submitter submit`을 자동 실행하지 않는다.

```bash
moa-submitter login
moa-submitter submit
moa-submitter status <SUBMISSION_ID>
moa-submitter log <SUBMISSION_ID>
```

운영 공지가 있으면 공개 문서보다 최신 운영 공지를 우선한다.

## 개발자 검증

```bash
python3 -m unittest discover -s pipeline/tests -v
python3 -m compileall -q pipeline
bash -n scripts/rngd_test.sh scripts/rngd/remote_entrypoint.sh
git diff --check
```

테스트는 가짜 compiler/Arena 경계를 사용하므로 실제 원격 Job을 제출하지 않는다.
