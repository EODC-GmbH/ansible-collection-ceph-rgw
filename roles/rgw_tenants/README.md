# rgw_tenants

Provisions each tenant as a dedicated RGW account. Per tenant it reconciles:

- the RGW account, at a fixed `account_id`;
- the primary account-root user, which creates the buckets;
- any additional account-root users (`root_users`);
- static-key service users (`users`), created inert: `rgw_iam` puts their policy;
- the account's storage quota;
- the account's OIDC provider, via the IAM API;
- the tenant's buckets.

IAM roles and account **entity** limits are managed by `rgw_iam`.

Runs on `hosts: mons`; cluster work runs on `rgw_tenants_bootstrap_host`.
Requires `rgw_sts` to have run first, and asserts `rgw_s3_auth_use_sts == true`
before changing anything.

Entry points: `main` (provision, or a read-only report/discover), and `verify`
via `tasks_from: verify`.

## Variables

| Variable | Default | Meaning |
|---|---|---|
| `rgw_tenants_list` | `[]` | The tenants. Schema below. |
| `rgw_tenants_endpoint` | `""` | RGW S3/IAM/STS endpoint reachable from the bootstrap host. Required. |
| `rgw_tenants_oidc` | `{provider_url, client_ids, thumbprints}` | The IdP realm to federate. `thumbprints` is the 40-hex SHA1 of the JWKS signing cert, not the TLS chain. Required. |
| `rgw_tenants_dir` | `""` | Control-host drop-in directory; every `*.yml`/`*.yaml` in it is appended to `rgw_tenants_list`. Other files are ignored. |
| `rgw_tenants_limit` | `[]` | Tenant names a run provisions or tears down; empty means all. The rest are still validated and count as managed for discover, report and verify. An unknown name stops the run. |
| `rgw_tenants_default_quota` | `{}` | Fallback storage quota for tenants that omit their own. `{}` leaves them at RGW's unlimited default. |
| `rgw_tenants_rotate_keys` | `false` | Gate: roll every tenant's user keys (account-root, additional root, service) to the vaulted values. |
| `rgw_tenants_allow_teardown` | `false` | Gate: run the `state: absent` decommission path. |
| `rgw_tenants_allow_purge` | `false` | Gate: let teardown destroy data. |
| `rgw_tenants_allow_root_user_removal` | `true` | Honor a `root_users` entry marked `state: absent`. |
| `rgw_tenants_report` | `false` | Read-only usage-vs-quota report, then end the play. |
| `rgw_tenants_discover` | `false` | Read-only brownfield discovery, then end the play. |
| `rgw_tenants_discover_output_dir` | `{{ playbook_dir }}/rgw-tenants-discovery` | Where discovery writes adoption stubs. |
| `rgw_tenants_validate_certs` | `true` | TLS verification for the boto3 calls against `rgw_tenants_endpoint`. |
| `rgw_tenants_ca_bundle` | `""` | PEM file on the bootstrap host for a private CA, used instead of turning verification off. |
| `rgw_tenants_s3_region` | `us-east-1` | SigV4 signing region boto3 sends; does not affect bucket placement. |
| `rgw_tenants_bootstrap_host` | `groups['mons'][0]` | Host that runs the ceph commands. |
| `rgw_tenants_fsid` | `""` | Forwarded to the modules. Empty lets each read it from `/etc/ceph/ceph.conf`. |

Vault, per tenant: `vault_rgw_tenants_<name>_access_key` / `_secret_key` for the
primary account-root, `vault_rgw_tenants_<name>_root_<slug>_access_key` /
`_secret_key` for each extra root user, and
`vault_rgw_tenants_<name>_svc_<slug>_access_key` / `_secret_key` for each
service user (`<name>` with hyphens as underscores). All are asserted before
provisioning.

## Per-tenant item schema

