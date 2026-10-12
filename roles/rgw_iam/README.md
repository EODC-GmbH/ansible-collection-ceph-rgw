# rgw_iam

Reconciles the IAM roles a federated user assumes inside a tenant account.
Run it after `rgw_tenants`, which creates the accounts, account-root users and
OIDC providers the roles depend on.

A user holds grants in one multivalued OIDC claim.
Each grant is the full path of a group the user belongs to,
`/services/s3/<tenant>/<role>` (the root is `rgw_iam_grant_root`),
and selects one IAM role in one account.
Each account holds two namespaces:

| Name | Owner | Reconciled |
|---|---|---|
| `<tenant>-<role>` | the platform | from config, and pruned when it leaves config |
| `<tenant>-x-<name>` | the tenant | never touched here (`rgw_iam_delegated_infix`) |

A tenant's static-key service users (`users:` in its entry, created by
`rgw_tenants`) get the same composed policy a role with those capabilities
would, as an inline user policy named `rgw_iam_policy_name`. Delegated-IAM
capabilities are refused for service users. Other inline policies on such a
user are reported, and removed under `rgw_iam_prune`.

The policies are put through the IAM API with the tenant's vaulted account-root keys.
The role first compares them with the cluster's keys for the account-root user
and stops with `vault keys for <name> do not match the cluster's keys` if they differ.
Run `rgw_tenants` with `rotate_keys: true` for that tenant, or restore the previous vault keys.
The role also stops if the account-root user is not found,
meaning `rgw_tenants` has not run for that tenant yet.

Under `--check` the preview skips a tenant's service-user policies,
and lists it as `account-root user not created yet, or key rotation pending`,
when its account-root user does not exist yet
or when it has `rotate_keys` (or `rgw_tenants_rotate_keys`) on and its keys differ.

## Capabilities

Roles and service users are composed from named capability bundles.
These names are what `roles[].capabilities`, `users[].capabilities`
and `rgw_add_service_user_capabilities` take.
Each bundle has a `scope` that decides the resources in the generated policy.

| Capability | Scope | Actions |
|---|---|---|
| `objects-read` | buckets | `ListBucket`, `ListBucketVersions`, `GetObject`, `GetObjectTagging` |
| `objects-write` | buckets | `PutObject`, `PutObjectTagging`, `DeleteObject`, `AbortMultipartUpload`, `ListBucketMultipartUploads`, `ListMultipartUploadParts` |
| `bucket-create` | buckets | `CreateBucket`, `DeleteBucket` |
| `bucket-policy-read` | buckets | `GetBucketPolicy` |
| `versioning` | buckets | `GetBucketVersioning`, `PutBucketVersioning` |
| `lifecycle` | buckets | `GetLifecycleConfiguration`, `PutLifecycleConfiguration` |
| `tagging` | buckets | `GetBucketTagging`, `PutBucketTagging` |
| `account-list` | account | `ListAllMyBuckets` |
| `iam-delegate` | delegated-iam | Create, read, update, tag and delete roles named `<tenant>-x-*`, and their inline policies |

`buckets` scope covers the tenant's buckets, or the role's own `buckets:` and `prefixes:`.
`account` scope is `"*"`, because RGW checks `ListAllMyBuckets` against an ARN only `"*"` matches,
and RGW still answers only for the calling account.
`delegated-iam` covers `arn:aws:iam::<account>:role/<tenant>-x-*`, where `x` is `rgw_iam_delegated_infix`.
`s3:PutBucketPolicy` is in no bundle, since a bucket policy is the way to share across accounts.
RGW has no permissions boundary, so a holder of `iam-delegate` can create a `<tenant>-x-` role
with any S3 permission inside its own account.

The default catalogue:

| Role | Capabilities |
|---|---|
| `reader` | `objects-read` |
| `writer` | `objects-read`, `objects-write` |
| `owner` | `writer` plus `bucket-create`, `bucket-policy-read`, `versioning`, `lifecycle`, `tagging` |
| `iamadmin` | `owner` plus `iam-delegate` |

Every role also gets `rgw_iam_baseline_capabilities`, by default `account-list`.
The source of truth is `defaults/main.yml`, which also lists RGW's action names.

## Variables

