# Reproduce 20260912-154317-phone-walkthrough-noop

Review both patches before applying them. These commands use a separate Git
worktree and do not change the current checkout.

```bash
git worktree add ../reproduce-20260912-154317-phone-walkthrough-noop 668fe2320a0f6cb3dfb43f141ef3e0b8120318be

if [ -s "$PWD/pipeline/records/20260912-154317-phone-walkthrough-noop/baseline.patch" ]; then
  git -C ../reproduce-20260912-154317-phone-walkthrough-noop apply --check "$PWD/pipeline/records/20260912-154317-phone-walkthrough-noop/baseline.patch"
  git -C ../reproduce-20260912-154317-phone-walkthrough-noop apply "$PWD/pipeline/records/20260912-154317-phone-walkthrough-noop/baseline.patch"
fi
if [ -s "$PWD/pipeline/records/20260912-154317-phone-walkthrough-noop/candidate.patch" ]; then
  git -C ../reproduce-20260912-154317-phone-walkthrough-noop apply --check "$PWD/pipeline/records/20260912-154317-phone-walkthrough-noop/candidate.patch"
  git -C ../reproduce-20260912-154317-phone-walkthrough-noop apply "$PWD/pipeline/records/20260912-154317-phone-walkthrough-noop/candidate.patch"
fi

cargo furiosa-opt compile ops::sliding_attention_output --exact \
  --manifest-path ../reproduce-20260912-154317-phone-walkthrough-noop/Cargo.toml \
  --dump-schedule ../reproduce-20260912-154317-phone-walkthrough-noop/target/reproduced.schedule.json
```

Tool versions should be captured when reproducing:

```bash
rustup show active-toolchain
cargo furiosa-opt --version
furiosa-arena --version
```

The recorded RNGD cycles and any new measurement are separate observations.
