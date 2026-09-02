# underknowledge.ceph_rgw

Native Ceph RGW STS plus OIDC-federated, per-tenant S3 for
kayobe / cephadm deployments. End users hold no long-lived S3 keys:
they log in to an OIDC IdP and get short-lived, tenant- and
tier-scoped S3 credentials via `AssumeRoleWithWebIdentity`.

Two roles:

| Role | What it does |
|------|--------------|
| `rgw_sts` | Enables native RGW STS. Sets `rgw_sts_key` and `rgw_s3_auth_use_sts`. Creates standalone account-scoped `AssumeRole` IAM roles. Restarts RGW on change. |
| `rgw_tenants` | Per tenant: an RGW account, an account-root user (creates buckets, never handed out), a registered OIDC provider, per-tier web-identity IAM roles (`<tenant>-admin/writer/reader`), and buckets. Runs after `rgw_sts`. |

## How a login turns into S3 access

```
  user logs in to the OIDC IdP (realm, client "ceph-s3")
      access token carries: iss, aud=ceph-s3, org=<tenant>, tier=<tier>

  boto3 sts.assume_role_with_web_identity(RoleArn=<tenant>-<tier>, WebIdentityToken=token)

  RGW validates the token against the account's OIDC provider and matches the role
  trust policy (Federated provider, <provider>:org == tenant, <provider>:tier == tier),
  then returns temporary credentials scoped by the permission policy to
  arn:aws:s3:::<tenant>-*

  boto3 s3 with the temporary creds: only this tenant's buckets, only this tier's actions
```

Each tenant is a separate RGW account. A role in one account
cannot reach another account's buckets. The per-tier permission policy
narrows access within the account to a bucket-name prefix
(`<tenant>-*` by default).

"Tenant" here means one of these accounts, not an RGW *tenant*. They are
different objects, and the difference is what decides whether this survives the
next two Ceph releases: tentacle deprecated tenant-level IAM in favour of
accounts, and `CreateRole`, `PutRolePolicy`, tenant names in policy documents
and `sts:GetSessionToken` are all removed no sooner than the V release. Nothing
here uses any of them. Both roles refuse to run on a Ceph older than squid and
warn on one newer than they were tested against.

**[docs/CEPH-VERSIONS.md](docs/CEPH-VERSIONS.md)** has the supported range, the
reasoning behind each rule, where every deprecated call stands, what is still
fragile, and how to re-check when a new Ceph lands. Read it before an upgrade.

## Install (kayobe)

Add it to `etc/kayobe/ansible/requirements.yml` under `collections:`
(see [`examples/kayobe-config/ansible/requirements.yml.snippet`](examples/kayobe-config/ansible/requirements.yml.snippet)).
Kayobe pulls it from git with `ansible-galaxy`, same as the cephadm collection:

```yaml
collections:
  - name: https://github.com/Underknowledge/ansible-collection-ceph-rgw.git
    type: git
    version: main        # pin to a tag or commit for production
```

Then `kayobe control host bootstrap` installs it.

## Wire into the cephadm chain

1. Copy the two chain plays into `etc/kayobe/ansible/ceph/`:
   [`cephadm-sts.yml`](examples/kayobe-config/ansible/ceph/cephadm-sts.yml),
   [`cephadm-tenants.yml`](examples/kayobe-config/ansible/ceph/cephadm-tenants.yml).
2. Append the two `import_playbook` lines to `etc/kayobe/ansible/ceph/cephadm.yml`
   after `cephadm-commands-post.yml`
   (see [`cephadm.yml.snippet`](examples/kayobe-config/ansible/ceph/cephadm.yml.snippet)).
   Order matters: STS after commands-post, tenants last.
   `rgw_tenants` asserts at runtime that `rgw_s3_auth_use_sts` is already `true`.
3. Add the config to your environment's `cephadm.yml` and vault
   (see [`examples/kayobe-config/environments/example/`](examples/kayobe-config/environments/example/)).

Run this part alone with `kayobe playbook run .../ansible/ceph/cephadm.yml --tags cephadm-sts`
(then `--tags cephadm-tenants`). Each `cephadm shell` call takes roughly 12s.
A full tenant set is around 30 calls. Allow a timeout of ten minutes or more.

