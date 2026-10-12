# Setting up a Keycloak realm for RGW web-identity, by hand

Manual setup of a Keycloak realm through the admin console (screen names as in
Keycloak 24-26). RGW checks every value below at assume time and answers any mismatch
with the same `AccessDenied`, so run the verification in step 8.

## 1. The contract RGW holds the realm to

| What | Must be | Where it is checked |
|---|---|---|
| Issuer | `https://<host>/realms/<realm>` (or `/auth/realms/<realm>` on older layouts), no trailing slash | `rgw_tenants_oidc.provider_url`; RGW builds the trust-condition key by stripping only the scheme |
| Token signature | RS256 / ES* / PS*. HS256 is rejected | RGW STS |
| Signing-key thumbprint | SHA1 of the `use=sig` key's `x5c` certificate. Not the TLS chain | `rgw_tenants_oidc.thumbprints` |
| Audience | the token's `aud` contains a value listed in `rgw_tenants_oidc.client_ids` (e.g. `ceph-s3`) | RGW STS |
| Grant claim | a JSON array of group paths `/services/s3/<tenant>/<role>`, in the claim named `s3iam` (`rgw_iam_grant_claim`), rooted at `rgw_iam_grant_root` | each `<tenant>-<role>` trust policy: `ForAnyValue:StringEquals {"<issuer-host>/realms/<realm>:s3iam": "/services/s3/<tenant>/<role>"}` |

Each value in the claim is the full path of a group the user belongs to.
RGW compares the whole string verbatim; the slashes have no meaning to it.
If `rgw_iam_grant_root` or `rgw_iam_grant_claim` changes on the Ceph side,
change the mapper and group tree to match, or every assume fails with `AccessDenied`.

`<tenant>` is the tenant's `name`, e.g. `aviato-data`.
It is not the variable key `rgw_tenant_aviato_data`, and it is not the account id.
`<role>` is one of the catalogue roles (`reader`, `writer`, `owner`, `iamadmin` by default)
or a per-tenant extra role from the tenant's `roles:` list.

## 2. Keep users from granting themselves access

Users cannot add themselves to a group, so a claim built from group membership
cannot be set by the user it describes.

Do not feed the claim from a user-attribute mapper.
The account API accepts writes to unmanaged attributes,
so a user could put any `/services/s3/<tenant>/<role>` into such an attribute and assume that role.
Also set **Realm settings → General → Unmanaged attributes** to **Only administrators can edit**
(`unmanaged_attribute_policy = ADMIN_EDIT`),
so a user-attribute mapper added later cannot carry user-written values.

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

On the same dedicated scope, choose **Configure a new mapper → Group Membership**:

| Field | Value |
|---|---|
| Name | `s3iam` |
| Token Claim Name | `s3iam` (match `rgw_iam_grant_claim`) |
| Full group path | on, so the value is `/services/s3/<tenant>/<role>` instead of the leaf name |
| Add to ID token / access token / userinfo | on / on / on |

A Group Membership mapper always emits an array,
so a user in several tenants carries every grant.

## 6. Groups: the grants themselves

**Groups → Create group**, as a tree whose paths are the grants:

```
/services/s3
/services/s3/<tenant>                  one per tenant
/services/s3/<tenant>/<role>           one per grant; membership is the grant
```

The mapper emits the leaf's path and the trust policy matches it, so groups need no attributes.
Create a leaf for every role the Ceph side reconciles for that tenant:
the whole catalogue plus that tenant's extra `roles:`.

Example for `aviato-data` with the default catalogue:

| Group path | Grant it carries |
|---|---|
| `/services/s3/aviato-data/reader` | `aviato-data` reader |
| `/services/s3/aviato-data/writer` | `aviato-data` writer |
| `/services/s3/aviato-data/owner` | `aviato-data` owner |
| `/services/s3/aviato-data/iamadmin` | `aviato-data` iamadmin |

Anyone who can create or move groups under `/services/s3/` can mint a grant for any tenant.
Keep the group tree admin-only.
Give tenant owners and delegation tools one right only:
adding or removing members of existing leaf groups.

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

Put the hash of the key that signs tokens (normally the realm's active RS256 key)
into `rgw_tenants_oidc.thumbprints`.
During a key rotation, list both the old and the new key's hashes.
Whenever the realm's signing key rotates, recompute and re-run `rgw_tenants`,
which recreates the provider in every tenant account.
RGW reads the new provider on the next assume without a restart.

If the RGW log shows `Signature can not be validated with the JWKS present`,
no JWKS key validated the token.
Check that a thumbprint matches the signing key's `x5c` cert
and that the token has not expired.
Squid releases without the fix from ceph PR #64936 only check the first key in the JWKS.

## 8. Verify before handing it to Ceph

Get a token for a test user who is a member of one leaf group:

```sh
TOKEN=$(curl -s "$ISSUER/protocol/openid-connect/token" \
  -d grant_type=password -d client_id=ceph-s3 \
  -d username=<test-user> --data-urlencode password=<pw> | jq -r .access_token)
cut -d. -f2 <<<"$TOKEN" | tr '_-' '/+' | base64 -d 2>/dev/null | jq '{iss, aud, s3iam}'
```

Expect all of the following:

- `iss` is the same string as `provider_url`.
- `aud` contains `ceph-s3`.
- `s3iam` is an array of full group paths, e.g. `["/services/s3/aviato-data/reader"]`,
  even for a single grant.
  Add the user to a second leaf and check that both paths appear.

Log in to the account console as that user
and confirm they cannot change their own group membership.
Check that no user-writable attribute feeds `s3iam`.

After the Ceph side has been converged:

```sh
aws --endpoint-url https://objects.example sts assume-role-with-web-identity \
  --role-arn arn:aws:iam::<account_id>:role/aviato-data-reader \
  --role-session-name check --web-identity-token "$TOKEN"
```

## 9. Collection config

```yaml
rgw_tenants_oidc:
  provider_url: "<issuer, no trailing slash>"
  client_ids: ["ceph-s3"]
  thumbprints: ["<step 7>"]
```
