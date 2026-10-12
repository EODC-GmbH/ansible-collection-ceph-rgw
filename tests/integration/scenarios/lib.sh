# shellcheck shell=bash disable=SC2154 # the globals are set by scenarios.sh
# Sourced by scenarios.sh: cluster access and assertions, independent of the code under test.
# Uses tenant, dropin, tdir, work, repo, here and common from scenarios.sh.

# ---- cluster access ----------------------------------------------------------------------
ssh_() { ssh -o BatchMode=yes -o LogLevel=ERROR "$RGW_TEST_SSH" "$@"; }
runtime=""
image=""
# assert_ssh_host: stops unless RGW_TEST_SSH reads the test cluster's ceph.conf, since drift is
# injected there; then picks the container runtime and the ceph image.
assert_ssh_host() {
  local found
  found=$(ssh_ 'sudo cat /etc/ceph/ceph.conf' 2>/dev/null \
    | sed -n 's/^[[:space:]]*fsid[[:space:]]*=[[:space:]]*\([0-9a-f-]*\).*/\1/p' | head -1) \
    || true
  if [[ "$found" != "$RGW_TEST_FSID" ]]; then
    echo "FAIL: $RGW_TEST_SSH is in cluster '${found:-unknown}', not $RGW_TEST_FSID" >&2
    exit 2
  fi
  runtime=${RGW_TEST_RUNTIME:-$(ssh_ 'command -v docker >/dev/null && echo docker || echo podman')}
  # shellcheck disable=SC2016 # $(...) runs on the host
  image=${RGW_TEST_IMAGE:-$(ssh_ "sudo $runtime inspect --format '{{.Config.Image}}' \
    \$(sudo $runtime ps --format '{{.Names}}' | grep -m1 -- -mon)" 2>/dev/null)}
  [[ -n "$image" ]] || { echo "no mon container on $RGW_TEST_SSH; set RGW_TEST_IMAGE" >&2; exit 2; }
}
remote() { # remote <entrypoint> <args...>
  # shellcheck disable=SC2029 # meant to expand locally
  ssh_ "sudo $runtime run --rm --net=host -v /etc/ceph:/etc/ceph:ro \
    --entrypoint $1 $image $(printf '%q ' "${@:2}") </dev/null" 2>/dev/null
}
ra() { remote radosgw-admin "$@"; }
ceph_() { remote ceph "$@"; }
jq_() { python3 -c "import json, sys; d = json.load(sys.stdin); $1"; }
account() { sed -n 's/^  account_id: "\(RGW[0-9]*\)"/\1/p' "$dropin"; }
rgw_started() {
  ceph_ orch ps --daemon-type rgw --format json \
    | jq_ 'print(sorted(x.get("started", "") for x in d))'
}
first_key() {
  ra user info --uid "$1" \
    | jq_ 'k = d["keys"]; print(k[0]["access_key"], k[0]["secret_key"]) if k else print("")'
}
role_names() {
  ra role list --account-id "$1" | jq_ 'print(" ".join(sorted(r["RoleName"] for r in d)))'
}
trust_of() { # trust_of <role> <account>
  ra role get --role-name "$1" --account-id "$2" | jq_ 'print(d["AssumeRolePolicyDocument"])'
}

# ---- running and asserting ---------------------------------------------------------------
results=()
current=""
fail() {
  results+=("FAIL  $current: $*")
  printf '%s\n' "${results[@]}"
  echo "FAIL: $current: $*  (logs in $work)" >&2
  exit 1
}
pass() { results+=("PASS  $current${1:+: $1}"); echo "   ok  $1"; }
skip() { results+=("SKIP  $current: $1"); echo "   skip  $1"; }
first_error() { grep -m1 -E '^(fatal|failed):' "$work/$1.log" | cut -c1-300; }

# play <log> [args...]: converge.yml (PB=<playbook> for another) through the runner.
play() {
  local name=$1
  shift
  "${RGW_TEST_RUNNER[@]}" "${PB:-$here/converge.yml}" "${common[@]}" "$@" \
    </dev/null >"$work/$name.log" 2>&1
}
# local_play <log> <playbook> [args...]: a localhost generator playbook, no runner.
local_play() {
  local name=$1 pb=$2
  shift 2
  ansible-playbook "$repo/playbooks/$pb" -e "${pb%.yml}_output_dir=$tdir" "$@" \
    </dev/null >"$work/$name.log" 2>&1
}
changed() {
  awk '/ : ok=[0-9]+ +changed=/ {
         for (i = 1; i <= NF; i++) if ($i ~ /^changed=/) { split($i, kv, "="); n += kv[2] }
       } END { print n + 0 }' "$work/$1.log"
}
changed_tasks() {
  awk '/^TASK \[/ {t = $0} /^changed: / && !s[t]++ {printf "%s; ", t}' "$work/$1.log" \
    | cut -c1-600
}
expect_ok() { # expect_ok <log> [args...]
  local name=$1
  shift
  play "$name" "$@" || fail "$name exited non-zero: $(first_error "$name")"
}
expect_clean() { # expect_clean <log> [args...]
  expect_ok "$@"
  [[ $(changed "$1") -eq 0 ]] || fail "$1 reported changed=$(changed "$1"): $(changed_tasks "$1")"
  pass "$1 clean"
}
# The yaml callback wraps long messages and doubles a quote inside '...'; undo both.
log_has() { tr -s ' \n' ' ' <"$work/$1.log" | sed "s/''/'/g" | grep -qE "$2"; }
expect_fail() { # expect_fail <log> <message regex> [args...]
  local name=$1 pattern=$2
  shift 2
  if play "$name" "$@"; then fail "$name succeeded; expected a refusal matching /$pattern/"; fi
  log_has "$name" "$pattern" || fail "$name failed without /$pattern/: $(first_error "$name")"
  pass "$name refused: /$pattern/"
}
expect_local_fail() { # expect_local_fail <log> <message regex> <playbook> [args...]
  local name=$1 pattern=$2
  shift 2
  if local_play "$name" "$@"; then fail "$name succeeded; expected /$pattern/"; fi
  log_has "$name" "$pattern" || fail "$name failed without /$pattern/"
  pass "$name refused: /$pattern/"
}
expect_task_changed() { # expect_task_changed <log> <task-name regex>
  awk -v p="$2" '/^TASK \[/ {t = $0} /^changed: / && t ~ p {f = 1} END {exit !f}' \
    "$work/$1.log" || fail "$1: no change in a task matching /$2/; changed: $(changed_tasks "$1")"
}
dropin_edit() { # dropin_edit <python stmt on s>: rewrite the test drop-in text
  python3 - "$dropin" "$1" <<'EOF'
import sys
path, stmt = sys.argv[1], sys.argv[2]
s = open(path).read()
ns = {"s": s}
exec(stmt, ns)
assert ns["s"] != s, "edit did not apply"
open(path, "w").write(ns["s"])
EOF
}
