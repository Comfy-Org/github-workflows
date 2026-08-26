"""Unit tests for the centralized public-repo hygiene checker (BE-8654).

Two things are under test here. The first is the ordinary one: each of the
three detection categories still fires, and the two per-repo knobs behave.

The second is the reason this file moved into github-workflows at all -- that
a PR in the CALLER repo cannot alter the checker or its allowlist. The
`TamperResistanceTest` case below asserts that directly, by planting a
wide-open copy of the checker (and every other override shape someone might
reach for) inside the scanned tree and proving the real checker's verdict does
not move. That is a property of the checker itself, so it is worth an
assertion rather than a comment: the workflow half of the guarantee -- loading
this file from a pinned ref instead of the caller's checkout -- is asserted by
`.github/workflows/test-public-repo-hygiene.yml`.
"""

import ast
import codecs
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import check_public_repo_hygiene as checker  # noqa: E402


class RepoFixture:
    """A throwaway git work tree, because the checker scans TRACKED files."""

    def __init__(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = self.dir.name
        self._git("init", "-q")
        # Local-only config so the fixture never depends on the machine's.
        self._git("config", "user.email", "test@example.invalid")
        self._git("config", "user.name", "test")

    def _git(self, *args):
        subprocess.run(
            ["git", *args], cwd=self.root, check=True, capture_output=True
        )

    def write(self, rel, content, track=True):
        path = os.path.join(self.root, rel)
        if os.path.dirname(rel):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        mode = "wb" if isinstance(content, bytes) else "w"
        with open(path, mode) as fh:
            fh.write(content)
        if track:
            self._git("add", "--", rel)
        return path

    def cleanup(self):
        self.dir.cleanup()


class CheckerTestCase(unittest.TestCase):
    def setUp(self):
        self.repo = RepoFixture()
        self.addCleanup(self.repo.cleanup)

    def run_checks(self, **kwargs):
        return checker.run_checks(self.repo.root, **kwargs)

    def findings(self, **kwargs):
        return self.run_checks(**kwargs).findings

    def warnings(self, **kwargs):
        return self.run_checks(**kwargs).warnings


class TicketIdCategoryTest(CheckerTestCase):
    def test_ticket_shaped_id_is_flagged(self):
        self.repo.write("README.md", "See BE-1234 for context.\n")
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("possible internal ticket ID", findings[0])
        self.assertIn("BE-1234", findings[0])
        self.assertTrue(findings[0].startswith("README.md:1:"), findings[0])

    def test_builtin_acronyms_are_not_flagged(self):
        self.repo.write("docs.md", "Encoded UTF-8, hashed SHA-256, dated ISO-8601.\n")
        self.assertEqual(self.findings(), [])

    def test_shape_boundaries(self):
        # One letter and seven letters are both outside [A-Z]{2,6}; one digit
        # and seven digits are both outside \d{2,6}. Keeping these pinned is
        # what stops a "small" regex tidy-up from silently widening or
        # narrowing the category.
        self.repo.write(
            "edge.md",
            "A-1234 ABCDEFG-1234 BE-1 BE-1234567 be-1234 BE-12 ABCDEF-123456\n",
        )
        flagged = sorted(f.split(": ")[-1] for f in self.findings())
        self.assertEqual(flagged, ["'ABCDEF-123456'", "'BE-12'"])

    def test_public_identifier_namespaces_clear_by_prefix(self):
        # `\b[A-Z]{2,6}-\d{2,6}\b` matches `CVE-2021` INSIDE `CVE-2021-44228`
        # (the `\b` holds against the following hyphen), so a SECURITY.md or a
        # dependency changelog reddened what adopters wire in as a REQUIRED
        # check. An exact-token carve-out would cost one entry per year prefix
        # and break again each January, so the namespace is allowlisted instead.
        self.repo.write(
            "SECURITY.md",
            "Patched CVE-2021-44228 (see also CVE-2024-3094).\n"
            "Classified as CWE-89; the API follows PEP-484 and RFC-9110,\n"
            "with ISO-19115 metadata written as UTF-16 or UTF-32.\n",
        )
        self.assertEqual(self.findings(), [])

    def test_an_allowlisted_prefix_does_not_clear_a_longer_acronym(self):
        # The carve-out is the namespace token itself, not any acronym starting
        # with those letters -- `CVEX-1234` is still ticket-shaped.
        self.repo.write("README.md", "See CVEX-1234 and ISOP-99 for context.\n")
        findings = self.findings()
        self.assertEqual(len(findings), 2, findings)

    def test_extra_ticket_allow_is_additive(self):
        self.repo.write("README.md", "GPU-100 and SHA-256 and BE-1234.\n")
        findings = self.findings(extra_ticket_allow=["GPU-100"])
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("BE-1234", findings[0])
        # ...and the built-ins survive the extension.
        self.assertFalse(any("SHA-256" in f for f in findings))

    def test_extra_ticket_allow_is_case_insensitive(self):
        self.repo.write("README.md", "GPU-100\n")
        self.assertEqual(self.findings(extra_ticket_allow=["gpu-100"]), [])


class InternalMarkerCategoryTest(CheckerTestCase):
    MARKERS = (
        "https://www.notion.so/team/page",
        "https://comfy.notion.site/page",
        "https://comfy.slack.com/archives/C123",
        "https://comfy.slack.com/client/T1/C1",
        "https://app.slack.com/client/T1",
        "https://docs.google.com/document/d/abc",
        "https://drive.google.com/file/d/abc",
        "https://app.datadoghq.com/dashboard/abc",
        "https://us.posthog.com/project/1/insights",
        "https://linear.app/comfyorg/issue/AA-1",
        "see incident-42 for the postmortem",
    )

    def test_each_marker_is_flagged(self):
        # At least one -- not exactly one. A line can satisfy two patterns
        # (`app.slack.com/client/` hits both the host and the path rule) and
        # both copies this replaces emitted one finding per matching pattern.
        # Preserving that keeps the merged checker's output identical to the
        # scripts it retires, which is what the parity check compares.
        for marker in self.MARKERS:
            with self.subTest(marker=marker):
                repo = RepoFixture()
                self.addCleanup(repo.cleanup)
                repo.write("notes.md", marker + "\n")
                findings = [
                    f
                    for f in checker.run_checks(repo.root).findings
                    if "collaboration-tool marker" in f
                ]
                self.assertGreaterEqual(len(findings), 1, f"{marker}: {findings}")

    def test_marker_match_is_case_insensitive(self):
        self.repo.write("notes.md", "HTTPS://APP.DATADOGHQ.COM/dash\n")
        self.assertTrue(
            any("collaboration-tool marker" in f for f in self.findings())
        )

    def test_public_lookalikes_are_not_flagged(self):
        # `posthog.com` and `slack.com` on their own are public marketing
        # pages; only the project/archive paths are internal.
        self.repo.write(
            "notes.md",
            "https://posthog.com/docs and https://slack.com/intl/en-gb/\n",
        )
        self.assertEqual(self.findings(), [])

    # Neighbouring REGISTRABLE domains -- anyone can buy these, which is what
    # makes flagging them a leak-report someone has to triage.
    LOOKALIKE_HOSTS = (
        "https://fooslack.com/archives/x",
        "https://evil-posthog.com/project/1",
        "https://my-linear.app/x",
        "https://foonotion.so/page",
        "https://app.slack.com.evil.com/",
    )
    # Same-namespace neighbours of the google-led patterns. Only Google can
    # create `*.google.com`, so these are not third-party lookalikes and pin
    # something narrower: the left anchor drops a host inside the SAME
    # registrable domain, not just a neighbouring one.
    #
    # `myapp.datadoghq.com` is deliberately NOT here. Datadog hands customers
    # their own `<name>.datadoghq.com` sub-domain, so that namespace is not
    # vendor-only and a custom-sub-domain org really would be missed -- a
    # genuine gap, recorded under README "Known limitations" rather than
    # pinned here as if it were correct. (BE-8729 review.)
    SAME_NAMESPACE_HOSTS = (
        "https://mydocs.google.com/doc/1",
        "https://xdrive.google.com/file/d/abc",
    )

    def test_lookalike_hosts_are_not_flagged(self):
        # Every one of these was a finding before the DNS-label anchors: `\b`
        # is not a host boundary (a hyphen is a non-word character, so
        # `\bposthog\.com` matched inside `evil-posthog.com`) and half the
        # patterns carried no left anchor at all. A letter, digit or hyphen
        # before the host means a DIFFERENT registrable domain; only a dot is a
        # real subdomain edge. `app.slack.com.evil.com` is the right-hand twin:
        # it is the one host-only pattern, so `\b` accepted a following label.
        #
        # `notlinear.app` is deliberately absent -- `t`->`l` puts a word
        # character where `\blinear` needed a boundary, so it never matched
        # even before the fix and would pin nothing.
        for marker in self.LOOKALIKE_HOSTS + self.SAME_NAMESPACE_HOSTS:
            with self.subTest(marker=marker):
                repo = RepoFixture()
                self.addCleanup(repo.cleanup)
                repo.write("notes.md", marker + "\n")
                self.assertEqual(checker.run_checks(repo.root).findings, [])

    PORTED_MARKERS = (
        "https://www.notion.so:443/team/page",
        "https://app.datadoghq.com:8443/x",
        "https://linear.app:443/comfy/issue/AA-1",
    )

    def test_an_explicit_port_does_not_bypass_the_check(self):
        # A port sits between the host and the path, so every pattern that
        # required `/` straight after the host was a one-token bypass away
        # from silence.
        for marker in self.PORTED_MARKERS:
            with self.subTest(marker=marker):
                repo = RepoFixture()
                self.addCleanup(repo.cleanup)
                repo.write("notes.md", marker + "\n")
                findings = [
                    f
                    for f in checker.run_checks(repo.root).findings
                    if "collaboration-tool marker" in f
                ]
                self.assertGreaterEqual(len(findings), 1, f"{marker}: {findings}")

    def test_an_empty_port_does_not_bypass_the_check(self):
        # `port = *DIGIT` in RFC 3986, so `https://notion.so:/page` is a valid
        # URL whose host is `notion.so` -- an empty port just means the default.
        # `(?::\d+)?` could not match the bare colon and then could not match
        # the required `/` either, which is the same one-token bypass `:443`
        # was. (BE-8729 review.)
        for marker in (
            "https://notion.so:/page",
            "https://docs.google.com:/document/d/abc",
            "https://comfy.slack.com:/archives/C123",
        ):
            with self.subTest(marker=marker):
                repo = RepoFixture()
                self.addCleanup(repo.cleanup)
                repo.write("notes.md", marker + "\n")
                findings = [
                    f
                    for f in checker.run_checks(repo.root).findings
                    if "collaboration-tool marker" in f
                ]
                self.assertGreaterEqual(len(findings), 1, f"{marker}: {findings}")

    def test_a_port_that_is_not_a_port_still_flags_the_readable_host(self):
        # Rounds 3 and 4 rejected `app.slack.com:443.evil.com` with a
        # `:\d+\.[A-Za-z0-9-]` alternative, on the premise that the host
        # "continues past the port" and really resolves to `evil.com`. It does
        # not. `port = *DIGIT`, so WHATWG's port state fails on the `.` and the
        # URL does not parse at all; `urlsplit(...).hostname` is
        # `app.slack.com`; curl and Go's `net/url` both error on the port. The
        # only host any parser reads on that line is the internal one, so
        # suppressing the line was a MISS, and it also cost the `:2.5` /
        # `:1.0.1` prose misses that shared the shape. Round 5 removed the
        # alternative; all four of these must now be flagged. The genuine
        # phishing form is `...@evil.com`, which the `[.:]...@` alternative
        # rejects -- pinned separately below. Do NOT restore `:\d+\.` (nor the
        # `_PORT` group round 3 shed) to "fix" a suffix host: this test is what
        # says the suffix host is not the leak. (BE-8729 review, round 5.)
        for marker in (
            "https://app.slack.com:443.evil.com/x",
            "https://app.slack.com:8443.evil.com",
            "See app.slack.com:2.5 release notes",
            "See app.slack.com:1.0.1 release notes",
        ):
            with self.subTest(marker=marker):
                repo = RepoFixture()
                self.addCleanup(repo.cleanup)
                repo.write("notes.md", marker + "\n")
                findings = [
                    f
                    for f in checker.run_checks(repo.root).findings
                    if "collaboration-tool marker" in f
                ]
                self.assertEqual(len(findings), 1, f"{marker}: {findings}")

    def test_the_phishing_form_of_a_ported_suffix_host_is_still_rejected(self):
        # The half of the shape above that IS a bypass: with userinfo, the real
        # host is `evil.com` whatever precedes the `@`. This is what licenses
        # removing `:\d+\.[A-Za-z0-9-]` -- the phishing case never needed it.
        for marker in (
            "https://app.slack.com:443.evil.com@evil.com/",
            "https://app.slack.com:8443.evil.com@evil.com/x",
        ):
            with self.subTest(marker=marker):
                repo = RepoFixture()
                self.addCleanup(repo.cleanup)
                repo.write("notes.md", marker + "\n")
                self.assertEqual(checker.run_checks(repo.root).findings, [])

    def test_the_userinfo_delimiter_is_not_read_as_a_host_boundary(self):
        # `https://app.slack.com@evil.com/` has host `evil.com` -- the `@`
        # makes everything before it userinfo. That is the same lookalike-host
        # false positive as `app.slack.com.evil.com`, in the canonical phishing
        # shape, so the right anchor rejects `@` too. A leading `admin@` is the
        # other direction and still matches, since `_HOST_L` only bars a label
        # character. (BE-8729 review.)
        for marker, want in (
            ("https://app.slack.com@evil.com/", 0),
            ("https://app.slack.com:443@evil.com/", 0),
            # Userinfo BETWEEN the host and the `@`. The first cut only
            # rejected an `@` adjacent to the host or to an all-digit port, so
            # all three of these stayed flagged. (BE-8729 review, round 3.)
            ("https://app.slack.com:@evil.com/", 0),
            ("https://app.slack.com:secret@evil.com/", 0),
            ("https://app.slack.com.@evil.com/", 0),
            # ...but the rejection must not reach across prose to an unrelated
            # address, which is why it is anchored on a leading `.`/`:` and
            # stops at a URL delimiter.
            ("mail admin@app.slack.com about it", 1),
            ("app.slack.com, and email bob@example.com", 1),
            ("app.slack.com,bob@example.com", 1),
        ):
            with self.subTest(marker=marker):
                repo = RepoFixture()
                self.addCleanup(repo.cleanup)
                repo.write("notes.md", marker + "\n")
                findings = [
                    f
                    for f in checker.run_checks(repo.root).findings
                    if "collaboration-tool marker" in f
                ]
                self.assertEqual(len(findings), want, f"{marker}: {findings}")

    def test_unicode_case_folding_does_not_read_another_domain_as_the_real_host(self):
        # `re.IGNORECASE` on its own folds Unicode, so Python matched U+0131
        # and U+0130 against `i` and these read as the real hosts. UTS-46 does
        # NOT: it leaves U+0131 alone and maps U+0130 to `i` + a combining dot,
        # so neither host resolves anywhere near the real one. Flagging them
        # was a false positive and `re.ASCII` removes it, the same way
        # `REPO_REF_RE` scopes its ignore-case flag. (BE-8729 review.)
        for marker in (
            "https://l\u0131near.app/x",
            "https://not\u0130on.so/page",
        ):
            with self.subTest(marker=marker):
                repo = RepoFixture()
                self.addCleanup(repo.cleanup)
                repo.write("notes.md", marker + "\n")
                self.assertEqual(checker.run_checks(repo.root).findings, [])

    def test_a_uts46_mapped_spelling_of_a_covered_host_is_out_of_scope(self):
        # The OTHER half of the `re.ASCII` trade, pinned separately because it
        # is a miss rather than a fix and the distinction is easy to lose.
        # UTS-46 *maps* U+017F to `s` and U+212A to `k`, so unlike the two
        # above these really do resolve to the covered host in any client, and
        # narrowing the ignore-case flag makes them silent. That is the same
        # scope line the README already draws for punycode, percent-encoded and
        # defanged spellings: this is a guard against an accidental paste, not
        # against someone spelling a link so it does not look like one.
        # (BE-8729 review.)
        for marker in (
            "https://\u017flack.com/archives/C123",
            "https://slac\u212a.com/archives/C123",
        ):
            with self.subTest(marker=marker):
                repo = RepoFixture()
                self.addCleanup(repo.cleanup)
                repo.write("notes.md", marker + "\n")
                findings = [
                    f
                    for f in checker.run_checks(repo.root).findings
                    if "collaboration-tool marker" in f
                ]
                self.assertEqual(findings, [], f"{marker}: {findings}")

    def test_an_idn_neighbour_still_reports_as_the_real_host(self):
        # A KNOWN LIMITATION, pinned so changing it is deliberate. The left
        # anchor is ASCII, so `énotion.so` clears it and reports as
        # `notion.so`. The blunt fix -- a second lookbehind rejecting any
        # non-ASCII character -- would also silence a real link written after a
        # curly quote, an em dash or CJK prose, and a missed leak costs more
        # than an extra finding. See README "Known limitations".
        repo = RepoFixture()
        self.addCleanup(repo.cleanup)
        repo.write("notes.md", "https://\u00e9notion.so/page\n")
        findings = [
            f
            for f in checker.run_checks(repo.root).findings
            if "collaboration-tool marker" in f
        ]
        self.assertEqual(len(findings), 1, findings)

    def test_the_host_only_pattern_tolerates_a_trailing_root_label(self):
        # The README scopes the trailing-root-label limitation to the
        # `/`-requiring patterns, because this one does NOT share it: its right
        # anchor rejects a following LABEL, and `.` followed by `/` is not one.
        # (BE-8729 review.)
        repo = RepoFixture()
        self.addCleanup(repo.cleanup)
        repo.write("notes.md", "https://app.slack.com./x\n")
        findings = [
            f
            for f in checker.run_checks(repo.root).findings
            if "collaboration-tool marker" in f
        ]
        self.assertGreaterEqual(len(findings), 1, findings)

    def test_the_host_only_pattern_still_ends_on_punctuation_or_a_port(self):
        # The right anchor replacing `\b` on `app.slack.com` is
        # `(?!\.?[A-Za-z0-9-]|@|[.:][A-Za-z0-9._~%:-]{0,64}@)`, which has to
        # reject a following LABEL and the userinfo family -- without rejecting
        # ordinary punctuation, a real port, or end of line. Quote it in full
        # when it changes: one revision of this comment read `:\d`, the
        # alternative round 3 removed because it lost `app.slack.com:2FA`, and
        # the next read `:\d+\.[A-Za-z0-9-]`, the one round 5 removed because
        # a non-numeric port is not a host continuation at all. Both are
        # pinned as must-match below. A stale quote here points the next editor
        # at a regression its own test data forbids.
        # (BE-8729 review, rounds 4 and 5.)
        for marker in (
            "Ask in app.slack.com.",
            "Ask in app.slack.com:443 if you self-host.",
            "https://app.slack.com",
            # A colon in PROSE, not a port -- each one lost by a different cut
            # of the backtracking fix, which is why they are all pinned. The
            # bare-`:` alternative lost the two `:general` shapes (the
            # `: the #general` one survived it, because `\d*` let the port
            # swallow the lone colon). Narrowing that to `:\d` restored them
            # and lost `:2FA` instead. Not consuming the port at all is what
            # keeps every one of them. (BE-8729 review, rounds 2 and 3.)
            "Ask in app.slack.com: the #general channel.",
            "Ask in app.slack.com:general, not here.",
            "Ask in app.slack.com:443: our workspace.",
            "Configure app.slack.com:2FA settings before then.",
        ):
            with self.subTest(marker=marker):
                repo = RepoFixture()
                self.addCleanup(repo.cleanup)
                repo.write("notes.md", marker + "\n")
                findings = [
                    f
                    for f in checker.run_checks(repo.root).findings
                    if "collaboration-tool marker" in f
                ]
                self.assertEqual(len(findings), 1, f"{marker}: {findings}")

    def test_the_userinfo_run_does_not_reach_across_prose_to_a_later_at(self):
        # The userinfo alternative is `[.:][A-Za-z0-9._~%:-]{0,64}@`, and the
        # CLASS is what keeps it honest. The first cut was `[.:][^\s/?#@]*@`,
        # "anything but a URL delimiter", which crossed commas, quotes and
        # braces -- so any unrelated `@` later in the same non-whitespace run
        # satisfied the lookahead and silenced a REAL reference. Every line
        # here matched the pre-PR `\bapp\.slack\.com\b` and must keep matching:
        # a miss is the one direction this guard cannot afford.
        #
        # It stops the run on SPECIFIC characters, not on "prose" generally:
        # whitespace, `/?#`, quotes, commas, braces and the sub-delims
        # (`+!$&'()*;=`). `:` is deliberately IN the class, so a colon-chained
        # run does still reach a later `@` -- see
        # `test_a_colon_chained_run_still_reaches_a_later_at`.
        for marker in (
            # A comma is not a credential character; the address is separate.
            "app.slack.com:443,ops@example.com",
            # JSON: `"` closed the run, but the old class walked straight past
            # it into an entirely different field's value.
            '{"slack":"app.slack.com:443","owner":"bob@example.com"}',
            # The shape the stop is SUPPOSED to keep matching, and the one the
            # round-3 suite left unpinned -- `/` ends the authority, so the
            # `@` in the query string is not userinfo and the real host here
            # really is `app.slack.com`. Without a case, a "simplification"
            # back to `[.:]\S*@` would pass the whole suite.
            "https://app.slack.com:443/ssb/redirect?to=bob@x.com",
            # A non-breaking space is not whitespace under `re.ASCII`, so the
            # old `[^\s/?#@]*` crossed it too. The class excludes it outright.
            "app.slack.com:443\u00a0ops@example.com",
        ):
            with self.subTest(marker=marker):
                repo = RepoFixture()
                self.addCleanup(repo.cleanup)
                repo.write("notes.md", marker + "\n")
                findings = [
                    f
                    for f in checker.run_checks(repo.root).findings
                    if "collaboration-tool marker" in f
                ]
                self.assertEqual(len(findings), 1, f"{marker}: {findings}")

    def test_a_colon_chained_run_still_reaches_a_later_at(self):
        # A KNOWN MISS, pinned so it cannot drift unnoticed. `:` is inside the
        # userinfo class because `:user:pass@evil.com` is real userinfo and
        # dropping it would reopen that phishing shape -- but the same colon
        # lets the run chain past prose to an unrelated `@`, so each of these
        # satisfies `[.:][A-Za-z0-9._~%:-]{0,64}@` and goes silent where the
        # pre-PR `\bapp\.slack\.com\b` matched. This is the residue of the
        # round-4 class narrowing, which fixed the comma, quote, brace and
        # non-breaking-space cases pinned above; the colon case survives it by
        # design. Documented in the README's "Known limitations".
        # (BE-8729 review, round 5.)
        for marker in (
            "app.slack.com:443:ops@example.com",
            "slack=app.slack.com:owner:ops@example.com",
            "app.slack.com:2024-01-15:incident@comfy.org",
        ):
            with self.subTest(marker=marker):
                repo = RepoFixture()
                self.addCleanup(repo.cleanup)
                repo.write("notes.md", marker + "\n")
                findings = [
                    f
                    for f in checker.run_checks(repo.root).findings
                    if "collaboration-tool marker" in f
                ]
                self.assertEqual(findings, [], f"{marker}: {findings}")

    def test_the_userinfo_length_bound_over_flags_past_64_characters(self):
        # BOTH sides of the `{0,64}` boundary, so a future edit to the bound
        # cannot tell itself it is free. The bound exists for cost (see the
        # timing test below), but it has a correctness cost too, in the
        # over-flag direction: userinfo longer than 64 characters puts the `@`
        # out of the run's reach, every backtrack fails, no other alternative
        # fires (`\.?[A-Za-z0-9-]` sees `:`, bare `@` sees `:`), and the line
        # is flagged as `app.slack.com` although the real host is `evil.com`.
        # Realistic when a token or JWT rides as the basic-auth password.
        # Not a leak -- a leak guard may over-flag -- but it is a real trade
        # and the bullet beside the pattern names it. (BE-8729 review, round 5.)
        for length, want in ((64, 0), (65, 1)):
            marker = "https://app.slack.com:" + ("a" * length) + "@evil.com/"
            with self.subTest(length=length):
                repo = RepoFixture()
                self.addCleanup(repo.cleanup)
                repo.write("notes.md", marker + "\n")
                findings = [
                    f
                    for f in checker.run_checks(repo.root).findings
                    if "collaboration-tool marker" in f
                ]
                self.assertEqual(len(findings), want, f"{length}: {findings}")

    def test_a_label_character_adjacent_to_the_host_is_a_known_miss(self):
        # KNOWN MISSES, pinned together because they are one trade seen from
        # both sides: a label character (letter, digit, hyphen, and on the
        # right also a non-ASCII continuation) touching the host reads as part
        # of a different name.
        #
        # For the TWO-label hosts that is exactly right -- `fooslack.com` IS a
        # different registrable domain, which is the false positive this PR
        # removed. For the THREE-label hosts it over-corrects: `my-app.slack.com`
        # is a real slack.com workspace host and matched before, as a
        # substring. Same for a prose hyphen on the right
        # (`app.slack.com-hosted`), where `\b` used to hold because a hyphen is
        # a non-word character. Recovering either costs an over-flag on a shape
        # that really IS a different registrable name (`app.slack.com-evil.com`
        # -> `com-evil.com`), and closing the non-ASCII side is the same blunt
        # fix the IDN-neighbour limitation already declines on the left.
        # All are in the README's "Known limitations". (BE-8729 review, round 5.)
        for marker in (
            "https://my-app.slack.com/ssb/redirect",
            "app.slack.com-hosted workspace",
            "Ask in app.slack.com--we use it daily.",
        ):
            with self.subTest(marker=marker):
                repo = RepoFixture()
                self.addCleanup(repo.cleanup)
                repo.write("notes.md", marker + "\n")
                findings = [
                    f
                    for f in checker.run_checks(repo.root).findings
                    if "collaboration-tool marker" in f
                ]
                self.assertEqual(findings, [], f"{marker}: {findings}")

    def test_a_non_ascii_label_continuation_reports_as_the_literal_host(self):
        # The right-hand half of the IDN-neighbour limitation, which was
        # written for the LEFT boundary only. The right anchor's classes are
        # ASCII, so a non-ASCII label continuation is not rejected and the
        # checker reports `app.slack.com` where the real host differs. Same
        # safe (over-flag) direction and same cause as the left-hand case, and
        # declined for the same reason: rejecting any non-ASCII character
        # adjacent to a host would silence a real link written against CJK
        # prose. (BE-8729 review, round 5.)
        for marker in (
            "https://app.slack.com.\u4e2d\u56fd/",
            "https://app.slack.com\u00e9vil.com/",
        ):
            with self.subTest(marker=marker):
                repo = RepoFixture()
                self.addCleanup(repo.cleanup)
                repo.write("notes.md", marker + "\n")
                findings = [
                    f
                    for f in checker.run_checks(repo.root).findings
                    if "collaboration-tool marker" in f
                ]
                self.assertEqual(len(findings), 1, f"{marker}: {findings}")

    def test_the_userinfo_run_is_length_bounded_so_cost_stays_linear(self):
        # `MAX_FILE_BYTES` bounds a FILE; nothing bounds a LINE. With an
        # unbounded `[^\s/?#@]*@`, each of the ~L/14 host positions on
        # `('app.slack.com:' * N) + '@'` rescanned the whole remaining tail
        # before the lookahead succeeded -- quadratic, ~10^12 character steps
        # at the 5 MiB cap, so a tracked file could turn a required check into
        # an unexplained 15-minute job timeout. `{0,64}` caps the per-position
        # work, which is what makes total cost linear in line length.
        #
        # An ABSOLUTE ceiling, not a ratio. A ratio looks tempting but does not
        # discriminate: quadratic growth is only ~4x per doubling, which is too
        # close to linear's ~2x to gate on a noisy shared runner -- and the
        # bounded pattern matches at the first position anyway, so its timings
        # are sub-millisecond and their ratio is pure noise. The gap in
        # absolute terms is enormous and is what this asserts: on this input
        # the bounded pattern finishes in well under a millisecond while the
        # unbounded one takes ~6s, so the 2s ceiling has ~3x margin below the
        # regression and several orders of magnitude above the correct
        # behaviour. A slower runner only pushes the regression further past
        # the ceiling.
        pattern = next(
            p
            for p in checker.INTERNAL_MARKER_RES
            if r"app\.slack\.com" in p.pattern
        )
        line = ("app.slack.com:" * (200_000 // 14)) + "@"
        start = time.perf_counter()
        pattern.search(line)
        elapsed = time.perf_counter() - start
        self.assertLess(
            elapsed,
            2.0,
            f"scanning one {len(line)}-character line took {elapsed:.2f}s; "
            "the userinfo run looks unbounded again (quadratic backtracking)",
        )


class RepoReferenceCategoryTest(CheckerTestCase):
    def test_unknown_repo_is_flagged_default_deny(self):
        self.repo.write("README.md", "See Comfy-Org/some-internal-thing.\n")
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("not in the known-public allowlist", findings[0])
        self.assertIn("Comfy-Org/some-internal-thing", findings[0])

    def test_known_public_repos_pass(self):
        self.repo.write(
            "README.md",
            "Comfy-Org/ComfyUI Comfy-Org/comfy-cli Comfy-Org/ComfyUI_frontend\n",
        )
        self.assertEqual(self.findings(), [])

    def test_github_workflows_is_allowlisted(self):
        # The regression that surfaced BE-8654: both per-repo copies were
        # missing this repo, so a caller pinned at
        # `Comfy-Org/github-workflows/...` failed the hygiene check it was
        # being added alongside.
        self.repo.write(
            ".github/workflows/ci.yml",
            "    uses: Comfy-Org/github-workflows/.github/workflows/x.yml@abc\n",
        )
        self.assertEqual(self.findings(), [])

    def test_sentence_final_period_is_stripped(self):
        # BE-8697, the change this test was flipped by: the repo-name class
        # includes `.`, so prose ending "...built on Comfy-Org/ComfyUI." used
        # to be flagged as `ComfyUI.`. Both per-repo scripts did that and this
        # checker carried it over on purpose, because the migration's proof was
        # findings-parity against them -- a proof that ended when that
        # migration merged. A GitHub slug can never end in `.`, so a trailing
        # one is always sentence punctuation.
        self.repo.write("README.md", "Built on Comfy-Org/ComfyUI.\n")
        self.assertEqual(self.findings(), [])

    def test_lowercase_org_segment_is_still_flagged(self):
        # The bypass BE-8697 closes: GitHub resolves owner names
        # case-insensitively, so `comfy-org/x` reaches the same repo as
        # `Comfy-Org/x` and default-deny has to see both spellings.
        self.repo.write("README.md", "See comfy-org/some-internal-thing.\n")
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("not in the known-public allowlist", findings[0])
        self.assertIn("some-internal-thing", findings[0])

    def test_org_segment_casings_of_public_repos_pass(self):
        # Widening the match must not turn public references into findings.
        self.repo.write(
            "README.md",
            "comfy-org/comfyui and COMFY-ORG/ComfyUI and CoMfY-oRg/comfy-cli\n",
        )
        self.assertEqual(self.findings(), [])

    def test_repo_allowlist_membership_is_case_insensitive(self):
        # `ComfyUI` and `Comfy-Desktop` are stored in their canonical GitHub
        # spelling; a differently-cased reference resolves to the same public
        # repo, so it is not a leak.
        self.repo.write(
            "README.md",
            "Comfy-Org/comfyui Comfy-Org/comfy-desktop Comfy-Org/COMFYUI_FRONTEND\n",
        )
        self.assertEqual(self.findings(), [])

    def test_near_miss_repo_name_is_still_flagged(self):
        # Casefolding admits no name that is not in the allowlist -- only
        # other CASINGS of names that are.
        self.repo.write("README.md", "Comfy-Org/ComfyUI2\n")
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("Comfy-Org/ComfyUI2", findings[0])

    def test_model_host_url_is_a_different_namespace(self):
        # The false-positive class this skip exists for: comfy-cli ships
        # download URLs for public Comfy-Org model weights on Hugging Face, and
        # 19 of its 29 findings were those URLs tested against a GITHUB-repo
        # allowlist they could never be in.
        self.repo.write(
            "download.py",
            "URL = 'https://huggingface.co/Comfy-Org/"
            "stable-diffusion-v1-5-archive/resolve/main/v1-5.safetensors'\n",
        )
        self.assertEqual(self.findings(), [])

    def test_model_host_spellings_that_put_an_owner_after_a_path_segment(self):
        # `hf.co` is Hugging Face's own short domain, and the API, collections
        # and datasets/spaces routes put the owner one segment further along.
        # Every one has to end exactly where `Comfy-Org` starts or the skip
        # misses. `collections/` is an owner-first route like the others, and
        # omitting it left those URLs reporting as GitHub-repo leaks -- the
        # same false-positive class this change exists to remove.
        self.repo.write(
            "notes.md",
            "https://hf.co/Comfy-Org/ace_step_1.5_ComfyUI_files\n"
            "https://huggingface.co/api/models/Comfy-Org/Qwen-Image_ComfyUI\n"
            "https://huggingface.co/datasets/Comfy-Org/some-eval-set\n"
            "https://huggingface.co/spaces/Comfy-Org/some-demo\n"
            "https://huggingface.co/collections/Comfy-Org/some-set-abc123\n",
        )
        self.assertEqual(self.findings(), [])

    def test_model_host_url_shapes_that_still_anchor(self):
        # `_AUTHORITY_L` requires a delimiter or start-of-line before the
        # match, and the scheme separator is consumed rather than tolerated --
        # so these ordinary embeddings have to keep clearing.
        self.repo.write(
            "notes.md",
            "http://huggingface.co/Comfy-Org/plain-http\n"
            "//huggingface.co/Comfy-Org/protocol-relative\n"
            "huggingface.co/Comfy-Org/bare-host-at-line-start\n"
            "See [weights](https://hf.co/Comfy-Org/in-a-markdown-link) here\n"
            "<https://hf.co/Comfy-Org/in-angle-brackets>\n"
            'url="https://hf.co/Comfy-Org/in-double-quotes"\n'
            "https://HuggingFace.co:443/Comfy-Org/host-case-and-port\n",
        )
        self.assertEqual(self.findings(), [])

    def test_a_path_segment_is_not_a_url_authority(self):
        # The skip gates a SUPPRESSION, so its left anchor is `_AUTHORITY_L`,
        # not the detection-side `_HOST_L`: under `_HOST_L` a model host
        # appearing as a PATH segment of some other host satisfied the
        # lookbehind (`/` is not a DNS-label character) and silenced the
        # reference. Only a real authority may clear one.
        self.repo.write(
            "README.md",
            "https://internal.example/mirror/hf.co/Comfy-Org/"
            "some-internal-thing\n",
        )
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("some-internal-thing", findings[0])

    def test_an_api_route_without_a_resource_segment_is_not_a_model_host(self):
        # Hugging Face routes `/api/{models,datasets,spaces}/<owner>`, never
        # `/api/<owner>`, so pairing `api/` with a required resource segment
        # keeps the exception inside URLs that actually resolve.
        self.repo.write(
            "README.md",
            "https://huggingface.co/api/Comfy-Org/some-internal-thing\n",
        )
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("some-internal-thing", findings[0])

    def test_model_host_path_segments_are_case_sensitive(self):
        # `_HOST_FLAGS`'s `re.IGNORECASE` is there for the HOST, where DNS
        # really is case-insensitive, and it reaches the path too. HF route
        # segments are case-SENSITIVE, so `(?-i:...)` scopes the flag back off
        # and these spellings -- which resolve nowhere -- no longer clear.
        self.repo.write(
            "README.md",
            "https://huggingface.co/MODELS/Comfy-Org/upper-case-route\n"
            "https://huggingface.co/Datasets/Comfy-Org/title-case-route\n",
        )
        findings = self.findings()
        self.assertEqual(len(findings), 2, findings)
        self.assertIn("upper-case-route", " ".join(findings))
        self.assertIn("title-case-route", " ".join(findings))

    def test_model_host_skip_does_not_reach_github_on_the_same_line(self):
        # The skip is keyed on the offset the model-host prefix ENDS at, not on
        # the line containing one, so a github.com reference sitting beside a
        # Hugging Face URL is still denied.
        self.repo.write(
            "README.md",
            "Weights at https://huggingface.co/Comfy-Org/some-model, code at "
            "https://github.com/Comfy-Org/some-internal-thing\n",
        )
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("some-internal-thing", findings[0])
        self.assertNotIn("some-model", findings[0])

    def test_a_model_host_lookalike_does_not_get_the_skip(self):
        # A different registrable name is not the model host, so a reference
        # hidden behind one is still a finding. All three spellings are
        # covered, not just the hyphen: `_` and a non-ASCII leading letter both
        # satisfied the detection-side `_HOST_L` lookbehind this used to reuse,
        # which is exactly how a suppression gated on it failed OPEN. Pinning
        # only `evil-huggingface.co` read as lookalike coverage it did not have.
        for host in (
            "evil-huggingface.co",
            "evil_huggingface.co",
            "\u00e9huggingface.co",
            "evil-hf.co",
            "hf.co.evil.example",
        ):
            with self.subTest(host=host):
                self.repo.write(
                    "README.md",
                    f"https://{host}/Comfy-Org/some-internal-thing\n",
                )
                findings = self.findings()
                self.assertEqual(len(findings), 1, findings)
                self.assertIn("some-internal-thing", findings[0])

    def test_a_bare_name_is_not_cleared_by_a_model_host_elsewhere(self):
        # Default-deny is unchanged for the spelling that actually leaks: a
        # bare `Comfy-Org/<name>` in prose names a GitHub repo whatever URLs
        # sit around it.
        self.repo.write(
            "README.md",
            "See https://huggingface.co/Comfy-Org/some-model and also "
            "Comfy-Org/some-model for the code.\n",
        )
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("some-model", findings[0])

    def test_team_handle_casing_and_sentence_final_period(self):
        # The empirically-confirmed false positive: CODEOWNERS spells the
        # handle `@Comfy-Org/Comfy-Cloud-Team` while the allowlist stores the
        # slug, and prose puts a period after it. The period strip runs BEFORE
        # the team/repo fork, so the team branch gets it too -- the `.git`
        # strip further down is repo-branch-only and never reaches here.
        self.repo.write(
            "docs/owners.md",
            "Owned by @Comfy-Org/Comfy-Cloud-Team.\n"
            "Also @Comfy-Org/Core-Engine-Team and @comfy-org/comfy-cloud-team.\n",
        )
        self.assertEqual(self.findings(), [])

    def test_unknown_team_with_lowercase_org_is_flagged(self):
        self.repo.write(".github/CODEOWNERS", "docs/ @comfy-org/secret-squad\n")
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("@Comfy-Org/secret-squad", findings[0])
        self.assertIn("a team not in the known-public allowlist", findings[0])

    def test_bare_org_slash_period_is_not_a_finding(self):
        # `Comfy-Org/.` names no repo at all once the sentence punctuation is
        # stripped, so there is nothing to report and nothing to crash on.
        self.repo.write("README.md", "Everything lives under Comfy-Org/.\n")
        self.assertEqual(self.findings(), [])

    def test_trailing_git_suffix_survives_the_period_strip(self):
        # The `.git` strip runs on the already-period-stripped name, so a URL
        # at the end of a sentence still resolves to the public repo.
        self.repo.write(
            "docs/install.md",
            "Clone https://github.com/Comfy-Org/comfy-typescript-sdk.git.\n",
        )
        self.assertEqual(self.findings(), [])

    def test_trailing_git_suffix_is_stripped(self):
        self.repo.write(
            "package.json",
            '{"repository": "https://github.com/Comfy-Org/comfy-typescript-sdk.git"}\n',
        )
        self.assertEqual(self.findings(), [])

    def test_codeowners_team_handles(self):
        self.repo.write(
            ".github/CODEOWNERS",
            "* @Comfy-Org/comfy-cloud-team\ndocs/ @Comfy-Org/secret-squad\n",
        )
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("@Comfy-Org/secret-squad", findings[0])
        self.assertIn("a team not in the known-public allowlist", findings[0])

    def test_npm_scope_of_a_public_repo_is_not_a_team_finding(self):
        # `@comfy-org/<pkg>` is an npm / GitHub Packages scope, not a CODEOWNERS
        # handle, and those coordinates are REQUIRED to be lowercase. Before the
        # org segment matched case-insensitively this spelling did not match at
        # all; once it did, it landed in the `@` branch and a dependency on a
        # known-PUBLIC repo was reported as "a team not in the known-public
        # allowlist" -- with no caller-side escape short of `exclude_paths:`,
        # since neither allowlist is a workflow input. The `@` branch therefore
        # admits the repo allowlist too, for a spelling that could BE an npm
        # coordinate.
        self.repo.write(
            "package.json",
            '{"dependencies": {"@comfy-org/comfy-typescript-sdk": "^1.0.0"}}\n',
        )
        self.assertEqual(self.findings(), [])

    def test_npm_crossing_survives_the_codeowners_gate_in_a_manifest(self):
        # The BE-8857 gate is keyed on the FILE, so it must not disturb the
        # crossing anywhere else: `@comfy-org/comfy-cli` -- a lowercase name
        # that collides with an allowlisted repo, i.e. exactly the spelling the
        # gate denies in CODEOWNERS -- still clears in a manifest, which is the
        # false-positive class the crossing exists to fix.
        self.repo.write(
            "package.json",
            '{"dependencies": {"@comfy-org/comfy-cli": "^1.0.0"}}\n',
        )
        self.assertEqual(self.findings(), [])

    def test_lowercase_team_spelling_in_codeowners_is_flagged(self):
        # GitHub team slugs are lowercase BY CONSTRUCTION and GitHub resolves
        # the org segment case-insensitively, so `@comfy-org/comfy-cli` in a
        # CODEOWNERS file is a real, functional team handle -- yet the
        # lowercase-only narrowing cleared it against the REPO allowlist, and
        # silently, because `comfy-cli` is a public repo. In a CODEOWNERS file
        # an `@`-prefixed reference is unambiguously an owner handle (npm
        # coordinates never appear there), so the crossing is denied and
        # default-deny is restored. (BE-8857.)
        self.repo.write("CODEOWNERS", "* @comfy-org/comfy-cli\n")
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("@Comfy-Org/comfy-cli", findings[0])
        self.assertIn("team not in the known-public allowlist", findings[0])

    def test_codeowners_gate_matches_by_basename_in_every_honored_location(
        self,
    ):
        # GitHub honors CODEOWNERS at the repo root, `.github/` and `docs/`.
        # The gate matches on the posix BASENAME, so all three are covered --
        # and `rel` is a git-tracked path, `/`-separated on every host OS.
        for rel in ("CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS"):
            with self.subTest(rel=rel):
                repo = RepoFixture()
                self.addCleanup(repo.cleanup)
                repo.write(rel, "* @comfy-org/comfy-cli\n")
                findings = checker.run_checks(repo.root).findings
                self.assertEqual(len(findings), 1, findings)
                self.assertIn("@Comfy-Org/comfy-cli", findings[0])
                self.assertIn(
                    "team not in the known-public allowlist", findings[0]
                )

    def test_codeowners_gate_only_reads_the_owner_fields(self):
        # The gate is keyed on the OWNER FIELDS of a CODEOWNERS line, not on
        # the file as a whole. A scoped monorepo PATH PATTERN carries the same
        # `@comfy-org/<name>` spelling without being an owner handle, so
        # classifying the file wholesale turned it into a hard "team not in the
        # known-public allowlist" finding on a required check. The pattern
        # keeps the npm crossing (`comfy-cli` is a public repo); the owner
        # after it is an allowlisted team. Neither is a finding.
        # (BE-8857 review.)
        self.repo.write(
            ".github/CODEOWNERS",
            "/packages/@comfy-org/comfy-cli/** @comfy-org/comfy-cloud-team\n",
        )
        self.assertEqual(self.findings(), [])

    def test_codeowners_gate_does_not_read_a_comment_as_an_owner(self):
        # CODEOWNERS legally carries `#` comments, and a comment naming a
        # package is not an owner handle. (BE-8857 review.)
        self.repo.write(
            "CODEOWNERS",
            "# we depend on @comfy-org/comfy-cli from npm\n",
        )
        self.assertEqual(self.findings(), [])

    def test_codeowners_gate_still_reads_owners_before_a_trailing_comment(
        self,
    ):
        # A trailing `#` is not a comment introducer to GitHub, so it stays
        # INSIDE the span (the over-flag direction). Either way the owner
        # fields in front of it are read. (BE-8857 review, round 2.)
        self.repo.write(
            "CODEOWNERS", "* @comfy-org/comfy-cli  # owns everything\n"
        )
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("@Comfy-Org/comfy-cli", findings[0])

    def test_codeowners_gate_reads_an_owner_only_line(self):
        # A line whose FIRST token is `@`-prefixed carries no path pattern, so
        # the whole line is owners -- the "default owners" shape people write
        # under a `# default owners` comment. Reading its lone token as a
        # pattern left a real owner handle on the crossing, which the
        # filename-casefold rationale argues against verbatim.
        # (BE-8857 review, round 2.)
        self.repo.write("CODEOWNERS", "@comfy-org/comfy-cli\n")
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("@Comfy-Org/comfy-cli", findings[0])

    def test_codeowners_gate_ignores_a_pattern_with_no_owners(self):
        # A pattern-only line has no owner fields to deny, so the crossing
        # behaves as it does in every other file.
        self.repo.write(
            "CODEOWNERS", "docs/\n# @comfy-org/comfy-cli is on npm\n"
        )
        self.assertEqual(self.findings(), [])

    def test_a_hash_only_comments_out_a_WHOLE_codeowners_line(self):
        # GitHub honors `#` as a comment introducer only at the START of a
        # line. Ending the scanned body at the first `#` ANYWHERE was a
        # one-character bypass of the whole gate: each of these left the handle
        # outside any computed span, so it fell back to the lowercase crossing
        # and cleared against the REPO allowlist -- the exact path BE-8857
        # exists to close. (BE-8857 review, round 2.)
        for line in (
            "docs/#archive/** @comfy-org/comfy-cli",
            "*# @comfy-org/comfy-cli",
            "* @comfy-org/comfy-cloud-team #x @comfy-org/comfy-cli",
        ):
            with self.subTest(line=line):
                repo = RepoFixture()
                self.addCleanup(repo.cleanup)
                repo.write("CODEOWNERS", line + "\n")
                findings = checker.run_checks(repo.root).findings
                self.assertEqual(len(findings), 1, findings)
                self.assertIn("@Comfy-Org/comfy-cli", findings[0])

    def test_codeowners_gate_does_not_split_the_pattern_on_an_escaped_space(
        self,
    ):
        # Honoring `\ ` as a literal space inside the pattern let the FIRST
        # owner handle be read as pattern text and cleared through the
        # crossing. GitHub's CODEOWNERS parser diverges from gitignore on
        # escapes anyway, and the worst case of not honoring it is a false
        # positive rather than a bypass. (BE-8857 review, round 2.)
        self.repo.write("CODEOWNERS", "foo\\ @comfy-org/comfy-cli\n")
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("@Comfy-Org/comfy-cli", findings[0])

    def test_codeowners_gate_only_covers_the_locations_github_honors(self):
        # Keying on the BASENAME alone gave owner-field grammar to any file
        # spelled that way, so PROSE in an unrelated file parsed as
        # pattern-then-owners and a line that cleared before BE-8857 became a
        # hard finding on a required check. GitHub reads CODEOWNERS from the
        # repo root, `.github/` and `docs/` only. (BE-8857 review, round 2.)
        # The NAME matches case-insensitively, but the DIRECTORY does not:
        # `DOCS/` and `.GitHub/` are distinct tracked paths GitHub does not
        # read. (BE-8857 review, round 3.)
        for rel in (
            "tests/fixtures/CODEOWNERS",
            "docs/notes/codeowners",
            "a/b/CODEOWNERS",
            "DOCS/CODEOWNERS",
            ".GitHub/CODEOWNERS",
        ):
            with self.subTest(rel=rel):
                repo = RepoFixture()
                self.addCleanup(repo.cleanup)
                repo.write(rel, "Owned by @comfy-org/comfy-cli today\n")
                self.assertEqual(checker.run_checks(repo.root).findings, [])

    def test_codeowners_gate_does_not_read_a_scoped_pattern_as_owners(self):
        # GitHub parses field one as the path pattern UNCONDITIONALLY, so the
        # "first token is `@`-prefixed, therefore the line is all owners"
        # branch has to test owner SHAPE (at most one `/`, no trailing `/`, no
        # glob) or a root-level scoped pattern hands its own `comfy-cli` to the
        # deny -- a hard finding on a required check, while the rooted spelling
        # `/packages/@comfy-org/comfy-cli/**` cleared. (BE-8857 review, r3.)
        for line in (
            "@comfy-org/comfy-cli/** @comfy-org/comfy-cloud-team",
            "@comfy-org/comfy-cli/**",
            "@comfy-org/comfy-cli/",
        ):
            with self.subTest(line=line):
                repo = RepoFixture()
                self.addCleanup(repo.cleanup)
                repo.write("CODEOWNERS", line + "\n")
                self.assertEqual(checker.run_checks(repo.root).findings, [])

    def test_codeowners_gate_looks_past_a_utf8_bom(self):
        # A UTF-8 BOM survives decoding (`_BOM_CODECS` handles only UTF-16/32)
        # and U+FEFF is not `\s`, so it joined the first token and defeated
        # both first-character decisions on line 1: the handle below yielded no
        # span at all and cleared against the REPO allowlist.
        # (BE-8857 review, round 3.)
        self.repo.write(
            "CODEOWNERS", "\ufeff@comfy-org/comfy-cli\n".encode("utf-8")
        )
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("@Comfy-Org/comfy-cli", findings[0])

    def test_codeowners_gate_looks_past_a_utf8_bom_on_a_comment(self):
        # The other direction of the same bug: a BOM'd comment line stopped
        # being recognized as one, putting a package mention in the span.
        self.repo.write(
            "CODEOWNERS",
            "\ufeff# we depend on @comfy-org/comfy-cli\n".encode("utf-8"),
        )
        self.assertEqual(self.findings(), [])

    def test_codeowners_gate_over_flags_a_package_in_trailing_prose(self):
        # ACCEPTED, pinned rather than narrowed. GitHub does not read a
        # trailing `#` as a comment, so the span runs to end of line and a
        # package named in trailing prose is reported. Stopping at a
        # whitespace-delimited `#` token would re-open the round-2 bypass
        # `* @comfy-org/<team> #x @comfy-org/comfy-cli`, and over-flagging is
        # the direction a leak gate should be wrong in -- the remedy is to
        # reword the comment. (BE-8857 review, round 3.)
        self.repo.write(
            "CODEOWNERS",
            "* @comfy-org/comfy-cloud-team  # see @comfy-org/comfy-cli on npm\n",
        )
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("@Comfy-Org/comfy-cli", findings[0])

    def test_codeowners_gate_matches_the_file_name_case_insensitively(self):
        # Git records the file name the author typed, and this was the only
        # exact-case identity test left on a path where the org segment, the
        # `.git` strip and both allowlists are all case-insensitive -- so a
        # `codeowners` spelling walked the gate and the lowercase crossing
        # cleared a real team handle against the REPO allowlist again.
        # (BE-8857 review.)
        for rel in ("codeowners", "Codeowners", ".github/CODEOWNERS"):
            with self.subTest(rel=rel):
                repo = RepoFixture()
                self.addCleanup(repo.cleanup)
                repo.write(rel, "* @comfy-org/comfy-cli\n")
                findings = checker.run_checks(repo.root).findings
                self.assertEqual(len(findings), 1, findings)
                self.assertIn("@Comfy-Org/comfy-cli", findings[0])

    def test_only_ascii_space_and_tab_delimit_a_codeowners_field(self):
        # `\s` on a `str` pattern is Unicode-wide, but GitHub delimits
        # CODEOWNERS fields on space and tab: with U+00A0 (and a mid-file
        # U+FEFF, which the round-3 BOM fix discarded on EVERY line) in the
        # leading class, each of these read as a whole-line comment, computed
        # no span, and let the handle fall back to the lowercase crossing and
        # clear against the REPO allowlist -- the round-2 `*#` bypass one
        # invisible character along, while GitHub reads field one as
        # `\xa0#`/`\ufeff#` and the handle as a functional owner.
        # (BE-8857 review, round 4.)
        for label, body in (
            ("nbsp before the hash", "\xa0# @comfy-org/comfy-cli\n"),
            (
                "BOM after line 1",
                "* @comfy-org/comfy-cloud-team\n"
                "\ufeff# @comfy-org/comfy-cli\n",
            ),
            ("nbsp inside the pattern", "*\xa0 @comfy-org/comfy-cli\n"),
        ):
            with self.subTest(label=label):
                repo = RepoFixture()
                self.addCleanup(repo.cleanup)
                repo.write("CODEOWNERS", body)
                findings = checker.run_checks(repo.root).findings
                self.assertEqual(len(findings), 1, findings)
                self.assertIn("@Comfy-Org/comfy-cli", findings[0])

    def test_a_bom_is_only_skipped_on_the_first_line(self):
        # The BOM skip is decoding residue at offset 0 of the FILE, so it is
        # keyed on the line NUMBER rather than on a charset. Both directions
        # of line 1 still behave as round 3 fixed them.
        # (BE-8857 review, round 4.)
        self.repo.write(
            "CODEOWNERS", "\ufeff@comfy-org/comfy-cli\n".encode("utf-8")
        )
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("@Comfy-Org/comfy-cli", findings[0])

    def test_a_codeowners_line_ends_only_at_a_newline(self):
        # `str.splitlines()` also ends a line at `\r`, `\v`, `\f`,
        # `\x1c`-`\x1e`, `\x85`, U+2028 and U+2029, but a CODEOWNERS line
        # ends only at `\n`. That used to affect just the reported line
        # number; since the owner-field gate it decides a VERDICT, and one
        # control character handed the checker a second "line" starting `#`
        # -- no span, so the handle cleared through the lowercase crossing,
        # while GitHub sees ONE line with that handle in its owner fields.
        # (BE-8857 review, round 4.)
        for sep in ("\r", "\x0b", "\x0c", "\x1c", "\x85", "\u2028"):
            with self.subTest(sep=sep):
                repo = RepoFixture()
                self.addCleanup(repo.cleanup)
                repo.write(
                    "CODEOWNERS",
                    "* @comfy-org/comfy-cloud-team"
                    + sep
                    + "# @comfy-org/comfy-cli\n",
                )
                findings = checker.run_checks(repo.root).findings
                self.assertEqual(len(findings), 1, findings)
                self.assertIn("@Comfy-Org/comfy-cli", findings[0])

    def test_a_crlf_codeowners_file_still_reads_its_owner_fields(self):
        # The `\n`-only split keeps a TRAILING `\r` off the last token: it is
        # a CRLF file's terminator, not content, and carrying it in would
        # strip owner shape off a default-owners line and drop the span.
        # (BE-8857 review, round 4.)
        for body in (
            "* @comfy-org/comfy-cli\r\n",
            "@comfy-org/comfy-cli\r\n",
        ):
            with self.subTest(body=body):
                repo = RepoFixture()
                self.addCleanup(repo.cleanup)
                repo.write("CODEOWNERS", body)
                findings = checker.run_checks(repo.root).findings
                self.assertEqual(len(findings), 1, findings)
                self.assertIn("@Comfy-Org/comfy-cli", findings[0])

    def test_owner_only_branch_needs_the_handle_to_be_the_only_field(self):
        # Owner SHAPE alone only catches the GLOB spellings of a pattern. On a
        # line that HAS a second field, GitHub reads field one as the pattern
        # however it is spelled, so a root-level scoped package directory --
        # one `/`, no trailing `/`, no metacharacter, i.e. handle-SHAPED --
        # handed its own `comfy-cli` to the deny and hard-failed a required
        # check, with `exclude_paths:` the only caller-side remedy. That is
        # the round-3 false positive one spelling along.
        # (BE-8857 review, round 4.)
        self.repo.write(
            "CODEOWNERS",
            "@comfy-org/comfy-cli @comfy-org/comfy-cloud-team\n",
        )
        self.assertEqual(self.findings(), [])

    def test_a_second_field_makes_field_one_a_pattern_not_an_owner(self):
        # The other direction of the same bound: field one is no longer read
        # as an owner, so a handle in the REAL owner fields is still denied.
        self.repo.write(
            "CODEOWNERS", "@comfy-org/comfy-cloud-team @comfy-org/comfy-cli\n"
        )
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("@Comfy-Org/comfy-cli", findings[0])

    def test_codeowners_file_name_compares_lowercased_not_casefolded(self):
        # A fold is not a case-insensitive comparison:
        # `"codeownerſ".casefold()` is `"codeowners"` (U+017F folds to `s`),
        # so a tracked `codeownerſ` -- not a case variant of anything GitHub
        # reads -- got owner-field grammar and its prose parsed as
        # pattern-then-owners into a hard finding on a required check. Same
        # distinction round 3 drew for the DIRECTORY.
        # (BE-8857 review, round 4.)
        self.repo.write("codeowner\u017f", "Owned by @comfy-org/comfy-cli today\n")
        self.assertEqual(self.findings(), [])

    def test_an_invisible_leading_character_makes_a_pattern_only_line(self):
        # ACCEPTED, pinned rather than closed. Once fields delimit on ASCII
        # space and tab, `\xa0@comfy-org/comfy-cli` is a single field, and
        # GitHub reads a single field as a PATTERN -- the line grants nobody
        # ownership, so there are no owner fields to deny and the ordinary
        # crossing applies, exactly as it does for a pattern with no owners.
        # Default-deny still covers the leak case: the crossing clears only
        # names already in the PUBLIC repo allowlist.
        # (BE-8857 review, round 4.)
        self.repo.write("CODEOWNERS", "\xa0@comfy-org/comfy-cli\n")
        self.assertEqual(self.findings(), [])

    def test_codeowners_gate_leaves_the_team_allowlist_path_alone(self):
        # The gate denies the repo CROSSING only. An allowlisted TEAM spelled
        # the way GitHub actually stores it (lowercase) still clears, or the
        # gate would flag every legitimate owner line in the file.
        self.repo.write("CODEOWNERS", "* @comfy-org/comfy-cloud-team\n")
        self.assertEqual(self.findings(), [])

    def test_the_npm_crossing_does_not_clear_a_github_team_spelling(self):
        # Naming a team after the repo it owns is the commonest CODEOWNERS
        # convention there is, so an unconditional crossing cleared exactly the
        # likely collision: a team handle waved through because a public repo
        # shares its name. npm scopes cannot be spelled with a capital, so the
        # canonical GitHub team casing does not reach the crossing and
        # default-deny holds for it.
        self.repo.write("CODEOWNERS", "* @Comfy-Org/ComfyUI_frontend\n")
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("@Comfy-Org/ComfyUI_frontend", findings[0])
        self.assertIn("team not in the known-public allowlist", findings[0])

    def test_canonical_team_casing_in_codeowners_stays_flagged(self):
        # The BE-8857 gate is additive: the canonical `@Comfy-Org/<name>`
        # spelling never reached the crossing to begin with (it is not
        # lowercase), so it is reported exactly as it was before the gate.
        self.repo.write("CODEOWNERS", "* @Comfy-Org/comfy-cli\n")
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("@Comfy-Org/comfy-cli", findings[0])
        self.assertIn("team not in the known-public allowlist", findings[0])

    def test_at_prefixed_name_on_neither_allowlist_is_still_flagged(self):
        # Admitting the repo allowlist widens the `@` branch by exactly the
        # public repo names and nothing else -- default-deny still holds.
        self.repo.write("package.json", '{"x": "@comfy-org/secret-pkg"}\n')
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("@Comfy-Org/secret-pkg", findings[0])

    def test_trailing_git_suffix_is_stripped_case_insensitively(self):
        # The `.git` strip has to be as case-insensitive as the match above it
        # and the membership test below it, or `ComfyUI.GIT` keeps its suffix,
        # casefolds to `comfyui.git` and is reported as a leak for a repo the
        # allowlist already covers.
        self.repo.write(
            "docs/install.md",
            "Clone Comfy-Org/ComfyUI.GIT or Comfy-Org/comfy-cli.Git\n",
        )
        self.assertEqual(self.findings(), [])

    def test_case_insensitive_git_strip_does_not_admit_a_private_name(self):
        self.repo.write("README.md", "Comfy-Org/some-internal-thing.GIT\n")
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("Comfy-Org/some-internal-thing", findings[0])

    def test_org_slash_dotgit_names_no_repo(self):
        # `Comfy-Org/.git` clears the period strip untouched (it has no TRAILING
        # dot) and is then consumed WHOLE by the `.git` strip, so the empty
        # check has to run after that strip too -- otherwise this yields the
        # repo-less "reference to Comfy-Org/" that names nothing actionable.
        self.repo.write("README.md", "The bare remote is Comfy-Org/.git\n")
        self.assertEqual(self.findings(), [])

    def test_name_class_stays_ascii_under_the_scoped_ignorecase(self):
        # `re.IGNORECASE` applied to the WHOLE pattern also widens `[A-Za-z]`:
        # under Unicode case-folding it matches U+017F and U+212A, so
        # `.casefold()` at the membership test would fold `comfy-typeſcript-sdk`
        # back onto an allowlisted name -- a default-deny bypass. Scoping the
        # flag to the org segment keeps the CAPTURE ASCII; the characters it
        # cannot read are then handled as a partly-read name (below) rather than
        # as a name boundary.
        self.repo.write("README.md", "Comfy-Org/comfy-typeſcript-sdk\n")
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("homoglyph", findings[0])

    def test_a_name_the_ascii_class_only_partly_read_is_never_cleared(self):
        # The capture class has no right boundary, so the match stops at the
        # first non-ASCII character -- and the allowlist would then be tested
        # against a PREFIX of what the file actually says. U+2010 HYPHEN renders
        # identically to `-` on github.com, so `Comfy-Org/comfyui‐internal`
        # captured `comfyui`, casefolded into the known-public set and passed
        # clean while the full private name sat in the tree.
        self.repo.write("README.md", "See Comfy-Org/comfyui‐internal\n")
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        # The finding quotes the WHOLE name, not the prefix that was read.
        self.assertIn("comfyui‐internal", findings[0])
        self.assertIn("homoglyph", findings[0])
        # "Add it to the allowlist" is not the remedy for a homoglyph, so the
        # ordinary message would send the reader the wrong way.
        self.assertNotIn("confirm it's public and add it", findings[0])

    def test_a_kelvin_sign_after_a_public_name_is_a_finding_not_a_pass(self):
        # U+212A casefolds to `k`, so absorbing it silently would clear a name
        # that is not the allowlisted one. It is a letter, so it continues the
        # name, so the reference is only partly read and is never cleared.
        self.repo.write("other.md", "Built on Comfy-Org/comfyuiK\n")
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("homoglyph", findings[0])

    def test_a_cyrillic_homoglyph_is_caught_like_the_dash_one(self):
        # The rule is every non-ASCII name character, not just the ones that
        # casefold onto ASCII: Cyrillic `а` folds to nothing ASCII, so narrowing
        # the rule to fold-collisions would leave the capture stopping at it and
        # `comfyui` clearing on its own -- the same bypass, one alphabet over.
        self.repo.write("README.md", "Comfy-Org/comfyui\u0430internal\n")
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("homoglyph", findings[0])

    def test_prose_butted_against_an_allowlisted_name_is_a_known_cost(self):
        # The flip side of the rule above, asserted so it is a DECISION rather
        # than a surprise: non-Latin prose immediately after an allowlisted name
        # (no separator) reads as part of the name and is reported. The message
        # names the remedy, and a separator clears it.
        self.repo.write("ja.md", "Comfy-Org/ComfyUIを使う\n")
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("adjacent PROSE", findings[0])

        self.repo.write("ja.md", "Comfy-Org/ComfyUI を使う\n")
        self.assertEqual(self.findings(), [])

    def test_the_partly_read_rule_covers_the_team_branch_too(self):
        self.repo.write("CODEOWNERS", "* @Comfy-Org/core-engine‐team\n")
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("@Comfy-Org/core-engine‐team", findings[0])

    def test_ordinary_prose_after_a_public_name_is_not_a_partial_read(self):
        # Only characters that CONTINUE a name (letters, digits, marks, dash and
        # connector punctuation) mean the class stopped mid-token. A curly
        # apostrophe, an em dash and an ellipsis end it -- otherwise every
        # changelog written with smart quotes would produce homoglyph findings
        # for repos that are on the allowlist.
        self.repo.write(
            "README.md",
            "Comfy-Org/ComfyUI’s frontend — see Comfy-Org/comfy-cli…\n",
        )
        self.assertEqual(self.findings(), [])

    def test_the_org_segment_must_start_a_token(self):
        # Without a left boundary the org segment matched inside a longer word,
        # so `NotComfy-Org/private` was reported as a reference to THIS org.
        self.repo.write("README.md", "NotComfy-Org/whatever is a different org\n")
        self.assertEqual(self.findings(), [])

        # Every real spelling begins at a separator, and all of them still match.
        self.repo.write(
            "urls.md",
            "https://github.com/Comfy-Org/secret-one\n"
            "see Comfy-Org/secret-two\n"
            '"Comfy-Org/secret-three"\n',
        )
        self.assertEqual(len(self.findings()), 3, self.findings())

    def test_team_allowlist_does_not_leak_into_repo_allowlist(self):
        # A team handle and a repo reference share a namespace in the source
        # text but not in the allowlists; crossing them would let a public team
        # name whitelist a private repo of the same name.
        self.repo.write("README.md", "Comfy-Org/comfy-cloud-team\n")
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("which is not in the known-public allowlist", findings[0])


class ModelHostLinkLabelTest(CheckerTestCase):
    """The markdown-label shape of the model-host false positive.

    Comfy-Org publishes model weights under `huggingface.co/Comfy-Org/`, and
    those names collided with the GitHub default-deny with no remedy available
    to a caller: they cannot be allowlisted (they are not GitHub repos --
    `gh repo view` returns NOT_FOUND for all five in the comfy-cli sweep) and
    they cannot be deleted (they tell users where to download the weights).

    `MODEL_HOST_PREFIX_RE`, and its tests in `RepoReferenceCategoryTest`, cover
    the URL shape, where the host sits in front of the reference. This class
    covers the shape the line-level scan cannot reach: in
    `[Comfy-Org/<model>](https://huggingface.co/Comfy-Org/<model>)` the same
    name appears twice and only the target has a host in front of it, so the
    label is a bare token preceded by `[`, and the link TARGET is what says
    which namespace it names.

    Deliberately narrow: the host spellings, routes, ports, lookalikes and IDN
    neighbours are the URL shape's business and are pinned there, so nothing
    here re-tests them. What is here is the label shape clearing, and the ways
    clearing it could become a bypass.
    """

    def test_markdown_label_over_a_huggingface_link_is_not_a_reference(self):
        # Shape 2, from comfy-cli's gallery fixtures: the bare token really is
        # in the file, as the LINK TEXT, so no host precedes it.
        self.repo.write(
            "gallery.md",
            "[Comfy-Org/Qwen-Image-Edit_ComfyUI]"
            "(https://huggingface.co/Comfy-Org/Qwen-Image-Edit_ComfyUI)\n",
        )
        self.assertEqual(self.findings(), [])

    def test_github_url_is_still_flagged(self):
        self.repo.write(
            "README.md",
            "https://github.com/Comfy-Org/some-internal-thing\n",
        )
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("not in the known-public allowlist", findings[0])

    def test_markdown_label_naming_a_different_repo_is_still_flagged(self):
        # The reason the label test compares NAMES and not just the target's
        # host: a label naming one repo over a link to another is not a
        # reference to the model the link points at. Exactly one finding -- the
        # label; the target's own copy clears as shape 1.
        self.repo.write(
            "gallery.md",
            "[Comfy-Org/some-internal-thing]"
            "(https://huggingface.co/Comfy-Org/Qwen-Image_ComfyUI)\n",
        )
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("Comfy-Org/some-internal-thing", findings[0])

    def test_markdown_label_over_a_github_link_is_still_flagged(self):
        # The label shape clears only over the named non-GitHub hosts. A
        # github.com target is a GitHub reference twice over, so both the label
        # and the URL are findings.
        self.repo.write(
            "gallery.md",
            "[Comfy-Org/some-internal-thing]"
            "(https://github.com/Comfy-Org/some-internal-thing)\n",
        )
        self.assertEqual(len(self.findings()), 2, self.findings())

    def test_a_label_over_a_path_segment_host_link_is_still_flagged(self):
        # The label shape runs the SAME host test against the link target, so
        # the tightened anchor has to hold on that path as well: two findings,
        # the label and the target's own copy.
        self.repo.write(
            "gallery.md",
            "[Comfy-Org/some-internal-thing]"
            "(https://evil.example/huggingface.co/Comfy-Org/some-internal-thing)\n",
        )
        self.assertEqual(len(self.findings()), 2, self.findings())

    def test_a_non_ascii_name_after_a_huggingface_host_is_not_a_homoglyph_finding(
        self,
    ):
        # The host test runs BEFORE the homoglyph branch: "rewrite the name in
        # ASCII" is not the remedy for a Hugging Face path, which is not a
        # GitHub reference at all.
        self.repo.write(
            "README.md",
            "https://huggingface.co/Comfy-Org/mod\u2010el\n",
        )
        self.assertEqual(self.findings(), [])

    def test_an_at_prefixed_label_over_a_huggingface_link_is_still_checked(self):
        # A team handle / npm coordinate labelling a model link is not a
        # spelling the label skip clears -- it stays on the team path.
        #
        # Written as a REAL markdown label. The original fixture was
        # `@Comfy-Org/some-team(https://...)` with no `]` before the `(`, so
        # `_MD_LINK_OPEN_RE` never matched and the assertion held on the missing
        # bracket rather than on the guard it names -- it passed identically
        # with `not at_prefixed` deleted. Two independent guards now hold this
        # shape (the `@` is not a `[`, and `at_prefixed` short-circuits before
        # the call), so what is pinned is the OUTCOME. (BE-8910 review.)
        self.repo.write(
            "CODEOWNERS",
            "* [@Comfy-Org/some-team]"
            "(https://huggingface.co/Comfy-Org/some-team)\n",
        )
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("team not in the known-public allowlist", findings[0])

    def test_a_homoglyph_tail_on_the_link_target_does_not_clear_the_label(self):
        # `REPO_REF_RE`'s name class is ASCII, so the target was compared on a
        # PARTIAL read while the label side was whole: the target here names
        # `some-internal-thing<U+2010>model`, a DIFFERENT repo, and used to
        # compare equal and silence the label. The label is the finding; the
        # target's own copy is a Hugging Face path and clears. (BE-8910 review.)
        self.repo.write(
            "gallery.md",
            "[Comfy-Org/some-internal-thing]"
            "(https://huggingface.co/Comfy-Org/some-internal-thing\u2010model)\n",
        )
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("Comfy-Org/some-internal-thing", findings[0])

    def test_a_percent_escape_on_the_link_target_does_not_clear_the_label(self):
        # The same prefix-vs-full-name hole reached through an escape rather
        # than a homoglyph: `%` ends the ASCII name class, so the target read as
        # `some-internal-thing` while it actually names `...%2Dother`.
        self.repo.write(
            "gallery.md",
            "[Comfy-Org/some-internal-thing]"
            "(https://huggingface.co/Comfy-Org/some-internal-thing%2Dother)\n",
        )
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("Comfy-Org/some-internal-thing", findings[0])

    def test_a_label_over_a_truncated_destination_is_not_cleared(self):
        # The destination bound TRUNCATES, so a name running to the cut is read
        # partially -- the third door into the prefix-vs-full-name hole. The
        # name here is sized so the cut lands exactly on it: what is left of the
        # bound after `https://huggingface.co/Comfy-Org/`. The URL actually
        # names `<name>bcd`, a different repo. Synthetic on purpose -- no real
        # GitHub or Hugging Face name is this long -- because the bound, not the
        # name, is what is under test. (BE-8910 review.)
        name = "a" * (
            checker._MD_LINK_DEST_MAX - len("https://huggingface.co/Comfy-Org/")
        )
        self.repo.write(
            "gallery.md",
            f"[Comfy-Org/{name}]"
            f"(https://huggingface.co/Comfy-Org/{name}bcd)\n",
        )
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("Comfy-Org/" + "a" * 40, findings[0])

    def test_a_label_over_a_long_but_complete_destination_still_clears(self):
        # The other side of that trade, and the reason truncation is DETECTED
        # rather than declined: real Hugging Face destinations run past the
        # bound routinely, and refusing to read a long one would re-open the
        # false-positive class this skip exists to close. This destination is
        # comfortably over 256 characters and the name is nowhere near the cut.
        deep = "/resolve/main/split_files/diffusion_models/" + "x" * 200
        dest = f"https://huggingface.co/Comfy-Org/a-model{deep}.safetensors"
        self.assertGreater(len(dest), checker._MD_LINK_DEST_MAX)
        self.repo.write("gallery.md", f"[Comfy-Org/a-model]({dest})\n")
        self.assertEqual(self.findings(), [])

    def test_a_bare_reference_that_opens_no_label_is_still_flagged(self):
        # `_MD_LINK_OPEN_RE` only looks FORWARD, so a bare reference in prose
        # that merely happened to be followed by `](<matching URL>` was read as
        # link text with no `[` ever opening a label. The reference has to start
        # the label. (BE-8910 review.)
        self.repo.write(
            "notes.md",
            "see Comfy-Org/some-internal-thing"
            "](https://huggingface.co/Comfy-Org/some-internal-thing)\n",
        )
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("Comfy-Org/some-internal-thing", findings[0])


class ScanScopeTest(CheckerTestCase):
    def test_untracked_files_are_not_scanned(self):
        self.repo.write("leak.md", "BE-1234\n", track=False)
        self.assertEqual(self.findings(), [])

    def test_binary_files_are_skipped(self):
        self.repo.write("blob.bin", b"BE-1234\x00\xff\xfe", track=True)
        self.repo.write("ok.md", "clean\n")
        result = self.run_checks()
        self.assertEqual(result.findings, [])
        self.assertEqual(result.skipped, [("binary", 1)])

    def test_undecodable_text_is_skipped(self):
        self.repo.write("latin.txt", b"BE-1234 caf\xe9\n", track=True)
        self.repo.write("ok.md", "clean\n")
        result = self.run_checks()
        self.assertEqual(result.findings, [])
        self.assertEqual(result.skipped, [("non-UTF-8", 1)])

    def test_directory_exclusion_prunes_subtree_and_is_counted(self):
        self.repo.write("src/generated/a.py", "BE-1234\n")
        self.repo.write("src/generated/deep/b.py", "BE-5678\n")
        self.repo.write("src/hand.py", "BE-9999\n")
        result = self.run_checks(excludes=["src/generated/"])
        self.assertEqual(len(result.findings), 1, result.findings)
        self.assertIn("src/hand.py", result.findings[0])
        self.assertEqual(result.exclusions, [("src/generated/", 2)])

    def test_file_exclusion_is_exact(self):
        self.repo.write("scripts/check.py", "BE-1234\n")
        self.repo.write("scripts/check.py.bak", "BE-5678\n")
        result = self.run_checks(excludes=["scripts/check.py"])
        self.assertEqual(len(result.findings), 1, result.findings)
        self.assertIn("scripts/check.py.bak", result.findings[0])
        self.assertEqual(result.exclusions, [("scripts/check.py", 1)])

    def test_exclusion_that_matches_nothing_is_still_reported(self):
        # A typo'd exclusion has to be visible in the log. Reporting only the
        # ones that fired is how a repo ends up believing it excluded a tree
        # it never named correctly.
        self.repo.write("a.md", "hello\n")
        self.assertEqual(
            self.run_checks(excludes=["typo/"]).exclusions, [("typo/", 0)]
        )

    def test_root_exclusion_is_rejected(self):
        for bad in ("/", ".", "./", "", "   "):
            with self.subTest(bad=bad):
                with self.assertRaises(checker.ConfigError):
                    self.run_checks(excludes=[bad])

    def test_parent_traversal_is_rejected(self):
        with self.assertRaises(checker.ConfigError):
            self.run_checks(excludes=["../elsewhere/"])

    def test_non_git_root_is_a_config_error(self):
        with tempfile.TemporaryDirectory() as plain:
            with self.assertRaises(checker.ConfigError):
                checker.run_checks(plain)

    def test_a_root_that_does_not_exist_says_so_rather_than_blaming_git(self):
        # subprocess.run(cwd=...) raises FileNotFoundError both when `git` is
        # missing and when the cwd does not exist, so an unqualified handler
        # sends the operator hunting for a git that is right there (BE-8654
        # review).
        missing = os.path.join(self.repo.root, "no-such-dir")
        with self.assertRaises(checker.ConfigError) as caught:
            checker.run_checks(missing)
        self.assertIn("not an existing directory", str(caught.exception))
        self.assertNotIn("git is not available", str(caught.exception))

    def test_a_root_naming_a_file_is_a_config_error_not_a_traceback(self):
        # NotADirectoryError escaping uncaught exits 1 -- the code the workflow
        # reads as "internal-only references found" rather than the exit 2 every
        # other unusable-configuration path returns.
        not_a_dir = self.repo.write("plain.md", "clean\n")
        with self.assertRaises(checker.ConfigError) as caught:
            checker.run_checks(not_a_dir)
        self.assertIn("cannot run git in", str(caught.exception))


class TrackedPathSurfaceTest(CheckerTestCase):
    """The tracked PATH string is scanned, not just the file's contents.

    A public tree publishes its file listing: `docs/Comfy-Org/<a-private
    repo>/placeholder.md` names that repo to anyone who clones or browses the
    repository, and until BE-9399 such a tree passed clean because only file
    CONTENTS and symlink target strings were read. The path now goes through
    the SAME `_line_findings` the contents do -- same regexes, same
    allowlists, same suppressors, same caller-side knobs -- so the two surfaces
    cannot drift apart.

    The symlink target string, the third surface, is pinned separately by
    `test_symlink_target_string_is_scanned_but_never_followed`.
    """

    def test_private_repo_name_in_a_tracked_path_is_a_finding(self):
        self.repo.write(
            "docs/Comfy-Org/some-private-repo/placeholder.md", "clean\n"
        )
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("(tracked path)", findings[0])
        self.assertIn("some-private-repo", findings[0])

    def test_the_location_label_distinguishes_the_two_surfaces(self):
        # Same name in both places is TWO findings, and a reader has to be able
        # to tell "rename the file" from "edit the file" apart.
        self.repo.write(
            "docs/Comfy-Org/some-private-repo/notes.md",
            "See Comfy-Org/some-private-repo for context.\n",
        )
        findings = self.findings()
        self.assertEqual(len(findings), 2, findings)
        path_finding = [f for f in findings if "(tracked path)" in f]
        line_finding = [f for f in findings if "(tracked path)" not in f]
        self.assertEqual(len(path_finding), 1, findings)
        self.assertEqual(len(line_finding), 1, findings)
        self.assertTrue(
            path_finding[0].startswith(
                "docs/Comfy-Org/some-private-repo/notes.md (tracked path): "
            ),
            path_finding[0],
        )
        self.assertTrue(
            line_finding[0].startswith(
                "docs/Comfy-Org/some-private-repo/notes.md:1: "
            ),
            line_finding[0],
        )

    def test_an_allowlisted_repo_name_in_a_path_is_clean(self):
        self.repo.write("docs/Comfy-Org/ComfyUI/x.md", "clean\n")
        self.assertEqual(self.findings(), [])

    def test_a_ticket_shaped_path_component_is_a_finding(self):
        # `TICKET_RE`'s `\b` fires at `/` and at `-`, so a directory component
        # and a filename prefix both match -- the same token rules the content
        # scan uses, no path-specific boundary.
        self.repo.write("notes/BE-1234/plan.md", "clean\n")
        self.repo.write("BE-5678-notes.md", "clean\n")
        findings = sorted(self.findings())
        self.assertEqual(len(findings), 2, findings)
        self.assertTrue(all("(tracked path)" in f for f in findings), findings)
        self.assertIn("BE-5678", findings[0])
        self.assertIn("BE-1234", findings[1])

    def test_allowlisted_acronyms_in_a_path_are_clean(self):
        # Built-in allowlist and the caller-side `--ticket-allow` both reach
        # the path surface, because there is only one matcher to reach.
        self.repo.write("src/UTF-8/decode.py", "clean\n")
        self.repo.write("src/GPU-100/kernel.py", "clean\n")
        self.assertEqual(
            self.findings(extra_ticket_allow=["GPU-100"]), []
        )

    def test_a_name_glued_to_the_org_segment_is_not_a_path_finding(self):
        # `REPO_REF_RE`'s left lookbehind is `[A-Za-z0-9_]`, which does NOT
        # include `/` -- that is what lets a path component match at all. The
        # pin is the other half: a letter immediately before `Comfy-Org` is a
        # different name, on a path exactly as in prose.
        self.repo.write("aComfy-Org/x.md", "clean\n")
        self.repo.write("docs/aComfy-Org/y.md", "clean\n")
        self.assertEqual(self.findings(), [])

    def test_an_internal_marker_in_a_path_is_a_finding(self):
        self.repo.write("docs/notion.so/exported-page.md", "clean\n")
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("(tracked path)", findings[0])
        self.assertIn("internal collaboration-tool marker", findings[0])

    def test_the_path_surface_inherits_the_content_suppressors(self):
        # Both false-positive suppressors the repo category carries apply here
        # too, for the same reason the allowlists do: one matcher, not two.
        # The npm/GitHub Packages scope crossing...
        self.repo.write(
            "packages/@comfy-org/comfy-cli/package.json", "{}\n"
        )
        # ...and the model-host prefix, which reads as a DIFFERENT namespace.
        self.repo.write("hf.co/Comfy-Org/some-model/config.json", "{}\n")
        self.assertEqual(self.findings(), [])

    def test_a_nested_model_host_mirror_path_over_flags(self):
        # KNOWN, ACCEPTED over-flag, pinned so a later "tidy-up" of
        # `MODEL_HOST_PREFIX_RE` notices it: the suppressor's left anchor
        # (`_AUTHORITY_L`) rejects a preceding `/`, since in prose a slash
        # before a host means the host is really a PATH segment of something
        # else. On a tracked path every segment is preceded by a slash, so a
        # mirror checked out UNDER a directory is not suppressed while the
        # same mirror at the tree root is. Over-flagging is the safe direction
        # for a leak guard, and a repo that really vendors such a tree clears
        # it with one `exclude_paths:` entry. Measured across the 11,415
        # tracked paths of nine Comfy-Org public repos: zero occurrences.
        self.repo.write(
            "models/hf.co/Comfy-Org/some-model/config.json", "{}\n"
        )
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("(tracked path)", findings[0])
        self.assertIn("Comfy-Org/some-model", findings[0])
        self.assertEqual(
            self.findings(excludes=["models/"]), []
        )

    def test_excluding_the_leaky_path_suppresses_it_and_still_counts(self):
        # `exclude_paths:` is the caller's escape hatch for a false positive,
        # and it has to cover the path surface too -- otherwise a repo that
        # excluded a vendored tree would be reddened by that tree's own name
        # with no way to clear it. The exclusion still reports its count, so
        # the hole stays named in the log.
        self.repo.write(
            "vendor/Comfy-Org/some-private-repo/placeholder.md", "clean\n"
        )
        self.repo.write("ok.md", "clean\n")
        result = self.run_checks(excludes=["vendor/"])
        self.assertEqual(result.findings, [])
        self.assertEqual(result.exclusions, [("vendor/", 1)])
        self.assertEqual(result.scanned, 1)

    def test_a_path_finding_fires_on_an_entry_whose_body_is_skipped(self):
        # The path is published in the tree whether or not the bytes inside are
        # ever read, so the path scan is independent of `check_file` -- it runs
        # for a binary blob, and for every other entry the reader declines.
        self.repo.write(
            "assets/Comfy-Org/some-private-repo/logo.bin",
            b"\x00\xff\xfe",
            track=True,
        )
        self.repo.write("ok.md", "clean\n")
        result = self.run_checks()
        self.assertEqual(len(result.findings), 1, result.findings)
        self.assertIn("(tracked path)", result.findings[0])
        self.assertIn("some-private-repo", result.findings[0])
        # ...and it does NOT make the unread blob count as scanned: that number
        # is files read as TEXT, and a path finding proves nothing about the
        # bytes inside.
        self.assertEqual(result.skipped, [("binary", 1)])
        self.assertEqual(result.scanned, 1)

    def test_a_path_finding_fires_on_a_dangling_symlink_entry(self):
        # The other body-less entry shape, and the one that already had its
        # target string scanned -- so this pins that the PATH is scanned as
        # well as the target, not instead of it.
        os.symlink(
            "../nowhere", os.path.join(self.repo.root, "BE-4242.link")
        )
        self.repo._git("add", "--", "BE-4242.link")
        self.repo.write("ok.md", "clean\n")
        findings = self.run_checks().findings
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("(tracked path)", findings[0])
        self.assertIn("BE-4242", findings[0])


class CoverageReportingTest(CheckerTestCase):
    """Nothing the checker skips may be invisible in the log (BE-8654).

    A guard that silently declines to scan something is worse than no guard,
    because the green run reads as coverage. Each skip route therefore has to
    leave a trace: an exclusion leaves a count, an unreadable file leaves a
    warning, and a run that scanned nothing at all says so.
    """

    def test_unreadable_file_is_named_rather_than_disappearing(self):
        # A tracked path the checker cannot read at all -- here a directory
        # that git tracks a file under, made unreadable. Unlike binary/
        # non-UTF-8 this is not an expected skip, so it names the file.
        self.repo.write("locked/secret.md", "clean\n")
        self.repo.write("ok.md", "clean\n")
        locked = os.path.join(self.repo.root, "locked")
        os.chmod(locked, 0o000)
        self.addCleanup(os.chmod, locked, 0o755)
        if os.access(os.path.join(locked, "secret.md"), os.R_OK):
            # root ignores the mode bits, and CI containers often run as root.
            raise unittest.SkipTest("permissions are not enforced for this user")
        result = self.run_checks()
        self.assertEqual(result.findings, [])
        self.assertEqual(len(result.warnings), 1, result.warnings)
        self.assertIn("locked/secret.md", result.warnings[0])
        self.assertIn("NOT scanned", result.warnings[0])
        self.assertEqual(result.skipped, [("unreadable", 1)])

    def test_an_unopenable_file_warns_instead_of_crashing(self):
        # REGRESSION. The test above reaches the `os.lstat` failure path (the
        # directory itself is unreadable). This one reaches `open()`'s: a
        # mode-000 REGULAR file in a readable directory, so `lstat` succeeds
        # and only the read fails. That branch returned a FIVE-tuple while the
        # docstring, every sibling branch and the single caller's unpack use
        # four, so any EACCES/EMFILE/IO error on a tracked file raised
        # `ValueError: too many values to unpack (expected 4)` and exited 1 --
        # which `public-repo-hygiene.yml` renders as "internal-only references
        # found", with no finding listed, on a repo that is in fact clean.
        # (BE-8729 review, round 5.)
        locked = self.repo.write("locked.md", "clean\n")
        self.repo.write("ok.md", "clean\n")
        os.chmod(locked, 0o000)
        self.addCleanup(os.chmod, locked, 0o644)
        if os.access(locked, os.R_OK):
            # root ignores the mode bits, and CI containers often run as root.
            raise unittest.SkipTest("permissions are not enforced for this user")
        result = self.run_checks()
        self.assertEqual(result.findings, [])
        self.assertEqual(len(result.warnings), 1, result.warnings)
        self.assertIn("locked.md", result.warnings[0])
        self.assertIn("NOT scanned", result.warnings[0])
        self.assertEqual(result.skipped, [("unreadable", 1)])

    def test_symlink_target_string_is_scanned_but_never_followed(self):
        # `open()` follows a symlink, so scanning one would read whatever it
        # points AT rather than the target string git stores in the blob: a
        # link out of the repo would pull arbitrary runner content into a
        # PUBLIC run log, and one to /dev/zero or a FIFO would hang or OOM the
        # job. But the TARGET STRING is what this repo publishes, so skipping
        # the entry outright discarded the one thing worth checking (BE-8654
        # review): `os.readlink` reads it without opening anything.
        outside = os.path.join(tempfile.mkdtemp(), "outside.md")
        self.addCleanup(os.remove, outside)
        with open(outside, "w") as fh:
            fh.write("Comfy-Org/super-secret-repo\n")
        os.symlink(outside, os.path.join(self.repo.root, "link.md"))
        # ...and the dangling case, which cannot be opened at all -- yet its
        # target string is still tracked, still published, and here it leaks.
        os.symlink(
            "../Comfy-Org/private-thing/BE-4242.md",
            os.path.join(self.repo.root, "dangling"),
        )
        self.repo._git("add", "--", "link.md", "dangling")
        self.repo.write("ok.md", "clean\n")

        result = self.run_checks()
        # The content on the far side of the link is NOT reported -- it is not
        # this repo's, and echoing it would be the leak, not the guard.
        self.assertFalse(
            any("super-secret-repo" in f for f in result.findings),
            result.findings,
        )
        # The target STRING is, on both the resolvable and the dangling link.
        self.assertEqual(len(result.findings), 2, result.findings)
        self.assertTrue(
            any("Comfy-Org/private-thing" in f for f in result.findings),
            result.findings,
        )
        self.assertTrue(
            any("BE-4242" in f for f in result.findings), result.findings
        )
        # Reading the target string IS coverage, so these count as scanned --
        # but each still names itself, because the file body was not read.
        self.assertEqual(result.skipped, [])
        self.assertEqual(result.scanned, 3)
        link_warnings = [w for w in result.warnings if "symlink" in w]
        self.assertEqual(len(link_warnings), 2, result.warnings)
        for w in link_warnings:
            self.assertIn("TARGET STRING", w)

    def test_a_fifo_is_still_skipped_outright(self):
        # A symlink has a target string worth scanning; a device node or FIFO
        # has nothing this repo stores, and reading one blocks. git cannot
        # track a FIFO directly, so reach the case the way reality would: a
        # tracked regular file that IS one by the time the scan lstats it.
        fifo = self.repo.write("pipe", "clean\n")
        os.remove(fifo)
        os.mkfifo(fifo)
        self.addCleanup(os.remove, fifo)
        self.repo.write("ok.md", "clean\n")
        result = self.run_checks()
        self.assertEqual(result.findings, [])
        self.assertEqual(result.skipped, [("not a regular file", 1)])
        self.assertEqual(result.scanned, 1)
        self.assertEqual(len(result.warnings), 1, result.warnings)
        self.assertIn("not a regular file", result.warnings[0])

    def test_oversized_file_is_truncated_loudly_not_dropped(self):
        # An unbounded read is a runner-memory DoS a PR author controls. Most
        # of a large file is still worth scanning, so the cap truncates and
        # names the unread tail rather than skipping the file.
        with unittest.mock.patch.object(checker, "MAX_FILE_BYTES", 64):
            self.repo.write("big.md", "Comfy-Org/nope\n" + "x" * 200 + "\nBE-1234\n")
            result = self.run_checks()
        # The part within the cap is still checked...
        self.assertEqual(len(result.findings), 1, result.findings)
        self.assertIn("Comfy-Org/nope", result.findings[0])
        # ...and the part beyond it is a named warning, not a silent drop.
        self.assertEqual(result.scanned, 1)
        self.assertEqual(result.skipped, [])
        self.assertEqual(len(result.warnings), 1, result.warnings)
        self.assertIn("big.md", result.warnings[0])
        self.assertIn("only the first 64", result.warnings[0])

    def test_truncation_landing_mid_codepoint_still_scans_the_head(self):
        # The cap can split a multi-byte character. Reporting the whole file as
        # non-UTF-8 would turn a size limit into a detection hole.
        with unittest.mock.patch.object(checker, "MAX_FILE_BYTES", 20):
            self.repo.write(
                "wide.md", ("Comfy-Org/nope\n" + "é" * 40).encode()
            )
            result = self.run_checks()
        self.assertEqual(len(result.findings), 1, result.findings)
        self.assertEqual(result.skipped, [])

    def test_a_bad_byte_far_from_the_cap_is_non_utf8_not_a_short_scan(self):
        # The mid-codepoint fallback must be GATED on the error landing at the
        # truncation boundary. `data[:exc.start]` is valid UTF-8 by
        # construction, so an ungated fallback silently "succeeds" for a
        # genuinely non-UTF-8 file: commit filler past the cap, one bad byte
        # near the TOP, then the internal references, and the file is scanned
        # only up to that byte, still counted as scanned, and reported under a
        # warning claiming the whole cap was read (BE-8654 review).
        with unittest.mock.patch.object(checker, "MAX_FILE_BYTES", 64):
            self.repo.write(
                "sneaky.md",
                b"ok\n\xff\nComfy-Org/nope\nBE-1234\n" + b"x" * 200,
                track=True,
            )
            self.repo.write("ok.md", "clean\n")
            result = self.run_checks()
        # Not scanned-to-the-bad-byte-and-called-covered: a real skip, counted.
        self.assertEqual(result.findings, [])
        self.assertEqual(result.skipped, [("non-UTF-8", 1)])
        self.assertEqual(result.scanned, 1)
        # And no warning that overstates what was read.
        self.assertFalse(
            any("only the first" in w for w in result.warnings), result.warnings
        )

    def test_a_committed_utf16_blob_is_decoded_rather_than_called_binary(self):
        # No gitattribute is involved here: the NUL bytes are in what git
        # STORES, so `_work_tree_encoded` never sees this file, and the `binary`
        # skip would drop it from the scan entirely -- a per-run count that
        # never touches the verdict -- while GitHub still renders the blob as
        # readable text. Every one of these encodings is self-describing via a
        # BOM, so the decode is exact rather than guessed.
        for name, encoding in (
            ("le.md", "utf-16-le"),
            ("be.md", "utf-16-be"),
            ("u32.md", "utf-32-le"),
        ):
            with self.subTest(encoding=encoding):
                repo = RepoFixture()
                self.addCleanup(repo.cleanup)
                bom = {
                    "utf-16-le": codecs.BOM_UTF16_LE,
                    "utf-16-be": codecs.BOM_UTF16_BE,
                    "utf-32-le": codecs.BOM_UTF32_LE,
                }[encoding]
                repo.write(
                    name,
                    bom + "See Comfy-Org/definitely-not-public\n".encode(encoding),
                )
                result = checker.run_checks(repo.root)
                self.assertEqual(result.skipped, [], result.skipped)
                self.assertEqual(result.scanned, 1)
                self.assertEqual(len(result.findings), 1, result.findings)

    def test_a_utf8_bom_does_not_hide_a_reference_on_the_first_line(self):
        # UTF-8 is not in the BOM table -- these bytes decode down the ordinary
        # path, leaving a U+FEFF at the head of line 1. That must not swallow a
        # reference sitting immediately behind it: U+FEFF is not a word
        # character, so neither the ticket pattern's `\b` nor the repo pattern's
        # left boundary is affected.
        self.repo.write(
            "bom.md", codecs.BOM_UTF8 + b"Comfy-Org/definitely-not-public BE-1234\n"
        )
        result = self.run_checks()
        self.assertEqual(result.scanned, 1)
        self.assertEqual(len(result.findings), 2, result.findings)

    def test_a_bom_over_bytes_that_do_not_decode_is_still_not_scanned(self):
        # Failing OPEN here would be worse than the binary skip it replaces: a
        # mislabelled BOM must not become "scanned, found nothing".
        self.repo.write("lie.md", codecs.BOM_UTF32_LE + b"\xff\xff\xff")
        result = self.run_checks()
        self.assertEqual(result.skipped, [("non-UTF-8", 1)])
        self.assertEqual(result.scanned, 0)

    def test_a_git_lfs_pointer_stub_is_named_as_a_coverage_hole(self):
        # `actions/checkout` leaves LFS off, so an LFS-tracked file is present
        # only as its pointer stub. Reading the stub and counting the file as
        # covered is a silent hole -- and one a PR could use deliberately.
        self.repo.write(
            "asset.bin",
            checker.LFS_POINTER_PREFIX
            + "\noid sha256:" + "a" * 64 + "\nsize 12345\n",
        )
        self.repo.write("ok.md", "clean\n")
        result = self.run_checks()
        self.assertEqual(result.findings, [])
        # The stub parses as text, but none of the file's actual bytes were
        # read, so it must NOT prop up the coverage claim: only `ok.md` counts.
        self.assertEqual(result.scanned, 1)
        self.assertEqual(result.skipped, [("git-LFS pointer", 1)])
        lfs = [w for w in result.warnings if "git-LFS" in w]
        self.assertEqual(len(lfs), 1, result.warnings)
        self.assertIn("asset.bin", lfs[0])

    def test_a_file_only_pretending_to_be_an_lfs_stub_is_still_scanned(self):
        # Classifying on the first line alone made the skip an OPT-OUT any
        # tracked file could take: type the magic line, and everything below it
        # went unread while the file still rendered as plain text on github.com
        # -- and with one other clean file present the zero-scan net did not
        # fire either. A genuine stub has to satisfy the whole pointer grammar.
        self.repo.write(
            "notes.md",
            checker.LFS_POINTER_PREFIX
            + "\nSee Comfy-Org/definitely-not-public and BE-1234\n",
        )
        self.repo.write("ok.md", "clean\n")
        result = self.run_checks()
        self.assertEqual(len(result.findings), 2, result.findings)
        self.assertEqual(result.scanned, 2)
        self.assertEqual(result.skipped, [])

    def test_an_oversized_pointer_lookalike_is_not_a_stub_either(self):
        # A real stub is ~130 bytes. The size ceiling stops a file that carries
        # a valid-looking header ABOVE a payload from riding the classification.
        self.repo.write(
            "big.md",
            checker.LFS_POINTER_PREFIX
            + "\noid sha256:" + "c" * 64 + "\nsize 1\n"
            + "# padding Comfy-Org/definitely-not-public\n"
            + "x" * checker.LFS_POINTER_MAX_BYTES + "\n",
        )
        result = self.run_checks()
        self.assertEqual(result.skipped, [])
        self.assertEqual(result.scanned, 1)
        self.assertEqual(len(result.findings), 1, result.findings)

    def test_a_genuine_stub_is_read_even_though_it_does_not_count_as_scanned(self):
        # Coverage and detection are separate questions, and this is the one
        # file kind where they diverge: the stub is not the file (so it cannot
        # prop up `scanned`), but it IS text this run has in hand, so it is
        # still checked. A real stub is hex and digits, so it yields nothing --
        # which is exactly why scanning it is free.
        self.repo.write(
            "asset.bin",
            checker.LFS_POINTER_PREFIX
            + "\noid sha256:" + "d" * 64 + "\nsize 7\n",
        )
        self.repo.write("ok.md", "clean\n")
        result = self.run_checks()
        self.assertEqual(result.findings, [])
        self.assertEqual(result.scanned, 1)
        self.assertEqual(result.skipped, [("git-LFS pointer", 1)])

    def test_an_all_lfs_repo_cannot_hold_the_zero_scan_net_open(self):
        # The reason the stub had to move out of `scanned`: `git lfs track
        # '*.md'` plus a commit carrying internal references would otherwise
        # exit 0 on a required status check, having read no content at all.
        self.repo.write(
            "doc.md",
            checker.LFS_POINTER_PREFIX
            + "\noid sha256:" + "b" * 64 + "\nsize 99\n",
        )
        result = self.run_checks()
        self.assertEqual(result.scanned, 0)
        self.assertEqual(result.skipped, [("git-LFS pointer", 1)])
        self.assertTrue(
            any("no files were scanned" in w for w in result.warnings),
            result.warnings,
        )
        self.assertEqual(checker._emit(result), 2)

    def test_a_submodule_gitlink_is_named_as_one_not_as_a_device_node(self):
        # `git ls-files` lists gitlinks, and the reusable workflow checks the
        # caller out without `submodules:` -- so this is the commonest way a
        # tracked path is not a regular file, and reporting it as a FIFO or
        # device node is the wrong type AND the wrong reason.
        self.repo.write("ok.md", "clean\n")
        sub = os.path.join(self.repo.root, "vendor", "lib")
        os.makedirs(sub)
        subprocess.run(["git", "init", "-q"], cwd=sub, check=True,
                       capture_output=True)
        for key, value in (("user.email", "t@example.invalid"),
                           ("user.name", "t")):
            subprocess.run(["git", "config", key, value], cwd=sub, check=True,
                           capture_output=True)
        with open(os.path.join(sub, "f.txt"), "w") as fh:
            fh.write("x\n")
        subprocess.run(["git", "add", "-A"], cwd=sub, check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-qm", "s"], cwd=sub, check=True,
                       capture_output=True)
        env = dict(os.environ, GIT_ALLOW_PROTOCOL="file")
        subprocess.run(
            ["git", "-c", "protocol.file.allow=always", "submodule", "add",
             "-q", "./vendor/lib", "vendor/lib"],
            cwd=self.repo.root, check=True, capture_output=True, env=env,
        )
        result = self.run_checks()
        gitlink = [w for w in result.warnings if "vendor/lib:" in w]
        self.assertEqual(len(gitlink), 1, result.warnings)
        self.assertIn("submodule gitlink", gitlink[0])
        self.assertNotIn("device node", gitlink[0])
        self.assertIn(("submodule gitlink", 1), result.skipped)

    def test_a_work_tree_encoding_attribute_is_a_hard_failure(self):
        # The blob git stores stays UTF-8 and plainly readable on GitHub, while
        # checkout writes NUL-laden UTF-16 to disk -- which this scan, which
        # reads the WORK TREE, skips as binary. A green run over content the
        # guard never looked at, from a two-line commit.
        self.repo.write(".gitattributes", "*.md working-tree-encoding=UTF-16\n")
        # Written as the work tree would actually hold it -- git validates the
        # round-trip on `add`, and the blob it stores from these bytes is UTF-8
        # with the reference in plain view.
        self.repo.write(
            "leak.md", "See Comfy-Org/definitely-not-public\n".encode("utf-16")
        )
        with self.assertRaises(checker.ConfigError) as ctx:
            self.run_checks()
        self.assertIn("working-tree-encoding", str(ctx.exception))
        self.assertIn("leak.md", str(ctx.exception))

    def test_an_excluded_path_may_carry_a_work_tree_encoding_attribute(self):
        # The conversion only hides something this run was going to read. On an
        # excluded path the hole is already named in the log by its exclusion
        # count, so failing there would be a false alarm with no way to clear it.
        # `UTF-16` fits TICKET_RE, so the fixture's own `.gitattributes` would
        # once have been a category-1 finding in its own right; the `UTF`
        # namespace now clears by prefix (TICKET_ALLOWED_PREFIXES), so the
        # canonical spelling is safe to write here.
        self.repo.write(
            ".gitattributes", "vendor/* working-tree-encoding=UTF-16\n"
        )
        self.repo.write("vendor/blob.md", "clean\n".encode("utf-16"))
        self.repo.write("ok.md", "clean\n")
        result = self.run_checks(excludes=["vendor/"])
        self.assertEqual(result.findings, [])
        self.assertEqual(result.scanned, 2)

    def test_a_mangled_work_tree_gitattributes_cannot_hide_the_conversion(self):
        # Resolved from the WORK TREE, the guard reads the same `.gitattributes`
        # a conversion may itself have mangled: a commit that applies
        # `working-tree-encoding=UTF-16` to the attributes file ALONGSIDE the
        # leaking file leaves an unparseable attributes file on disk, every path
        # comes back `unspecified`, and the guard fails open over exactly the
        # commit it exists to catch. The property being asserted is about the
        # bytes git STORES, so the question goes to the index (`--cached`).
        self.repo.write(".gitattributes", "*.md working-tree-encoding=UTF-16\n")
        self.repo.write(
            "leak.md", "See Comfy-Org/definitely-not-public\n".encode("utf-16")
        )
        # Mangle only the work-tree copy; the INDEX still holds the readable
        # rule, which is what checkout acted on.
        self.repo.write(
            ".gitattributes",
            "*.md working-tree-encoding=UTF-16\n".encode("utf-16"),
            track=False,
        )
        with self.assertRaises(checker.ConfigError) as ctx:
            self.run_checks()
        self.assertIn("leak.md", str(ctx.exception))

    def test_a_utf8_working_tree_encoding_is_not_a_conversion(self):
        # UTF-8 is the identity mapping git skips re-encoding for, so it hides
        # nothing -- and an exit 2 the caller can only clear by excluding paths
        # costs real coverage.
        self.repo.write(".gitattributes", "*.md working-tree-encoding=UTF-8\n")
        self.repo.write("doc.md", "clean\n")
        result = self.run_checks()
        self.assertEqual(result.findings, [])
        self.assertEqual(result.scanned, 2)

    def test_the_utf8_exemption_does_not_cover_the_bom_variants(self):
        # `UTF-8-BOM` and `UTF-8BOM` DO rewrite the bytes, so they stay fatal.
        # Asserted against the resolver rather than a fixture because git
        # refuses to stage a file whose declared BOM variant it cannot
        # round-trip, so the case cannot be built as a repo.
        for value in ("UTF-8-BOM", "utf-8bom", "UTF-16"):
            with self.subTest(value=value):
                out = "\0".join(["a.md", "working-tree-encoding", value]) + "\0"
                with unittest.mock.patch.object(
                    checker.subprocess,
                    "run",
                    return_value=unittest.mock.Mock(stdout=out.encode()),
                ):
                    self.repo.write("a.md", "clean\n")
                    self.assertEqual(
                        checker._work_tree_encoded(self.repo.root, ["a.md"]),
                        ["a.md"],
                    )

    def test_an_encoding_rule_matching_a_symlink_is_not_a_conversion(self):
        # `git check-attr` answers purely by PATH PATTERN, so a rule like
        # `*.bin working-tree-encoding=UTF-16` also "converts" a tracked symlink
        # named `notes.bin` -- an entry checkout writes with no encoding step at
        # all. Only a regular file can actually be re-encoded.
        self.repo.write(".gitattributes", "*.bin working-tree-encoding=UTF-16\n")
        os.symlink("target.txt", os.path.join(self.repo.root, "notes.bin"))
        self.repo._git("add", "--", "notes.bin")
        result = self.run_checks()
        self.assertEqual(result.findings, [])
        self.assertEqual(result.scanned, 2)

    def test_per_file_warnings_are_capped_without_losing_the_accounting(self):
        # `warnings` embeds a path per entry and has six producers; uncapped, a
        # tree of tens of thousands of tracked symlinks floods a PUBLIC run log
        # -- the same derived-output problem the finding caps close, through a
        # door they do not cover.
        self.repo.write("ok.md", "clean\n")
        count = checker.MAX_WARNINGS_TOTAL + 5
        for i in range(count):
            link = os.path.join(self.repo.root, f"link{i}")
            os.symlink("target.txt", link)
            self.repo._git("add", "--", f"link{i}")
        result = self.run_checks()
        self.assertEqual(len(result.warnings), checker.MAX_WARNINGS_TOTAL + 1)
        self.assertIn("+5 more per-file warning(s)", result.warnings[-1])
        # Coverage accounting is NOT capped: every symlink is still counted.
        self.assertEqual(result.scanned, count + 1)

    def test_a_buried_truncation_warning_still_leaves_a_coverage_count(self):
        # `scanned`/`skipped` cannot express "read, but not all of it": a file
        # truncated at the read cap counts as `scanned` with no skipped kind, so
        # before `partial` the per-file `::warning::` was the ONLY record that
        # coverage was partial -- and that warning is subject to the cap above.
        # Enough cheap tracked symlinks sorting ahead of a large file therefore
        # buried the one line saying its tail was never read, and the report
        # then claimed full coverage over a file it had only partly read.
        for i in range(checker.MAX_WARNINGS_TOTAL):
            os.symlink("target.txt", os.path.join(self.repo.root, f"aaa{i:04d}"))
            self.repo._git("add", "--", f"aaa{i:04d}")
        with unittest.mock.patch.object(checker, "MAX_FILE_BYTES", 64):
            self.repo.write("zz-big.md", "clean\n" + "x" * 200 + "\n")
            result = self.run_checks()
        # The warning naming the file lost its budget to the symlinks...
        self.assertFalse(
            [w for w in result.warnings if "zz-big.md" in w], result.warnings
        )
        # ...but the coverage arithmetic survives it.
        self.assertEqual(result.partial, [(checker.PARTIAL_READ, 1)])

    def test_a_capped_findings_file_is_counted_as_partial_too(self):
        # The per-FILE findings cap is the other producer with no count of its
        # own: the file is fully read but only partly REPORTED.
        with unittest.mock.patch.object(checker, "MAX_FINDINGS_PER_FILE", 3):
            self.repo.write("many.md", "Comfy-Org/nope\n" * 10)
            result = self.run_checks()
        self.assertEqual(len(result.findings), 3, result.findings)
        self.assertEqual(result.partial, [(checker.PARTIAL_FINDINGS, 1)])

    def test_a_clean_run_reports_no_partial_coverage(self):
        self.repo.write("ok.md", "clean\n")
        self.assertEqual(self.run_checks().partial, [])

    def test_findings_are_capped_per_file_without_softening_the_verdict(self):
        # The read cap bounds bytes READ; nothing bounded what was DERIVED from
        # them. One matching line copied once per matching pattern is how a
        # 5 MiB file becomes tens of MiB of retained strings and a flooded
        # PUBLIC run log (BE-8654 review).
        with unittest.mock.patch.object(checker, "MAX_FINDINGS_PER_FILE", 5):
            self.repo.write("many.md", "Comfy-Org/nope\n" * 50)
            result = self.run_checks()
        self.assertEqual(len(result.findings), 5, result.findings)
        capped = [w for w in result.warnings if "more than 5 findings" in w]
        self.assertEqual(len(capped), 1, result.warnings)
        self.assertIn("many.md", capped[0])
        # Capping is an enumeration limit, never an amnesty.
        self.assertIn("still FAILS", capped[0])
        self.assertEqual(checker._emit(result), 1)

    def test_findings_are_capped_per_run_too(self):
        with unittest.mock.patch.object(checker, "MAX_FINDINGS_TOTAL", 3):
            self.repo.write("a.md", "Comfy-Org/nope\n" * 5)
            self.repo.write("b.md", "Comfy-Org/nope\n" * 5)
            result = self.run_checks()
        self.assertEqual(len(result.findings), 3, result.findings)
        self.assertTrue(
            any("across the repo" in w for w in result.warnings), result.warnings
        )
        # Coverage accounting stays COMPLETE past the cap -- only the
        # enumeration stops.
        self.assertEqual(result.scanned, 2)
        self.assertEqual(checker._emit(result), 1)

    def test_a_matched_line_is_echoed_only_as_a_bounded_excerpt(self):
        # Category-2 findings echo the whole matched line, which is
        # attacker-controlled and can be the entire file.
        self.repo.write("huge.md", "https://notion.so/" + "z" * 5000 + "\n")
        result = self.run_checks()
        self.assertEqual(len(result.findings), 1, result.findings)
        self.assertIn("line truncated", result.findings[0])
        self.assertLess(len(result.findings[0]), 400, result.findings[0])

    def test_an_unbounded_repo_name_match_is_bounded_in_the_finding(self):
        # REPO_REF_RE's name class has no length bound, so `Comfy-Org/` plus
        # 5 MiB of word characters is ONE match whose text is the whole file --
        # the excerpt bound on whole LINES does not cover it.
        self.repo.write("wide.md", "Comfy-Org/" + "a" * 5000 + "\n")
        result = self.run_checks()
        self.assertEqual(len(result.findings), 1, result.findings)
        self.assertIn("truncated", result.findings[0])
        self.assertLess(len(result.findings[0]), 600, len(result.findings[0]))

    def test_binary_and_non_utf8_skips_are_counted_not_warned(self):
        # These are ordinary, expected skips, so warning on each would bury the
        # unreadable case in noise -- but silent is not an option either: one
        # stray byte appended to a document hides the whole file while it still
        # renders as text on GitHub. They get a per-run count.
        self.repo.write("blob.bin", b"\x00\xff", track=True)
        self.repo.write("latin.txt", b"caf\xe9\n", track=True)
        self.repo.write("ok.md", "clean\n")
        result = self.run_checks()
        self.assertEqual(result.warnings, [])
        self.assertEqual(result.skipped, [("binary", 1), ("non-UTF-8", 1)])
        self.assertEqual(result.scanned, 1)

    def test_scanning_nothing_is_never_reported_as_clean(self):
        self.repo.write("only.md", "clean\n")
        result = self.run_checks(excludes=["only.md"])
        self.assertEqual(result.findings, [])
        self.assertEqual(result.scanned, 0)
        self.assertTrue(
            any("no files were scanned" in w for w in result.warnings),
            result.warnings,
        )

    def test_a_repo_whose_files_are_all_unscannable_is_not_clean(self):
        # The zero-scan net has to count files the READER declined too, not
        # just excluded ones -- otherwise a tree of binaries reports "scanned"
        # coverage it never had.
        self.repo.write("blob.bin", b"\x00\xff", track=True)
        result = self.run_checks()
        self.assertEqual(result.scanned, 0)
        self.assertTrue(
            any("no files were scanned" in w for w in result.warnings),
            result.warnings,
        )

    def test_empty_repo_warns_too(self):
        self.assertTrue(
            any("no files were scanned" in w for w in self.warnings())
        )

    def test_tracked_content_at_the_reserved_path_is_a_hard_failure(self):
        # The reusable workflow checks THIS repo out at `_public_repo_hygiene/`
        # inside the caller's workspace, so tracked content there is shadowed
        # by that checkout and can never be examined. Skipping it quietly would
        # have made the reserved path a parking spot for internal references
        # that still ships green; it is exit 2 instead.
        self.repo.write("_public_repo_hygiene/x.py", "BE-1234 Comfy-Org/private\n")
        self.repo.write("real.md", "clean\n")
        with self.assertRaises(checker.ConfigError) as ctx:
            self.run_checks()
        self.assertIn("_public_repo_hygiene/", str(ctx.exception))
        self.assertIn("RESERVED", str(ctx.exception))
        self.assertEqual(
            checker.main(["--root", self.repo.root]), 2
        )

    def test_the_reserved_path_costs_nothing_when_it_is_absent(self):
        # The ordinary case: the workflow's checkout lands UNTRACKED there, so
        # `git ls-files` never lists it and the run says nothing about it.
        self.repo.write("real.md", "clean\n")
        result = self.run_checks()
        self.assertEqual(result.exclusions, [])
        self.assertEqual(result.skipped, [])

    def test_undecodable_path_bytes_do_not_crash_the_report(self):
        # `git ls-files` paths arrive via surrogateescape, so a filename holding
        # non-UTF-8 bytes carries lone surrogates. Printing one to a strict
        # stdout used to be a UnicodeEncodeError instead of a finding.
        name = b"le\xe9k.md"
        path = os.path.join(self.repo.root.encode(), name)
        try:
            with open(path, "wb") as fh:
                fh.write(b"Comfy-Org/super-secret-repo\n")
        except OSError as exc:
            # APFS enforces UTF-8 filenames, so this case cannot be built on
            # macOS at all. It is real on the ubuntu-latest runners, where ext4
            # takes any byte but `/` and NUL — skip rather than delete, so the
            # assertion still runs where the behaviour exists.
            raise unittest.SkipTest(f"filesystem rejects non-UTF-8 names: {exc}")
        self.repo._git("add", "-A")
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        # The escaping is what makes this printable at all.
        checker._esc_cmd(findings[0]).encode("utf-8")


class TamperResistanceTest(CheckerTestCase):
    """A caller-side edit to the allowlist must have NO effect (BE-8654).

    This is the acceptance criterion the whole move exists for. The scanned
    tree below contains every shape a PR author might reach for to widen the
    allowlist from inside the repo being checked -- an in-tree copy of the
    checker with a wide-open list, a config file, and (separately) environment
    variables named after the module constants. The verdict must not move.
    """

    WIDE_OPEN_COPY = (
        "PUBLIC_COMFY_ORG_REPOS = {'super-secret-repo'}\n"
        "PUBLIC_COMFY_ORG_TEAMS = {'secret-squad'}\n"
        "TICKET_ALLOWLIST = {'BE-1234'}\n"
        "INTERNAL_MARKER_RES = []\n"
        "def main():\n"
        "    return 0\n"
    )

    LEAK = (
        "Tracking BE-1234 in Comfy-Org/super-secret-repo, "
        "owned by @Comfy-Org/secret-squad. "
        "Dashboard: https://app.datadoghq.com/dash/1\n"
    )

    def _plant_and_scan(self):
        self.repo.write("LEAK.md", self.LEAK)
        # (1) The historical shape: the checker run straight out of the PR's
        #     own checkout, which is exactly what a PR could rewrite.
        self.repo.write(
            "scripts/check_public_repo_hygiene.py", self.WIDE_OPEN_COPY
        )
        # (2) The same trick at the centralized path, in case a caller tries
        #     to shadow the pinned checkout by vendoring the directory.
        self.repo.write(
            ".github/public-repo-hygiene/check_public_repo_hygiene.py",
            self.WIDE_OPEN_COPY,
        )
        # (3) A config file the checker might have been tempted to read.
        self.repo.write(
            ".public-repo-hygiene.json",
            '{"public_repos": ["super-secret-repo"], '
            '"public_teams": ["secret-squad"]}\n',
        )
        return self.findings()

    def _assert_leak_still_caught(self, findings):
        blob = "\n".join(findings)
        self.assertIn("Comfy-Org/super-secret-repo", blob)
        self.assertIn("@Comfy-Org/secret-squad", blob)
        self.assertIn("BE-1234", blob)
        self.assertIn("collaboration-tool marker", blob)

    def test_in_tree_checker_copy_and_config_are_inert(self):
        self._assert_leak_still_caught(self._plant_and_scan())

    def test_environment_cannot_widen_the_allowlists(self):
        # Nothing about the allowlists is env-driven. Asserting it keeps a
        # future "just make it configurable" from quietly reopening the hole.
        for name in (
            "PUBLIC_COMFY_ORG_REPOS",
            "PUBLIC_COMFY_ORG_TEAMS",
            "TICKET_ALLOWLIST",
            "HYGIENE_PUBLIC_REPOS",
            "EXTRA_PUBLIC_REPOS",
        ):
            os.environ[name] = "super-secret-repo,secret-squad,BE-1234"
            self.addCleanup(os.environ.pop, name, None)
        self._assert_leak_still_caught(self._plant_and_scan())

    def test_caller_inputs_cannot_reach_the_repo_allowlist(self):
        # The two knobs that ARE caller-supplied are additive and scoped: the
        # ticket allowlist takes acronyms, not repo names, and an exclusion
        # drops files from the scan without ever widening a category.
        self.repo.write("LEAK.md", self.LEAK)
        findings = self.findings(
            extra_ticket_allow=["BE-1234"], excludes=["scripts/"]
        )
        blob = "\n".join(findings)
        self.assertIn("Comfy-Org/super-secret-repo", blob)
        self.assertIn("@Comfy-Org/secret-squad", blob)
        self.assertNotIn("possible internal ticket ID", blob)

    def test_allowlists_are_immutable_constants(self):
        # A mutable set would let anything that imports this module mutate the
        # allowlist in place; frozenset makes that a TypeError rather than a
        # silently-widened check.
        for const in (
            checker.PUBLIC_COMFY_ORG_REPOS,
            checker.PUBLIC_COMFY_ORG_TEAMS,
            checker.TICKET_ALLOWLIST,
            # The casefolded views are what membership actually consults
            # (BE-8697), so they carry the same invariant as the lists they
            # derive from -- a mutable one would be the same hole one hop down.
            checker._PUBLIC_REPOS_CF,
            checker._PUBLIC_TEAMS_CF,
        ):
            self.assertIsInstance(const, frozenset)

    def test_casefolded_views_cover_their_source_lists(self):
        # The views are derived at import so they cannot drift, but pin it:
        # an entry that vanished under casefold (or a hand-written second copy
        # of the list) would silently un-allowlist a public name.
        self.assertEqual(
            checker._PUBLIC_REPOS_CF,
            {n.casefold() for n in checker.PUBLIC_COMFY_ORG_REPOS},
        )
        self.assertEqual(
            checker._PUBLIC_TEAMS_CF,
            {n.casefold() for n in checker.PUBLIC_COMFY_ORG_TEAMS},
        )
        # And no two canonical entries collide under casefold, which would mean
        # the human-edited list carries a duplicate spelling.
        self.assertEqual(
            len(checker._PUBLIC_REPOS_CF), len(checker.PUBLIC_COMFY_ORG_REPOS)
        )
        self.assertEqual(
            len(checker._PUBLIC_TEAMS_CF), len(checker.PUBLIC_COMFY_ORG_TEAMS)
        )

    def test_allowlist_contains_no_obviously_private_shape(self):
        # The allowlist is safe to host in a public repo only because it lists
        # public names exclusively. Nothing enforces that but review -- and
        # this, which at least pins the invariant that it is a flat set of
        # plain repo names, not a mapping carrying private metadata.
        for name in checker.PUBLIC_COMFY_ORG_REPOS | checker.PUBLIC_COMFY_ORG_TEAMS:
            self.assertIsInstance(name, str)
            self.assertRegex(name, r"^[A-Za-z0-9._-]+$")


class AllowlistSourceOrderTest(unittest.TestCase):
    """The allowlists are edited as SOURCE TEXT, so pin the source text.

    `PUBLIC_COMFY_ORG_REPOS` is a `frozenset`, which has no order at all at
    run time -- the only place an order exists is the literal in the module,
    and that is precisely where a human looks to answer "is `<name>` already
    in here?" before adding one. Left to review, entries accrete at the bottom
    in arrival order (BE-8855 found three appended that way), the list stops
    being scannable, and the next addition is a duplicate nobody spots.

    An exact textual duplicate is invisible to every other assertion in this
    file, because the set literal silently collapses it before any test can
    see it -- `{"a", "a"}` is just `{"a"}`. So it has to be caught in the AST,
    which is the one view that still has both copies.
    """

    def _literal_entries(self, name):
        """The set literal's elements, in the order they appear in the file."""
        with open(checker.__file__, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=checker.__file__)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if not any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets
            ):
                continue
            # `frozenset({...})` -- unwrap the call to reach the set literal.
            self.assertIsInstance(node.value, ast.Call, name)
            self.assertEqual(len(node.value.args), 1, name)
            literal = node.value.args[0]
            self.assertIsInstance(literal, ast.Set, name)
            entries = []
            for elt in literal.elts:
                self.assertIsInstance(elt, ast.Constant, name)
                self.assertIsInstance(elt.value, str, name)
                entries.append(elt.value)
            return entries
        self.fail(f"no assignment to {name} found in {checker.__file__}")

    def _assert_sorted_and_unique(self, name, runtime):
        entries = self._literal_entries(name)
        # Casefolded, because that is how a reader alphabetizes and how
        # membership is tested (BE-8697); a case-sensitive sort would demand
        # `ComfyUI` sort before `comfy-cli`, which reads as unsorted.
        self.assertEqual(
            entries,
            sorted(entries, key=str.casefold),
            f"{name} is not in case-insensitive alphabetical order",
        )
        # Duplicates are counted CASEFOLDED for the same reason the sort is:
        # membership is casefolded (BE-8697), so `comfyui` next to `ComfyUI`
        # is two spellings of ONE allowlist entry. That is also the duplicate
        # shape this list is most likely to grow, and the one nothing else
        # here can see -- a case-sensitive count sees no repeat, the casefold
        # sort is stable so the pair stays adjacent and reads as ordered, and
        # the frozenset keeps both spellings so the set and len comparisons
        # below hold.
        folded = [e.casefold() for e in entries]
        duplicates = sorted({f for f in folded if folded.count(f) > 1})
        self.assertEqual(
            duplicates, [], f"{name} has duplicate entries (case-insensitive)"
        )
        # And the literal this test read is the one the module actually uses,
        # not some other assignment that happens to share the name.
        self.assertEqual(set(entries), set(runtime), name)
        self.assertEqual(len(entries), len(runtime), name)

    def test_repo_allowlist_is_sorted_and_duplicate_free(self):
        self._assert_sorted_and_unique(
            "PUBLIC_COMFY_ORG_REPOS", checker.PUBLIC_COMFY_ORG_REPOS
        )

    def test_team_allowlist_is_sorted_and_duplicate_free(self):
        self._assert_sorted_and_unique(
            "PUBLIC_COMFY_ORG_TEAMS", checker.PUBLIC_COMFY_ORG_TEAMS
        )

    def test_the_six_repos_verified_public_for_be_8855_are_allowlisted(self):
        # Each verified PUBLIC at implementation time, twice: `gh repo view
        # Comfy-Org/<name> --json visibility` returned PUBLIC, and an
        # UNAUTHENTICATED `api.github.com/repos/Comfy-Org/<name>` returned 200
        # (a private repo 404s to an anonymous caller). Pinned here so the
        # reshuffle BE-8855 performed on this literal -- and the next one --
        # cannot drop one on the floor; `_assert_sorted_and_unique` above
        # constrains ORDER and uniqueness, not membership, so nothing else
        # notices a deletion.
        #
        # This records a point-in-time check; it is NOT a live visibility
        # probe and must not be read as one (a unit test cannot reach the
        # network, and a scheduled drift check that can is tracked
        # separately). So if one of these repos is later confirmed PRIVATE,
        # dropping it from `PUBLIC_COMFY_ORG_REPOS` is the security-correct
        # fix and this tuple is expected to shrink with it in the same commit
        # -- the red build is a prompt to edit both, never a reason to keep a
        # private name on a default-deny allowlist.
        for name in (
            "comfy-skills",
            "ComfyUI-Manager",
            "ComfyUI-test-framework",
            "cookiecutter-comfy-extension",
            "CustomNodeComfyMath",
            "workflow_templates",
        ):
            self.assertIn(name.casefold(), checker._PUBLIC_REPOS_CF, name)


class OutputTest(CheckerTestCase):
    def test_workflow_command_injection_is_escaped(self):
        raw = "a%b\nc"
        self.assertEqual(checker._esc_cmd(raw), "a%25b%0Ac")

    def test_lone_surrogate_is_made_printable(self):
        # The runnable half of the non-UTF-8-path case that only the Linux
        # runner can build end to end: a path decoded with `surrogateescape`
        # carries lone surrogates, and printing one to a strict-UTF-8 stdout
        # raises UnicodeEncodeError. It must survive escaping AND encoding.
        escaped = checker._esc_cmd("le\udce9k.md")
        self.assertEqual(escaped, "le\\udce9k.md")
        escaped.encode("utf-8")  # would raise before the fix

    def test_exit_codes(self):
        self.repo.write("ok.md", "nothing to see\n")
        self.assertEqual(checker.main(["--root", self.repo.root]), 0)
        self.repo.write("bad.md", "Comfy-Org/nope\n")
        self.assertEqual(checker.main(["--root", self.repo.root]), 1)
        self.assertEqual(
            checker.main(["--root", self.repo.root, "--exclude", "/"]), 2
        )

    def test_a_scan_that_read_nothing_exits_2_not_0(self):
        # Enumerating every top-level directory in `exclude_paths` disables the
        # whole scan without ever naming the repo root, so the root-exclusion
        # rejection alone would be one spelling away from pointless.
        self.repo.write("docs/only.md", "clean\n")
        self.assertEqual(
            checker.main(["--root", self.repo.root, "--exclude", "docs/"]), 2
        )

    def test_a_path_finding_is_listed_even_when_nothing_was_scanned(self):
        # The two can co-occur since the path surface was added: a repo of
        # nothing but binaries leaks in its own file listing while `scanned`
        # stays 0. The exit code is still 2 -- a run that read no text proves
        # nothing about the contents -- but the finding has to be PRINTED, or
        # the operator reads "nothing was scanned" and goes hunting for a
        # configuration problem instead of the leak.
        self.repo.write(
            "assets/Comfy-Org/some-private-repo/logo.bin",
            b"\x00\xff\xfe",
            track=True,
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = checker.main(["--root", self.repo.root])
        out = buf.getvalue()
        self.assertEqual(code, 2, out)
        self.assertIn("some-private-repo", out)
        self.assertIn("(tracked path)", out)
        self.assertIn("SCANNED: 0 file(s)", out)
        self.assertIn("nothing was scanned", out)

    def test_multi_value_inputs_are_split(self):
        self.assertEqual(
            checker._split_values(["a/, b/\nc/", "", "  ", "d/"]),
            ["a/", "b/", "c/", "d/"],
        )


if __name__ == "__main__":
    unittest.main()
