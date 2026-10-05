# rgw_sts

Enables native Ceph RGW STS on a cephadm-deployed cluster: sets the
session-token signing key (`rgw_sts_key`), turns on STS auth for the S3 endpoint
(`rgw_s3_auth_use_sts`), and creates standalone IAM roles for `AssumeRole` /
`GetSessionToken`. RGW reads both settings at startup, so the role restarts
RGW when either changes.

Run it first, as its own play, before `rgw_tenants`. OIDC providers are managed
by `rgw_tenants` and the web-identity tenant roles by `rgw_iam`.

Runs on `hosts: mons`; cluster work runs on `rgw_sts_bootstrap_host` through the
`rgw_config` and `rgw_role` modules.

## Variables

| Variable | Default | Meaning |
|---|---|---|
| `rgw_sts_key` | `""` | STS signing key, from vault (`vault_rgw_sts_key`). Required: when empty, Ceph falls back to its public default `sts`, and the role refuses to enable STS auth. |
| `rgw_sts_roles` | `[]` | Standalone IAM roles: `{name, account_id, trust_policy, policies: [{name, document}]}`. `account_id` is required. |
| `rgw_sts_service_name` | `rgw.rgw` | cephadm RGW service to restart on change. |
| `rgw_sts_bootstrap_host` | `groups['mons'][0]` | Host that runs the ceph commands. |
| `rgw_sts_fsid` | `""` | Optional; only forwarded to the modules, which otherwise read it from `/etc/ceph/ceph.conf`. |

Policies listed under a role are authoritative: a policy removed from config is
deleted from the role. Every task supports `--check`.

## Rotating the signing key

`rgw_sts_key` signs and encrypts every STS session token. Changing it
**invalidates every live session** when RGW restarts.

Ceph's option description says "16 hexadecimal characters" but suggests
`openssl rand -hex 16`, which emits 32. Use the command. RGW does not validate
the format.

```sh
openssl rand -hex 16          # 32 hex chars; store as vault_rgw_sts_key
kayobe playbook run .../ansible/ceph/cephadm-sts.yml --check --diff
kayobe playbook run .../ansible/ceph/cephadm-sts.yml
```

Then check federation with `roles/rgw_tenants/files/verify_federation.py`; a
key problem shows up as `AccessDenied` at assume time.

### Checking for drift without printing the key

Compare the key by length and hash rather than printing it with
`ceph config get`:

```yaml
- underknowledge.ceph_rgw.rgw_config_info:
    options: [rgw_sts_key]
  register: live
- ansible.builtin.debug:
    msg: >-
      stored len={{ live.config.rgw_sts_key | length }}
      sha1={{ live.config.rgw_sts_key | hash('sha1') | truncate(12, true, '') }}
      matches config: {{ live.config.rgw_sts_key == rgw_sts_key }}
```

`ceph config log 200` shows when the option last changed, but also prints old
and new values in plaintext.
