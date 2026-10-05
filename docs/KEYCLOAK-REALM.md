# Setting up a Keycloak realm for RGW web-identity, by hand

Manual setup of a Keycloak realm through the admin console (screen names as in
Keycloak 24-26). RGW checks every value below at assume time and answers any mismatch
with the same `AccessDenied`, so run the verification in step 8.

## 1. The contract RGW holds the realm to

| What | Must be | Where it is checked |
|---|---|---|
| Issuer | `https://<host>/realms/<realm>` (or `/auth/realms/<realm>` on older layouts), **no trailing slash** | `rgw_tenants_oidc.provider_url`; RGW builds the trust-condition key by stripping only the scheme |
| Token signature | RS256 / ES* / PS*. HS256 is rejected | RGW STS |
| Signing-key thumbprint | SHA1 of the `use=sig` key's `x5c` certificate. Not the TLS chain | `rgw_tenants_oidc.thumbprints` |
| Audience | the token's `aud` contains a value listed in `rgw_tenants_oidc.client_ids` (e.g. `ceph-s3`) | RGW STS |
| Grant claim | a **JSON array** of `"<tenant>:<role>"` strings, named `ceph_grants` (`rgw_iam_grant_claim`) | each `<tenant>-<role>` trust policy: `ForAnyValue:StringEquals {"<issuer-host>/realms/<realm>:ceph_grants": "<tenant>:<role>"}` |

`<tenant>` is the tenant's `name`, e.g. `acme-data`. It is not the variable key
`rgw_tenant_acme_data`, and it is not the account id. `<role>` is one of the
catalogue roles (`reader`, `writer`, `owner`, `iamadmin` by default) or a per-tenant extra
role from the tenant's `roles:` list.

## 2. User profile: keep users from granting themselves access

The grant mapper (step 5) aggregates attribute values from the user **and** every group
they belong to. If a user can edit their own `ceph_grants` attribute in the account
console, they can put any `<tenant>:<role>` into their own token.

In **Realm settings → General → Unmanaged attributes**, choose **Only administrators can
edit** (`ADMIN_EDIT`). `Enabled` lets users edit unmanaged attributes themselves.
Alternatively, declare `ceph_grants` under **Realm settings → User profile → Create
attribute** with *Who can edit* = admin only and *Who can view* = admin.

The group-aggregated claim has only been tested with unmanaged attributes `Enabled`. The
token check in step 8 confirms it still arrives under `ADMIN_EDIT`.

## 3. The `ceph-s3` client

**Clients → Create client**:

- Client type `OpenID Connect`, Client ID `ceph-s3` (whatever you list in
  `rgw_tenants_oidc.client_ids`).
- Client authentication **off** (public).
- Authentication flow: `Standard flow` on. Turn on `Direct access grants` only if a CLI or
  the smoke test fetches tokens by username/password. Turn on `OAuth 2.0 Device
  Authorization Grant` if your CLI uses the device flow instead.
- Valid redirect URIs: the browser-facing tool's callback, e.g. `https://objects.example/*`.

## 4. Audience mapper

**Clients → ceph-s3 → Client scopes → ceph-s3-dedicated → Configure a new mapper →
Audience**:

- Name `aud-ceph-s3`, Included Client Audience `ceph-s3`.
- Add to ID token **on**, Add to access token **on**.

This puts `aud=ceph-s3` in the access token as well as the ID token, so either works
as the web-identity token.

## 5. Grant mapper

On the same dedicated scope, choose **Configure a new mapper → User Attribute**:

| Field | Value |
|---|---|
| Name | `ceph-grants` |
| User Attribute | `ceph_grants` |
| Token Claim Name | `ceph_grants` |
| Claim JSON Type | `String` |
| Add to ID token / access token / userinfo | on / on / on |
| **Multivalued** | **on**. Without it the claim is a string and only one grant survives |
| **Aggregate attribute values** | **on**. Without it group attributes are not collected |

If either switch is off, a user in several tenants ends up with one grant and gets
`AccessDenied` everywhere else.

## 6. Groups: the grants themselves

**Groups → Create group**, as a tree:

```
/ceph
/ceph/<tenant>                         one per tenant
/ceph/<tenant>/<role>                  one per grant, attribute ceph_grants = <tenant>:<role>
```

On each leaf group, under **Attributes**, set key `ceph_grants` and value `<tenant>:<role>`,
matching the group's own path. Create a leaf for every role the Ceph side reconciles for
that tenant: the whole catalogue plus that tenant's extra `roles:`.

Example for `acme-data` with the default catalogue:

| Group | Attribute `ceph_grants` |
|---|---|
| `/ceph/acme-data/reader` | `acme-data:reader` |
| `/ceph/acme-data/writer` | `acme-data:writer` |
| `/ceph/acme-data/owner` | `acme-data:owner` |
| `/ceph/acme-data/iamadmin` | `acme-data:iamadmin` |

**The prefix rule.** Grant strings share one namespace across all tenants. Isolation
depends on every group under `/ceph/<tenant>/` carrying a value that starts with
`<tenant>:`, so anyone who can edit group attributes can break it. Give tenant owners and
delegation tools only the right to **add or remove members** of leaf groups.

Granting access means adding a user to a leaf group; revoking means removing them.
Issued sessions stay valid until they expire (`rgw_iam_session_duration`, 1h by default).

## 7. Thumbprint

```sh
ISSUER=https://idp.example/realms/myrealm
curl -s "$ISSUER/protocol/openid-connect/certs" | python3 -c '
import sys, json, base64, hashlib
for k in json.load(sys.stdin)["keys"]:
    if k.get("use") == "sig" and k.get("x5c"):
        print(k["kid"], k["alg"],
              hashlib.sha1(base64.b64decode(k["x5c"][0])).hexdigest())'
```

Put the hash of the key that signs tokens (normally the realm's active RS256 key) into
`rgw_tenants_oidc.thumbprints`. During a key rotation, list both the old and the new key's
hashes. Whenever the realm's signing key rotates, recompute and re-run `rgw_tenants`,
which recreates the provider in every tenant account and restarts RGW.

## 8. Verify before handing it to Ceph

Get a token for a test user who is a member of one leaf group:

```sh
TOKEN=$(curl -s "$ISSUER/protocol/openid-connect/token" \
  -d grant_type=password -d client_id=ceph-s3 \
  -d username=<test-user> --data-urlencode password=<pw> | jq -r .access_token)
cut -d. -f2 <<<"$TOKEN" | tr '_-' '/+' | base64 -d 2>/dev/null | jq '{iss, aud, ceph_grants}'
```

Expect all of the following:

- `iss` equals `provider_url` exactly.
- `aud` contains `ceph-s3`.
- `ceph_grants` is an **array**, e.g. `["acme-data:reader"]`, even for a single grant.
  Add the user to a second leaf and check that both values appear.

Log in to the account console as that user and confirm that `ceph_grants` cannot be
edited there.

After the Ceph side has been converged:

```sh
aws --endpoint-url https://objects.example sts assume-role-with-web-identity \
  --role-arn arn:aws:iam::<account_id>:role/acme-data-reader \
  --role-session-name check --web-identity-token "$TOKEN"
```

## 9. Collection config

```yaml
rgw_tenants_oidc:
  provider_url: "<issuer, no trailing slash>"
  client_ids: ["ceph-s3"]
  thumbprints: ["<step 7>"]
```
