#!/usr/bin/env bash
set -u; cd "$(dirname "$0")/.."
T=$(mktemp -d); fail=0
cat > "$T/hermes" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$@" > "$STUB_OUT"; [ "$1" = auth ] && { echo "logged in"; exit 0; }; cat >/dev/null
# Verbatim wording probed from the real hermes 2026-09-10 (v3-stock root, proxy down):
#   `hermes chat --continue no-such-session-xyz` without --create-if-missing -> exit 1.
[ -n "${STUB_NO_SESSION:-}" ] && { echo "No session found matching 'arena-Y'." >&2; exit 1; }
[ -n "${STUB_SESSION_LIMIT:-}" ] && { echo "session limit reached" >&2; exit 1; }
echo answer
EOF
cat > "$T/claude" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$@" > "$STUB_OUT"; cat >/dev/null
for a in "$@"; do
  [ "$a" = --resume ] && [ -n "${STUB_NO_SESSION:-}" ] && { echo "No conversation found with session ID" >&2; exit 1; }
  [ "$a" = --resume ] && [ -n "${STUB_SESSION_LIMIT:-}" ] && { echo "session limit reached" >&2; exit 1; }
done
echo answer
EOF
chmod +x "$T/hermes" "$T/claude"; W=$(mktemp -d)
check(){ if grep -qx -- "$2" "$STUB_OUT"; then echo "  ok    $1"; else echo "  FAIL  $1 (argv: $(tr '\n' ' ' < "$STUB_OUT"))"; fail=1; fi; }
absent(){ if grep -qx -- "$2" "$STUB_OUT"; then echo "  FAIL  $1"; fail=1; else echo "  ok    $1"; fi; }
export STUB_OUT="$T/argv"
# hermes-task: ohne Sitzung wie heute
echo task | HERMES_BIN="$T/hermes" bin/hermes-task "$W" >/dev/null; absent "hermes-task without session: no --continue" "--continue"
echo task | HERMES_BIN="$T/hermes" ARENA_SESSION_NAME=arena-X ARENA_SESSION_CONTINUE=0 bin/hermes-task "$W" >/dev/null
check "hermes-task round 1: --continue" "--continue"; check "hermes-task round 1: name" "arena-X"; check "hermes-task: --create-if-missing" "--create-if-missing"
echo task | HERMES_BIN="$T/hermes" HERMES_PROVIDER=anthropic HERMES_MODEL=m ARENA_SESSION_NAME=arena-Y ARENA_SESSION_CONTINUE=1 bin/hermes-task-model "$W" >/dev/null
check "hermes-task-model: --continue arena-Y" "arena-Y"
absent "hermes-task-model round 2: no --create-if-missing" "--create-if-missing"
# I2: a session lost between rounds must NOT be silently re-created. CONTINUE=1 drops
# --create-if-missing; hermes's own not-found text then maps to exit 9 (runner takes its one
# marked fresh-session fallback) while any other failure stays exit 8 (recorded, no retry).
echo task | HERMES_BIN="$T/hermes" ARENA_SESSION_NAME=arena-Y ARENA_SESSION_CONTINUE=1 bin/hermes-task "$W" >/dev/null
absent "hermes-task round 2: no --create-if-missing" "--create-if-missing"; check "hermes-task round 2: --continue" "--continue"
echo task | HERMES_BIN="$T/hermes" STUB_NO_SESSION=1 ARENA_SESSION_NAME=arena-Y ARENA_SESSION_CONTINUE=1 bin/hermes-task "$W" >/dev/null 2>&1; rc=$?
[ "$rc" = 9 ] && echo "  ok    hermes-task continue of missing session -> exit 9" || { echo "  FAIL  hermes-task missing session -> exit $rc (want 9)"; fail=1; }
echo task | HERMES_BIN="$T/hermes" STUB_SESSION_LIMIT=1 ARENA_SESSION_NAME=arena-Y ARENA_SESSION_CONTINUE=1 bin/hermes-task "$W" >/dev/null 2>&1; rc=$?
[ "$rc" = 8 ] && echo "  ok    hermes-task session limit (generic error) -> exit 8" || { echo "  FAIL  hermes-task session limit -> exit $rc (want 8)"; fail=1; }
echo task | HERMES_BIN="$T/hermes" STUB_NO_SESSION=1 ARENA_SESSION_NAME=arena-Y ARENA_SESSION_CONTINUE=0 bin/hermes-task "$W" >/dev/null 2>&1; rc=$?
[ "$rc" = 8 ] && echo "  ok    hermes-task round 1 not-found is not a fallback -> exit 8" || { echo "  FAIL  hermes-task round 1 not-found -> exit $rc (want 8)"; fail=1; }
echo task | HERMES_BIN="$T/hermes" STUB_NO_SESSION=1 HERMES_PROVIDER=anthropic HERMES_MODEL=m ARENA_SESSION_NAME=arena-Y ARENA_SESSION_CONTINUE=1 bin/hermes-task-model "$W" >/dev/null 2>&1; rc=$?
[ "$rc" = 9 ] && echo "  ok    hermes-task-model continue of missing session -> exit 9" || { echo "  FAIL  hermes-task-model missing session -> exit $rc (want 9)"; fail=1; }
# claude-code-task
echo task | CLAUDE_BIN="$T/claude" CC_MODEL=m bin/claude-code-task "$W" >/dev/null; absent "cc without session: no --session-id" "--session-id"; check "cc without session: --safe-mode" "--safe-mode"
echo task | CLAUDE_BIN="$T/claude" CC_MODEL=m ARENA_SESSION_ID=11111111-1111-1111-1111-111111111111 ARENA_SESSION_CONTINUE=0 bin/claude-code-task "$W" >/dev/null
check "cc round 1: --session-id" "--session-id"; absent "cc round 1: no --resume" "--resume"; check "cc round 1: --safe-mode" "--safe-mode"
echo task | CLAUDE_BIN="$T/claude" CC_MODEL=m ARENA_SESSION_ID=11111111-1111-1111-1111-111111111111 ARENA_SESSION_CONTINUE=1 bin/claude-code-task "$W" >/dev/null
check "cc round 2: --resume" "--resume"; absent "cc round 2: no --session-id" "--session-id"; check "cc round 2: --safe-mode" "--safe-mode"
echo task | CLAUDE_BIN="$T/claude" STUB_NO_SESSION=1 CC_MODEL=m ARENA_SESSION_ID=11111111-1111-1111-1111-111111111111 ARENA_SESSION_CONTINUE=1 bin/claude-code-task "$W" >/dev/null; rc=$?
[ "$rc" = 9 ] && echo "  ok    cc resume of missing session -> exit 9" || { echo "  FAIL  cc resume of missing session -> exit $rc (want 9)"; fail=1; }; check "cc resume of missing: --safe-mode" "--safe-mode"
echo task | CLAUDE_BIN="$T/claude" STUB_SESSION_LIMIT=1 CC_MODEL=m ARENA_SESSION_ID=11111111-1111-1111-1111-111111111111 ARENA_SESSION_CONTINUE=1 bin/claude-code-task "$W" >/dev/null; rc=$?
[ "$rc" = 8 ] && echo "  ok    cc session limit (generic error) -> exit 8" || { echo "  FAIL  cc session limit -> exit $rc (want 8)"; fail=1; }; check "cc session limit: --safe-mode" "--safe-mode"
rm -rf "$T" "$W"; [ "$fail" = 0 ] && echo WRAPPERS PASSED || { echo WRAPPERS FAILED; exit 1; }
