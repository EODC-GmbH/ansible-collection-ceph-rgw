# shellcheck shell=bash disable=SC2154 # the globals are set by scenarios.sh
# Sourced by scenarios.sh: key and identity drift, which a run refuses instead of repairing.

step_nokey() {
  local svc=iam-$tenant-svc-reader key
  read -r key _ <<<"$(first_key "$svc")"
  ra key rm --uid "$svc" --access-key "$key" >/dev/null
  expect_fail nokey "$svc holds no key on the cluster"
  expect_fail nokey-check "$svc holds no key on the cluster" --check
  expect_ok nokey-rotate -e rgw_tenants_rotate_keys=true
  [[ "$(first_key "$svc" | cut -d' ' -f1)" == "$key" ]] \
    || fail "rotation did not reinstall the vaulted key"
  expect_clean nokey-second
}

step_mismatch() {
  local root=${tenant}root key
  read -r key _ <<<"$(first_key "$root")"
  ra key create --uid "$root" --gen-access-key --gen-secret >/dev/null
  ra key rm --uid "$root" --access-key "$key" >/dev/null
  expect_fail mismatch "vault keys for $tenant do not match the cluster's keys for $root"
  expect_fail mismatch-check "do not match the cluster's keys for $root" --check
  expect_ok mismatch-rotate -e rgw_tenants_rotate_keys=true
  [[ "$(keys_of "$root")" == "$key" ]] || fail "rotation did not leave exactly the vaulted key"
  expect_clean mismatch-second
}

keys_of() {
  ra user info --uid "$1" | jq_ 'print(" ".join(sorted(k["access_key"] for k in d["keys"])))'
}
secret_of() {
  ra user info --uid "$1" \
    | jq_ "print(''.join(k['secret_key'] for k in d['keys'] if k['access_key'] == '$2'))"
}
words() { tr ' ' '\n' <<<"$*" | LC_ALL=C sort | xargs; }
new_pair() {
  python3 -c 'import secrets, string as s
pick = lambda abc, n: "".join(secrets.choice(abc) for _ in range(n))
print(pick(s.ascii_uppercase + s.digits, 20), pick(s.ascii_letters + s.digits, 40))'
}
expect_keys() { # expect_keys <uid> <access keys...>: exactly these keys on the cluster
  local want
  want=$(words "${@:2}")
  [[ "$(keys_of "$1")" == "$want" ]] || fail "$1 holds [$(keys_of "$1")], expected [$want]"
}

# Extra keys through access_keys, on the tenant (its primary root) and on a service user.
# Asserted on the cluster: the keys are no_log, so the logs mask them.
step_keys() {
  local root=${tenant}root svc=iam-$tenant-svc-reader
  local backup=${tdir%/*}/$tenant.pre-keys.yml
  # A rerun after a failure starts from the drop-in as the earlier steps left it.
  if grep -q 'access_keys:' "$dropin"; then
    cp "$backup" "$dropin"
  else
    cp "$dropin" "$backup"
  fi
  local rp sp k1 s1 k2 s2 k3 s3 kx sx
  rp=$(keys_of "$root")
  sp=$(keys_of "$svc")
  [[ -n "$rp" && "$sp" != *" "* && -n "$sp" ]] || fail "expected one key each on $root and $svc"
  read -r k1 s1 <<<"$(new_pair)"
  read -r k2 s2 <<<"$(new_pair)"
  local e1="    - access_key: \"$k1\"\n" e2="        - access_key: \"$k2\"\n"
  local x1="      secret_key: \"$s1\"\n" x2="          secret_key: \"$s2\"\n"
  dropin_edit "s = s.replace('  root_user: $root\n',
    '  root_user: $root\n  access_keys:\n$e1$x1', 1)"
  dropin_edit "s = s.replace('    - name: reader\n',
    '    - name: reader\n      access_keys:\n$e2$x2', 1)"

  expect_ok keys-add
  expect_task_changed keys-add "primary account-root user"
  expect_task_changed keys-add "declared service user"
  expect_keys "$root" "$rp" "$k1"
  expect_keys "$svc" "$sp" "$k2"
  [[ "$(secret_of "$svc" "$k2")" == "$s2" ]] || fail "$k2 was not created with the declared secret"
  expect_clean keys-add-second
  expect_clean keys-add-check --check

  read -r _ sx <<<"$(new_pair)"
  ra key create --uid "$svc" --access-key "$k2" --secret-key "$sx" >/dev/null
  [[ "$(secret_of "$svc" "$k2")" == "$sx" ]] || fail "could not change the secret of $k2 by hand"
  expect_fail keys-drift "access_keys entry whose secret differs"
  expect_fail keys-drift-check "access_keys entry whose secret differs" --check
  [[ "$(secret_of "$svc" "$k2")" == "$sx" ]] || fail "a refused run changed the secret of $k2"

  read -r k3 s3 <<<"$(new_pair)"
  read -r kx _ <<<"$(new_pair)"
  ra key create --uid "$svc" --access-key "$k3" --secret-key "$s3" >/dev/null
  ra key create --uid "$root" --access-key "$kx" --secret-key "$s3" >/dev/null
  expect_ok keys-rotate -e rgw_tenants_rotate_keys=true
  [[ "$(secret_of "$svc" "$k2")" == "$s2" ]] || fail "rotation did not restore the secret of $k2"
  expect_keys "$root" "$rp" "$k1"
  expect_keys "$svc" "$sp" "$k2"
  expect_clean keys-rotate-second

  # Without rotation an undeclared key is reported and left alone.
  ra key create --uid "$svc" --access-key "$k3" --secret-key "$s3" >/dev/null
  expect_clean keys-unmanaged
  expect_keys "$svc" "$sp" "$k2" "$k3"
  ra key rm --uid "$svc" --access-key "$k3" >/dev/null

  local a1="      state: absent\n" a2="          state: absent\n"
  dropin_edit "s = s.replace('$e1$x1', '$e1$a1', 1)"
  dropin_edit "s = s.replace('$e2$x2', '$e2$a2', 1)"
  expect_ok keys-absent
  expect_keys "$root" "$rp"
  expect_keys "$svc" "$sp"
  expect_clean keys-absent-second

  dropin_edit "s = s.replace('$e2$a2', '$e2$a2        - access_key: \"$sp\"\n$a2', 1)"
  expect_fail keys-conflict "the primary access_key is declared absent"
  expect_keys "$svc" "$sp"

  cp "$backup" "$dropin"
  rm "$backup"
  expect_clean keys-restored
  pass "extra keys added, drift refused and rotated, undeclared kept then pruned, absent removed"
}

step_identity() {
  local user=iam-$tenant-root-breakglass
  ra user modify --uid "$user" --display-name "not-$user" >/dev/null
  expect_fail identity-check "display" --check
  expect_fail identity "display"
  [[ "$(ra user info --uid "$user" | jq_ 'print(d["display_name"])')" == "not-$user" ]] \
    || fail "a refused run changed the user"
  ra user modify --uid "$user" --display-name "$user" >/dev/null
  expect_clean identity-restored
}
