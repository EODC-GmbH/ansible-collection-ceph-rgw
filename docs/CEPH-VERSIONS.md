# Ceph versions: what this collection needs, and what breaks when

Why this file exists: Ceph is removing the IAM interface its STS support was originally
built on. If you do not know which side of that line your automation sits on, you find out
during an upgrade. This is the answer, with the reasoning, so it can be re-checked instead
of re-derived.

## Supported range

| Ceph | Status | What decides it |
|---|---|---|
| 18.x reef and older | **refused** | No RGW accounts. Every tenant here *is* an account. |
| 19.x squid | supported | Accounts, account-root users, `bucket list --account-id`, `account stats`, native STS. |
| 20.x tentacle | supported | Tenant-level IAM deprecated but present. Nothing here uses it. |
| 21.x | untested, warns | Expected to work. Verify with `rgw_tenants_plan=true`. |
| 22.x (V) and later | untested, warns | Where the removals below land. We are clear of them — re-verify anyway. |

Set by `rgw_sts_ceph_major_min` / `rgw_tenants_ceph_major_min` (floor, hard failure) and
`*_ceph_major_tested` (warning threshold). `*_ceph_version_check: false` turns off the
whole preflight -- all three checks, the version floor, the RGW-is-deployed assert and the
unreadable-version assert, not just the bound it is named for -- which you need for a vendor
rebuild whose version string cannot be parsed. You then own verifying that accounts and IAM
behave the way these roles assume. Note an empty `-e *_ceph_version_check=` reads as false.

Newer-than-tested warns rather than fails on purpose. A collection that refuses to run on a
new Ceph becomes the reason nobody upgrades.

## What Ceph is removing

From the [tentacle release notes](https://docs.ceph.com/en/latest/releases/tentacle/):

> The User Account feature introduced in Squid provides first-class support for IAM APIs and
> policy. Our preliminary STS support was based on tenants, and exposed some IAM APIs to
> admins only. This tenant-level IAM functionality is now deprecated in favor of accounts.
> While we'll continue to support the tenant feature itself for namespace isolation, the
> following features will be removed no sooner than the V release:
> - Tenant-level IAM APIs including CreateRole, PutRolePolicy and PutUserPolicy,
> - Use of tenant names instead of accounts in IAM policy documents,
> - Interpretation of IAM policy without cross-account policy evaluation,
> - S3 API support for cross-tenant names such as `Bucket='tenant:bucketname'`
> - STS Lite and `sts:GetSessionToken`.

Tentacle is 20, so V is 22 — two releases of runway, and "no sooner than" is a floor, not a
date. Where we stand on each bullet:

| Removed at V | Us |
|---|---|
| `CreateRole` / `PutRolePolicy` (tenant-level) | Clear. Every role is account-scoped: `rgw_tenants` creates them through the IAM API as the account root, and `rgw_sts` refuses an `rgw_sts_roles` entry without an `account_id`. |
| `PutUserPolicy` | Not used anywhere. |
| Tenant names in policy documents | Clear. `tenant-trust.json.j2` names `arn:aws:iam::$account_id:oidc-provider/…`; `tenant-perm.json.j2` uses bare `arn:aws:s3:::$prefix*`. |
| Policy evaluation without cross-account rules | No effect. A role and the buckets it grants live in the same account, so there is no second account owner whose grant would newly be required. |
| `tenant:bucketname` S3 names | Never used. |
| STS Lite, `sts:GetSessionToken` | Clear, but watch the wording. Nothing calls it; the federated path is `AssumeRoleWithWebIdentity`, which is **not** on the list. Earlier docs in this repo advertised GetSessionToken as a feature — they no longer do. |

"Tenant" in this collection means an RGW **account**, not an RGW *tenant*. They are different
objects and the difference is the whole of the above. A tenanted account is still fine — Ceph
keeps tenants for bucket-namespace isolation — we just do not put IAM in one.

## Rule 1: which interface does what

Ceph splits this, and so do we:

- **`radosgw-admin`** — accounts, account-root users, account quotas and limits, user
  suspend/enable, bucket removal. Ceph: *"accounts and account root users must be created by
  an administrator using radosgw-admin or the Admin Ops API."* There is no other way.
- **The IAM API, as the account root** — roles, role policies, OIDC providers. Ceph:
  administered *"through the IAM API available through the RGW endpoint"*. This is the
  accounts-era interface, it is AWS-modelled with versioned policy documents, it returns
  typed error codes instead of an exit status, and it has real update calls
  (`UpdateAssumeRolePolicy`) where the CLI has none.

Corollary: never parse `radosgw-admin` output to make a decision the IAM or S3 API can answer
directly. CLI output shape is not a stable interface. The APIs are.

Two places still violate it, and the second is bigger than the first:

- The role-prune loop in `provision_roles.yml` and `teardown.yml` scopes a *destructive*
  `role list` / `role delete` with `--account-id`. The `$prefix` guard is what keeps that
  safe today. Note the prefix is the only bound: a tenant that self-serves roles under its
  own name through the IAM API — which is the point of giving it an account root — will have
  them pruned too, and `rgw_tenants_plan=true` does not model the prune at all.
- The whole of `rgw_sts`'s role lifecycle (`role get`, `role-trust-policy modify`,
  `role-policy get`, `role-policy put`) parses CLI output to drive a *write*. `rgw_tenants`
  does the same job over the IAM API in `manage_role.py`, in 79 lines, with typed
  `NoSuchEntity` narrowing. `rgw_sts_roles` defaults to `[]` and nothing in this repo sets
  it, so none of that code runs in the shipped configuration — but it is the larger
  violation, not the prune loop.

