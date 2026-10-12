# underknowledge.ceph_rgw

Ansible collection for multi-tenant S3 on a cephadm-deployed Ceph RGW. Each tenant is its own RGW
account, and end users get short-lived credentials for one role in one tenant from an OIDC login
instead of static S3 keys.

For each tenant a run leaves the RGW account, its account-root user, the buckets, an OIDC provider
inside the account, and the IAM roles `<tenant>-reader`, `<tenant>-writer`, `<tenant>-owner` and
`<tenant>-iamadmin`.

## What it adds over radosgw-admin

RGW squid has accounts, OIDC providers, IAM roles and STS, and all of them can be created by hand
with `radosgw-admin` and boto3. The collection keeps them matching a config file. Beyond that:

- One login can hold roles in several tenants. See
  [How a login becomes S3 access](#how-a-login-becomes-s3-access).
- Platform roles and tenant roles share an account. `rgw_iam` reconciles `<tenant>-<role>` from
  config and never touches the `<tenant>-x-<name>` roles a tenant creates for itself.
- It can run on a schedule. `--check --diff` shows every step before it happens, and teardown,
  purge, key rotation and role pruning sit behind gates that are off by default. See
  [Destructive gates](#destructive-gates) for the exceptions.
- It fails instead of repairing where a repair could break access: a user whose display name or
  account-root status drifted, vaulted keys that differ from the cluster's, and a teardown of an
  account that still holds data.
- Each tenant can live in its own drop-in file. The generator playbooks write those files with
  fresh keys already vault-encrypted. See [Drop-in tenant files](#drop-in-tenant-files).
- Trust policies are updated in place, so live sessions survive a policy change. A new OIDC
  thumbprint applies without restarting RGW.

## What it leaves to you

- `rgw_enable_apis`, TLS on the endpoint, and the IdP. See [Prerequisites](#prerequisites) and
  [`docs/KEYCLOAK-REALM.md`](docs/KEYCLOAK-REALM.md).
- Session revocation, which RGW does not have.
- The Ceph version, which the roles do not check.
- S3 keys on the `radosgw-admin` command line are visible to local users of the bootstrap host.

## Roles

| Role | What it does |
|---|---|
| [`rgw_sts`](roles/rgw_sts/README.md) | Enables native RGW STS: the session-token signing key, `rgw_s3_auth_use_sts`, and standalone `AssumeRole` / `GetSessionToken` roles. |
| [`rgw_tenants`](roles/rgw_tenants/README.md) | Per tenant: an RGW account, its account-root user(s), static-key service users, storage quota, registered OIDC provider and buckets. |
| [`rgw_iam`](roles/rgw_iam/README.md) | Reconciles the web-identity IAM roles `<tenant>-<role>` a federated user assumes inside those accounts, and the scoped inline policy of each service user. |

Run them in that order, as three separate plays. `rgw_tenants` asserts that `rgw_s3_auth_use_sts`
is already true, and `rgw_iam`'s trust policies reference the OIDC providers `rgw_tenants`
registers.

## How a login becomes S3 access

A user holds grants in one multivalued OIDC claim (`rgw_iam_grant_claim`, default `s3iam`). Each
grant is the full path of a group the user belongs to, `<rgw_iam_grant_root>/<tenant>/<role>`
(root default `/services/s3`), and selects one IAM role in one account, so a single login can carry
different roles in several tenants.

```
  user logs in to the OIDC IdP (realm, client "ceph-s3")
      access token carries: iss, aud=ceph-s3,
      s3iam=["/services/s3/aviato/reader", "/services/s3/hooli/owner", ...]

  boto3 sts.assume_role_with_web_identity(
      RoleArn=arn:aws:iam::<account_id>:role/<tenant>-<role>, WebIdentityToken=token)

  RGW validates the token against that account's OIDC provider and matches the role
  trust policy (Federated <account>:oidc-provider/<host>, and the condition
  ForAnyValue:StringEquals on "<host>:s3iam" == "<root>/<tenant>/<role>"),
  then returns temporary credentials scoped by the role's permission policy

  boto3 s3 with the temporary creds: only this account's buckets, only this role's actions
```

A role in one account cannot reach another account's buckets. Within the account, the permission
policy narrows access to the tenant's buckets (`<tenant>-*` by default) and, optionally, to named
`buckets` and object `prefixes`.

Each account holds two role namespaces:

| Name | Owner | Reconciled |
|---|---|---|
| `<tenant>-<role>` | the platform | from config by `rgw_iam`, and pruned when it leaves config |
| `<tenant>-x-<name>` | the tenant | never touched by `rgw_iam` (`rgw_iam_delegated_infix`) |

## Prerequisites

The collection does not configure these.

- **Ceph squid (19.x) or newer.** Every tenant is an RGW account, and accounts arrived in squid.
  The RGW behaviour described here was observed on 19.2.4.
- **`rgw_enable_apis` must contain `sts`, `iam` and `accounts`.** Without `accounts` the account
  and account-user calls fail. For example:
  `ceph config set client.rgw rgw_enable_apis 's3, sts, iam, accounts, admin, notifications'`.
  The value replaces the whole list, so keep any other API you serve (`swift`, `swift_auth`,
  `s3website`).
- **`radosgw-admin` reachable on the bootstrap host** (`groups['mons'][0]` by default), natively,
  in a container, or through `cephadm shell` (slowest, tried last).
- **`boto3` importable on `rgw_tenants_api_host`** (`localhost` by default),
  by the Python Ansible uses for that host.
  The OIDC-provider, bucket and user-policy steps run there without `become`,
  the last one for every `rgw_iam` service user.
  Ansible discovers that Python unless the inventory sets `ansible_python_interpreter`.
  kayobe lists `localhost` without one,
  so the delegated steps get the control host's system Python, not the kayobe venv.
  Install boto3 there, or set `ansible_python_interpreter` for `localhost`
  to a Python that has it.
  The host must reach `rgw_tenants_endpoint`.
  RGW answers `AccessDenied` both for a key nobody holds and for a missing permission,
  so the bootstrap host looks up each tenant's key holder with `radosgw-admin` first
  and passes the result to those steps.
  To keep everything on the Ceph host, set `rgw_tenants_api_host` to the bootstrap host.
- **TLS.** The boto3 steps verify `rgw_tenants_endpoint`'s certificate.
  A public-CA certificate needs nothing.
  For a private CA, set `rgw_tenants_ca_bundle` to a PEM file on `rgw_tenants_api_host`.
- **An IdP that emits the grant claim** (`s3iam` by default) as a multivalued string claim of group
  paths, plus an audience the provider's `client_ids` list.
  [`docs/KEYCLOAK-REALM.md`](docs/KEYCLOAK-REALM.md) sets up a Keycloak realm by hand.

## Install

The roles do not depend on kayobe. Outside it, install with `ansible-galaxy collection install` and
run the three roles as plays against the bootstrap host. The examples below use a kayobe layout,
where the collection is either vendored or installed with galaxy.

**Vendored.** Kayobe puts `$KAYOBE_CONFIG_PATH/ansible/collections` on the collections path, so a
copy there resolves by FQCN with no install step:

```sh
git clone https://github.com/Underknowledge/ansible-collection-ceph-rgw.git /tmp/ceph_rgw
rsync -a --delete --exclude .git --exclude tests --exclude __pycache__ \
  /tmp/ceph_rgw/ $KAYOBE_CONFIG_PATH/ansible/collections/ansible_collections/underknowledge/ceph_rgw/
```

Do not also list the collection in `requirements.yml`, or `kayobe control host bootstrap` installs
a second copy.

**Galaxy.** Add it to `etc/kayobe/ansible/requirements.yml` (see
[`requirements.yml.snippet`](examples/kayobe-config/ansible/requirements.yml.snippet)), then run
`kayobe control host bootstrap`:

```yaml
collections:
  - name: https://github.com/Underknowledge/ansible-collection-ceph-rgw.git
    type: git
    version: main/2025.1   # or another tag/commit
```

Copy the three chain plays from
[`examples/kayobe-config/ansible/ceph/`](examples/kayobe-config/ansible/ceph/) into
`etc/kayobe/ansible/ceph/`, and append their `import_playbook` lines to `cephadm.yml` after
`cephadm-commands-post.yml` (see
[`cephadm.yml.snippet`](examples/kayobe-config/ansible/ceph/cephadm.yml.snippet)).

## Minimum working configuration

In `environments/<env>/cephadm.yml`:

```yaml
rgw_sts_key: "{{ vault_rgw_sts_key }}"
rgw_tenants_endpoint: "http://ceph-rgw.internal.example:8080"
rgw_tenants_oidc:
  provider_url: "https://idp.example.com/realms/myrealm"   # no trailing slash
  client_ids: ["ceph-s3"]
  thumbprints: ["<40-hex sha1 of the JWKS signing cert>"]  # see below
rgw_tenants_list:
  - name: aviato
    account_id: "RGW00000000000000001"
    buckets: ["aviato-data"]
```

In the vault: `vault_rgw_sts_key`, plus `vault_rgw_tenants_aviato_access_key` / `_secret_key`.
Then:

```sh
kayobe playbook run .../ansible/ceph/cephadm.yml --tags cephadm-sts
kayobe playbook run .../ansible/ceph/cephadm.yml --tags cephadm-tenants
kayobe playbook run .../ansible/ceph/cephadm.yml --tags cephadm-iam
```

That yields the account `tenant-aviato`, its account-root user, the bucket `aviato-data`, an OIDC
provider inside the account, and the four IAM roles `aviato-reader`, `aviato-writer`,
`aviato-owner`, `aviato-iamadmin`.

## Variables

Variables and vaulted secrets are prefixed with the role name (`rgw_sts_*`, `vault_rgw_sts_*`,
...). The commonly set ones are below; each role's README and `meta/argument_specs.yml` have the
full list.

### rgw_sts

| Variable | Default | Meaning |
|---|---|---|
| `rgw_sts_key` | `""` | STS session-token signing key, from vault. Required: the role refuses to enable STS auth while it is empty. |
| `rgw_sts_roles` | `[]` | Standalone `AssumeRole` / `GetSessionToken` roles. Usually empty. |

### rgw_tenants

| Variable | Default | Meaning |
|---|---|---|
| `rgw_tenants_endpoint` | `""` | RGW S3/IAM/STS endpoint `rgw_tenants_api_host` can reach. Required. |
| `rgw_tenants_api_host` | `localhost` | Host that runs the boto3 steps. Its Python needs boto3. `rgw_iam_api_host` defaults to it. |
| `rgw_tenants_validate_certs` | `true` | Verify the endpoint's TLS certificate in the boto3 steps. |
| `rgw_tenants_ca_bundle` | `""` | PEM CA bundle on `rgw_tenants_api_host`, for an endpoint with a private-CA certificate. |
| `rgw_tenants_oidc` | `{provider_url, client_ids, thumbprints}` | The IdP realm to federate. Required. |
| `rgw_tenants_list` | `[]` | The tenants (schema below). |
| `rgw_tenants_dir` | `""` | Control-host directory of drop-in tenant files, each appended to `rgw_tenants_list`. |
| `rgw_tenants_default_quota` | `{}` | Fallback storage quota for tenants that omit their own. |

### rgw_iam

| Variable | Default | Meaning |
|---|---|---|
| `rgw_iam_grant_claim` | `s3iam` | Name of the claim carrying the grant group paths. |
| `rgw_iam_grant_root` | `/services/s3` | Root of the grant group tree; a grant is `<root>/<tenant>/<role>`. |
| `rgw_iam_roles` | `reader`, `writer`, `owner`, `iamadmin` | The global catalogue; every tenant gets one role per entry. |
| `rgw_iam_capabilities` | see [rgw_iam capabilities](roles/rgw_iam/README.md#capabilities) | Named action bundles the catalogue composes from. |
| `rgw_iam_session_duration` | `3600` | Session lifetime in seconds, bounded 3600..43200 by RGW. |
| `rgw_iam_account_limits` | `{max_roles: 100}` | Per-account entity ceilings. |

`rgw_iam_provider_url` defaults from `rgw_tenants_oidc.provider_url` and `rgw_iam_dir` from
`rgw_tenants_dir`, so `rgw_iam` also runs standalone.

## Tenant entry schema

One entry per tenant in `rgw_tenants_list`, or one drop-in file per tenant in `rgw_tenants_dir`.
`limits`, `roles` and `session_duration` are read by `rgw_iam`; the rest by `rgw_tenants`.

| Key | Required | Meaning |
|---|---|---|
| `name` | yes | Tenant slug, `^[a-z0-9][a-z0-9-]*$`, at most 55 chars: account-root uid stem and default bucket prefix. No underscore: RGW rejects one in the `<tenant>-<role>` IAM role name. |
| `account_id` | yes | RGW account id, `RGW` plus 17 digits, unique and stable. |
| `state` | no | `present` (default) or `absent` (decommission). |
| `buckets` | no | Bucket names to create as account-root; each must start with `bucket_prefix`. |
| `bucket_prefix` | no | Prefix this tenant's buckets must carry. Default `<name>-`. |
| `quota` | no | Storage cap `{enabled, max_size, max_objects}`. `max_size` in bytes, `-1` unlimited. |
| `root_user` | no | Primary account-root uid, `^[a-z0-9][a-z0-9_-]*$`. Default `<name>root`. |
| `root_users` | no | Additional account-root users, `[{name, state?, suspended?}]` → uid `iam-<name>-root-<slug>`. |
| `rotate_keys` | no | Per-tenant equivalent of `rgw_tenants_rotate_keys`. |
| `purge_objects` | no | With `state: absent`, the destroy-data gate. |
| `limits` | no | Entity ceilings `{max_users, max_roles, max_groups, max_buckets, max_access_keys}`, `-1` unlimited. Consumed by `rgw_iam`. |
| `roles` | no | Extra platform IAM roles for this tenant alone, `[{name, capabilities, buckets?, prefixes?}]`. Consumed by `rgw_iam`. |
| `session_duration` | no | Session lifetime in seconds for this tenant's roles. Consumed by `rgw_iam`. |
| `users` | no | Static-key service users, `[{name, capabilities, buckets?, prefixes?, state?, suspended?}]` → uid `iam-<name>-svc-<slug>`. Created by `rgw_tenants`, scoped by `rgw_iam`. |

`rgw_tenants` validates entries before anything is created. The `rgw_iam` keys are declared in its
`meta/argument_specs.yml` too, so a typo in them fails there; `rgw_iam` does not validate entries
itself.

Per tenant the vault must hold `vault_rgw_tenants_<name>_access_key` / `_secret_key`, plus
`vault_rgw_tenants_<name>_root_<slug>_access_key` / `_secret_key` for each extra root user, and
`vault_rgw_tenants_<name>_svc_<slug>_access_key` / `_secret_key` for each service user. (`<name>`
with its hyphens written as underscores.)

## Drop-in tenant files

Set `rgw_tenants_dir` and every `*.yml`/`*.yaml` in it is loaded and appended to
`rgw_tenants_list`. Deleting a file only stops managing the tenant and leaves its account in the
cluster; to remove it, see [Decommission a tenant](#decommission-a-tenant). Each file holds both the
config and the vaulted keys:

```yaml
# rgw_tenants.d/raviga-web.yml
rgw_tenant_raviga_web:
  name: raviga-web
  account_id: "RGW17887187248433597"
  root_user: raviga-webroot
  buckets:
    - raviga-web-data
    - raviga-web-logs

vault_rgw_tenants_raviga_web_access_key: !vault |
          $ANSIBLE_VAULT;1.1;AES256
          ...
vault_rgw_tenants_raviga_web_secret_key: !vault |
          $ANSIBLE_VAULT;1.1;AES256
          ...
```

The tenant `name` is hyphenated, but Ansible variable names cannot be, so they use underscores.
Drop-ins are collected by `varnames '^rgw_tenant_[a-z0-9_]+$'`; a `rgw_tenant_*` variable with a
hyphen fails the run. `rgw_new_tenant.yml` writes both spellings.

Drop-ins are appended to any inline `rgw_tenants_list` and deduplicated by name, last wins, so a
drop-in overrides an inline entry. `rgw_iam_dir` defaults to `rgw_tenants_dir`.

### `playbooks/rgw_new_tenant.yml`

Generates one such file. The roles never generate keys, they read them from the vault; this
playbook creates them. It does not touch the cluster.

```sh
ansible-playbook playbooks/rgw_new_tenant.yml -e rgw_new_tenant=raviga-web
```

It prompts for the account name when `-e rgw_new_tenant=` is not given. The name must already be
canonical (lowercase `[a-z0-9-]`), because the account name cannot be changed later. Anything else,
such as `Hooli_Data`, fails and names the canonical spelling to re-run with.
`-e rgw_new_tenant="Hooli Data"` fails too: `-e k=v` ends the value at the first space, so Ansible
would pass only `Hooli`.

It allocates an `account_id` (`RGW` + `date +%s%N` truncated to 17 digits, or pass
`-e rgw_new_tenant_account_id=`), mints a 20-char `IAM`-prefixed access key and a 40-char secret
from `random.SystemRandom`, and writes `<tenant>.yml` into `rgw_new_tenant_output_dir` (default
`rgw_tenants.d` next to the playbook, created with mode `0700` if missing; an existing directory is
left as it is). Add `-e '{"rgw_new_tenant_buckets": [...]}'`, `-e '{"rgw_new_tenant_quota": {...}}'`
or `-e '{"rgw_new_tenant_root_users": ["backup", "Erika Schröder"]}'` to seed those keys. The file
uses the layout of the example above; the full schema is in the `rgw_tenants` role README and
`meta/argument_specs.yml`.

Re-running for an existing tenant is refused unless `-e rgw_new_tenant_force=true`, which rewrites
the file from the given inputs, keeps the existing `account_id` and its `root_user` (a different
`rgw_new_tenant_account_id` is refused), and mints new keys. Force without an existing file is
refused too, so a typo in the name or output directory cannot mint a second account_id. Existing
users only pick up the new keys on a rotation run (`rgw_tenants_rotate_keys: true`, see the role
README). Until then `rgw_tenants` fails with
`vault keys for <name> do not match the cluster's keys`.

A vault password is required. The generators use the first of:

1. `-e rgw_new_tenant_vault_password_file=<path>`
2. Ansible's vault settings: `ANSIBLE_VAULT_PASSWORD_FILE` or `vault_password_file` in
   `ansible.cfg`, then `vault_identity_list` (the `vault_encrypt_identity` entry, or else the first
   one)
3. In a kayobe environment, `KAYOBE_VAULT_PASSWORD`. `playbooks/files/kayobe_vault_password.sh`
   hands it to `ansible-vault` as a password script, so the password stays out of argv and off disk.

With none of these set the run fails. `rgw_add_root_user.yml` and `rgw_add_service_user.yml`
resolve the password the same way, with `-e rgw_add_root_user_vault_password_file=<path>` and
`-e rgw_add_service_user_vault_password_file=<path>`.

### `playbooks/rgw_add_root_user.yml`

Adds one account-root user to an existing drop-in. It merges in the `root_users` entry and its two
`!vault` blocks and leaves every existing ciphertext byte-for-byte intact. The config part is
rewritten in the `rgw_new_tenant` layout. Templated values such as
`max_size: "{{ 500 * 1024 ** 3 }}"` are kept as written; comments inside the tenant block are
dropped.

```sh
ansible-playbook playbooks/rgw_add_root_user.yml \
  -e rgw_add_root_user_tenant=raviga-web -e '{"rgw_add_root_user": "Erika Schröder"}'
```

Without `-e` it prompts for both. Give the tenant as written in the drop-in; a spelling that needs
canonicalizing fails, as in `rgw_new_tenant`. A user name with spaces needs the JSON form above,
because `-e k=v` ends the value at the first space; the run fails if that drops words. The user name
is sanitized to a descriptor slug, so the uid becomes `iam-raviga-web-root-erika_schroeder`, and
the report shows the slug next to the input. Re-running with the same user is a no-op. The drop-in
is read from and written to `rgw_add_root_user_output_dir`, which defaults to `rgw_tenants.d` next
to the playbook.

To remove a root user, set `state: absent` and keep the entry until `rgw_tenants` has run
`user rm`, then delete the entry and its two vault blocks. Deleting the entry first orphans the
cluster user. `suspended: true` keeps the keys and blocks access.

### `playbooks/rgw_add_service_user.yml`

Same as above, but writes a static-key service user: a `users:` entry plus its two `!vault` blocks,
uid `iam-<tenant>-svc-<slug>`. Its inputs are `rgw_add_service_user_tenant`,
`rgw_add_service_user`, `rgw_add_service_user_capabilities`, `rgw_add_service_user_output_dir` and
`rgw_add_service_user_vault_password_file`.

```sh
ansible-playbook playbooks/rgw_add_service_user.yml \
  -e rgw_add_service_user_tenant=raviga-web -e rgw_add_service_user=etl \
  -e '{"rgw_add_service_user_capabilities": ["objects-read"]}'
```

`rgw_tenants` creates the user with no permissions; `rgw_iam` adds its inline policy. Narrow it
with `buckets:` / `prefixes:` on the entry. Intended for machine identities that cannot do an OIDC
login; a leaked static key stays valid until it is rotated or the user is removed.

## Read-only modes

None of these change anything on the cluster.

| Mode | Command | Covers |
|---|---|---|
| Drift preview | `--check --diff` | Every step the role would take, including the OIDC provider and the IAM roles a decommission would remove. |
| Usage report | `-e rgw_tenants_report=true` | Used size/objects vs each account quota, and every tenant at or over 80%. Cached account stats, no `--sync-stats`. |
| Brownfield discovery | `-e rgw_tenants_discover=true` | Inventories every RGW account, classifies it managed/unmanaged, and writes an adoption stub per unmanaged account to `rgw_tenants_discover_output_dir`. |
| Verification | `cephadm-tenants-verify.yml` | Accounts, buckets and quotas. No IdP, no user password. |
| IAM plan | `--check --diff` on `rgw_iam` | Every role missing, mis-trusted, stale or broken. A run with no changes means IAM matches config. |

Unit tests cover the logic that needs no cluster (IAM composition and diffing, usage arithmetic,
OIDC replacement, account-root key matching, idempotency against an in-memory cluster): run
`python3 -m unittest discover -s tests/unit` from the collection root.
`tests/integration/converge_twice.sh` converges the three roles twice against a live test cluster,
named by fsid in a test-cluster file so a run cannot reach another cluster, with a check-mode run
before and after, and fails unless the second converge reports `changed=0`. See
[`tests/integration/README.md`](tests/integration/README.md).

`roles/rgw_tenants/files/verify_federation.py` tests login, assume and scoped S3 for one user,
including negative checks:

```sh
EP=https://s3.example.com \
IDP=https://idp.example.com/realms/myrealm/protocol/openid-connect/token \
CLIENT=ceph-s3 ACCOUNT_ID=RGW00000000000000001 \
TENANT=aviato ROLE=reader EXPECT=ro PASSWORD=... \
CROSS_BUCKET=hooli-data \
python3 roles/rgw_tenants/files/verify_federation.py
```

`USERNAME` defaults to `<TENANT>-<ROLE>`, and that login user must hold the grant
`/services/s3/<TENANT>/<ROLE>` in the grant claim. `EXPECT` is `rw` by default, or `ro` when a
write must be denied. `CROSS_BUCKET` names another tenant's bucket, which must be unreachable.
`BUCKET` is the tenant bucket under test, `<TENANT>-data` by default, and `AWS_REGION` defaults to
`us-east-1`.

## Destructive gates

The roles are idempotent. Operations that destroy data or credentials are gated:

| Gate | Default | Effect when off |
|---|---|---|
| `rgw_tenants_rotate_keys` (or per-tenant `rotate_keys`) | `false` | The primary keys of an existing account-root user, additional root user or service user are never touched. Keys declared under `access_keys` are still added (`present`) or removed (`absent`). |
| `rgw_tenants_allow_teardown` | `false` | `state: absent` entries are reported and skipped, so committing one never deletes a tenant. |
| `rgw_tenants_allow_purge` | `false` | A tenant's `purge_objects` has no effect; a non-empty bucket or account fails the run instead of being destroyed. |
| `rgw_tenants_allow_root_user_removal` | `true` | `root_users` marked `state: absent` are reported and skipped. |
| `rgw_iam_prune` | `false` | Stale platform roles and stray policies are reported, not removed. |

Service users marked `state: absent` on a present tenant have no gate and are removed on the next
run.

### Rotate keys

Update `vault_rgw_tenants_<name>_access_key` / `_secret_key` (or the `_root_<slug>_` /
`_svc_<slug>_` ones for an additional root user or service user), set
`rgw_tenants_rotate_keys: true` (or the tenant's `rotate_keys`), run once, set it back. The task
installs the vaulted key and removes every other access key on that user, except those declared
`present` under the user's `access_keys`. A key left behind by an interrupted rotation goes on the
next run with the gate on. It is idempotent while on. Changing the vaulted keys without the gate
fails the run before any S3 step (see the role README).

### Decommission a tenant

Set `state: absent` on the entry and run with `-e rgw_tenants_allow_teardown=true`. Teardown is
idempotent and runs in this order: buckets, OIDC provider, IAM roles, service users, additional
root users, the primary account-root user, the account. It first checks the vaulted account-root
keys against the cluster and fails on a mismatch, since S3 would otherwise report the buckets and
OIDC provider as already gone. Restore the previous keys to continue.

```yaml
rgw_tenants_list:
  - name: aviato
    account_id: "RGW00000000000000001"
    state: absent
    buckets: ["aviato-data"]  # keep the list so they get removed
    # purge_objects: true   # only if they still hold data (destructive)
```

The IAM step deletes every role under the `<name>-` prefix, including any `<name>-x-*` roles the
tenant created, after removing their policies. `rgw_iam` skips tenants marked `state: absent`.

If the tenant created its own buckets, users or roles, `account rm` refuses the non-empty account
and the run fails without deleting anything. Setting `purge_objects: true` and
`-e rgw_tenants_allow_purge=true` runs `account rm --purge-data` instead. Keep the entry and its
vault keys until the run reports all `OK`, then delete both.

## Modules and filters

The roles are built from these, and they can be called from your own plays. Each module documents
its options with `ansible-doc underknowledge.ceph_rgw.<name>`.

| Module | Purpose |
|---|---|
| `rgw_account` | Manage a Ceph RGW account and its storage quota. |
| `rgw_account_info` | Read-only inventory of Ceph RGW accounts. |
| `rgw_account_user` | Manage a Ceph RGW account user (account-root or service user) and its S3 keys. |
| `rgw_bucket` | Manage a tenant's S3 buckets with its account-root keys. |
| `rgw_config` | Set one Ceph cluster configuration option, on drift only. |
| `rgw_config_info` | Read Ceph runtime configuration options. |
| `rgw_iam_sync` | Reconcile the per-tenant IAM roles a federated user assumes. |
| `rgw_key_holder_info` | Find the RGW user that holds an access key, for the boto3 modules' `key_holder`. |
| `rgw_oidc_provider` | Register an OIDC realm as an RGW OIDC provider in one account. |
| `rgw_orch_restart` | Restart a cephadm-orchestrated service and wait for its daemons. |
| `rgw_role` | Manage an RGW IAM role, its trust policy and its inline policies. |
| `rgw_role_info` | Read-only inventory of the IAM roles in an RGW account. |
| `rgw_user_policy` | Manage the inline permission policy of an RGW account user. |

| Filter | Purpose |
|---|---|
| `ceph_account_slug`, `ceph_descriptor`, `ceph_root_uid`, `ceph_account_valid` | Build and check identifiers, see [Naming conventions](#naming-conventions). |
| `rgw_service_user_name` | The uid of a service user, `iam-<tenant>-svc-<name>`. |
| `rgw_service_users` | The declared service users from an IAM spec, each with its composed policy. |
| `ceph_usage_report`, `ceph_over_quota` | Join account stats to tenants with usage percentages, and list those over a threshold. |

## Naming conventions

The generator playbooks build identifiers with the `ceph_naming` filters and
`rgw_service_user_name` for service users. The primary root is `<account>root`, built inline. The
user ARN is `<account_id>:user/<display-name>`, so uid and display-name are always the same string.

| Thing | Format | Example |
|---|---|---|
| Tenant name (the RGW account is `tenant-<name>`) | lowercase `[a-z0-9-]`, ≤55 | `raviga-data-prod` |
| Primary root | `<account>root` (the role default) | `raviga-data-prodroot` |
| Extra root | `iam-<account>-root-<descriptor>` | `iam-hooli-data-root-backup` |
| Service user | `iam-<account>-svc-<descriptor>` | `iam-hooli-data-svc-etl` |
| IdP grant group | `<rgw_iam_grant_root>/<account>/<role>` | `/services/s3/hooli-data/uploaders` |

The two slugs fold in opposite directions. A person or label goes through `ceph_descriptor`:
umlauts transliterated (`ö→oe`, `ä→ae`, `ü→ue`, `ß→ss`), other diacritics stripped, spaces and
hyphens folded to `_`, so `"Erika Schröder"` becomes `erika_schroeder`. A hyphen-free descriptor
keeps `iam-<account>-root-<descriptor>` splittable on `-root-`. An account name goes through
`ceph_account_slug`, which folds spaces and underscores to `-`, because RGW role names cannot
contain `_` and roles are named `<account>-<role>`.

The generators do not sanitize account names. A name that `ceph_account_slug` would change fails
the run, because the account name determines the uid, the vault key names, the bucket prefix and
every role ARN, and cannot be changed later. User names are sanitized and the slug is reported.

`rgw_new_tenant` writes `root_user: <name>root`, the role's default, so generated and hand-written
tenants name the primary root the same way. A drop-in that pins `root_user: iam-<name>-root` keeps
working, and a forced regeneration keeps that value. Do not rename a primary root that exists on
the cluster: the role would create a second one.

## Compute the OIDC thumbprint

The thumbprint is the SHA1 of the JWKS signing cert (the `use=sig` key's `x5c`), not the IdP's TLS
chain. The role rejects any value that is not 40 hex characters, including the example's
`CHANGEME` placeholder.

```sh
curl -s https://idp.example.com/realms/myrealm/protocol/openid-connect/certs \
  | python3 -c 'import sys,json,base64,hashlib; \
      k=[k for k in json.load(sys.stdin)["keys"] if k.get("use")=="sig"][0]; \
      der=base64.b64decode(k["x5c"][0]); print(hashlib.sha1(der).hexdigest())'
```

Recompute and re-run whenever the realm signing key rotates. RGW cannot update thumbprints, so the
module deletes and recreates the provider (rolling back if the recreate fails). RGW reads the
provider on every `AssumeRoleWithWebIdentity`, so it needs no restart (verified on 19.2.4).

If assumes fail with `Signature can not be validated with the JWKS present`, no key in the realm's
JWKS validated the token. Check that a registered thumbprint matches the signing key's `x5c` cert
and that the token has not expired. Squid releases without the fix from ceph PR #64936 only accept
a token signed by the first key in the JWKS, so they fail once the realm publishes more than one
key.

## Operational notes

- `rgw_sts` restarts RGW when it changes the STS config. `rgw_orch_restart` returns once every
  daemon of the service is running again (up to `wait_timeout`, 300s by default), and
  `rgw_tenants` waits for the endpoint to answer before its boto3 steps. `rgw_iam` waits too when
  a tenant declares service users, the only case where it calls the endpoint.
- `rgw_iam_provider_url` must have no trailing slash, or every assume fails with `AccessDenied`.
  The role asserts this.
- Policy action names are RGW's, not AWS's. One unrecognised name makes RGW discard the whole
  policy, so `rgw_iam` treats any parse error as fatal.
- Trust policies are updated in place. Recreating a role would change its `RoleId` and invalidate
  live sessions.
- Tagging a role breaks every assume of it, and the tag cannot be removed on 19.2.4. `rgw_iam`
  reports such a role as `BROKEN` and rebuilds it under `rgw_iam_prune`.
- There is no session revocation. A withdrawn grant stays usable until the session expires
  (`rgw_iam_session_duration`).
- Account-scoped `radosgw-admin` commands need `--account-id`, or they 404.
- `radosgw-admin` and `ceph config set` take keys on argv. The tasks are `no_log`, but the keys are
  visible to local users of the bootstrap host while the command runs. The boto3 steps take
  credentials from the environment.

## License

Apache-2.0.
