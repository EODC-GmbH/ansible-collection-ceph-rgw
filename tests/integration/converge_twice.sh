#!/usr/bin/env bash
# Converge-twice check of this checkout against a live cluster.
#
#   converge_twice.sh [-c test-cluster.env] [--check-only]
#
# The cluster and the runner come from the test-cluster file (see test-cluster.env.example);
# RGW_TEST_* in the shell are ignored. The run stops before step 1 unless the runner reaches
# the cluster named by RGW_TEST_FSID. The runner gets converge.yml plus each step's flags:
#   1. preview   --check --diff   must succeed; reports what step 2 will change
#   2. converge                   must succeed (this one changes the cluster)
#   3. converge                   must succeed and report changed=0
#   4. recheck   --check          must report changed=0
# --check-only stops after step 1.
#
# This checkout is built and installed into a scratch directory
# placed first on ANSIBLE_COLLECTIONS_PATH,
# so the run uses it over any copy the runner would find.
# Logs stay in that directory.
set -euo pipefail

usage() {
  sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'
  exit 2
}

check_only=false
config=""
while [[ $# -gt 0 ]]; do
  case $1 in
    -c) config=$2; shift 2 ;;
    --check-only) check_only=true; shift ;;
    *) usage ;;
  esac
done

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo=$(cd "$here/../.." && pwd)
playbook="$here/converge.yml"
work=$(mktemp -d "${TMPDIR:-/tmp}/converge-twice.XXXXXX")

# shellcheck source-path=SCRIPTDIR source=lib.sh
source "$here/lib.sh"
load_test_cluster "$config"
use_this_checkout "$repo" "$work"
assert_test_cluster "$work"

# Sum of changed= across every PLAY RECAP host line.
changed_total() {
  awk '/ : ok=[0-9]+ +changed=/ {
         for (i = 1; i <= NF; i++) if ($i ~ /^changed=/) { split($i, kv, "="); n += kv[2] }
       } END { print n + 0 }' "$1"
}

# The tasks and handlers that reported changed, once each.
changed_tasks() {
  awk '/^(TASK|RUNNING HANDLER) \[/ {task = $0}
       /^changed: / && !seen[task]++ {print "  " task}' "$1"
}

step() {
  local name=$1
  shift
  echo "== $name: $* $playbook"
  if ! "$@" "$playbook" "${extra[@]}" >"$work/$name.log" 2>&1; then
    tail -n 40 "$work/$name.log" >&2
    echo "FAIL: $name exited non-zero; full log in $work/$name.log" >&2
    exit 1
  fi
  echo "   changed=$(changed_total "$work/$name.log")  log: $work/$name.log"
}

require_no_change() {
  local name=$1 count
  count=$(changed_total "$work/$name.log")
  if [[ "$count" -ne 0 ]]; then
    echo "FAIL: $name reported changed=$count. Tasks that changed:" >&2
    changed_tasks "$work/$name.log" >&2
    exit 1
  fi
}

extra=(--check --diff)
step preview "${RGW_TEST_RUNNER[@]}"
changed_tasks "$work/preview.log"
if $check_only; then
  echo "check-only: stopping before the converge"
  exit 0
fi

extra=()
step converge "${RGW_TEST_RUNNER[@]}"
step second "${RGW_TEST_RUNNER[@]}"
require_no_change second

extra=(--check)
step recheck "${RGW_TEST_RUNNER[@]}"
require_no_change recheck

echo "PASS: second converge and the check after it reported no changes"