| Variable | Default | Meaning |
|---|---|---|
| `rgw_iam_tenants` | `[]` | Tenants to reconcile; merged by name with `rgw_tenants_list` and the drop-ins. |
| `rgw_iam_dir` | `rgw_tenants_dir` | Drop-in tenant directory, loaded so this role also runs standalone. |
| `rgw_iam_limit` | `rgw_tenants_limit` | Tenant names a run reconciles; empty means all. Prune acts per reconciled tenant, so the others' roles are left alone. An unknown name stops the run. |
| `rgw_iam_provider_url` | `rgw_tenants_oidc.provider_url` | OIDC issuer, matching the token's `iss` exactly, with no trailing slash (asserted). |
| `rgw_iam_grant_claim` | `s3iam` | Name of the claim carrying the grant group paths. |
| `rgw_iam_grant_root` | `/services/s3` | Root of the grant group tree; a grant is `<root>/<tenant>/<role>` (asserted to start with `/`, no trailing slash). |
| `rgw_iam_roles` | `reader`, `writer`, `owner`, `iamadmin` | The global catalogue; every tenant gets one role per entry. |
| `rgw_iam_capabilities` | see [Capabilities](#capabilities) | Named action bundles, each `{scope, actions}`. |
| `rgw_iam_baseline_capabilities` | `[account-list]` | Granted to every role regardless of catalogue entry. |
| `rgw_iam_session_duration` | `3600` | Session lifetime in seconds, bounded 3600..43200 by RGW. |
| `rgw_iam_account_limits` | `{max_roles: 100}` | Per-account entity ceilings. `{}` leaves RGW's own defaults. |
| `rgw_iam_policy_name` | `tenant` | Name of the single inline policy written on each platform role. |
| `rgw_iam_delegated_infix` | `x` | Infix marking the tenant-managed namespace this role never touches. |
| `rgw_iam_endpoint` | `rgw_tenants_endpoint` | IAM API endpoint for service-user policies (radosgw-admin cannot put an inline user policy). |
| `rgw_iam_s3_region` | `rgw_tenants_s3_region` | SigV4 signing region for those IAM calls. |
| `rgw_iam_validate_certs` / `rgw_iam_ca_bundle` | from `rgw_tenants_*` | TLS verification for that endpoint. |
| `rgw_iam_prune` | `false` | Remove roles and policies no longer in the spec, and rebuild a role tagged out of band. |
| `rgw_iam_bootstrap_host` | `groups['mons'][0]` | Host that runs the ceph commands. |
| `rgw_iam_fsid` | `""` | Forwarded to the modules. Empty lets each read it from `/etc/ceph/ceph.conf`. |

A tenant declares its own `roles:`, `session_duration:` and `limits:` in the
same entry `rgw_tenants` consumes (see that role's README). A tenant role may be
narrowed to specific `buckets:` and object `prefixes:`; a prefix-scoped role also
gets a matching `s3:prefix` condition on listing.

## Running it

```sh
kayobe playbook run .../ansible/ceph/cephadm-iam.yml --check --diff            # plan
kayobe playbook run .../ansible/ceph/cephadm-iam.yml                           # apply
```

A `--check` run with no changes means IAM matches config.

Output tokens: `ok` / `CHANGED` / `WOULD` per role, plus `EXTRA` (a policy this
role did not put), `STALE` (a platform role no longer in the spec) and `BROKEN`
(a role tagged out of band, whose every assume fails). The last three are
only reported unless `rgw_iam_prune` is set.

Every run (apply or `--check`) also reports `required_groups`,
the `<rgw_iam_grant_root>/<tenant>/<role>` group paths the IdP must contain
for these roles to be reachable.
The collection does not create these groups.
The list is built from the same spec as the trust policies
and can be handed to whatever provisions the realm.

## How it works

Composition, validation and the diff live in the `rgw_iam_sync` module, which
reads each account's roles in one call. `templates/iam-spec.json.j2` only emits
data, so the logic is unit-tested without a cluster:

```sh
python3 -m unittest discover -s tests/unit
```

RGW behaviour (19.2.4) the role works around:

- A trust policy is updated in place. Recreating the role would change its
  `RoleId` and invalidate live sessions.
- RGW rejects a policy document it cannot parse **as a whole**: one unrecognised
  action name leaves the role with no permissions, which looks like a trust or
  grant failure at assume time. Any parse error is fatal here.
- RGW validates role names against two different patterns. The role checks
  names against both, and the length, before reconciling.
- Tagging a role breaks every assume of it, and `UntagRole` is a no-op.
  Recreating it under `rgw_iam_prune` is the only fix.
- There is no session revocation: a withdrawn grant stays usable until the
  session expires.

## What it does not do

OIDC providers (`rgw_tenants`), the STS signing key and `rgw_s3_auth_use_sts`
(`rgw_sts`), and anything in the `<tenant>-x-*` namespace a tenant minted for
itself.

It does not touch the IdP: the client, the group-membership mapper and the groups
are managed outside this collection.
Three values must agree between the IdP and the trust policies:
the issuer (`rgw_iam_provider_url`), the claim name (`rgw_iam_grant_claim`)
and the grant root (`rgw_iam_grant_root`).
`docs/KEYCLOAK-REALM.md` describes the IdP side.
