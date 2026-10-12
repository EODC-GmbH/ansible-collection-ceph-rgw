# shellcheck shell=bash disable=SC2154 # the globals are set by scenarios.sh
# Sourced by scenarios.sh: the steps that build the test tenant and break it.

# An account cannot be removed while it has an OIDC provider, and only IAM, as one of the
# account's roots, removes one: a temporary root user does it with boto3 on RGW_TEST_SSH.
drop_oidc_providers() {
  local acct=$1 uid=scenario-reset-$1 creds
  : "${RGW_TEST_ENDPOINT:?set RGW_TEST_ENDPOINT to purge an account with an OIDC provider}"
  creds=$(ra user create --uid "$uid" --account-id "$acct" --account-root --display-name "$uid" \
    | jq_ 'print(d["keys"][0]["access_key"], d["keys"][0]["secret_key"])')
  # shellcheck disable=SC2029 # expanded locally on purpose
  ssh_ "python3 - $RGW_TEST_ENDPOINT $creds" 2>/dev/null <<'PY'
import sys
import boto3
ep, ak, sk = sys.argv[1:4]
iam = boto3.client("iam", endpoint_url=ep, aws_access_key_id=ak, aws_secret_access_key=sk,
                   region_name="us-east-1")
for p in iam.list_open_id_connect_providers()["OpenIDConnectProviderList"]:
    iam.delete_open_id_connect_provider(OpenIDConnectProviderArn=p["Arn"])
PY
  ra user rm --uid "$uid" >/dev/null
}

purge_account() { # purge_account <account>: roles, buckets with objects, users, providers
  local acct=$1 r p b u
  for r in $(role_names "$acct"); do
    for p in $(ra role-policy list --role-name "$r" --account-id "$acct" \
        | jq_ 'print(" ".join(d))'); do
      ra role-policy delete --role-name "$r" --account-id "$acct" --policy-name "$p" >/dev/null
    done
    ra role delete --role-name "$r" --account-id "$acct" >/dev/null
  done
  for b in $(ra bucket list --account-id "$acct" | jq_ 'print(" ".join(d))'); do
    ra bucket rm --bucket "$b" --purge-objects >/dev/null
  done
  for u in $(ra user list --account-id "$acct" | jq_ 'print(" ".join(d))'); do
    ra user rm --uid "$u" >/dev/null
  done
  drop_oidc_providers "$acct"
  ra account rm --account-id "$acct" >/dev/null
}

step_reset() {
  local acct
  for acct in $(ra account list | jq_ 'print(" ".join(d))'); do
    [[ "$(ra account get --account-id "$acct" | jq_ 'print(d["name"])')" == "tenant-$tenant" ]] \
      || continue
    echo "   purging leftover account $acct"
    purge_account "$acct"
  done
  rm -f "$dropin"
  pass "no $tenant account left"
}

