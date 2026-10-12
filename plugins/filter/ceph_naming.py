# Naming filters for ceph_rgw. The single place these identifiers get built, so the roles
# and playbooks stay in sync.
#
# The user ARN is <account_id>:user/<display-name> and trust-policy Principals match
# against it, so uid and display-name must be the identical Ceph-safe string: a divergence
# makes STS AssumeRole silently stop matching.
#
#   account name  <org>-<purpose>[-<qualifier>]   lowercase, [a-z0-9-], <=55ch  raviga-data-prod
#   extra root    iam-<account>-root-<descriptor>                               iam-raviga-data-prod-root-erika_schroeder
#
# The primary root (<account>root) is built inline by the role and generators, and service
# user uids come from rgw_service_user_name in rgw_iam_users.py.
#
# The charset is forced by RGW, which validates an IAM role name TWICE and applies both:
# rgw_rest_iam.cc validate_iam_role_name is [\w+=,.@-]+, and rgw_role.cc
# RGWRole::validate_input is [A-Za-z0-9:=,.@-]+, reached unconditionally from
# RadosRole::create. The intersection has no underscore, and roles are named
# <account>-<role>, so an account name cannot contain one either. 55 characters leaves
# room for the longest role suffix under RGW's MAX_ROLE_NAME_LEN of 64.
import re
import unicodedata

# Umlauts go to digraphs (ö->oe), mapped before the NFKD strip below, which would
# otherwise drop the diaeresis and leave a plain "o".
_GERMAN = {"ö": "oe", "ä": "ae", "ü": "ue", "ß": "ss"}

_ACCOUNT_RE = re.compile(r"^[a-z0-9][a-z0-9-]*\Z")
_MAX_ACCOUNT_LEN = 55
_DESCRIPTOR_RE = re.compile(r"^[a-z0-9_]+$")


def ceph_descriptor(name):
    """Slug a real name/label down to [a-z0-9_]+.

    Handles umlauts (ö->oe), strips accents (José->jose), folds spaces and hyphens to '_'.
    Hyphens must go: the account name may contain them, so keeping the descriptor
    hyphen-free is what leaves iam-<account>-root-<descriptor> splittable on the literal
    '-root-'. Raises if nothing usable survives.
    """
    text = str(name).strip().lower()
    for src, dst in _GERMAN.items():
        text = text.replace(src, dst)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("-", "_").replace(" ", "_")
    text = re.sub(r"[^a-z0-9_]", "", text)
    text = re.sub(r"_+", "_", text).strip("_")
    if not text:
        raise ValueError("ceph_descriptor: %r sanitizes to an empty string" % (name,))
    return text


def ceph_account_slug(name):
    """Slug a label down to a valid account name: [a-z0-9-], <=55 chars.

    The mirror image of ceph_descriptor: an account name folds spaces and underscores to
    '-' because RGW's two role-name validators intersect at [A-Za-z0-9=,.@-] and roles are
    named <account>-<role>, so an underscore in an account name makes every one of its
    roles uncreatable. Raises if nothing usable survives, or if the result is too long to
    carry a role suffix under MAX_ROLE_NAME_LEN.
    """
    text = str(name).strip().lower()
    for src, dst in _GERMAN.items():
        text = text.replace(src, dst)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("_", "-").replace(" ", "-")
    text = re.sub(r"[^a-z0-9-]", "", text)
    text = re.sub(r"-+", "-", text).strip("-")
    if not text:
        raise ValueError("ceph_account_slug: %r sanitizes to an empty string" % (name,))
    if len(text) > _MAX_ACCOUNT_LEN:
        raise ValueError(
            "ceph_account_slug: %r sanitizes to %r, %d characters. The ceiling is %d, so "
            "that <account>-<role> stays under RGW's MAX_ROLE_NAME_LEN of 64."
            % (name, text, len(text), _MAX_ACCOUNT_LEN))
    return text


def ceph_root_uid(account, descriptor):
    """Build an extra root uid iam-<account>-root-<descriptor>.

    descriptor must already be [a-z0-9_]+ (run person names through ceph_descriptor
    first). Feed the result to both --uid and --display-name; they must match.
    """
    if not ceph_account_valid(account):
        raise ValueError("ceph_root_uid: %r is not a valid account name" % (account,))
    if not _DESCRIPTOR_RE.match(str(descriptor or "")):
        raise ValueError("ceph_root_uid: descriptor %r is not [a-z0-9_]+" % (descriptor,))
    return "iam-%s-root-%s" % (account, descriptor)


def ceph_account_valid(account):
    """True for a lowercase [a-z0-9-] name of at most 55 characters (raviga-data-prod)."""
    text = str(account)
    return bool(_ACCOUNT_RE.match(text)) and len(text) <= _MAX_ACCOUNT_LEN


class FilterModule:
    """Ceph RGW naming-convention filters."""

    def filters(self):
        return {
            "ceph_account_slug": ceph_account_slug,
            "ceph_descriptor": ceph_descriptor,
            "ceph_root_uid": ceph_root_uid,
            "ceph_account_valid": ceph_account_valid,
        }
