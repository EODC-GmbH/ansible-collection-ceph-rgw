# underknowledge.ceph_rgw

Ansible collection for multi-tenant S3 with OIDC federation on a
cephadm-deployed Ceph RGW. Each tenant is a separate RGW account. End users log
in to an OIDC IdP and get short-lived, tenant- and role-scoped credentials
through `AssumeRoleWithWebIdentity` instead of static S3 keys.

| Role | What it does |
|---|---|
| [`rgw_sts`](roles/rgw_sts/README.md) | Enables native RGW STS: the session-token signing key, `rgw_s3_auth_use_sts`, and standalone `AssumeRole` / `GetSessionToken` roles. |
| [`rgw_tenants`](roles/rgw_tenants/README.md) | Per tenant: an RGW account, its account-root user(s), static-key service users, storage quota, registered OIDC provider and buckets. |
| [`rgw_iam`](roles/rgw_iam/README.md) | Reconciles the web-identity IAM roles `<tenant>-<role>` a federated user assumes inside those accounts, and the scoped inline policy of each service user. |

Run them in that order, as three separate plays. `rgw_tenants` asserts that
`rgw_s3_auth_use_sts` is already true, and `rgw_iam`'s trust policies reference
the OIDC providers `rgw_tenants` registers.

## Prerequisites

The collection does not configure these.

- **Ceph squid (19.x) or newer.** Every tenant is an RGW account, and accounts
  arrived in squid. The RGW behaviour described here was observed on 19.2.4.
  The roles do not check the Ceph version.
- **`rgw_enable_apis` must contain `sts`, `iam` and `accounts`.** Without
  `accounts` the account and account-user calls fail. For example:
  `ceph config set client.rgw rgw_enable_apis 's3, sts, iam, accounts, admin, notifications'`.
  The value replaces the whole list, so keep any other API you serve
  (`swift`, `swift_auth`, `s3website`).
- **`radosgw-admin` reachable on the bootstrap host** (`groups['mons'][0]` by
  default), natively, in a container, or through `cephadm shell` (slowest, tried
  last).
- **`boto3` importable by the Python that runs the modules** on the bootstrap
  host. Only the OIDC-provider and bucket steps need it.
- **TLS.** The boto3 steps verify `rgw_tenants_endpoint`'s certificate. A
  public-CA certificate needs nothing; for a private CA set
  `rgw_tenants_ca_bundle` to a PEM file on the bootstrap host.
- **An IdP that emits the grant claim** (`ceph_grants` by default) as a
  multivalued string claim, plus an audience the provider's `client_ids` list.
  [`docs/KEYCLOAK-REALM.md`](docs/KEYCLOAK-REALM.md) sets up a Keycloak realm by hand.

**Untested:** Keystone S3 auth (`rgw_s3_auth_use_keystone true`) and Swift on
the same RGW. The collection does not touch Keystone, but it was only tested
with Keystone auth off.

## Install

Either vendor it or install it with galaxy.

**Vendored.** Kayobe puts `$KAYOBE_CONFIG_PATH/ansible/collections` on the
collections path, so a copy there resolves by FQCN with no install step:

```sh
git clone https://github.com/EODC-GmbH/ansible-collection-ceph-rgw.git /tmp/ceph_rgw
rsync -a --delete --exclude .git --exclude tests --exclude __pycache__ \
  /tmp/ceph_rgw/ $KAYOBE_CONFIG_PATH/ansible/collections/ansible_collections/underknowledge/ceph_rgw/
```

Do not also list the collection in `requirements.yml`, or
`kayobe control host bootstrap` installs a second copy.

**Galaxy.** Add it to `etc/kayobe/ansible/requirements.yml` (see
[`requirements.yml.snippet`](examples/kayobe-config/ansible/requirements.yml.snippet)),
then run `kayobe control host bootstrap`:

```yaml
collections:
  - name: https://github.com/EODC-GmbH/ansible-collection-ceph-rgw.git
    type: git
    version: main/2025.1   # or a tag/commit
```

