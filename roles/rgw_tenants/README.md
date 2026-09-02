# rgw_tenants

Provision OIDC-federated, per-tenant S3 on Ceph RGW. Per tenant it creates:

- an RGW account (fixed id);
- an account-root user (creates buckets; never handed to end users);
- a registered OIDC provider (via the IAM API;
  squid has no working `radosgw-admin oidc-provider`);
- the per-tier web-identity IAM roles (`<tenant>-admin/writer/reader`),
  with trust conditions on the token's `org`/`tier` claims and a
  permission policy scoped to `arn:aws:s3:::<tenant>-*`;
- the tenant's buckets.

Requires `rgw_sts` first: the role asserts `rgw_s3_auth_use_sts == true` and
waits for RGW to answer before its boto3 steps.
Runs on `hosts: mons`. Ceph work goes through `cephadm shell` on the bootstrap
host; the OIDC-provider, IAM-role and bucket helpers run on that host directly,
so give it `python3` with `boto3`. `meta/argument_specs.yml` and per-tenant
asserts validate inputs.

## Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `rgw_tenants_list` | `[]` | Tenants: `{ name, account_id, state?, buckets?, quota?, limits?, root_user?, root_users?, rotate_keys?, purge_objects?, bucket_prefix? }`. |
| `rgw_tenants_dir` | `""` | Control-host directory of drop-in tenant files, each appended to `rgw_tenants_list`. Empty = not loaded. |
| `rgw_tenants_endpoint` | `""` | RGW S3/IAM/STS endpoint reachable from the bootstrap host. |
| `rgw_tenants_s3_region` | `us-east-1` | The region boto3 signs with (a SigV4 placeholder, not a cloud region). Change only if the zonegroup has an `api_name`, or `CreateBucket` fails on the `LocationConstraint`. |
| `rgw_tenants_oidc` | `{provider_url,client_ids,thumbprints}` | The IdP realm to federate. Thumbprint is the SHA1 of the JWKS signing cert (see collection README recipe). |
| `rgw_tenants_tiers` | admin/writer/reader | Tier to S3 actions. |
| `rgw_tenants_default_quota` | `{}` | Fallback `quota` for tenants that omit their own; `{}` = none. |
| `rgw_tenants_rotate_keys` | `false` | Gate: roll every tenant's account-root keys to the vaulted value. |
| `rgw_tenants_prune_roles` | `false` | Gate: delete `<name>-<tier>` roles whose tier left `rgw_tenants_tiers`. |
| `rgw_tenants_allow_teardown` | `false` | Gate: run the `state: absent` decommission path. |
| `rgw_tenants_allow_purge` | `false` | Gate: let a tenant's `purge_objects` destroy data during teardown. |
| `rgw_tenants_allow_root_user_removal` | `true` | Honor a `root_users` entry marked `state: absent` (`user rm`). |
| `rgw_tenants_service_name` | `rgw.rgw` | cephadm RGW service to restart on OIDC change. |
| `rgw_tenants_bootstrap_host` | `groups['mons'][0]` | Host that runs the ceph commands. |
| `rgw_tenants_fsid` | `""` | Empty discovers it via `ceph fsid`. Validated as a UUID: it becomes a directory component, where a wrong value builds paths nothing reads instead of failing. |
| `rgw_tenants_ceph_major_min` | `19` | Oldest Ceph major to run against, checked on the **RGW daemons**. Squid is the floor: every tenant here is an RGW account, and accounts are a squid feature. |
| `rgw_tenants_ceph_major_tested` | `20` | Newest major tested. Past it the role warns and continues. See [docs/CEPH-VERSIONS.md](../../docs/CEPH-VERSIONS.md). |
| `rgw_tenants_ceph_version_check` | `true` | Run the version check at all. Off for a build whose version string cannot be parsed. |

Built for a scheduled converge (hourly is fine): every task is idempotent and
reports `changed` only on real drift. Four destructive paths — key rotation,
role pruning, teardown, data purge — stay off until you turn them on.
`rgw_tenants_allow_root_user_removal` is the fifth gate and defaults on,
because removing an account-root user destroys no data.

Vault, per tenant: `vault_rgw_tenants_<name>_access_key` / `_secret_key`
(asserted defined before provisioning).

## Per-tenant item schema

