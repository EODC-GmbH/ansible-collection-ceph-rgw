#!/usr/bin/env bash
# converge_twice.sh against the in-memory cluster instead of a live one.
#
#   inmemory.sh [<case>...]
#
# The real roles run with connection=local, through a test-cluster file written per case
# whose fsid is the one in inmemory/ceph.conf. radosgw-admin and ceph on PATH are
# inmemory/bin, and rgw_tenants_endpoint is inmemory/endpoint.py; both answer from one
# state file as the release in FAKE_CEPH_RELEASE (default squid) does.
# FAKE_CEPH_PYTHON must name a Python with ansible-core and boto3 (default python3).
# Cases, all by default:
#   sts-enabled   rgw_sts already applied, then the four converge steps
#   greenfield    an empty cluster, then the four converge steps
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
mem="$here/inmemory"
export FAKE_CEPH_PYTHON=${FAKE_CEPH_PYTHON:-python3}
export FAKE_CEPH_RELEASE=${FAKE_CEPH_RELEASE:-squid}
if ! "$FAKE_CEPH_PYTHON" -c 'import ansible, boto3' 2>/dev/null; then
  echo "FAIL: $FAKE_CEPH_PYTHON lacks ansible-core or boto3; set FAKE_CEPH_PYTHON" >&2
  exit 2
fi
export PATH="$mem/bin:$PATH"
work=$(mktemp -d "${TMPDIR:-/tmp}/inmemory.XXXXXX")
endpoint_pid=""
trap '[[ -z "$endpoint_pid" ]] || kill "$endpoint_pid" 2>/dev/null' EXIT

seed() {
  case $1 in
    sts-enabled)
      ceph config set client.rgw rgw_sts_key inmemory-sts-key-0123
      ceph config set client.rgw rgw_s3_auth_use_sts true
      ;;
    greenfield) ;;
    *) echo "unknown case: $1" >&2; exit 2 ;;
  esac
}

run_case() {
  local name=$1 dir="$work/$1"
  mkdir -p "$dir"
  export FAKE_CEPH_STATE="$dir/state.json"
  seed "$name"
  "$FAKE_CEPH_PYTHON" "$mem/endpoint.py" "$dir/port" 2>"$dir/endpoint.log" &
  endpoint_pid=$!
  for _ in $(seq 50); do [[ -s "$dir/port" ]] && break; sleep 0.1; done
  echo "=== $name (state: $FAKE_CEPH_STATE)"
  local rc=0 runner
  runner=(ansible-playbook -i "$mem/inventory.yml" -e "@$mem/vars.yml"
    -e "rgw_tenants_endpoint=http://127.0.0.1:$(cat "$dir/port")")
  {
    echo "RGW_TEST_FSID=$(sed -n 's/^fsid = //p' "$mem/ceph.conf")"
    echo "RGW_TEST_CEPH_CONF=$(printf '%q' "$mem/ceph.conf")"
    echo "RGW_TEST_RUNNER=($(printf '%q ' "${runner[@]}"))"
  } >"$dir/test-cluster.env"
  "$here/converge_twice.sh" -c "$dir/test-cluster.env" || rc=$?
  kill "$endpoint_pid"
  endpoint_pid=""
  return "$rc"
}

cases=("$@")
[[ ${#cases[@]} -gt 0 ]] || cases=(sts-enabled greenfield)
failed=0
for name in "${cases[@]}"; do
  run_case "$name" || failed=1
done
exit "$failed"
