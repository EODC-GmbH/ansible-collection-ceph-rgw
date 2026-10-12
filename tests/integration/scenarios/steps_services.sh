# shellcheck shell=bash disable=SC2154 # the globals are set by scenarios.sh
# Sourced by scenarios.sh: OIDC, STS, the read-only modes, federation and teardown.

step_oidc() {
  local before
  before=$(rgw_started)
  PB=$here/oidc_drop.yml play oidc-drop -e "scenario_dropin=$dropin" -e "scenario_tenant=$tenant" \
    || fail "dropping the provider failed"
  expect_task_changed oidc-drop "Remove the provider"
  expect_ok oidc-repair
  expect_task_changed oidc-repair "Register the OIDC provider"
  expect_clean oidc-second
  [[ "$(rgw_started)" == "$before" ]] || fail "RGW restarted for an OIDC change"
  pass "provider recreated without an RGW restart"
}

step_sts() {
  local before
  before=$(rgw_started)
  ceph_ config set client.rgw rgw_s3_auth_use_sts false
  expect_ok sts-repair
  expect_task_changed sts-repair "Enable STS auth"
  grep -q "RUNNING HANDLER \[underknowledge.ceph_rgw.rgw_sts : Restart RGW\]" \
    "$work/sts-repair.log" || fail "no restart handler ran"
  [[ "$(ceph_ config get client.rgw rgw_s3_auth_use_sts)" == true ]] \
    || fail "rgw_s3_auth_use_sts not restored"
  [[ "$(rgw_started)" != "$before" ]] || fail "RGW start times did not change"
  expect_clean sts-second
  pass "STS re-enabled, handler waited for the restart"
}

estate() { # every account with its roles and users, for read-only checks
  local a
  for a in $(ra account list | jq_ 'print(" ".join(sorted(d)))'); do
    ra account get --account-id "$a"
    ra role list --account-id "$a" \
      | jq_ 'print(json.dumps(sorted(d, key=lambda r: r["RoleName"]), sort_keys=True))'
    ra user list --account-id "$a"
    ra bucket list --account-id "$a"
  done
}

step_readonly() {
  local before ro=(-e scenario_role=rgw_tenants)
  before=$(estate | md5sum)
  PB=$here/role.yml expect_ok ro-report "${ro[@]}" -e rgw_tenants_report=true
  PB=$here/role.yml expect_ok ro-discover "${ro[@]}" -e rgw_tenants_discover=true
  PB=$here/role.yml expect_ok ro-verify "${ro[@]}" -e scenario_tasks_from=verify
  [[ $(changed ro-report) -eq 0 && $(changed ro-verify) -eq 0 ]] \
    || fail "a read-only mode reported changes"
  [[ -z "$(ls -A "$work/discover")" ]] \
    || fail "discover wrote stubs for a fully managed estate: $(ls "$work/discover")"
  [[ "$(estate | md5sum)" == "$before" ]] || fail "a read-only mode changed the cluster"
  pass "report, discover, verify changed nothing"
}