```yaml
- name: acme                          # slug: account-root uid + default bucket prefix; [a-z0-9_], starts alnum
  account_id: "RGW00000000000000001"  # "RGW" + 17 digits, unique and stable
  state: present                      # optional; "absent" decommissions (see below)
  buckets: ["acme-data", "acme-logs"] # optional; each must start with the prefix
  quota:                              # optional storage cap; omit for unlimited
    enabled: true                     #   enforce it (false = tracked but not enforced)
    max_size: "{{ 100 * 1024 ** 3 }}" #   BYTES or -1; let kayobe do the GiB math
    max_objects: -1                   #   count or -1
  limits:                             # optional entity cap; omit for RGW defaults
    max_users: 10                     #   or -1 = unlimited
    max_buckets: 50                   #   or -1 = unlimited
  root_user: acmeadmin                # optional; default "<name>root" ([a-z0-9][a-z0-9_-]*)
  root_users:                         # optional; extra account-root users
    - name: erika_schroeder           #   [a-z0-9][a-z0-9_]*; uid == iam-acme-root-erika_schroeder
      state: present                  #   optional; "absent" runs user rm (gated, see above)
      suspended: false                #   optional; reconciled each converge (user suspend/enable)
  rotate_keys: false                  # optional; roll this tenant's keys (gated, see below)
  purge_objects: false                # optional; with state:absent, force-delete non-empty buckets
  bucket_prefix: "acme-"              # optional; default "<name>-" (the tier ARN scope)
```

## Day-to-day operations

| Task | How |
|------|-----|
| **Add** a tenant / bucket / tier | append to `rgw_tenants_list` / `item.buckets` / `rgw_tenants_tiers`, re-run |
| **Storage** increase/decrease/limit | edit `item.quota` (see the quota table below) |
| **Entity** limit (users/buckets) | set `item.limits.max_users` / `max_buckets` (`-1` = unlimited) |
| **Change** tier permissions / trust | edit `rgw_tenants_tiers[].actions`; the permission policy is always re-put |
| **Rotate** account-root keys | change the vaulted keys, set `rgw_tenants_rotate_keys: true` (or `item.rotate_keys`), run, set back to false |
| **Remove** a tier's orphan roles | set `rgw_tenants_prune_roles: true` after dropping the tier, run |
| **Decommission** a tenant | set `item.state: absent`, run with `-e rgw_tenants_allow_teardown=true` (add `item.purge_objects` + `-e rgw_tenants_allow_purge=true` to destroy data) |

### Rotate account-root keys (gated)

Provisioning never touches an existing user's keys. To roll one: update
`vault_rgw_tenants_<name>_access_key` / `_secret_key`, set
`rgw_tenants_rotate_keys: true` (or per-tenant `rotate_keys: true`), run once,
then set it back to false. The task reconciles the vaulted key onto the
account-root user and prunes any older access key, so the tenant ends with
exactly the vaulted key. It is idempotent while on and rotates only on drift.

### Decommission a tenant (state: absent)

Committing `state: absent` does nothing until an operator runs deliberately with
`-e rgw_tenants_allow_teardown=true`; a converge with a pending absent entry
only warns. Destroying data needs a **second** gate: a tenant's `purge_objects`
takes effect only with `-e rgw_tenants_allow_purge=true`, so "delete the tenant"
and "destroy its data" stay two explicit decisions.

Once allowed, teardown is idempotent and runs in this order: buckets, OIDC
provider, tier roles, extra root users, account-root user, account. It removes
what the role provisioned. If the tenant also created its own buckets/users/roles via
self-service, plain `account rm` refuses (RGW won't drop a non-empty account)
and the run fails without deleting anything. The two purge gates together
cascade (`account rm --purge-data`, which also purges bucket data); a non-empty
bucket in `item.buckets` behaves the same way.

Provider removal authenticates as the tenant's own account-root (the bucket,
user and account steps are `radosgw-admin`), so keep the
`vault_rgw_tenants_<name>_*` keys until the run reports all `OK`, then
delete the list entry and the vault keys. Re-runs are safe: once the account is
gone, every teardown step reports `OK`/`ABSENT` (the OIDC step treats an
already-removed account as success).

## Adjust a tenant's quota (increase / decrease / limit)

`quota` is a tenant's storage ceiling. Edit the tenant entry, re-run the role
(or the `cephadm-tenants` play): the quota task reads the account's current cap
and re-applies only on drift.

| Goal | Change |
|------|--------|
| Increase | raise `max_size` / `max_objects` |
| Decrease | lower them (data already over the new cap stays; new writes are refused) |
| Unlimited | `max_size: -1` and `max_objects: -1`, or `enabled: false` |
| Track, don't enforce | keep the numbers, set `enabled: false` |

## Preview changes (dry run)

