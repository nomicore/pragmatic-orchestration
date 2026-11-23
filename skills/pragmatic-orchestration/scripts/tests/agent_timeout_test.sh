#!/bin/bash
# PORCH_AGENT_TIMEOUT: a slow agent is stopped and reported failed (exit 124)
# while the fast agent's answer is kept. Offline, fakes only, about 5 seconds.
set -euo pipefail
TESTS="$(cd "$(dirname "$0")" && pwd)"
PORCH="$TESTS/../porch"
FAKES="$TESTS/fakes"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

cat > "$TMP/slow-codex" <<'SH'
#!/bin/bash
sleep 60
echo LATE
SH
chmod +x "$TMP/slow-codex"

export PORCH_CONFIG="$TESTS/fixtures/test-config.json"
export PORCH_BIN_CODEX="$TMP/slow-codex"
export PORCH_BIN_CLAUDE="$FAKES/fake-claude"
export PORCH_SUPPRESS_SHELL_WARN=1
export PORCH_RUN_DIR="$TMP/run"
mkdir -p "$PORCH_RUN_DIR"

PASS=0; FAIL=0
check() { if eval "$2"; then echo "  PASS  $1"; PASS=$((PASS+1)); else echo "  FAIL  $1"; FAIL=$((FAIL+1)); fi; }

start=$(date +%s)
set +e
out=$(cd "$TMP" && PORCH_AGENT_TIMEOUT=2 "$PORCH" review ask --progress none --xml \
      -a codex,claude-code "Review this" 2>"$TMP/err")
rc=$?
set -e
elapsed=$(( $(date +%s) - start ))

check "returns long before the slow agent would finish (${elapsed}s)" '[[ $elapsed -lt 30 ]]'
check "partial exit code (2), got $rc" '[[ $rc -eq 2 ]]'
check "slow agent failed with exit 124" 'grep -q "id=\"codex\".*status=\"failed\" exit-code=\"124\"" <<<"$out"'
check "timeout named in the slow agent error" 'grep -q "PORCH_AGENT_TIMEOUT=2s" <<<"$out"'
check "fast agent kept its answer" 'grep -q "id=\"claude-code\".*status=\"ok\"" <<<"$out"'
check "no stray slow process left" '! pgrep -f "$TMP/slow-codex" >/dev/null'

set +e
out=$(cd "$TMP" && PORCH_BIN_CODEX="$FAKES/fake-codex" "$PORCH" review ask --progress none --xml \
      -a codex "Review this" 2>/dev/null)
set -e
check "without the variable nothing changes" 'grep -q "id=\"codex\".*status=\"ok\"" <<<"$out"'

echo "agent_timeout_test: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
