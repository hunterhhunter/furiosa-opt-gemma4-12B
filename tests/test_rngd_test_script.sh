#!/bin/bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEST_ROOT"' EXIT

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

prepare_fixture() {
    local case_root="$1"

    mkdir -p "$case_root/scripts/rngd" "$case_root/target/release/deps" "$case_root/ref" "$case_root/bin"
    cp "$REPO_ROOT/scripts/rngd_test.sh" "$case_root/scripts/rngd_test.sh"
    printf '#!/bin/sh\nexit 0\n' > "$case_root/scripts/rngd/remote_entrypoint.sh"
    printf '#!/bin/sh\nexit 0\n' > "$case_root/target/release/deps/test_kernels-fixture"
    chmod +x "$case_root/scripts/rngd_test.sh" "$case_root/target/release/deps/test_kernels-fixture"
    : > "$case_root/ref/fixtures.safetensors"
}

install_fake_arena() {
    local case_root="$1"

    printf '%s\n' \
        '#!/bin/sh' \
        'case "$1" in' \
        '    submit)' \
        '        printf "%s\n" "$@" > "$ARENA_CAPTURE"' \
        '        if [ "${ARENA_FAIL:-0}" = 1 ]; then' \
        '            echo "server rejected submission" >&2' \
        '            exit 1' \
        '        fi' \
        '        echo "submitted job 42"' \
        '        ;;' \
        '    status) echo '\''{"status":"queued"}'\'' ;;' \
        '    logs) exit 0 ;;' \
        'esac' \
        > "$case_root/bin/furiosa-arena"
    chmod +x "$case_root/bin/furiosa-arena"
}

test_submit_uses_server_maximum_timeout() {
    local case_root="$TEST_ROOT/timeout"
    local capture="$case_root/arena-args"
    prepare_fixture "$case_root"
    install_fake_arena "$case_root"

    PATH="$case_root/bin:$PATH" ARENA_CAPTURE="$capture" \
        "$case_root/scripts/rngd_test.sh" --no-build --no-wait >/dev/null

    awk '
        previous == "--timeout" { found = ($0 == "70") }
        { previous = $0 }
        END { exit(found ? 0 : 1) }
    ' "$capture" || fail "submission did not use the fixed 70-second server timeout"
}

test_submit_failure_is_printed() {
    local case_root="$TEST_ROOT/error"
    local capture="$case_root/arena-args"
    local output
    local status
    prepare_fixture "$case_root"
    install_fake_arena "$case_root"

    set +e
    output=$(PATH="$case_root/bin:$PATH" ARENA_CAPTURE="$capture" ARENA_FAIL=1 \
        "$case_root/scripts/rngd_test.sh" --no-build --no-wait 2>&1)
    status=$?
    set -e

    [ "$status" -ne 0 ] || fail "submission failure returned success"
    printf '%s\n' "$output" | grep -Fq 'server rejected submission' \
        || fail "submission failure hid the server error"
}

test_client_wait_timeout_is_independent() {
    local case_root="$TEST_ROOT/wait-timeout"
    local capture="$case_root/arena-args"
    local output
    local status
    prepare_fixture "$case_root"
    install_fake_arena "$case_root"

    set +e
    output=$(timeout 2s env PATH="$case_root/bin:$PATH" ARENA_CAPTURE="$capture" \
        RNGD_WAIT_TIMEOUT=0 "$case_root/scripts/rngd_test.sh" --no-build 2>&1)
    status=$?
    set -e

    [ "$status" -ne 124 ] || fail "RNGD_WAIT_TIMEOUT did not control the client wait deadline"
    printf '%s\n' "$output" | grep -Fq "after 0s" \
        || fail "client wait timeout was not reported independently"
}

test_submit_uses_server_maximum_timeout
test_submit_failure_is_printed
test_client_wait_timeout_is_independent
echo "PASS: rngd_test.sh behavior"