`--account-id` is honoured on every role subcommand from squid onward and rejected outright
on reef, so the "silently ignored" case is not shipped — but nothing verified it, so the
trust comparison now asserts that `role get` reports the account we asked for. That check is
free: `role get` already returns `AccountId` in the JSON being parsed. Moving all of this
onto `list_roles` / `delete_role` / `update_assume_role_policy` is part of the module
migration.

## Rule 2: a failure is never an absence

`radosgw-admin` reports "this object does not exist" and "I cannot run at all" with the same
non-zero exit. Conflating them is the single most expensive mistake available here, because
it is silent: read a failure as an absence and the code proceeds to *create* what it just
failed to look up, or reports a teardown complete having done nothing.

So: an exit status that is not a recognised not-found fails the task with the stderr
attached. `rgw_sts`'s role and policy probes do this, and so does the account removal at the
end of `teardown.yml` — the worst place to get it wrong, because it runs after the buckets,
the provider, the roles and every user have already been deleted, so "absent" there reports
a tenant decommissioned while leaving a live account nothing can authenticate to.

The match has to be narrow to be worth anything. `No such file or directory` is only
`strerror(ENOENT)`: radosgw-admin prints it for a missing role and for an unreadable keyring
alike, so matching it reads a broken connection as an empty cluster. The probes key on the
role-pool read failure by name instead, and fail loudly with the stderr attached when the
wording is unfamiliar — being wrong in the direction of "stop" is survivable, being wrong in
the direction of "create" is not.

**This rule is not yet applied everywhere.** The `rgw_tenants` write paths
(`provision_accounts.yml`, `provision_root_users.yml`, and the bucket and user steps of
`teardown.yml`) still use `cmd >/dev/null 2>&1 || <treat as absent>`. They are listed below.

The same rule for API calls: narrow on the error code. `manage_buckets.py` treats only
404/`NoSuchBucket` as absent and errors on anything else. `verify_federation.py` counts a
denial as a denial only for `AccessDenied` and friends — it used to catch bare `ClientError`,
so a mistyped `CROSS_BUCKET` printed `PASS cross-tenant correctly denied` and proved nothing.
That was the check the account-isolation claim rested on.

## Rule 3: compare documents, not bytes

`radosgw-admin` re-serialises what it stores: its own key order, its own separators, sometimes
percent-encoded. `role-policy get` goes further and answers
`{"Permission policy": "<json text>"}` — the document wrapped in an envelope, as a string. A
byte compare against a rendered file therefore never matches, so the old `rgw_sts` diff re-put
every policy on every converge and reported `changed` forever while changing nothing.

Both documents now get canonicalised (parse, unwrap a single-key envelope that is not itself a
policy, percent-decode if needed, `sort_keys`) before comparison. Same for config values:
`ceph config get` renders a bool in whatever spelling the option stores — `true`, `1` and `on`
have all shipped — so `ceph_bool` normalises instead of comparing to the literal `true`.

## Rule 4: gate on the daemons, not the CLI

`preflight.yml` reads `ceph versions` and checks the **rgw** entries. During a staggered
cephadm upgrade the CLI on the bootstrap host and the RGW daemons are deliberately different
versions, and every assumption in these roles is about what the RGWs do. A mixed set reports
both ends and gates on the lowest.

An unparseable version string fails the check rather than passing it. A build that yields no
major number contributes an empty set, and an empty set satisfies every bound you can write —
so gating on the numbers alone would wave an unknown build straight through.

## `cephadm shell` is not a transparent pipe

It writes its own inference lines onto stdout ahead of the wrapped command's output, and
whether it does so depends on the build. Anything reading that stdout has to find the payload:
`ceph_json` scans for a document and stops where it ends, `ceph_bool` scans backwards for a
line that is a boolean, and `ceph_fsid` for one that is a UUID — unvalidated it became a
directory component in `/var/run/ceph/$fsid/`, where a wrong value does not fail but
silently builds paths nothing reads.

