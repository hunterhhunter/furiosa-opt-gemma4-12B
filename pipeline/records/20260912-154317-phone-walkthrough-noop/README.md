# phone-walkthrough-noop

- Experiment: 20260912-154317-phone-walkthrough-noop
- Kernel: sliding_attention_output
- State: REJECTED
- Hypothesis: 파이프라인 전체 동작 확인을 위해 소스 변경 없이 baseline과 candidate를 비교한다

## Source

- Base commit: 668fe2320a0f6cb3dfb43f141ef3e0b8120318be
- Baseline fingerprint: 6847551043afb11a2e6cc7fabb709cabe08d2fb8ef68050bd7f6c4ce2a1672d6
- Candidate fingerprint: 6847551043afb11a2e6cc7fabb709cabe08d2fb8ef68050bd7f6c4ce2a1672d6
- Change: source/candidate.patch

## Static result

- Baseline schedule: schedule/baseline.json
- Candidate schedule: schedule/candidate.json
- Review: schedule/analysis.md

## RNGD result

- Attempt: 1
- Arena Job: 21512
- Accuracy: PASS
- sliding_project_qkv cycle: 246694
- sliding_attention_output cycle: 400339
- decoder_feedforward cycle: 3703243

## Decision

REJECT: 파이프라인 전체 기능 검증용 no-op 실험으로 baseline과 candidate가 동일하여 성능 개선 근거가 없음

## Timeline

- 2026-09-12T15:43:17.948635+09:00 CREATED (success)
- 2026-09-12T15:43:31.765987+09:00 BASELINE_COMPILE (success)
- 2026-09-12T15:43:45.237305+09:00 CANDIDATE_COMPILE (success)
- 2026-09-12T15:43:57.463302+09:00 ANALYSIS_CREATED (success)
- 2026-09-12T15:44:42.417570+09:00 STATIC_APPROVED (success)
- 2026-09-12T15:45:08.710511+09:00 ARENA_JOB_CAPTURED (success)
- 2026-09-12T15:45:08.710511+09:00 ARENA_ATTEMPT (failure)
- 2026-09-12T15:46:18.039377+09:00 ARENA_SYNC (success)
- 2026-09-12T15:47:06.294123+09:00 ARENA_SYNC (success)
- 2026-09-12T15:47:24.912175+09:00 DECISION_REJECT (success)