# ---- federation: throwaway IdP users -> AssumeRoleWithWebIdentity -> scoped S3 -----------
fed_users=(ci-reader ci-writer ci-owner ci-analytics)
fed_seeded=(teamA/probe teamB/probe)
fed_key=""
fed_secret=""
# on_host <log> <script> [args...] [-- VAR=value...]: a python script on RGW_TEST_SSH
on_host() {
  local name=$1 script=$2 args=() envs=()
  shift 2
  while [[ $# -gt 0 && $1 != -- ]]; do args+=("$1"); shift; done
  [[ $# -gt 0 ]] && shift
  envs=("$@")
  local cmd
  cmd="env $(printf '%q ' "${envs[@]}") python3 -"
  [[ ${#args[@]} -eq 0 ]] || cmd+=" $(printf '%q ' "${args[@]}")"
  # shellcheck disable=SC2029 # expanded locally on purpose
  ssh_ "$cmd" <"$script" >"$work/$name.log" 2>&1
}
seeding() { # seeding <seed|unseed>: the probe objects in <tenant>-data, as the tenant root
  on_host "fed-$1" "$here/fed_probe.py" "$1" "$tenant-data" "${fed_seeded[@]}" -- \
    EP="$RGW_TEST_ENDPOINT" AK="$fed_key" SK="$fed_secret"
}
# Runs on exit too, so a failed check leaves no IdP users and no objects that block teardown.
fed_cleanup() {
  local u
  [[ -z "$fed_key" ]] || seeding unseed || true
  for u in "${fed_users[@]}"; do
    "${RGW_TEST_KC_ADMIN[@]}" rm-user "$u" >/dev/null 2>&1 || true
  done
  "${RGW_TEST_KC_ADMIN[@]}" rm-group "$grant_root/$tenant" >/dev/null 2>&1 || true
}
# fed <log> <script> <user> <role> [args...] [-- VAR=value...]: as <user>, on <tenant>-<role>
fed() {
  local name=$1 script=$2 user=$3 role=$4
  shift 4
  local args=()
  while [[ $# -gt 0 && $1 != -- ]]; do args+=("$1"); shift; done
  [[ $# -gt 0 ]] && shift
  on_host "$name" "$script" "${args[@]}" -- EP="$RGW_TEST_ENDPOINT" IDP="$RGW_TEST_IDP" \
    CLIENT="$RGW_TEST_CLIENT" ACCOUNT_ID="$(account)" TENANT="$tenant" ROLE="$role" \
    USERNAME="$user" PASSWORD="$fed_pw" "$@"
}
expect_fed() { # expect_fed <log> <script> <user> <role> [args...] [-- VAR=value...]
  fed "$@" || fail "$1: $(grep -m1 -E 'FAIL|Error|denied' "$work/$1.log" | cut -c1-300)"
  pass "$1"
}

step_federation() {
  if [[ -z "${RGW_TEST_IDP:-}" || -z "${RGW_TEST_KC_ADMIN:-}" ]]; then
    skip "set RGW_TEST_IDP and RGW_TEST_KC_ADMIN to run it"
    return
  fi
  : "${RGW_TEST_ENDPOINT:?set RGW_TEST_ENDPOINT for the federation step}"
  : "${RGW_TEST_CLIENT:?set RGW_TEST_CLIENT, the OIDC client id, for the federation step}"
  local g=$grant_root/$tenant verify=$repo/roles/rgw_tenants/files/verify_federation.py
  local probe=$here/fed_probe.py kc=("${RGW_TEST_KC_ADMIN[@]}") cross before
  fed_pw=$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')
  cross=$(ra bucket list | jq_ "print(next(b for b in sorted(d) if not b.startswith('$tenant-')))")
  trap fed_cleanup EXIT
  "${kc[@]}" user ci-reader "$fed_pw" "$g/reader"
  "${kc[@]}" user ci-writer "$fed_pw" "$g/writer"
  "${kc[@]}" user ci-owner "$fed_pw" "$g/owner" "$g/iamadmin"
  "${kc[@]}" user ci-analytics "$fed_pw" "$g/analytics"

  expect_fed fed-reader "$verify" ci-reader reader -- EXPECT=ro CROSS_BUCKET="$cross"
  expect_fed fed-writer "$verify" ci-writer writer -- EXPECT=rw CROSS_BUCKET="$cross"
  expect_fed fed-owner "$verify" ci-owner owner -- EXPECT=rw CROSS_BUCKET="$cross"
  expect_fed fed-reader-as-writer "$probe" ci-reader writer denied
  expect_fed fed-writer-as-owner "$probe" ci-writer owner denied

  read -r fed_key fed_secret <<<"$(first_key "${tenant}root")"
  seeding seed || fail "seeding $tenant-data failed"
  expect_fed fed-analytics-prefix "$probe" ci-analytics analytics \
    prefix "$tenant-data" teamA/ teamB/
  seeding unseed || fail "removing the probe objects from $tenant-data failed"
  fed_key=""

  # A recreated provider must serve the next assume without an RGW restart.
  before=$(rgw_started)
  PB=$here/oidc_drop.yml play fed-oidc-drop -e "scenario_dropin=$dropin" \
    -e "scenario_tenant=$tenant" || fail "dropping the provider failed"
  fed fed-no-provider "$verify" ci-reader reader -- EXPECT=ro \
    && fail "an assume worked with the provider removed"
  expect_ok fed-oidc-repair
  expect_fed fed-after-repair "$verify" ci-reader reader -- EXPECT=ro
  [[ "$(rgw_started)" == "$before" ]] || fail "RGW restarted for an OIDC change"
  pass "assume refused without the provider, works again right after the repair, no restart"

  fed_cleanup
  trap - EXIT
}

step_teardown() {
  local acct root=${tenant}root key secret
  acct=$(account)
  grep -q '^  state: absent$' "$dropin" \
    || dropin_edit "s = s.replace('  name: $tenant\n', '  name: $tenant\n  state: absent\n', 1)"
  # Without the gate, absent tenants are announced and left alone; it is not an error.
  expect_clean teardown-ungated
  log_has teardown-ungated "Re-run with -e rgw_tenants_allow_teardown=true" \
    || fail "teardown-ungated did not warn about the gate"
  ra account get --account-id "$acct" >/dev/null || fail "an ungated run removed the account"
  read -r key secret <<<"$(first_key "$root")"
  ra key create --uid "$root" --gen-access-key --gen-secret >/dev/null
  ra key rm --uid "$root" --access-key "$key" >/dev/null
  expect_fail teardown-mismatch "teardown cannot see its buckets" -e rgw_tenants_allow_teardown=true
  ra account get --account-id "$acct" >/dev/null || fail "a refused teardown removed the account"
  ra key create --uid "$root" --access-key "$key" --secret-key "$secret" >/dev/null
  expect_ok teardown -e rgw_tenants_allow_teardown=true
  [[ -z "$(ra account get --account-id "$acct" 2>/dev/null)" ]] || fail "account $acct still exists"
  expect_clean teardown-second -e rgw_tenants_allow_teardown=true
  rm -f "$dropin"
  # The tenant is gone, so no limit can name it: this one converges the whole estate.
  expect_clean teardown-forgotten -e '{"rgw_tenants_limit": []}'
  pass "teardown removed $acct; re-run and a whole-estate run without the drop-in are clean"
}
