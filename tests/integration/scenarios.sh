#!/usr/bin/env bash
# shellcheck disable=SC2329 # main calls the steps by name ("step_$s")
# Lifecycle scenarios of this checkout against a live test cluster.
#
#   scenarios.sh [-c test-cluster.env] [-s step,step...] [--keep]
#
# The cluster, the runner and every setting come from the test-cluster file
# (see test-cluster.env.example); RGW_TEST_* in the shell are ignored. Nothing runs unless
# both the runner's hosts and RGW_TEST_SSH belong to the cluster named by RGW_TEST_FSID.
#
# A throwaway tenant is generated with the operator playbooks, driven through drift, key and
# identity refusals, extra access_keys, OIDC and STS repair, the read-only modes and teardown,
# and asserted on exit codes, changed counts and messages. Drift is injected with
# radosgw-admin/ceph directly, never with the code under test. Steps run in order; the first
# failure stops the run.
#
# The real drop-ins are only read: the run uses a scratch tenants directory holding symlinks
# to them plus the test tenant, passed as -e rgw_tenants_dir. Every run is limited to the
# test tenant (rgw_tenants_limit); converge_twice.sh covers the whole estate.
#
# Steps: reset generate preview create drift nokey mismatch keys identity oidc sts readonly
#        federation teardown
# --keep stops before teardown and leaves the tenant for manual checks.
set -euo pipefail
trap 'echo "error: line $LINENO: $BASH_COMMAND" >&2' ERR

usage() {
  sed -n '3,22p' "$0" | sed 's/^# \{0,1\}//'
  exit 2
}

all_steps=(reset generate preview create drift nokey mismatch keys identity oidc sts readonly
  federation teardown)
steps=("${all_steps[@]}")
keep=false
config=""
while [[ $# -gt 0 ]]; do
  case $1 in
    -c) config=$2; shift 2 ;;
    -s) IFS=, read -r -a steps <<<"$2"; shift 2 ;;
    --keep) keep=true; shift ;;
    *) usage ;;
  esac
done

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo=$(cd "$here/../.." && pwd)
work=$(mktemp -d "${TMPDIR:-/tmp}/scenarios.XXXXXX")
# shellcheck source-path=SCRIPTDIR source=lib.sh
source "$here/lib.sh"
load_test_cluster "$config"

: "${RGW_TEST_SSH:?set RGW_TEST_SSH in the test-cluster file}"
tenants_src=${RGW_TEST_TENANTS_DIR:?set RGW_TEST_TENANTS_DIR in the test-cluster file}
[[ -d "$tenants_src" ]] || { echo "no drop-in directory $tenants_src" >&2; exit 2; }
tenant=${RGW_TEST_TENANT:-citest}
grant_root=${RGW_TEST_GRANT_ROOT:-/services/s3}

# Persistent, so -s can resume against the tenant an earlier run generated.
tdir=${RGW_TEST_STATE_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/ceph_rgw-scenarios}/tenants.d
dropin=$tdir/$tenant.yml
common=(-e "rgw_tenants_dir=$tdir" -e "rgw_tenants_discover_output_dir=$work/discover"
  -e "{\"rgw_tenants_limit\": [\"$tenant\"]}")

# shellcheck source-path=SCRIPTDIR source=scenarios/lib.sh
source "$here/scenarios/lib.sh"
# shellcheck source-path=SCRIPTDIR source=scenarios/steps_tenant.sh
source "$here/scenarios/steps_tenant.sh"
# shellcheck source-path=SCRIPTDIR source=scenarios/steps_keys.sh
source "$here/scenarios/steps_keys.sh"
# shellcheck source-path=SCRIPTDIR source=scenarios/steps_services.sh
source "$here/scenarios/steps_services.sh"

assert_ssh_host
use_this_checkout "$repo" "$work"
assert_test_cluster "$work"

mkdir -p "$tdir" "$work/discover"
find "$tdir" -maxdepth 1 -type l -delete
for f in "$tenants_src"/*.yml; do
  [[ "$(basename "$f")" == "$tenant.yml" ]] || ln -s "$f" "$tdir/"
done

main() {
  for s in "${steps[@]}"; do
    [[ " ${all_steps[*]} " == *" $s "* ]] || { echo "unknown step $s" >&2; exit 2; }
    if $keep && [[ $s == teardown ]]; then
      echo "--keep: leaving $tenant ($(account)); drop-in at $dropin"
      break
    fi
    current=$s
    echo "== $s"
    "step_$s"
  done
  printf '%s\n' "${results[@]}"
  echo "PASS: ${#results[@]} checks; logs in $work"
}
# main and exit on one line: bash has read it whole before main runs, so editing this file
# during a run cannot shift what bash reads next. The sourced files are read whole when sourced.
main; exit
