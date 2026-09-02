# rgw_sts

Enable native Ceph RGW STS on a cephadm-deployed cluster: sets the session-token
signing key (`rgw_sts_key`), turns on STS auth for the S3 endpoint
(`rgw_s3_auth_use_sts`), and creates standalone `AssumeRole` IAM roles. OIDC
web-identity federation belongs to the `rgw_tenants` role, which registers
providers via the IAM API (squid's `radosgw-admin oidc-provider` does not work
for that).

Before anything else it checks the **RGW daemons'** Ceph version and refuses to
run below squid — not the CLI's version, because a staggered cephadm upgrade
leaves the two different on purpose. See
[docs/CEPH-VERSIONS.md](../../docs/CEPH-VERSIONS.md).

Run it on `hosts: mons`, as its own play before `rgw_tenants`
(see the collection README). All `radosgw-admin`/`ceph` work happens on the
bootstrap host (`rgw_sts_bootstrap_host`, default `groups['mons'][0]`) through
`cephadm shell`. `meta/argument_specs.yml` validates inputs.

## Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `rgw_sts_key` | `""` | STS signing key (from vault, `vault_rgw_sts_key`). **Required** — the role fails on an empty key rather than leaving Ceph's insecure default in place. |
| `rgw_sts_roles` | `[]` | Standalone `AssumeRole` IAM roles: `{ name, account_id, trust_policy, policies: [{name, document}] }`. `account_id` is **required**: a role without one is tenant-level, the path Ceph removes at the V release. |
| `rgw_sts_service_name` | `rgw.rgw` | cephadm RGW service to restart on change. |
| `rgw_sts_bootstrap_host` | `groups['mons'][0]` | Host that runs the ceph commands. |
| `rgw_sts_fsid` | `""` | Empty discovers it via `ceph fsid`. Validated as a UUID: it becomes a directory component, where a wrong value builds paths nothing reads instead of failing. |
| `rgw_sts_ceph_major_min` | `19` | Oldest Ceph major to run against. Below it the role fails rather than letting radosgw-admin report an unrecognised subcommand as a missing object. |
| `rgw_sts_ceph_major_tested` | `20` | Newest major tested. Past it the role warns and continues — refusing to run on a new Ceph would make this collection the thing blocking an upgrade. |
| `rgw_sts_ceph_version_check` | `true` | Run preflight at all. Off disables **all three** of its checks — the version floor, the RGW-is-deployed assert and the unreadable-version assert — not just the version bound. You then own verifying the behaviour. |

## Notes

- RGW reads `rgw_sts_key` and `rgw_s3_auth_use_sts` at startup, so the role
  restarts RGW when either changes. The restart is async and settles in a few seconds.
- Each task compares before it writes and reports `changed` only on drift: the
  config tasks echo `CHANGED`, role creation `CREATED`, a trust-policy update
  `UPDATED-TRUST`, a permission-policy write `PUT`.
- Policy documents are compared *semantically*, not byte for byte. radosgw-admin
  re-serialises what it stores, and `role-policy get` wraps its answer in an
  envelope, so a string compare reports drift on every converge and rewrites a
  policy nobody touched.
- An edited `trust_policy` is applied in place via `role-trust-policy modify`.
  `role create` cannot change one, and recreating the role would mint a new
  RoleId and invalidate every live session.