## Configuration

Every knob is a variable; each role's `defaults/main.yml` plus its
`meta/argument_specs.yml` are the full list. Names are role-prefixed:
`rgw_sts_*` for the STS role, `rgw_tenants_*` for the tenants role. Vaulted
secrets take the same prefix (`vault_rgw_sts_*` / `vault_rgw_tenants_*`):

- `rgw_sts_key` from `vault_rgw_sts_key`: STS signing key.
- `rgw_tenants_endpoint`: RGW S3/IAM/STS endpoint the bootstrap host can reach.
- `rgw_tenants_oidc`: `{ provider_url, client_ids, thumbprints }` for the IdP.
- `rgw_tenants_list`: list of `{ name, account_id, buckets?, quota?, limits?, root_user?,
  root_users?, bucket_prefix?, state? }`.
- `rgw_tenants_tiers`: tier to actions (defaults admin/writer/reader).
- `rgw_tenants_default_quota`: fallback `{ enabled, max_size, max_objects }`
  applied to any tenant that omits its own `quota`. Default `{}` (no fallback).
- `rgw_tenants_s3_region`: the region boto3 signs with, default `us-east-1`.
  A SigV4 placeholder, not a cloud region: change it only if the zonegroup has
  an `api_name`, or `CreateBucket` fails on the `LocationConstraint`.
- `rgw_sts_service_name` / `rgw_tenants_service_name`: RGW cephadm service to
  restart (per-role; both default to `rgw.rgw`).
- Vault, per tenant: `vault_rgw_tenants_<name>_access_key` / `_secret_key`
  (the primary account-root), plus
  `vault_rgw_tenants_<name>_root_<slug>_access_key` / `_secret_key` for each extra root user.

`name` must be lowercase `[a-z0-9][a-z0-9_]*` (snake_case account name, see *Naming conventions*).
`account_id` must be `RGW` plus 17 digits. Every bucket must start
with the tenant's prefix (`<name>-` by default). The role asserts all of this
before touching the cluster.

### Naming conventions

Let the `rgw_new_tenant` and `rgw_add_root_user` playbooks build your
identifiers; they run every name through the `ceph_naming` filters.
A user ARN is `<account_id>:user/<display-name>`, so the filters feed one string
to both — a uid that differs from its display-name is unaddressable from a policy
that names the user. The per-tier trust policies match the federated provider
rather than a user, so nothing here depends on it today:

| Thing | Format | Example |
|-------|--------|---------|
| Account name | `<org>_<purpose>[_<qualifier>]`, lowercase snake_case | `initrode_data_prod`, `globex_data` |
| Primary root | `iam-<account>-root` | `iam-initrode_data_prod-root` |
| Extra root (service/person) | `iam-<account>-root-<descriptor>` | `iam-globex_data-root-backup`, `iam-initrode_data_prod-root-erika_schroeder` |
| Group | `<account>-<role>` (no `iam-` prefix) | `globex_data-uploaders` |

Person names are sanitized to the descriptor slug (German umlauts transliterated
`ö→oe`/`ä→ae`/`ü→ue`/`ß→ss`, other diacritics stripped,
spaces/hyphens folded to `_`), so `"Erika Schröder"` becomes `erika_schroeder`.
Underscore joins words inside one semantic unit; hyphen separates the structural segments of a uid.

Account names get the same sanitizing pass, then a validity check:
`"Acme Data Prod"` and `Acme-Data-Prod` both canonicalize to `acme_data_prod`,
and a single-word `acme` is rejected (a tenant needs at least two segments).
The name is permanent: it drives the uid, vault key names, bucket prefix and
role ARNs. The play warns whenever sanitizing changed your input; if the
canonical form is not what you meant, re-run with the name you actually want.

## Add a new tenant

A tenant's identity and keys must be in config *before* `rgw_tenants` runs.
The role never generates account-root keys and never writes anything back; the
vault is authoritative. `rgw_new_tenant` mints that identity once and stops, and
touches no cluster. Run it bare and it prompts for the account name, or pass the
name with `-e` to skip the prompt:

