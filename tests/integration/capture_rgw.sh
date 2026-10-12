#!/usr/bin/env bash
# Record a live cluster's answers as tests/unit/fixtures/<release>.json.
#
#   capture_rgw.sh [-c test-cluster.env]
#
# The cluster and the runner come from the test-cluster file (see test-cluster.env.example);
# RGW_TEST_* in the shell are ignored, and nothing runs unless the runner reaches the cluster
# named by RGW_TEST_FSID. The runner runs capture.yml, which creates the throwaway account
# RGW90000000000000001 (override with -e rgw_capture_account_id=...), records radosgw-admin,
# ceph and S3/IAM answers, and removes the account again.
# tests/unit/test_fake_matches_capture.py then checks the fake against the transcript.
set -euo pipefail

config=""
case "${1:-}" in
  "") ;;
  -c) config=${2:?-c needs a file} ;;
  *) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo=$(cd "$here/../.." && pwd)
work=$(mktemp -d "${TMPDIR:-/tmp}/capture-rgw.XXXXXX")

# shellcheck source-path=SCRIPTDIR source=lib.sh
source "$here/lib.sh"
load_test_cluster "$config"
use_this_checkout "$repo" "$work"
assert_test_cluster "$work"

if ! "${RGW_TEST_RUNNER[@]}" "$here/capture.yml" -e "rgw_capture_out=$repo/tests/unit/fixtures" \
    >"$work/capture.log" 2>&1; then
  tail -n 40 "$work/capture.log" >&2
  echo "FAIL: capture exited non-zero; full log in $work/capture.log" >&2
  exit 1
fi
git -C "$repo" status --short -- tests/unit/fixtures
echo "PASS: transcript written; log in $work/capture.log"
