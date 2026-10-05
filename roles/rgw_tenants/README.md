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
| `rgw_tenants_default_quota` | `{}` | Fallback storage quota for tenants that omit their own. `{}` leaves them at RGW's unlimited default. |
| `rgw_tenants_rotate_keys` | `false` | Gate: roll every tenant's account-root keys to the vaulted value. |
| `rgw_tenants_allow_teardown` | `false` | Gate: run the `state: absent` decommission path. |
| `rgw_tenants_allow_purge` | `false` | Gate: let teardown destroy data. |
| `rgw_tenants_allow_root_user_removal` | `true` | Honor a `root_users` entry marked `state: absent`. |
| `rgw_tenants_report` | `false` | Read-only usage-vs-quota report, then end the play. |
| `rgw_tenants_discover` | `false` | Read-only brownfield discovery, then end the play. |
| `rgw_tenants_discover_output_dir` | `{{ playbook_dir }}/rgw-tenants-discovery` | Where discovery writes adoption stubs. |
| `rgw_tenants_s3_region` | `us-east-1` | SigV4 signing region boto3 sends; does not affect bucket placement. |
| `rgw_tenants_service_name` | `rgw.rgw` | cephadm RGW service restarted on an OIDC change. |
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
- name: acme                          # slug: account-root uid stem + default bucket prefix
  account_id: "RGW00000000000000001"  # "RGW" + 17 digits, unique and stable
  state: present                      # optional; "absent" decommissions
  buckets: ["acme-data", "acme-logs"] # optional; each must start with the prefix
  bucket_prefix: "acme-"              # optional; default "<name>-"
  quota:                              # optional storage cap; omit for unlimited
    enabled: true                     #   false = tracked but not enforced
    max_size: "{{ 100 * 1024 ** 3 }}" #   BYTES or -1; let kayobe do the GiB math
    max_objects: -1                   #   count or -1
  root_user: iam-acme-root            # optional primary root; default "<name>root"
  root_users:                         # optional additional account-root users
    - name: backup                    #   uid iam-acme-root-backup; keys
      state: present                  #   vault_rgw_tenants_acme_root_backup_access_key/_secret_key
      suspended: false                #   optional; reconciled every converge
  users:                              # optional static-key service users (not account-root)
    - name: etl                       #   uid iam-acme-svc-etl; keys
      capabilities: [objects-read]    #   vault_rgw_tenants_acme_svc_etl_access_key/_secret_key
      buckets: ["acme-data"]          #   optional scope; default every "<name>-*" bucket
      prefixes: ["exports/*"]         #   optional; narrows objects and listing
      state: present                  #   absent = user rm (revokes the keys)
      suspended: false
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
      buckets: ["acme-data"]          #   optional; default every bucket of the tenant
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
| Adjust a storage quota | edit `item.quota` and re-run |
| Adjust an entity limit | set `item.limits.*` (`-1` = unlimited); applied by `rgw_iam` |
| Change what a role may do | `rgw_iam`: `rgw_iam_roles` / `rgw_iam_capabilities`, or the tenant's own `roles:` |
| Add, scope, suspend or offboard a service user | `playbooks/rgw_add_service_user.yml` to add; edit `item.users` (capabilities/buckets/prefixes are applied by `rgw_iam`) |
| Add, suspend or offboard a root user | edit `item.root_users` (`suspended: true` keeps the keys and blocks access; `state: absent` offboards) |
| Rotate account-root keys | change the vaulted keys, set `rgw_tenants_rotate_keys: true`, run, set it back |
| Decommission a tenant | `item.state: absent`, run with `-e rgw_tenants_allow_teardown=true` |

Every task is idempotent and reports `changed` only on drift. The gates are
described in the collection README.

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

**`--check --diff`** previews every change, including for a tenant that does not
exist yet. It fails on a cluster where `rgw_sts` has never been applied, because
this role asserts `rgw_s3_auth_use_sts == true` and a `--check` run of `rgw_sts`
does not set it. Apply `cephadm-sts.yml` first.

**Report** prints e.g.
`acme (RGW...001): size 12.4 GiB / 500.0 GiB (2.5%); objects 3540 / unlimited`,
then lists every tenant at or over 80% of a cap. Figures come from cached
account stats.

**Discover** prints a `MANAGED` / `UNMANAGED` line per RGW account (managed =
its `account_id` is already in `rgw_tenants_list`) and writes an adoption stub
`rgw_tenants_discover_output_dir/adopt-<account-id>.yml` for each unmanaged one.
To adopt: review the stub, add that account-root's **existing** S3 keys to the
vault as `vault_rgw_tenants_<name>_access_key` / `_secret_key` (they are kept
unless `rotate_keys: true`), merge the entry in and run the role.

Report and discover need no vault keys or OIDC config. Discover also works with
no tenant config at all; report lists a configured tenant with no account as
such.

## Verification

`tasks_from: verify` (or the example play `cephadm-tenants-verify.yml`) checks
accounts, buckets and quotas read-only, with no IdP and no user password. For
IAM roles run `rgw_iam` with `--check`; for the full login, assume and S3 path
use `files/verify_federation.py` (see the collection README).

## Idempotency

- Creating an account or user never touches an existing user's keys; only the
  gated rotation task does.
- Bucket names are cluster-global. A bucket name owned by another account
  (`403` on `HeadBucket`) fails the run.
- If recreating the OIDC provider fails, the previous one is restored.
- Teardown reports `OK`/`ABSENT` once the account is gone.
