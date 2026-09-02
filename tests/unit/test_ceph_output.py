"""Tests for the ceph_output filters.

These parsers are what stands between a Ceph release note and a broken converge, so the
cases are real output shapes rather than happy-path smoke.

Stdlib unittest on purpose: no pytest, no ansible, no cluster. Run it anywhere with
`python3 -m unittest discover -s tests/unit` from the collection root.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "plugins", "filter"))

from ceph_output import (  # noqa: E402
    ceph_bool,
    ceph_daemon_versions,
    ceph_fsid,
    ceph_json,
)

FSID = "1f2e3d4c-5b6a-7980-a1b2-c3d4e5f60718"

# What `cephadm shell -- ...` actually hands back: its own inference lines, then the payload.
PREAMBLE = "Inferring fsid %s\nUsing ceph image with id 'abc123'\n" % FSID

SQUID = '"ceph version 19.2.4 (abc) squid (stable)": 2'
TENTACLE = '"ceph version 20.2.0 (def) tentacle (stable)": 1'


def versions_output(*daemons):
    """Build a `ceph versions` document, cephadm preamble included."""
    return PREAMBLE + "{%s}" % ", ".join('"%s": {%s}' % (d, v) for d, v in daemons)


class TestCephBool(unittest.TestCase):
    def test_every_spelling_of_on(self):
        for stdout in ["true", "True", "TRUE", "1", "on", "yes", PREAMBLE + "true"]:
            with self.subTest(stdout=stdout):
                self.assertIs(ceph_bool(stdout), True)

    def test_every_spelling_of_off(self):
        for stdout in ["false", "False", "0", "off", "no", PREAMBLE + "false"]:
            with self.subTest(stdout=stdout):
                self.assertIs(ceph_bool(stdout), False)

    def test_the_literal_compare_this_replaces_would_have_failed(self):
        # The old code was `stdout | trim == 'true'`, which this real input defeats.
        self.assertNotEqual((PREAMBLE + "true").strip(), "true")
        self.assertIs(ceph_bool(PREAMBLE + "true"), True)


class TestCephJson(unittest.TestCase):
    def test_skips_a_cephadm_preamble_before_an_object(self):
        self.assertEqual(ceph_json(PREAMBLE + '{"a": 1}'), {"a": 1})

    def test_skips_a_cephadm_preamble_before_a_list(self):
        self.assertEqual(ceph_json(PREAMBLE + '[{"a": 1}]'), [{"a": 1}])

    def test_bare_document_still_parses(self):
        self.assertEqual(ceph_json('{"a": 1}'), {"a": 1})

    def test_picks_whichever_bracket_comes_first(self):
        self.assertEqual(ceph_json('note {"x": 1}'), {"x": 1})

    def test_no_document_raises(self):
        for stdout in ["", "no json here", "Inferring fsid abc"]:
            with self.subTest(stdout=stdout), self.assertRaises(ValueError):
                ceph_json(stdout)


class TestCephFsid(unittest.TestCase):
    def test_accepts_a_real_fsid(self):
        self.assertEqual(ceph_fsid(PREAMBLE + FSID), FSID)

    def test_lowercases(self):
        self.assertEqual(ceph_fsid(FSID.upper()), FSID)

    def test_rejects_anything_that_would_become_a_junk_path(self):
        # An unchecked value builds /var/run/ceph/$junk/ and every read there comes back empty.
        for value in [
            "not-a-fsid",
            "1f2e3d4c5b6a7980a1b2c3d4e5f60718",  # no hyphens
            "1f2e3d4c-5b6a-7980-a1b2-c3d4e5f6071",  # one short
            "zzzzzzzz-5b6a-7980-a1b2-c3d4e5f60718",  # not hex
        ]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                ceph_fsid(value)

    def test_a_multi_line_answer_does_not_become_a_multi_line_path(self):
        self.assertNotIn("\n", ceph_fsid(PREAMBLE + FSID))


class TestCephDaemonVersions(unittest.TestCase):
    def test_single_version(self):
        got = ceph_daemon_versions(versions_output(("rgw", SQUID)))
        self.assertIs(got["found"], True)
        self.assertEqual(got["versions"], ["19.2.4"])
        self.assertEqual(got["majors"], [19])
        self.assertEqual((got["min_major"], got["max_major"]), (19, 19))
        self.assertEqual(got["unparsed"], [])

    def test_a_staggered_upgrade_reports_both_ends(self):
        got = ceph_daemon_versions(versions_output(("rgw", SQUID + ", " + TENTACLE)))
        self.assertEqual(got["versions"], ["19.2.4", "20.2.0"])
        self.assertEqual((got["min_major"], got["max_major"]), (19, 20))

    def test_gates_on_the_rgws_not_the_mons(self):
        got = ceph_daemon_versions(versions_output(("mon", TENTACLE), ("rgw", SQUID)))
        self.assertEqual(got["majors"], [19], "must read rgw, not whatever the mons run")

    def test_no_rgw_deployed_is_found_false_not_an_error(self):
        got = ceph_daemon_versions(versions_output(("mon", SQUID)))
        self.assertIs(got["found"], False)
        self.assertEqual(got["majors"], [])
        self.assertIsNone(got["min_major"])

    def test_an_unrecognised_version_string_is_reported_not_dropped(self):
        # Dropping it leaves majors == [], which satisfies every bound a caller can
        # assert -- an unknown build would sail straight through the gate.
        got = ceph_daemon_versions(versions_output(("rgw", '"some vendor build": 1')))
        self.assertIs(got["found"], True)
        self.assertEqual(got["majors"], [])
        self.assertEqual(got["unparsed"], ["some vendor build"])

    def test_dev_builds_parse(self):
        got = ceph_daemon_versions(
            versions_output(("rgw", '"ceph version 20.2.0-123-gdeadbee (x) tentacle (dev)": 1'))
        )
        self.assertEqual(got["versions"], ["20.2.0"])
        self.assertEqual(got["unparsed"], [])

    def test_other_daemon_types_on_request(self):
        got = ceph_daemon_versions(versions_output(("osd", SQUID)), daemon="osd")
        self.assertEqual(got["majors"], [19])


class TrailingOutput(unittest.TestCase):
    """cephadm junk is not only ever in front of the payload."""

    def test_json_survives_a_line_after_the_document(self):
        self.assertEqual(ceph_json('{"a": 1}\nWARNING: deprecated\n'), {"a": 1})

    def test_json_survives_a_line_after_an_array(self):
        self.assertEqual(ceph_json("[1, 2]\ndone"), [1, 2])

    def test_json_takes_the_first_of_two_documents(self):
        self.assertEqual(ceph_json('{"a": 1}\n{"b": 2}'), {"a": 1})

    def test_json_skips_a_brace_that_starts_no_document(self):
        # A real cephadm preamble path: /var/lib/ceph/{fsid}/config.
        self.assertEqual(
            ceph_json("Inferring config /var/lib/ceph/{fsid}/config\n{\"a\": 1}"), {"a": 1}
        )

    def test_bool_ignores_a_trailing_warning(self):
        self.assertTrue(ceph_bool("Inferring fsid x\ntrue\nWARNING: podman noise"))

    def test_fsid_ignores_a_trailing_warning(self):
        self.assertEqual(ceph_fsid("%s\nWARNING: podman noise" % FSID), FSID)


class RefusesToGuess(unittest.TestCase):
    """Anything these cannot read has to raise, not come back as a usable answer."""

    def test_bool_raises_on_an_error_message(self):
        # The old set made this False, i.e. "the option is off" -- a value callers act on.
        with self.assertRaises(ValueError):
            ceph_bool("Error EINVAL: unrecognized config option")

    def test_bool_reads_the_false_spellings(self):
        for spelling in ("false", "0", "off", "no"):
            with self.subTest(spelling=spelling):
                self.assertFalse(ceph_bool(spelling))

    def test_none_is_not_the_string_none(self):
        for parser in (ceph_bool, ceph_json, ceph_fsid, ceph_daemon_versions):
            with self.subTest(parser=parser.__name__):
                with self.assertRaises(ValueError):
                    parser(None)

    def test_bytes_are_decoded_not_repred(self):
        self.assertTrue(ceph_bool(b"true\n"))
        self.assertEqual(
            ceph_fsid(b"3f1c0b2a-1111-2222-3333-444455556666\n"),
            "3f1c0b2a-1111-2222-3333-444455556666",
        )

    def test_daemon_versions_rejects_a_non_object_document(self):
        with self.assertRaises(ValueError):
            ceph_daemon_versions("[1, 2]", "rgw")

    def test_daemon_versions_rejects_a_daemon_entry_that_is_not_a_map(self):
        # Iterating a string here yielded 25 single characters in `unparsed`.
        with self.assertRaises(ValueError):
            ceph_daemon_versions('{"rgw": "ceph version 19.2.4 (a) squid (stable)"}', "rgw")

    def test_a_version_number_in_prose_does_not_count_as_a_version(self):
        got = ceph_daemon_versions('{"rgw": {"vendor fork of ceph version 1.0.0": 1}}', "rgw")
        self.assertEqual(got["majors"], [])
        self.assertEqual(got["unparsed"], ["vendor fork of ceph version 1.0.0"])


class VersionReporting(unittest.TestCase):
    def test_rebuilds_of_one_release_report_once(self):
        got = ceph_daemon_versions(
            '{"rgw": {"ceph version 19.2.4 (aaa) squid (stable)": 1,'
            ' "ceph version 19.2.4 (bbb) squid (stable)": 2}}',
            "rgw",
        )
        self.assertEqual(got["versions"], ["19.2.4"])

    def test_versions_sort_numerically_not_lexicographically(self):
        got = ceph_daemon_versions(
            '{"rgw": {"ceph version 19.2.10 (a) squid (stable)": 1,'
            ' "ceph version 19.2.4 (b) squid (stable)": 1}}',
            "rgw",
        )
        self.assertEqual(got["versions"], ["19.2.4", "19.2.10"])

    def test_vendor_package_suffix_still_parses(self):
        got = ceph_daemon_versions(
            '{"rgw": {"ceph version 19.2.1-227.el9cp (a) squid (stable)": 3}}', "rgw"
        )
        self.assertEqual(got["min_major"], 19)

    def test_overall_key_is_ignored(self):
        # Real `ceph versions` always carries one; none of the other fixtures do.
        got = ceph_daemon_versions(
            PREAMBLE + '{"rgw": {%s}, "overall": {%s}}' % (SQUID, SQUID), "rgw"
        )
        self.assertEqual(got["majors"], [19])

    def test_daemon_key_present_but_empty_is_not_found(self):
        self.assertFalse(ceph_daemon_versions('{"rgw": {}}', "rgw")["found"])


class TheDecisionTheParserExistsFor(unittest.TestCase):
    """The floor comparison itself, which nothing else in the suite exercised."""

    def test_reef_is_below_the_squid_floor(self):
        got = ceph_daemon_versions(
            PREAMBLE + '{"rgw": {"ceph version 18.2.4 (a) reef (stable)": 2}}', "rgw"
        )
        self.assertLess(got["min_major"], 19)

    def test_a_mixed_upgrade_gates_on_the_oldest(self):
        got = ceph_daemon_versions(
            PREAMBLE + '{"rgw": {"ceph version 18.2.4 (a) reef (stable)": 1, %s}}' % SQUID, "rgw"
        )
        self.assertEqual(got["min_major"], 18)
        self.assertEqual(got["max_major"], 19)

    def test_an_unreadable_build_alongside_a_good_one_is_still_reported(self):
        # The ordering that saves this case -- assert `unparsed` before the bound -- is
        # load-bearing: min_major alone says 19 and the unknown build sails through.
        got = ceph_daemon_versions(
            PREAMBLE + '{"rgw": {"ceph vNext (a) unknown": 1, %s}}' % SQUID, "rgw"
        )
        self.assertEqual(got["min_major"], 19)
        self.assertEqual(got["unparsed"], ["ceph vNext (a) unknown"])


if __name__ == "__main__":
    unittest.main()