```yaml
- name: aviato                        # slug: account-root uid stem + default bucket prefix
  account_id: "RGW00000000000000001"  # "RGW" + 17 digits, unique and stable
  state: present                      # optional; "absent" decommissions
  buckets: ["aviato-data", "aviato-logs"] # optional; each must start with the prefix
  bucket_prefix: "aviato-"            # optional; default "<name>-"
  quota:                              # optional storage cap; omit for unlimited
    enabled: true                     #   false = tracked but not enforced
    max_size: "{{ 100 * 1024 ** 3 }}" #   BYTES or -1; let kayobe do the GiB math
    max_objects: -1                   #   count or -1
  root_user: aviatoroot               # optional primary root; default "<name>root"
  root_users:                         # optional additional account-root users
    - name: backup                    #   uid iam-aviato-root-backup; keys
      state: present                  #   vault_rgw_tenants_aviato_root_backup_access_key/_secret_key
      suspended: false                #   optional; reconciled every converge
  users:                              # optional static-key service users (not account-root)
    - name: etl                       #   uid iam-aviato-svc-etl; keys
      capabilities: [objects-read]    #   vault_rgw_tenants_aviato_svc_etl_access_key/_secret_key
      buckets: ["aviato-data"]        #   optional scope; default every "<name>-*" bucket
      prefixes: ["exports/*"]         #   optional; narrows objects and listing
      state: present                  #   absent = user rm (revokes the keys)
      suspended: false
      access_keys:                    #   optional further keys of this user (also on
        - access_key: AKETLOLDKEY0000001 #   root_users entries and on the tenant itself)
          state: absent               #   absent: removed on the next run
  rotate_keys: false                  # optional; roll this tenant's keys (gated)
  purge_objects: false                # optional; with state:absent, force-delete non-empty buckets

  # Read from this same entry by rgw_iam:
  limits:                             # optional ENTITY cap, merged over rgw_iam_account_limits
    max_users: 10                     #   -1 = unlimited
    max_roles: 100
    max_groups: 10
    max_buckets: 50
    max_access_keys: 4
  roles:                              # optional extra platform roles for this tenant alone
    - name: uploaders                 #   the IAM role is "<tenant>-<name>"
      capabilities: [objects-read, objects-write]   # names from rgw_iam_capabilities
      buckets: ["aviato-data"]        #   optional; default every bucket of the tenant
      prefixes: ["team-a/"]           #   optional; adds a matching s3:prefix condition
  session_duration: 3600              # optional; seconds, RGW bounds it 3600..43200
```

Asserted before anything is created:

- `name`: `^[a-z0-9][a-z0-9-]*$`, at most 55 characters. No underscore: RGW
  rejects one in the `<tenant>-<role>` IAM role name.
- `root_user`: `^[a-z0-9][a-z0-9_-]*$`.
- `root_users[].name` and `users[].name`: `^[a-z0-9][a-z0-9_]*$`;
  `iam-<name>-svc-<user>` at most 64 characters.
- `account_id`: `RGW` plus 17 digits.
- every bucket: a valid S3 name starting with the tenant's prefix.

For the drop-in file form, the naming conventions and the generator playbooks,
see the collection README.

## Day-to-day operations

| Task | How |
|---|---|
| Add a tenant or bucket | append to `rgw_tenants_list` / `item.buckets`, or drop a file into `rgw_tenants_dir`, and re-run |
| Work on one tenant of many | `-e '{"rgw_tenants_limit": ["acme"]}'`; rgw_iam follows it through `rgw_iam_limit` |
| Adjust a storage quota | edit `item.quota` and re-run |
| Adjust an entity limit | set `item.limits.*` (`-1` = unlimited); applied by `rgw_iam` |
| Change what a role may do | `rgw_iam`: `rgw_iam_roles` / `rgw_iam_capabilities`, or the tenant's own `roles:` |
| Add, scope, suspend or offboard a service user | `playbooks/rgw_add_service_user.yml` to add; edit `item.users` (capabilities/buckets/prefixes are applied by `rgw_iam`) |
| Add, suspend or offboard a root user | edit `item.root_users` (`suspended: true` keeps the keys and blocks access; `state: absent` offboards) |
| Rotate user keys | change the vaulted keys, set `rgw_tenants_rotate_keys: true`, run, set it back |
| Add or revoke one more key of a user | list it under the user's `access_keys` as `present` (vaulted pair) or `absent` (access key only), and re-run |
| Decommission a tenant | `item.state: absent`, run with `-e rgw_tenants_allow_teardown=true` |

Every task is idempotent and reports `changed` only on drift. The gates are
described in the collection README.

Before any S3 call, on provision, `--check` and teardown,
the role compares the vaulted keys of each primary account-root user with the cluster's.
On a mismatch it stops with `vault keys for <name> do not match the cluster's keys`.
S3 answers keys the cluster does not hold as it answers a deleted user,
so without the check the S3 steps would treat an existing tenant as missing.
Additional root users and service users get the same check once they exist.

To fix a present tenant, run once with `rotate_keys: true`
or put the previous keys back in the vault.
Rotation does not run for a `state: absent` tenant.
For teardown, restore the previous keys,
or set the tenant back to present and rotate before tearing it down.

Under `--check` with `rotate_keys: true` the rotation is previewed but not applied,
so the cluster still rejects the new keys.
The OIDC and bucket steps are skipped for that tenant
and the run lists it as `key rotation pending`.

