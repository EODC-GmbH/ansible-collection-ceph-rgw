# Parsers for what Ceph commands actually print, as opposed to what you'd hope they print.
#
# `cephadm shell` is not a transparent pipe. It writes its own inference lines onto stdout
# around the wrapped command's output, and whether it does that depends on the build. So
# `register:` a cephadm-shell call and the buffer you get back is the payload with junk
# bracketing it. Feed that to `from_json` and the play dies on a parse error; compare it to
# a literal like 'true' and the comparison is simply always false. Find the payload, don't
# trust the buffer -- and do not assume the junk is only ever in front of it.
#
# Every parser here raises rather than returning a sentinel. A sentinel is indistinguishable
# from a real answer one `when:` later, which is the whole failure mode this file exists to
# remove. Raising costs a legible error at the exact task; returning "" costs a converge
# that does the wrong thing quietly. Callers must therefore never invoke these from an
# `assert`'s fail_msg: Ansible templates a task's args before the action runs, so a raising
# filter in fail_msg fails the task even when `that` would have passed, and the operator
# gets a templating error instead of the message you wrote. Compute into a fact, then
# reference the fact from both `that` and `fail_msg`.
#
# The version helpers gate on the RGW daemons, not on whatever `ceph --version` the
# bootstrap host happens to ship. During a staggered cephadm upgrade the CLI and the
# daemons are deliberately different versions, and every assumption in this collection is
# about what the RGWs do, not what the CLI is.
import json
import re

# Real keys are `ceph version 19.2.4 (a1b2c3...) squid (stable)`, vendor builds carry a
# package suffix (`19.2.1-227.el9cp`), dev builds a git describe (`19.2.4-123-gdeadbee`).
# Anchored on purpose: `unparsed` exists to stop unrecognised builds sailing through the
# bounds check, and a substring search would find a version number in arbitrary prose and
# defeat that.
_VERSION_RE = re.compile(r"^ceph version (\d+)\.(\d+)\.(\d+)")
_FSID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

_TRUE = ("true", "1", "on", "yes", "t", "y")
_FALSE = ("false", "0", "off", "no", "f", "n")


def _text(stdout, who):
    """The buffer as text, or a legible error naming what arrived instead.

    Without this, `str()` turns None into the string "None" and b"true\\n" into its repr,
    and both then parse as a perfectly ordinary -- wrong -- answer.
    """
    if isinstance(stdout, bytes):
        return stdout.decode("utf-8", "replace")
    if not isinstance(stdout, str):
        raise ValueError("%s: expected command output as text, got %s" % (who, type(stdout).__name__))
    return stdout


def _lines(stdout, who):
    lines = [ln.strip() for ln in _text(stdout, who).splitlines() if ln.strip()]
    if not lines:
        raise ValueError("%s: no output to read a value from" % (who,))
    return lines


def ceph_bool(stdout):
    """Whether a Ceph config option reads as on.

    `ceph config get` renders a bool as the option's stored form, and which spelling you
    get is not a documented contract -- true/false, 1/0 and on/off have all shipped.
    Comparing stdout to the literal 'true' is a bug waiting for a release note.

    Takes the last line that is a recognisable bool, so a trailing podman or deprecation
    warning does not become the answer. Raises when no line is one: `Error EINVAL: ...`
    must not read as "the option is off", because off is a legitimate value the caller
    will act on.
    """
    for line in reversed(_lines(stdout, "ceph_bool")):
        token = line.lower()
        if token in _TRUE:
            return True
        if token in _FALSE:
            return False
    raise ValueError(
        "ceph_bool: no line reads as a boolean: %r" % (_text(stdout, "ceph_bool")[:200],)
    )


def ceph_json(stdout):
    """Parse the JSON payload out of a Ceph command's stdout, preamble and all.

    Scans for a document rather than parsing the whole buffer, and stops at the end of the
    first one it finds instead of requiring it to run to EOF. Both halves matter: a cephadm
    inference line in front is not a parse error, and neither is a warning line behind --
    nor a `{fsid}` placeholder in a preamble path, which is a brace that starts no document.
    """
    text = _text(stdout, "ceph_json")
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char in "{[":
            try:
                return decoder.raw_decode(text, index)[0]
            except ValueError:
                continue
    raise ValueError("ceph_json: no JSON document in output: %r" % (text[:200],))


def ceph_fsid(stdout):
    """The cluster fsid, checked to actually be one.

    This ends up as a directory component in /var/run/ceph/$fsid/, so a bad value does not
    fail -- it quietly builds paths nothing reads and nothing writes. Check it here, where
    the error still says something useful. Searches for the line that is a UUID rather than
    taking the last one: cephadm appends as well as prepends, so position proves nothing.
    """
    for line in reversed(_lines(stdout, "ceph_fsid")):
        if _FSID_RE.match(line):
            return line.lower()
    raise ValueError(
        "ceph_fsid: no line is a cluster fsid: %r" % (_text(stdout, "ceph_fsid")[:200],)
    )


def ceph_daemon_versions(stdout, daemon="rgw"):
    """Summarise one daemon type's versions out of `ceph versions`.

    Returns a dict of:
      found      -- the cluster reports at least one daemon of this type. False means it is
                    not deployed (or every one of them is down, which `ceph versions` does
                    not distinguish), which is a different thing from a version being
                    wrong, so what to do about it is the caller's call.
      versions   -- distinct dotted versions, ordered numerically. Display only.
      majors     -- distinct major numbers, ascending.
      min_major  -- oldest major running, or None. Gate the floor on this.
      max_major  -- newest major running, or None. Gate the ceiling on this.
      unparsed   -- every version string the regex did not recognise. A caller that gates
                    on the numbers alone would wave through an unknown build, because an
                    empty set satisfies every bound you can write. Assert this is empty
                    first, or the rest of the dict means nothing.

    Note the floor is a major number, so a pre-GA 19.0/19.1 dev build satisfies `>= 19`
    even though accounts only became dependable in 19.2.x. See docs/CEPH-VERSIONS.md.
    """
    document = ceph_json(stdout)
    if not isinstance(document, dict):
        raise ValueError(
            "ceph_daemon_versions: expected a `ceph versions` object, got %s"
            % (type(document).__name__,)
        )
    entry = document.get(daemon) or {}
    if not isinstance(entry, dict):
        raise ValueError(
            "ceph_daemon_versions: %r is not a daemon-to-count map, it is %s"
            % (daemon, type(entry).__name__)
        )

    parsed, unparsed = set(), set()
    for raw in entry:
        match = _VERSION_RE.match(str(raw))
        if match:
            parsed.add(tuple(int(g) for g in match.groups()))
        else:
            unparsed.add(str(raw))

    majors = sorted({v[0] for v in parsed})
    return {
        "found": bool(entry),
        "versions": [".".join(str(n) for n in v) for v in sorted(parsed)],
        "majors": majors,
        "min_major": majors[0] if majors else None,
        "max_major": majors[-1] if majors else None,
        "unparsed": sorted(unparsed),
    }


class FilterModule:
    """Parsers for Ceph command output."""

    def filters(self):
        return {
            "ceph_bool": ceph_bool,
            "ceph_json": ceph_json,
            "ceph_fsid": ceph_fsid,
            "ceph_daemon_versions": ceph_daemon_versions,
        }