`plugins/filter/ceph_output.py` holds all of it, with tests in `tests/unit/`. Three rules
it follows, each of which was a bug first:

- **Find the document, don't slice to EOF.** `ceph_json` scans for a `{` or `[` that
  actually starts a document and stops where that document ends. Slicing from the first
  bracket to the end of the buffer dies on a trailing warning line, on a second document,
  and on a `{fsid}` placeholder inside a preamble path.
- **Search for a valid answer, don't trust position.** "The payload is last" only holds if
  cephadm never appends. `ceph_bool` and `ceph_fsid` scan backwards for a line that *is* a
  bool or *is* a UUID, so a trailing podman warning cannot become the value. No parser here
  takes a line on position alone; one that did was removed rather than left as a trap.
- **Raise, never sentinel.** An unrecognised bool is not `false`; a missing key is not an
  empty policy; `None` is not the string `"None"`. Each of those returns an answer the
  caller acts on. Because these raise, they must never be called from an `assert`'s
  `fail_msg`: Ansible templates task args before the action runs, so a raising filter there
  fails the task even when the assertion would have passed, and swallows the message you
  wrote. Compute into a fact, then reference the fact.

Run them with:

```
python3 -m unittest discover -s tests/unit
```

## Still on borrowed time

Known-fragile, deliberately not patched here because the module migration deletes the files
outright (the newer kayobe-config copy of this collection replaces these six staged scripts
with real Ansible modules on a shared exec layer, and carries a unit test that blacklists
their filenames):

- `discover_tenants.py`, `plan_tenants.py`, `report_tenants.py` each wrap `radosgw-admin` in a
  helper that ignores the exit status and the stderr, so any failure reads as "absent". The
  version gate closes the realistic case — a pre-squid cluster, where `account get` is an
  unrecognised subcommand — but an unreadable keyring still yields a dry-run that reports
  creating an estate that already exists. Read-only paths only.
- `provision_quotas.yml` and `plan_tenants.py` substitute `"?"` for a missing quota key, which
  never equals the desired value: a renamed key becomes a write plus `changed` on every
  converge instead of an error.
- `provision_root_users.yml` compares `suspended` to the literal `"1"`/`"0"`. If that field
  ever becomes a JSON bool, re-suspend fires forever and *unsuspend silently never fires*.
- The `role list` shape parser is copied four times in two distinct behaviours:
  `provision_roles.yml` and `teardown.yml` tolerate both response shapes; `discover_tenants.py`
  and `plan_tenants.py` do not, and raise on a dict response.
- `verify.yml` asserts by `regex_findall` over raw stdout, so a role name appearing anywhere
  in the output — including inside an error message — satisfies the check. It also loops over
  the configured tenant list rather than the present one, so a tenant you are decommissioning
  fails verification until you delete its entry.
- The `rgw_tenants` write paths still read any non-zero exit as an absence
  (`provision_accounts.yml`, `provision_root_users.yml`, the bucket and user steps of
  `teardown.yml`). This is the Rule 2 violation above, and it is the largest thing on this
  list: an unreadable keyring makes a teardown report every step `OK` having deleted nothing.
- The role prune and teardown role-delete loops do not `set -e`. If `role list` fails, the
  inline parser crashes, the loop body never runs, and the script still ends `echo OK` — a
  pruned-nothing run that reports no drift.
- `manage_role.py` puts the permission policy unconditionally and prints `PERM-PUT`, which
  `provision_roles.yml`'s `changed_when` does not match. Widen a tier's actions and every
  role is rewritten while the play reports `ok`.
- Removing a tenant's `quota` key skips the quota task entirely rather than clearing the cap,
  so the old quota stays and no drift is ever reported. Same for `max_users`/`max_buckets`,
  which also use the `"?"` sentinel.
- `rgw_tenants_tiers: []` is a legal configuration that fails on an undefined variable,
  because the role matrix is only built inside the tier loop.
- Key rotation is create-then-prune across two commands. Interrupted between them, the next
  converge sees the desired key present and reports `OK` with the old key still live — which
  matters because the reason to rotate is usually that the old one leaked.
- `tasks/main.yml` interpolates `bucket_prefix` into a regex, so a prefix containing regex
  metacharacters validates more loosely than it reads.

## When a new Ceph lands

1. Read the RGW section of its release notes against the five bullets above.
2. Bump `*_ceph_major_tested` only after a real `rgw_tenants_plan=true` on that release
   reports no drift on an already-converged cluster. A plan that suddenly wants to change
   everything is the signature of a shape or format change, not of drift.
3. Run `verify_federation.py` for one tier of one tenant, with `CROSS_BUCKET` pointed at a
   bucket that really exists in another account. That exercises the whole path: OIDC token,
   `AssumeRoleWithWebIdentity`, tier scoping, and the account boundary.
4. `python3 -m unittest discover -s tests/unit` for the parsers.
