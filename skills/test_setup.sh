#!/usr/bin/env bash
# Clean-install test for the skill's setup.py, as an engineer's first install would run it:
# an empty temporary HOME, a minimal PATH and no pip cache. It then checks the re-run, update
# and refusal paths, and writes everything to a log.
#
#   bash skills/test_setup.sh [--repo URL] [--ref TAG] [--keep] [--pip-cache DIR] [--python PY]
#
# --repo       where to clone from (default: setup.py's own, i.e. GitHub). Use a local bare clone
#              that carries the new tag to test a release before pushing it.
# --ref        the release to test (default: setup.py's PINNED_REF)
# --keep       keep the temporary folder (the log is always kept, next to it)
# --pip-cache  reuse a pip cache to iterate faster; the real clean test runs without one
# --python     the Python that runs setup.py (default: python3 on the minimal PATH; on macOS
#              that is the system's 3.9, which setup.py must cope with)
# macOS and Linux only; setup.py itself also runs on Windows (py -3), untested there.
set -euo pipefail

SETUP="$(cd "$(dirname "$0")" && pwd)/nfe-electrical-drawings/scripts/setup.py"
REPO_ARG=""; REF_ARG=""; KEEP=""; PIP_CACHE=""; BOOT_PY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --repo) REPO_ARG="$2"; shift 2 ;;
    --ref) REF_ARG="$2"; shift 2 ;;
    --keep) KEEP=yes; shift ;;
    --pip-cache) PIP_CACHE="$2"; shift 2 ;;
    --python) BOOT_PY="$2"; shift 2 ;;
    *) echo "usage: bash skills/test_setup.sh [--repo URL] [--ref TAG] [--keep] [--pip-cache DIR] [--python PY]" >&2; exit 1 ;;
  esac
done
REF="${REF_ARG:-$(sed -n 's/^PINNED_REF = "\([^"]*\)".*/\1/p' "$SETUP")}"
MIN_PATH="/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin"
BOOT_PY="${BOOT_PY:-$(PATH="$MIN_PATH" command -v python3)}"
T="$(mktemp -d "${TMPDIR:-/tmp}/nfe-setup-test.XXXXXX")"
LOG="$T.log"
mkdir -p "$T/home" "$T/tmp"
DEST="$T/home/nfe-electrical-toolkit"
FAILS=0

say() { echo "$*" | tee -a "$LOG"; }
# setup EXPECTED_EXIT EXPECTED_TEXT [ENV=VALUE ...] [--check]: run setup.py in the clean environment
setup() {
  local want="$1" text="$2" code=0 out
  shift 2
  local envs=() args=()
  for a in "$@"; do case "$a" in --*) args+=("$a") ;; *) envs+=("$a") ;; esac; done
  out="$(env -i HOME="$T/home" TMPDIR="$T/tmp" PATH="$MIN_PATH" \
    ${REPO_ARG:+NFE_TOOLKIT_REPO="$REPO_ARG"} ${REF_ARG:+NFE_TOOLKIT_REF="$REF_ARG"} \
    ${PIP_CACHE:+PIP_CACHE_DIR="$PIP_CACHE"} ${envs[@]+"${envs[@]}"} \
    "$BOOT_PY" -I "$SETUP" ${args[@]+"${args[@]}"} 2>&1)" || code=$?
  echo "$out" >> "$LOG"
  if [ "$code" = "$want" ] && grep -qF -- "$text" <<< "$out"; then
    say "PASS  exit $code, \"$text\""
  else
    say "FAIL  exit $code (wanted $want), wanted the text \"$text\""; FAILS=$((FAILS + 1))
  fi
}
step() { say ""; say "== $*"; }

say "setup.py clean-install test, $(date -u '+%Y-%m-%d %H:%M UTC')"
say "machine: $(uname -sm), $(sw_vers -productName 2>/dev/null || true) $(sw_vers -productVersion 2>/dev/null || true)"
say "setup.py sha256: $(shasum -a 256 "$SETUP" | cut -c1-16), run by: $BOOT_PY ($("$BOOT_PY" --version 2>&1)), release under test: $REF, repository: ${REPO_ARG:-setup.py default}"
say "temporary HOME: $T/home, pip cache: ${PIP_CACHE:-none (downloads everything)}"

step "1. --check before anything is installed"
setup 3 "STATUS: not installed" --check
step "2. first install from an empty HOME (clone, venv, pip install -e, synthetic tests)"
s=$(date +%s); setup 0 "RESULT: ready"; say "      took $(( $(date +%s) - s )) s"
step "3. --check after the install"
setup 0 "matches the pinned release" --check
step "4. second run: nothing to update"
setup 0 "already at"
step "5. an older checkout is updated to the pinned release"
pinned="$(git -C "$DEST" rev-parse HEAD)"
git -C "$DEST" -c advice.detachedHead=false checkout --quiet --detach HEAD~1
setup 3 "but this skill pins $REF" --check
setup 0 "updated to"
[ "$(git -C "$DEST" rev-parse HEAD)" = "$pinned" ] && say "PASS  back at $REF" || { say "FAIL  not at $REF"; FAILS=$((FAILS + 1)); }
step "6. local edits in the toolkit folder are never overwritten"
echo "# local edit" >> "$DEST/README.md"
setup 1 "has local changes"
git -C "$DEST" checkout --quiet -- README.md
step "7. a branch checked out (a development clone) is left alone"
git -C "$DEST" switch --quiet -c dev
setup 1 "has branch 'dev' checked out"
git -C "$DEST" -c advice.detachedHead=false checkout --quiet --detach "$pinned"; git -C "$DEST" branch --quiet -D dev
step "8. a release tag that changed upstream stops the update"
if git -C "$DEST" rev-parse -q --verify "refs/tags/$REF" >/dev/null; then
  orig="$(git -C "$DEST" rev-parse "refs/tags/$REF")"
  git -C "$DEST" update-ref "refs/tags/$REF" "$pinned~1"   # the local copy now differs from the remote's
  setup 1 "(would clobber existing tag)"   # git's own message, which setup.py must pass on
  git -C "$DEST" update-ref "refs/tags/$REF" "$orig"
else
  say "SKIP  $REF is not a tag"
fi
step "9. a work folder inside a git repository is refused"
git init --quiet "$T/project-repo"
setup 1 "blocked: move the work folder" NFE_WORKDIR="$T/project-repo/work" --check
step "10. the clean install still checks out"
setup 0 "matches the pinned release" --check

say ""
if [ "$FAILS" = 0 ]; then say "ALL PASSED"; else say "$FAILS FAILED"; fi
say "log: $LOG"
[ -n "$KEEP" ] || rm -rf "$T"
[ "$FAILS" = 0 ]