If the primary account-root user was suspended by hand, `--check` reports that it would
be enabled. RGW refuses a suspended user's keys, so the OIDC and bucket steps report
`not probed` with a warning. A real run enables the user first and then applies them.

### Keys beyond the primary pair

A user may hold more keys than the vaulted primary pair. Declare them under
`access_keys`, on the tenant for its primary account-root user or on a `root_users` or
`users` entry. A `present` key, given as a vaulted pair, is added when missing. A key
whose secret differs stops the run like a primary mismatch, until a run with
`rotate_keys`. An `absent` key needs only its access key and is removed on every run.

A key the user holds but nobody declared is left alone, and the module reports it in
`unmanaged_key_ids`. A run with `rotate_keys` removes it, along with the old primary,
so declare as `present` every key that should survive a rotation.

The role also checks each existing user's identity and never changes it.
The run stops (`--check` too) and names the mismatch
if a user's display name is not its uid or its account-root status differs from the config.
The user ARN and IAM `UserName` match on the display name,
so a rename would break anything written for the old name.
Fix the user by hand (`radosgw-admin user modify --display-name`,
or recreate it) or correct the config.
Teardown skips the identity check since it removes the user.

Primary root users created by the collection's first 0.1.0 build have the display name
`<tenant>-root-user` and fail this check. Later releases set it to the uid.
Once nothing refers to the old name (check the account's roles and bucket policies),
rename each to its uid:

```sh
radosgw-admin user modify --uid <tenant>root --account-id <account_id> --display-name <tenant>root
```

### Quota

A tenant that omits `quota` takes `rgw_tenants_default_quota`; when both
are empty the account is left at RGW's unlimited default.

| Goal | Change |
|---|---|
| Increase | raise `max_size` / `max_objects` |
| Decrease | lower them; data already over the new cap stays, new writes are refused |
| Unlimited | `max_size: -1` and `max_objects: -1`, or `enabled: false` |
| Track, do not enforce | keep the numbers, set `enabled: false` |

## Read-only modes

```sh
ansible-playbook cephadm-tenants.yml --check --diff                # drift preview
ansible-playbook cephadm-tenants.yml -e rgw_tenants_report=true    # usage vs quota
ansible-playbook cephadm-tenants.yml -e rgw_tenants_discover=true  # brownfield adoption
```

**`--check --diff`** previews every change, including for a tenant that does not exist yet.
RGW rejects the keys of an account-root user it does not have yet with `AccessDenied`,
as it does for a user lacking permission.
The OIDC and bucket steps then run `radosgw-admin user info --access-key` (hence `become`):
if no user holds the key they report `would create`, otherwise the run fails.

The preview fails on a cluster where `rgw_sts` has never been applied,
because this role asserts `rgw_s3_auth_use_sts == true`
and a `--check` run of `rgw_sts` does not set it.
Apply `cephadm-sts.yml` first.

**Report** prints e.g.
`aviato (RGW...001): size 12.4 GiB / 500.0 GiB (2.5%); objects 3540 / unlimited`,
then lists every tenant at or over 80% of a cap. Figures come from cached
account stats.

**Discover** prints a `MANAGED` / `UNMANAGED` line per RGW account
(managed = its `account_id` is already in `rgw_tenants_list`)
and writes an adoption stub `rgw_tenants_discover_output_dir/adopt-<account-id>.yml`
for each unmanaged one.
To adopt: review the stub,
add that account-root's existing S3 keys to the vault
as `vault_rgw_tenants_<name>_access_key` / `_secret_key`
(they are kept unless `rotate_keys: true`),
merge the entry in and run the role.
The run stops before changing anything if the pair is wrong,
or if `root_user` is not an account-root user or its display name is not its uid.
Discover picks the most likely user, which may be wrong for a hand-made account.

Report and discover need no vault keys or OIDC config. Discover also works with
no tenant config at all; report lists a configured tenant with no account as
such.

## Verification

`tasks_from: verify` (or the example play `cephadm-tenants-verify.yml`) checks
accounts, buckets and quotas read-only, with no IdP and no user password. For
IAM roles run `rgw_iam` with `--check`; for the full login, assume and S3 path
use `files/verify_federation.py` (see the collection README).

## Idempotency

- Creating an account or user never touches an existing user's keys;
  only the gated rotation task does.
- Bucket names are cluster-global.
  A bucket name owned by another account (`403` on `HeadBucket`) fails the run.
- If recreating the OIDC provider fails, the previous one is restored.
  Provider changes apply from the next assume without an RGW restart.
- Teardown reports `OK`/`ABSENT` once the account is gone.