Don't bother with `--check`: the provisioning steps are `command` tasks and skip
in check mode. Use the read-only **plan** mode instead. Per configured tenant it
reports what a real run would change (create account/user/role/bucket, move a
quota/limit, or decommission), then ends the play.

```sh
ansible-playbook cephadm-tenants.yml -e rgw_tenants_plan=true
```

Each tenant prints either `up to date (no changes)` or a list like
`SET quota size 21474836480->32212254720 ...`, `CREATE role acme-reader`,
`DECOMMISSION account ...`. Scope: account, account-root user, storage quota,
entity limits, per-tier roles, and buckets. The OIDC provider and policy bodies
reconcile on apply (they need the account-root keys) and are not previewed, so a
tenant with only that drift can still read as up to date. The effective quota
mirrors the apply path: a tenant's own `quota`, or `rgw_tenants_default_quota`
when that is unset or empty.

## Usage report (dry run)

Read-only usage-vs-quota for the configured tenants: used size/objects against
each account's quota, a percentage when the cap is finite, and a list of any
tenant at or over 80%. Needs no vault keys (only `radosgw-admin get/stats`), so
it works against tenants whose secrets you don't hold.

```sh
ansible-playbook cephadm-tenants.yml -e rgw_tenants_report=true
```

Example line: `acme (RGW...001): size 12.4 GiB / 500.0 GiB (2.5%); objects 3540 / unlimited`.
Figures come from the cached account stats (no `--sync-stats` recompute),
so they are as fresh as the last stats sync.

## Brownfield discovery / adoption

Point the role at a cluster that already has RGW accounts not created by this
role and it generates ready-to-edit tenant definitions for them. Discovery is
**read-only**: it runs `radosgw-admin ... list/get` only, and its sole writes are
stub files on the control host.

```sh
ansible-playbook cephadm-tenants.yml -e rgw_tenants_discover=true
```

For each RGW account it prints a `MANAGED` / `UNMANAGED` line
(managed = the `account_id` is already in `rgw_tenants_list`)
and writes an adoption stub
`rgw_tenants_discover_output_dir/adopt-<account-id>.yml` for every **unmanaged**
account. To adopt one: review/edit the stub, add the account-root's **existing**
S3 keys to your vault as `vault_rgw_tenants_<name>_access_key` / `_secret_key`
(the role reuses them and does not recreate the root user unless you set
`rotate_keys: true`), merge the entry into `rgw_tenants_list`, and run the role
normally. In discover mode the role ends after writing stubs (no provisioning,
no teardown, no STS/OIDC preflight), so it works before any tenant config exists.

| Variable | Default | Meaning |
|---|---|---|
| `rgw_tenants_discover` | `false` | Run read-only discovery instead of provisioning. |
| `rgw_tenants_discover_output_dir` | `{{ playbook_dir }}/rgw-tenants-discovery` | Where adoption stubs are written. |

## Idempotency notes

- IAM roles are provisioned via `files/manage_role.py` against the IAM API as the
  account root: create if absent, `UpdateAssumeRolePolicy` on trust drift, and
  always (re)put the permission policy. The IAM API is the interface Ceph
  documents for account-scoped IAM, and it is the one that survives the V-release
  removals — see [docs/CEPH-VERSIONS.md](../../docs/CEPH-VERSIONS.md). (A plain
  `role get || role create` shell path would silently ignore an edited trust
  policy: `role create` cannot change one. `radosgw-admin role-trust-policy
  modify` can, and that is what `rgw_sts` uses for its standalone roles.)
- The OIDC helper validates thumbprints (40-hex, non-empty) before any change
  and rolls back to the previous provider if a drift-driven recreate fails.
- Buckets: `files/manage_buckets.py` probes `HeadBucket` (not `CreateBucket`,
  which RGW answers 200 for a bucket you already own) and creates only on a 404.
  A 403 means the name is held by another account and surfaces as an error.
- Quota: reads the account's current cap and applies only on drift, so it
  reports `changed` only when it actually moved.
- Verification: `tasks_from: verify` asserts every `<tenant>-<tier>` role and
  every declared bucket is present, then prints desired-vs-live quota. It loads
  the `rgw_tenants_dir` drop-ins itself, and reports `NOTHING VERIFIED` instead
  of a pass when no tenant is configured. `files/verify_federation.py` covers the
  full login, assume and scoped S3 path, per user. Its denial checks assert on
  `AccessDenied` specifically, so a wrong `CROSS_BUCKET` fails the test instead
  of passing it.
