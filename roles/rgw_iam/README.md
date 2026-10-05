# rgw_iam

Reconciles the IAM roles a federated user assumes inside a tenant account.
Run it after `rgw_tenants`, which creates the accounts, account-root users and
OIDC providers the roles depend on.

A user holds grants of the form `<tenant>:<role>` in one multivalued OIDC claim.
Each grant selects one IAM role in one account. Each account holds two
namespaces:

| Name | Owner | Reconciled |
|---|---|---|
| `<tenant>-<role>` | the platform | from config, and pruned when it leaves config |
| `<tenant>-x-<name>` | the tenant | never touched here (`rgw_iam_delegated_infix`) |

A tenant's static-key service users (`users:` in its entry, created by
`rgw_tenants`) get the same composed policy a role with those capabilities
would, as an inline user policy named `rgw_iam_policy_name`. Delegated-IAM
capabilities are refused for service users. Other inline policies on such a
user are reported, and removed under `rgw_iam_prune`.

Roles are composed from named capability bundles. Each capability has a `scope`
(`buckets`, `account` or `delegated-iam`) that determines the generated
resources.

## Variables

| Variable | Default | Meaning |
|---|---|---|
| `rgw_iam_tenants` | `[]` | Tenants to reconcile; merged by name with `rgw_tenants_list` and the drop-ins. |
| `rgw_iam_dir` | `rgw_tenants_dir` | Drop-in tenant directory, loaded so this role also runs standalone. |
| `rgw_iam_provider_url` | `rgw_tenants_oidc.provider_url` | OIDC issuer, matching the token's `iss` exactly, with no trailing slash (asserted). |
| `rgw_iam_grant_claim` | `ceph_grants` | Name of the claim carrying the grants. |
| `rgw_iam_roles` | `reader`, `writer`, `owner`, `iamadmin` | The global catalogue; every tenant gets one role per entry. |
| `rgw_iam_capabilities` | see `defaults/main.yml` | Named action bundles, each `{scope, actions}`. |
| `rgw_iam_baseline_capabilities` | `[account-list]` | Granted to every role regardless of catalogue entry. |
| `rgw_iam_session_duration` | `3600` | Session lifetime in seconds, bounded 3600..43200 by RGW. |
| `rgw_iam_account_limits` | `{max_roles: 100}` | Per-account entity ceilings. `{}` leaves RGW's own defaults. |
| `rgw_iam_policy_name` | `tenant` | Name of the single inline policy written on each platform role. |
| `rgw_iam_delegated_infix` | `x` | Infix marking the tenant-managed namespace this role never touches. |
| `rgw_iam_endpoint` | `rgw_tenants_endpoint` | IAM API endpoint for service-user policies (radosgw-admin cannot put an inline user policy). |
| `rgw_iam_validate_certs` / `rgw_iam_ca_bundle` | from `rgw_tenants_*` | TLS verification for that endpoint. |
| `rgw_iam_dry_run` | `false` | Deprecated alias for `--check`. |
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

A `--check` run with no changes means IAM matches config. `rgw_iam_dry_run`
sets check mode for this role's tasks only.

Output tokens: `ok` / `CHANGED` / `WOULD` per role, plus `EXTRA` (a policy this
role did not put), `STALE` (a platform role no longer in the spec) and `BROKEN`
(a role tagged out of band, whose every assume fails). The last three are
only reported unless `rgw_iam_prune` is set.

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