step_generate() {
  local nt=(rgw_new_tenant.yml -e "rgw_new_tenant=$tenant")
  expect_local_fail gen-noncanonical "not in canonical form" \
    rgw_new_tenant.yml -e rgw_new_tenant=CI_Test
  expect_local_fail gen-split "ends at the first space" \
    rgw_new_tenant.yml -e "rgw_new_tenant=$tenant x"
  expect_local_fail gen-force-nofile "Force needs an existing" \
    "${nt[@]}" -e rgw_new_tenant_force=true
  expect_local_fail gen-malformed-id "is malformed" \
    "${nt[@]}" -e rgw_new_tenant_account_id=RGW123
  local taken
  taken=$(grep -ho 'RGW[0-9]\{17\}' "$tdir"/*.yml | head -1)
  expect_local_fail gen-dup-id "already exists" \
    "${nt[@]}" -e "rgw_new_tenant_account_id=$taken"
  [[ ! -e "$dropin" ]] || fail "a refused run wrote $dropin"

  local spec
  spec=$(printf '{"rgw_new_tenant_buckets": ["%s-data", "%s-logs"],
    "rgw_new_tenant_root_users": ["Jörg Ops"],
    "rgw_new_tenant_quota": {"enabled": true, "max_size": 10737418240, "max_objects": 10000}}' \
    "$tenant" "$tenant")
  local_play gen "${nt[@]}" -e "$spec" || fail "rgw_new_tenant failed"
  [[ "$(stat -c %a "$dropin")" == 600 ]] || fail "drop-in mode is not 0600"
  grep -q 'name: joerg_ops' "$dropin" || fail "root user name not sanitized to joerg_ops"
  expect_local_fail gen-exists "already exists" "${nt[@]}"

  local au=(rgw_add_root_user.yml -e "rgw_add_root_user_tenant=$tenant"
    -e rgw_add_root_user=breakglass)
  local as=(rgw_add_service_user.yml -e "rgw_add_service_user_tenant=$tenant"
    -e rgw_add_service_user=reader)
  local_play add-root "${au[@]}" || fail "rgw_add_root_user failed"
  local_play add-svc "${as[@]}" -e '{"rgw_add_service_user_capabilities": ["objects-read"]}' \
    || fail "rgw_add_service_user failed"
  local sum
  sum=$(md5sum <"$dropin")
  local_play add-root-again "${au[@]}" || fail "re-adding the root user failed"
  local_play add-svc-again "${as[@]}" || fail "re-adding the service user failed"
  grep -q "already declared" "$work/add-svc-again.log" \
    || fail "re-add did not report already declared"
  expect_local_fail add-svc-long "limited to 64" rgw_add_service_user.yml \
    -e "rgw_add_service_user_tenant=$tenant" -e "rgw_add_service_user=$(printf 'x%.0s' {1..60})"
  [[ "$(md5sum <"$dropin")" == "$sum" ]] || fail "a no-op or refused add changed the drop-in"

  # By hand, as an operator would: scope the service user, add a prefix-scoped role.
  dropin_edit "s = s.replace('''        - objects-read
''', '''        - objects-read
      buckets:
        - $tenant-data
  session_duration: 3600
  roles:
    - name: analytics
      capabilities:
        - objects-read
      buckets:
        - $tenant-data
      prefixes:
        - \"teamA/*\"
''', 1)"
  pass "drop-in generated: $(account)"
}

step_preview() {
  expect_ok preview --check --diff
  grep -q "WOULD   $tenant-analytics" "$work/preview.log" \
    || fail "preview lists no WOULD for $tenant-analytics"
  [[ $(changed preview) -gt 0 ]] || fail "preview of a new tenant reported no changes"
  [[ -z "$(ra account get --account-id "$(account)" 2>/dev/null)" ]] \
    || fail "--check created the account"
  pass "preview: changed=$(changed preview), nothing created"
  expect_fail limit-unknown "rgw_tenants_limit names \\['$tenant-nosuch'\\]" \
    -e "{\"rgw_tenants_limit\": [\"$tenant-nosuch\"]}"
  expect_fail limit-string "must be a list of tenant names" -e "rgw_tenants_limit=$tenant"
}

step_create() {
  expect_ok create
  [[ $(changed create) -gt 0 ]] || fail "create reported no changes"
  expect_clean create-second
  local acct roles trust t=$tenant
  acct=$(account)
  roles=$(role_names "$acct")
  [[ "$roles" == "$t-analytics $t-iamadmin $t-owner $t-reader $t-writer" ]] || fail "roles: $roles"
  trust=$(trust_of "$tenant-reader" "$acct")
  [[ "$trust" == *"$grant_root/$tenant/reader"* ]] \
    || fail "reader trust has no group-path grant: $trust"
  ra role list --account-id "$acct" | jq_ "
p = [x for r in d if r['RoleName'] == '$tenant-analytics' for x in r['PermissionPolicies']]
assert 'teamA/*' in json.dumps(p) and 's3:prefix' in json.dumps(p), p" \
    || fail "analytics role lacks the prefix scope"
  [[ "$(ra user list --account-id "$acct" | jq_ 'print(len(d))')" == 4 ]] || fail "expected 4 users"
  pass "account, 5 roles, 4 users, group-path trust, prefix scope"
}

step_drift() {
  local acct trust_doc open_doc
  acct=$(account)
  open_doc='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["s3:*"],
    "Resource":["*"]}]}'
  trust_doc='{"Version":"2012-10-17","Statement":[{"Effect":"Allow",
    "Principal":{"AWS":["arn:aws:iam::'$acct':root"]},"Action":["sts:AssumeRole"]}]}'
  local ac=(--account-id "$acct")
  ra role-policy put --role-name "$tenant-reader" "${ac[@]}" --policy-name tenant \
    --policy-doc "$open_doc" >/dev/null
  ra role-trust-policy modify --role-name "$tenant-writer" "${ac[@]}" \
    --assume-role-policy-doc "$trust_doc" >/dev/null
  ra quota set --quota-scope account "${ac[@]}" --max-objects 5 >/dev/null
  ra bucket rm --bucket "$tenant-logs" >/dev/null
  ra role create --role-name "$tenant-zz" "${ac[@]}" \
    --assume-role-policy-doc "$trust_doc" >/dev/null
  ra role create --role-name "$tenant-x-keep" "${ac[@]}" \
    --assume-role-policy-doc "$trust_doc" >/dev/null

  expect_ok drift-preview --check --diff
  local t
  for t in "Ensure one RGW account" "Create each tenant's buckets" "Reconcile IAM roles"; do
    expect_task_changed drift-preview "$t"
  done
  expect_ok drift-repair
  expect_clean drift-second
  ra role list "${ac[@]}" | jq_ "
p = [r for r in d if r['RoleName'] == '$tenant-reader'][0]['PermissionPolicies']
assert '\"s3:*\"' not in json.dumps(p), p" || fail "reader policy drift not repaired"
  [[ "$(trust_of "$tenant-writer" "$acct")" == *"$grant_root/$tenant/writer"* ]] \
    || fail "writer trust drift not repaired"
  [[ "$(ra account get "${ac[@]}" | jq_ 'print(d["quota"]["max_objects"])')" == 10000 ]] \
    || fail "quota not repaired"
  ra bucket list "${ac[@]}" | jq_ "assert '$tenant-logs' in d" || fail "bucket not recreated"

  expect_ok drift-prune -e rgw_iam_prune=true
  local roles
  roles=$(role_names "$acct")
  [[ "$roles" != *"$tenant-zz"* ]] || fail "prune kept the unmanaged $tenant-zz"
  [[ "$roles" == *"$tenant-x-keep"* ]] || fail "prune removed the tenant-delegated $tenant-x-keep"
  pass "6 drifts repaired; prune removed $tenant-zz, kept $tenant-x-keep"
}
