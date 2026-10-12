# shellcheck shell=bash
# Sourced by converge_twice.sh, capture_rgw.sh and scenarios.sh.

# use_this_checkout <repo> <work>
# Builds <repo> into <work>/collections, puts it first on ANSIBLE_COLLECTIONS_PATH,
# and fails unless Ansible resolves underknowledge.ceph_rgw to that build.
use_this_checkout() {
  local repo=$1 work=$2 doc resolved
  ansible-galaxy collection build --force --output-path "$work" "$repo" >/dev/null
  ansible-galaxy collection install --force -p "$work/collections" \
    "$work"/underknowledge-ceph_rgw-*.tar.gz >/dev/null 2>&1
  local default_paths="$HOME/.ansible/collections:/usr/share/ansible/collections"
  export ANSIBLE_COLLECTIONS_PATH="$work/collections:${ANSIBLE_COLLECTIONS_PATH:-$default_paths}"
  export ANSIBLE_NOCOLOR=1 ANSIBLE_FORCE_COLOR=0

  # `collection list` prints paths in no precedence order, so ask the plugin loader.
  # check_identity exists only in this tree, so it also proves the code is this checkout's.
  doc=$(ansible-doc -j underknowledge.ceph_rgw.rgw_account_user 2>/dev/null)
  resolved=$(python3 -c 'import json, sys
doc = json.load(sys.stdin)["underknowledge.ceph_rgw.rgw_account_user"]["doc"]
print(doc["filename"] if "check_identity" in doc["options"] else "")' <<<"$doc")
  echo "collection under test: ${resolved%/plugins/*}"
  if [[ "$resolved" != "$work/collections/"* ]]; then
    echo "FAIL: underknowledge.ceph_rgw does not resolve to this checkout's build" >&2
    exit 1
  fi
}

# load_test_cluster [file]
# Forgets every RGW_TEST_* the shell exported, then sources the test-cluster file
# (default ${XDG_CONFIG_HOME:-~/.config}/ceph_rgw-tests/test-cluster.env).
# Only that file names the cluster, so a stale export cannot point a run elsewhere.
load_test_cluster() {
  local file=${1:-${XDG_CONFIG_HOME:-$HOME/.config}/ceph_rgw-tests/test-cluster.env}
  local name
  for name in "${!RGW_TEST_@}"; do unset "$name"; done
  if [[ ! -f "$file" ]]; then
    echo "no test-cluster file $file" >&2
    echo "copy tests/integration/test-cluster.env.example there and fill it in" >&2
    exit 2
  fi
  # shellcheck source-path=SCRIPTDIR source=test-cluster.env.example
  source "$file"
  if [[ ! "${RGW_TEST_FSID:-}" =~ ^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$ ]]; then
    echo "$file: RGW_TEST_FSID must be the test cluster's fsid" >&2
    exit 2
  fi
  if [[ "$(declare -p RGW_TEST_RUNNER 2>/dev/null)" != "declare -a"* ]]; then
    echo "$file: RGW_TEST_RUNNER must be an array, e.g. (kayobe playbook run)" >&2
    exit 2
  fi
  local want=${RGW_TEST_KAYOBE_ENVIRONMENT:-}
  if [[ -n "$want" && "${KAYOBE_ENVIRONMENT:-}" != "$want" ]]; then
    echo "KAYOBE_ENVIRONMENT is '${KAYOBE_ENVIRONMENT:-}', $file wants '$want'" >&2
    exit 2
  fi
}

# assert_test_cluster <work>
# Runs fsid.yml through RGW_TEST_RUNNER and stops unless the ceph.conf of every host
# it reaches names RGW_TEST_FSID. Call it before anything that changes the cluster.
# RGW_TEST_CEPH_CONF points fsid.yml at another ceph.conf (inmemory.sh only).
assert_test_cluster() {
  local work=$1
  local args=(-e "rgw_test_fsid=$RGW_TEST_FSID")
  [[ -z "${RGW_TEST_CEPH_CONF:-}" ]] || args+=(-e "rgw_test_ceph_conf=$RGW_TEST_CEPH_CONF")
  if ! "${RGW_TEST_RUNNER[@]}" "$(dirname "${BASH_SOURCE[0]}")/fsid.yml" "${args[@]}" \
      </dev/null >"$work/fsid.log" 2>&1; then
    grep -h '"msg"' "$work/fsid.log" >&2 || tail -n 20 "$work/fsid.log" >&2
    echo "FAIL: the runner does not reach test cluster $RGW_TEST_FSID; nothing changed" >&2
    exit 2
  fi
  echo "test cluster: $RGW_TEST_FSID"
}