```sh
# Uses your existing vault password (ANSIBLE_VAULT_PASSWORD_FILE, or -e
# rgw_new_tenant_vault_password_file=<path>, e.g. kayobe's vault helper).
ansible-playbook playbooks/rgw_new_tenant.yml -e rgw_new_tenant=initrode_web
```

It allocates an `account_id` (a Unix-time id: `RGW` + `date +%s%N` truncated to 17 digits,
sortable; or pass `-e rgw_new_tenant_account_id=RGW<17 digits>`),
mints an `IAM`-prefixed 20-char access key and a 40-char secret key
from `random.SystemRandom`, and writes one self-contained drop-in file
`<tenant>.yml` into `rgw_new_tenant_output_dir` (default `rgw_tenants.d`
beside the playbook, not the directory you ran from).
Every run emits the primary root `iam-<tenant>-root`:

```yaml
# rgw_tenants.d/initrode_web.yml: config and vaulted keys, together
rgw_tenant_initrode_web:
  name: initrode_web
  account_id: "RGW17887187248433597"
  root_user: iam-initrode_web-root
  buckets: [initrode_web-data, initrode_web-logs]
vault_rgw_tenants_initrode_web_access_key: !vault |   # encrypted with your vault password
  $ANSIBLE_VAULT;1.1;AES256;...
vault_rgw_tenants_initrode_web_secret_key: !vault |
  $ANSIBLE_VAULT;1.1;AES256;...
```

Add `-e '{"rgw_new_tenant_quota": {...}}'`, `-e '{"rgw_new_tenant_buckets":
[...]}'`, or `-e '{"rgw_new_tenant_root_users": ["backup", "Erika Schröder"]}'`
to seed those keys (all optional; a bare run gives an account plus its primary root).
Point `rgw_tenants_dir` at that directory: the role loads every file in it and
appends each to `rgw_tenants_list`. Dropping a file in adds a tenant. Deleting
one only drops it from the list; nothing leaves the cluster. Removing a tenant
from the cluster is `state: absent` plus a teardown run (see *Decommission a tenant*).
Review, commit, then run `rgw_tenants` (`cephadm-tenants.yml`).
Re-running for an existing tenant is refused unless `-e rgw_new_tenant_force=true`
(which mints *new* keys).

## Root users (day-2)

`rgw_new_tenant` declares a tenant's primary `iam-<tenant>-root`; `rgw_tenants`
creates it on the cluster. Beyond it, each account can carry additional
**account-root users**: personalised keys handed to a tenant operator so they
can self-serve buckets in their own tenant. They live in the same drop-in, as a
`root_users` list plus their own vaulted keys. Buckets are account-owned, not
owned by the creating uid, so adding or removing a root user touches no tenant data.

**Add one.** The `rgw_add_root_user` playbook mints the key and merges it into
the drop-in, leaving every existing key and `!vault` block byte-for-byte intact.
Run it bare and it prompts for the tenant and the user (type the person's real
name at the prompt, spaces are fine, no quoting):

```sh
ansible-playbook playbooks/rgw_add_root_user.yml
#  Existing RGW tenant account: initrode_web
#  New root user, person name or service label: Erika Schröder
```