Copy the three chain plays from
[`examples/kayobe-config/ansible/ceph/`](examples/kayobe-config/ansible/ceph/)
into `etc/kayobe/ansible/ceph/`, and append their `import_playbook` lines to
`cephadm.yml` after `cephadm-commands-post.yml` (see
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
  - name: acme
    account_id: "RGW00000000000000001"
    buckets: ["acme-data"]
```

In the vault: `vault_rgw_sts_key`, plus
`vault_rgw_tenants_acme_access_key` / `_secret_key`. Then:

```sh
kayobe playbook run .../ansible/ceph/cephadm.yml --tags cephadm-sts
kayobe playbook run .../ansible/ceph/cephadm.yml --tags cephadm-tenants
kayobe playbook run .../ansible/ceph/cephadm.yml --tags cephadm-iam
```

That yields the account `acme`, its account-root user, the bucket `acme-data`,
an OIDC provider inside the account, and the four IAM roles `acme-reader`,
`acme-writer`, `acme-owner`, `acme-iamadmin`.

## How a login becomes S3 access

A user holds grants of the form `<tenant>:<role>` in one multivalued OIDC claim
(`rgw_iam_grant_claim`, default `ceph_grants`). Each grant selects one IAM
role in one account, so a single login can carry different roles in several
tenants.

```
  user logs in to the OIDC IdP (realm, client "ceph-s3")
      access token carries: iss, aud=ceph-s3,
      ceph_grants=["acme:reader", "globex:owner", ...]

  boto3 sts.assume_role_with_web_identity(
      RoleArn=arn:aws:iam::<account_id>:role/<tenant>-<role>, WebIdentityToken=token)

  RGW validates the token against that account's OIDC provider and matches the role
  trust policy (Federated <account>:oidc-provider/<host>, and the condition
  ForAnyValue:StringEquals on "<host>:ceph_grants" == "<tenant>:<role>"),
  then returns temporary credentials scoped by the role's permission policy

  boto3 s3 with the temporary creds: only this account's buckets, only this role's actions
```

A role in one account cannot reach another account's buckets. Within the account, the permission policy narrows
access to the tenant's buckets (`<tenant>-*` by default) and, optionally, to
named `buckets` and object `prefixes`.

Each account holds two role namespaces:

| Name | Owner | Reconciled |
|---|---|---|
| `<tenant>-<role>` | the platform | from config by `rgw_iam`, and pruned when it leaves config |
| `<tenant>-x-<name>` | the tenant | never touched by `rgw_iam` (`rgw_iam_delegated_infix`) |

## Variables

Variables and vaulted secrets are prefixed with the role name (`rgw_sts_*`,
`vault_rgw_sts_*`, ...). The commonly set ones are below; each role's README and
`meta/argument_specs.yml` have the full list.

### rgw_sts

| Variable | Default | Meaning |
|---|---|---|
| `rgw_sts_key` | `""` | STS session-token signing key, from vault. Required: the role refuses to enable STS auth while it is empty. |
| `rgw_sts_roles` | `[]` | Standalone `AssumeRole` / `GetSessionToken` roles. Usually empty. |

### rgw_tenants

| Variable | Default | Meaning |
|---|---|---|
| `rgw_tenants_endpoint` | `""` | RGW S3/IAM/STS endpoint the bootstrap host can reach. Required. |
| `rgw_tenants_validate_certs` | `true` | Verify the endpoint's TLS certificate in the boto3 steps. |
| `rgw_tenants_ca_bundle` | `""` | PEM CA bundle on the bootstrap host, for an endpoint with a private-CA certificate. |
| `rgw_tenants_oidc` | `{provider_url, client_ids, thumbprints}` | The IdP realm to federate. Required. |
| `rgw_tenants_list` | `[]` | The tenants (schema below). |
| `rgw_tenants_dir` | `""` | Control-host directory of drop-in tenant files, each appended to `rgw_tenants_list`. |
| `rgw_tenants_default_quota` | `{}` | Fallback storage quota for tenants that omit their own. |
| `rgw_tenants_service_name` | `rgw.rgw` | cephadm RGW service restarted on an OIDC change. |

### rgw_iam

| Variable | Default | Meaning |
|---|---|---|
| `rgw_iam_grant_claim` | `ceph_grants` | Name of the claim carrying the grants. |
| `rgw_iam_roles` | `reader`, `writer`, `owner`, `iamadmin` | The global catalogue; every tenant gets one role per entry. |
| `rgw_iam_capabilities` | see defaults | Named action bundles the catalogue composes from. |
| `rgw_iam_session_duration` | `3600` | Session lifetime in seconds, bounded 3600..43200 by RGW. |
| `rgw_iam_account_limits` | `{max_roles: 100}` | Per-account entity ceilings. |

`rgw_iam_provider_url` defaults from `rgw_tenants_oidc.provider_url` and
`rgw_iam_dir` from `rgw_tenants_dir`, so `rgw_iam` also runs standalone.

## Tenant entry schema

One entry per tenant in `rgw_tenants_list`, or one drop-in file per tenant in
`rgw_tenants_dir`. `limits`, `roles` and `session_duration` are read by
`rgw_iam`; the rest by `rgw_tenants`.

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

Entries are validated before anything is created. The `rgw_iam` keys are
declared in `rgw_tenants`' `meta/argument_specs.yml` too, because whichever role
loads an entry first validates it.

Per tenant the vault must hold `vault_rgw_tenants_<name>_access_key` /
`_secret_key`, plus `vault_rgw_tenants_<name>_root_<slug>_access_key` /
`_secret_key` for each extra root user, and
`vault_rgw_tenants_<name>_svc_<slug>_access_key` / `_secret_key` for each
service user. (`<name>` with its hyphens written as underscores.)

## Drop-in tenant files

Set `rgw_tenants_dir` and every `*.yml`/`*.yaml` in it is loaded and appended to
`rgw_tenants_list`: a tenant is added by dropping one file in and removed by
deleting it. Each file holds both the config and the vaulted keys:

```yaml
# rgw_tenants.d/initrode-web.yml
rgw_tenant_initrode_web:
  name: initrode-web
  account_id: "RGW17887187248433597"
  root_user: iam-initrode-web-root
  buckets: [initrode-web-data, initrode-web-logs]
vault_rgw_tenants_initrode_web_access_key: !vault |
  $ANSIBLE_VAULT;1.1;AES256;...
vault_rgw_tenants_initrode_web_secret_key: !vault |
  $ANSIBLE_VAULT;1.1;AES256;...
```

The tenant `name` is hyphenated, but the variable names are underscored,
because Ansible variable names are identifiers. Drop-ins are collected by
`varnames '^rgw_tenant_[a-z0-9_]+$'`; a `rgw_tenant_*` variable with a hyphen
fails the run. `rgw_new_tenant.yml` writes both spellings.

Drop-ins are appended to any inline `rgw_tenants_list` and deduplicated by name,
last wins, so a drop-in overrides an inline entry. `rgw_iam_dir` defaults to
`rgw_tenants_dir`.

### `playbooks/rgw_new_tenant.yml`

Generates one such file. The roles never generate keys, they read them from
the vault; this playbook creates them. It does not touch the cluster.

```sh
ansible-playbook playbooks/rgw_new_tenant.yml -e rgw_new_tenant=initrode-web
```

It prompts for the account name when `-e rgw_new_tenant=` is not given, allocates
an `account_id` (`RGW` + `date +%s%N` truncated to 17 digits, or pass
`-e rgw_new_tenant_account_id=`), mints a 20-char `IAM`-prefixed access key and a
40-char secret from `random.SystemRandom`, and writes `<tenant>.yml` into
`rgw_new_tenant_output_dir` (default `./rgw_tenants.d`). Add
`-e '{"rgw_new_tenant_buckets": [...]}'`, `-e '{"rgw_new_tenant_quota": {...}}'`
or `-e '{"rgw_new_tenant_root_users": ["backup", "Erika Schröder"]}'` to seed
those keys. Re-running for an existing tenant is refused unless
`-e rgw_new_tenant_force=true`, which rewrites the file from the given inputs,
keeps the existing `account_id` (a different `rgw_new_tenant_account_id` is
refused) and mints new keys. Existing users only pick up the new keys on a
rotation run (`rgw_tenants_rotate_keys: true`, see the role README). A vault
password is required: `ANSIBLE_VAULT_PASSWORD_FILE`, or
`-e rgw_new_tenant_vault_password_file=<path>`.

### `playbooks/rgw_add_root_user.yml`

Adds one account-root user to an existing drop-in, merging the `root_users`
entry and its two `!vault` blocks in and leaving every existing ciphertext
byte-for-byte intact.

```sh
ansible-playbook playbooks/rgw_add_root_user.yml \
  -e rgw_add_root_user_tenant=initrode-web -e '{"rgw_add_root_user": "Erika Schröder"}'
```

Run without `-e` it prompts for both. A name with spaces needs the JSON form
above, because `-e k=v` splits on spaces. The name is sanitized to a descriptor
slug, so the uid becomes `iam-initrode-web-root-erika_schroeder`. Re-running
with the same user is a no-op.

To remove a root user, set `state: absent` and **keep the entry** until
`rgw_tenants` has run `user rm`, then delete the entry and its two vault blocks.
Deleting the entry first orphans the cluster user. `suspended: true` keeps the
keys and blocks access.

### `playbooks/rgw_add_service_user.yml`

Same as above, but writes a static-key service user: a `users:` entry plus
its two `!vault` blocks, uid `iam-<tenant>-svc-<slug>`.

```sh
ansible-playbook playbooks/rgw_add_service_user.yml \
  -e rgw_add_root_user_tenant=initrode-web -e rgw_add_root_user=etl \
  -e '{"rgw_add_service_user_capabilities": ["objects-read"]}'
```

`rgw_tenants` creates the user with no permissions; `rgw_iam` adds its inline
policy. Narrow it with `buckets:` / `prefixes:` on the entry. Intended for
machine identities that cannot do an OIDC login; a leaked static key stays
valid until it is rotated or the user is removed.

## Read-only modes

None of these change anything on the cluster.

| Mode | Command | Covers |
|---|---|---|
| Drift preview | `--check --diff` | Every step the role would take, including the OIDC provider and the IAM roles a decommission would remove. |
| Usage report | `-e rgw_tenants_report=true` | Used size/objects vs each account quota, and every tenant at or over 80%. Cached account stats, no `--sync-stats`. |
| Brownfield discovery | `-e rgw_tenants_discover=true` | Inventories every RGW account, classifies it managed/unmanaged, and writes an adoption stub per unmanaged account to `rgw_tenants_discover_output_dir`. |
| Verification | `cephadm-tenants-verify.yml` | Accounts, buckets and quotas. No IdP, no user password. |
| IAM plan | `--check --diff` on `rgw_iam` | Every role missing, mis-trusted, stale or broken. A run with no changes means IAM matches config. |

Unit tests for the logic that needs no cluster (IAM composition and diffing,
naming, usage arithmetic, OIDC replacement):
`python3 -m unittest discover -s tests/unit` from the collection root.

`roles/rgw_tenants/files/verify_federation.py` tests login, assume and scoped S3
for one user, including negative checks:

```sh
EP=https://s3.example.com \
IDP=https://idp.example.com/realms/myrealm/protocol/openid-connect/token \
CLIENT=ceph-s3 ACCOUNT_ID=RGW00000000000000001 \
TENANT=acme ROLE=reader EXPECT=ro PASSWORD=... \
CROSS_BUCKET=globex-data \
python3 roles/rgw_tenants/files/verify_federation.py
```

`USERNAME` defaults to `<TENANT>-<ROLE>`, and that login user must hold the
grant `<TENANT>:<ROLE>` in the grant claim. `EXPECT` is `rw` by default, or `ro`
when a write must be denied. `CROSS_BUCKET` names another tenant's bucket, which
must be unreachable.

## Destructive gates

The roles are idempotent. Operations that destroy data or credentials are
gated:

| Gate | Default | Effect when off |
|---|---|---|
| `rgw_tenants_rotate_keys` (or per-tenant `rotate_keys`) | `false` | An existing account-root user's keys are never touched. |
| `rgw_tenants_allow_teardown` | `false` | `state: absent` entries are reported and skipped, so committing one never deletes a tenant. |
| `rgw_tenants_allow_purge` | `false` | A tenant's `purge_objects` has no effect; a non-empty bucket or account fails the run instead of being destroyed. |
| `rgw_tenants_allow_root_user_removal` | `true` | `root_users` marked `state: absent` are reported and skipped. |
| `rgw_iam_prune` | `false` | Stale platform roles and stray policies are reported, not removed. |

### Rotate account-root keys

Update `vault_rgw_tenants_<name>_access_key` / `_secret_key`, set
`rgw_tenants_rotate_keys: true` (or the tenant's `rotate_keys`), run once, set it
back. The task installs the vaulted key and removes every other access key on
that user. It is idempotent while on.

### Decommission a tenant

Set `state: absent` on the entry and run with
`-e rgw_tenants_allow_teardown=true`. Teardown is idempotent and runs in this
order: buckets, OIDC provider, IAM roles, additional root users, the primary
account-root user, the account.

```yaml
rgw_tenants_list:
  - name: acme
    account_id: "RGW00000000000000001"
    state: absent
    buckets: ["acme-data"]  # keep the list so they get removed
    # purge_objects: true   # only if they still hold data (destructive)
```

The IAM step deletes every role under the `<name>-` prefix, including any
`<name>-x-*` roles the tenant created, after removing their policies.
`rgw_iam` skips tenants marked `state: absent`.

If the tenant created its own buckets, users or roles, `account rm` refuses the
non-empty account and the run fails without deleting anything. Setting
`purge_objects: true` **and** `-e rgw_tenants_allow_purge=true` runs
`account rm --purge-data` instead. Keep the entry and its vault keys until the
run reports all `OK`, then delete both.

## Naming conventions

`rgw_new_tenant` and `rgw_add_root_user` build every identifier through the
`ceph_naming` filters (`ceph_descriptor`, `ceph_root_uid`, `ceph_account_valid`,
`ceph_group`). The user ARN is `<account_id>:user/<display-name>`, so uid and
display-name are always the same string.

| Thing | Format | Example |
|---|---|---|
| Account name | lowercase `[a-z0-9-]`, ≤55 | `initrode-data-prod` |
| Primary root | `iam-<account>-root` | `iam-initrode-data-prod-root` |
| Extra root | `iam-<account>-root-<descriptor>` | `iam-globex-data-root-backup` |
| Service user | `iam-<account>-svc-<descriptor>` | `iam-globex-data-svc-etl` |
| Group | `<account>-<role>`, no `iam-` prefix | `globex-data-uploaders` |

The two slugs fold in opposite directions. A person or label goes through
`ceph_descriptor`: umlauts transliterated (`ö→oe`, `ä→ae`,
`ü→ue`, `ß→ss`), other diacritics stripped, spaces **and hyphens** folded to
`_`, so `"Erika Schröder"` becomes `erika_schroeder`. A hyphen-free descriptor
keeps `iam-<account>-root-<descriptor>` splittable on `-root-`. An account name
goes through `ceph_account_slug`, which folds spaces and underscores **to `-`**,
because RGW role names cannot contain `_` and roles are named
`<account>-<role>`.

The generator warns when sanitizing changed the input. An account name cannot be
changed later: it determines the uid, the vault key names, the bucket prefix and
every role ARN. The role's own default `root_user` is `<name>root`, not
`iam-<name>-root`.

## Compute the OIDC thumbprint

The thumbprint is the SHA1 of the JWKS signing cert (the `use=sig` key's `x5c`),
**not** the IdP's TLS chain. The role rejects any value that is not 40 hex
characters, including the example's `CHANGEME` placeholder.

```sh
curl -s https://idp.example.com/realms/myrealm/protocol/openid-connect/certs \
  | python3 -c 'import sys,json,base64,hashlib; \
      k=[k for k in json.load(sys.stdin)["keys"] if k.get("use")=="sig"][0]; \
      der=base64.b64decode(k["x5c"][0]); print(hashlib.sha1(der).hexdigest())'
```

Recompute and re-run whenever the realm signing key rotates. RGW cannot update
thumbprints, so the module deletes and recreates the provider (rolling back if
the recreate fails) and the role restarts RGW.

## Operational notes

- RGW caches OIDC providers in memory, so both roles restart it after a
  provider change. `rgw_tenants` waits for RGW to answer before its boto3 steps.
- `rgw_iam_provider_url` must have no trailing slash, or every assume fails with
  `AccessDenied`. The role asserts this.
- Policy action names are RGW's, not AWS's. One unrecognised name makes RGW
  discard the whole policy, so `rgw_iam` treats any parse error as fatal.
- Trust policies are updated in place. Recreating a role would change its
  `RoleId` and invalidate live sessions.
- Tagging a role breaks every assume of it, and the tag cannot be removed on
  19.2.4. `rgw_iam` reports such a role as `BROKEN` and rebuilds it under
  `rgw_iam_prune`.
- There is no session revocation. A withdrawn grant stays usable until the
  session expires (`rgw_iam_session_duration`).
- Account-scoped `radosgw-admin` commands need `--account-id`, or they 404.
- `radosgw-admin` takes S3 keys only on its argv. Those tasks are `no_log`, but
  the keys are briefly visible in `ps` and `/proc/<pid>/cmdline` on the
  bootstrap host; mount `/proc` with `hidepid=2` there. The boto3 steps take
  credentials from the environment.

## License

Apache-2.0.