Or drive it non-interactively. A name with spaces then needs JSON
(Ansible's `-e k=v` form splits on spaces):

```sh
ansible-playbook playbooks/rgw_add_root_user.yml \
  -e rgw_add_root_user_tenant=initrode_web -e '{"rgw_add_root_user": "Erika Schröder"}'
```

`rgw_add_root_user` is any person name or service label; it is sanitized to the
descriptor slug, so the uid becomes `iam-initrode_web-root-erika_schroeder`.
Re-running with the same user is an idempotent no-op. This appends to the drop-in:

```yaml
rgw_tenant_initrode_web:
  # ...
  root_users:
    - name: erika_schroeder
vault_rgw_tenants_initrode_web_root_erika_schroeder_access_key: !vault | ...
vault_rgw_tenants_initrode_web_root_erika_schroeder_secret_key: !vault | ...
```

Review, commit, run `rgw_tenants`. The role creates the account-root user with those keys.

**Remove or suspend one.** No playbook, a desired-state edit the role reconciles:

- *Remove:* set the entry's `state: absent` (**keep it in the list**),
  run `rgw_tenants` once so it issues `radosgw-admin user rm`
  (gated by `rgw_tenants_allow_root_user_removal`, default `true`),
  *then* delete the entry and its two `vault_..._root_<slug>_*` blocks.
  Deleting the entry first orphans the cluster user, which the role can no longer remove.
- *Suspend:* set `suspended: true` (or back to `false`).
  The role reconciles suspend/enable each converge, keeping the keys but blocking access.

## Verify it worked

- Read-only, no IdP needed. Asserts every `<tenant>-<tier>` role and every
  declared bucket exists, then prints desired-vs-live quota:
  ```sh
  kayobe playbook run .../ansible/ceph/cephadm-tenants-verify.yml
  ```
  (the `rgw_tenants` role's `verify` task set; see the example play). It loads
  `rgw_tenants_dir` drop-ins itself, so it sees the same tenants a provisioning
  run does. With no tenants configured it reports `NOTHING VERIFIED` rather than
  a pass.
- Full federation path, per user:
  `roles/rgw_tenants/files/verify_federation.py`
  (needs `boto3` and the user's IdP password; env-driven, see its docstring).
  It checks the tier's allowed op, the reader deny, and — when `CROSS_BUCKET`
  names another tenant's bucket — that the account boundary keeps it unreachable.

## Compute the OIDC thumbprint

The thumbprint is the SHA1 of the JWKS signing cert (the `use=sig` key's `x5c`),
not the IdP TLS chain. The example ships a `CHANGEME` sentinel that the role
refuses to register. Compute the real one with:

```sh
curl -s https://idp.example.com/realms/myrealm/protocol/openid-connect/certs \
  | python3 -c 'import sys,json,base64,hashlib; \
      k=[k for k in json.load(sys.stdin)["keys"] if k.get("use")=="sig"][0]; \
      der=base64.b64decode(k["x5c"][0]); print(hashlib.sha1(der).hexdigest())'
```

Recompute and re-run whenever the realm signing key rotates.
On drift the OIDC helper deletes and recreates the provider
(RGW has no thumbprint-update operation) and the role restarts RGW.

## Decommission a tenant

Set `state: absent` on the tenant entry, then run deliberately with
`-e rgw_tenants_allow_teardown=true`; a scheduled converge only warns about
pending absent entries. Teardown order: buckets, OIDC provider, tier roles,
extra root users, account-root user, account.

```yaml
rgw_tenants_list:
  - name: acme
    account_id: "RGW00000000000000001"
    state: absent
    # purge_objects: true   # only if buckets still hold data (destructive)
    buckets: ["acme-data"]  # keep the bucket list so they get removed
```

This removes what the collection provisioned. If the tenant self-served its own
buckets/users/roles, plain `account rm` refuses on the non-empty account and the
run fails without deleting anything. Destroying data takes a second gate on top
of teardown: `purge_objects: true` on the entry *and*
`-e rgw_tenants_allow_purge=true`, which cascades
(`bucket rm --purge-objects`, `account rm --purge-data`).
A non-empty bucket in the list behaves the same way. Keep the entry (with
`state: absent`) and its `vault_rgw_tenants_*` keys until the run reports all
`OK`, then delete both. Only provider removal uses the tenant's own account-root
keys; the bucket, user and account steps are `radosgw-admin`.
Re-runs after the account is gone still report `OK`/`ABSENT`.

## Operational notes

- RGW caches the OIDC provider in memory, so the role restarts RGW after any
  provider change. `rgw_tenants` waits for RGW to answer before its boto3 steps
  (the STS role's restart is async).
- Account-scoped `radosgw-admin` operations need `--account-id` on every call, or they 404.
- Both interfaces can update a trust policy in place, and both are used:
  `rgw_tenants` provisions roles through the IAM API (`manage_role.py`,
  `UpdateAssumeRolePolicy`), and `rgw_sts` uses `radosgw-admin role-trust-policy
  modify`. Edit a trust policy and the next run applies it. Note the CLI spelling:
  `role-trust-policy modify` is the only one that has ever existed — the man page
  documents `role modify`, which is in no release, and `role update` changes only
  `--max-session-duration`.

## License

Apache-2.0.
