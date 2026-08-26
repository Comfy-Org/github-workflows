#!/usr/bin/env python3
"""Tests for check_workflow_pins.py — the `workflows_ref` default regression lint.

The lint is the only thing standing between this repo and a one-line
reintroduction of the BE-5546 hole (a `default: main` on `workflows_ref` lets a
caller SHA-pin `uses:` and still load mutable scripts). Its parsing is
text-level (no PyYAML in this repo), so the fixtures below pin the block
boundaries that text parsing is easy to get wrong: a `workflows_ref:` mentioned
in the header comment, a caller's `with:` value of the same name, and a
`default:` belonging to the NEXT input.
"""

import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import check_workflow_pins as cwp  # noqa: E402


def _reusable(ref_block, extra_inputs="", header=""):
    """A minimal `on: workflow_call` workflow whose workflows_ref block varies."""
    return (
        "name: Fixture\n"
        + header
        + "\non:\n"
        "  workflow_call:\n"
        "    inputs:\n"
        "      some_other:\n"
        "        type: string\n"
        "        required: false\n"
        "        default: hello\n"
        "      workflows_ref:\n" + ref_block + extra_inputs + "\n"
        "jobs:\n"
        "  check:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: echo hi\n"
    )


PINNED = "        description: Ref to load scripts from.\n        type: string\n        required: true\n"
DEFAULTED = PINNED.replace("required: true", "required: false") + "        default: main\n"


class FindDefaultsTests(unittest.TestCase):
    def _find(self, text):
        return cwp.find_workflows_ref_defaults(text.split("\n"))

    def test_required_no_default_is_clean(self):
        self.assertEqual(self._find(_reusable(PINNED)), [])

    def test_default_is_reported_with_its_line_number(self):
        text = _reusable(DEFAULTED)
        hits = self._find(text)
        self.assertEqual(len(hits), 1, hits)
        self.assertEqual(text.split("\n")[hits[0] - 1].strip(), "default: main")

    def test_a_later_inputs_default_is_not_attributed_to_workflows_ref(self):
        # The block ends at the next key of the same indentation. A naive
        # "search forward for default:" would blame this one on workflows_ref.
        trailing = (
            "      verbosity:\n"
            "        type: string\n"
            "        required: false\n"
            "        default: quiet\n"
        )
        self.assertEqual(self._find(_reusable(PINNED, extra_inputs=trailing)), [])
        self.assertEqual(len(self._find(_reusable(DEFAULTED, extra_inputs=trailing))), 1)

    def test_header_comment_mentioning_the_input_is_ignored(self):
        header = (
            "# Caller pattern:\n"
            "#   with:\n"
            "#     workflows_ref: main   # <- prose, not a declaration\n"
            "#         default: main\n"
        )
        self.assertEqual(self._find(_reusable(PINNED, header=header)), [])

    def test_a_caller_workflow_is_not_a_reusable_workflow(self):
        # `workflows_ref:` here is a `with:` VALUE in a caller — nothing to check.
        caller = (
            "name: CI\n"
            "on:\n"
            "  pull_request:\n"
            "jobs:\n"
            "  review:\n"
            "    uses: Comfy-Org/github-workflows/.github/workflows/x.yml@abc  # v1\n"
            "    with:\n"
            "      workflows_ref: abc\n"
        )
        self.assertIsNone(self._find(caller))

    def test_reusable_without_the_input_is_skipped(self):
        text = (
            "name: Fixture\n"
            "on:\n"
            "  workflow_call:\n"
            "    inputs:\n"
            "      max_lines:\n"
            "        type: number\n"
            "        required: false\n"
            "        default: 200\n"
        )
        self.assertIsNone(self._find(text))

    def test_inline_on_forms_are_skipped(self):
        self.assertIsNone(self._find("name: F\non: [push]\njobs: {}\n"))
        self.assertIsNone(self._find("name: F\non: push\njobs: {}\n"))

    def test_quoted_on_key_is_still_parsed(self):
        # YAML 1.1 turns a bare `on` into True, so some repos quote the key.
        self.assertEqual(len(self._find(_reusable(DEFAULTED).replace("\non:", '\n"on":'))), 1)

    def test_a_trailing_comment_on_the_on_key_is_not_an_inline_trigger_list(self):
        # `on:  # triggers` is the block form. Reading the comment as an inline
        # value would drop the whole file from the lint while CI still says OK.
        text = _reusable(DEFAULTED).replace("\non:\n", "\non:  # when this runs\n")
        self.assertEqual(len(self._find(text)), 1)

    def test_an_inline_trigger_list_with_a_comment_is_still_skipped(self):
        self.assertIsNone(self._find("name: F\non: [push]  # only pushes\njobs: {}\n"))

    def test_flow_mapping_default_on_the_input_line_is_caught(self):
        # The one-line reintroduction: no child lines for a block scan to walk.
        text = _reusable("").replace(
            "      workflows_ref:\n",
            "      workflows_ref: {type: string, required: false, default: main}\n",
        )
        hits = self._find(text)
        self.assertEqual(len(hits), 1, hits)
        self.assertIn("default: main", text.split("\n")[hits[0] - 1])

    def test_flow_mapping_without_a_default_is_clean(self):
        text = _reusable("").replace(
            "      workflows_ref:\n",
            "      workflows_ref: {type: string, required: true}\n",
        )
        self.assertEqual(self._find(text), [])

    def test_a_default_inside_a_folded_description_is_not_a_declaration(self):
        # `description: >-` continuation lines are indented DEEPER than the
        # input's own properties. This diff's own prose ("There is deliberately
        # no default: …") is one reflow away from starting such a line.
        folded = (
            "        description: >-\n"
            "          Ref to load scripts from. There is deliberately no\n"
            "          default: a floating default would defeat the pin.\n"
            "        type: string\n"
            "        required: true\n"
        )
        self.assertEqual(self._find(_reusable(folded)), [])

    def test_quoted_keys_do_not_hide_a_declaration_or_a_default(self):
        # Quoting any of these is valid Actions YAML and must not be an escape
        # hatch — the same one `on` already had.
        text = (
            _reusable(DEFAULTED)
            .replace("  workflow_call:", '  "workflow_call":')
            .replace("    inputs:", '    "inputs":')
            .replace("      workflows_ref:", '      "workflows_ref":')
            .replace("        default: main", '        "default": main')
        )
        self.assertEqual(len(self._find(text)), 1)


class GuardCoverageTests(unittest.TestCase):
    """Every `ref: ${{ inputs.workflows_ref }}` needs the guard in its own job.

    Dropping the default is only half the fix: a NEW job (or a new reusable
    workflow) that checks out at the ref without the guard reopens the `ref: ''`
    default-branch fallback, and the default-only lint stays green because it
    never declared a default to begin with.
    """

    # The real guard's shape: it RECEIVES the ref through `env:` and REJECTS an
    # empty one. Both halves matter to the detector — see
    # `test_a_step_that_only_handles_the_ref_is_not_a_guard`.
    GUARD = (
        "      - name: Require a pinned workflows_ref\n"
        "        env:\n"
        "          WORKFLOWS_REF: ${{ inputs.workflows_ref }}\n"
        "        run: |\n"
        '          REF="$(printf \'%s\' "$WORKFLOWS_REF" | tr -d \'[:space:]\')"\n'
        '          if [ -z "$REF" ]; then\n'
        "            exit 1\n"
        "          fi\n"
    )
    # groom.yml's variant (BE-8077): same guard, but the ref reaches it via the
    # `|| job.workflow_sha` fallback. The binding detector must accept BOTH
    # spellings or these seven real guards are never consulted.
    GUARD_WITH_FALLBACK = GUARD.replace(
        "${{ inputs.workflows_ref }}",
        "${{ inputs.workflows_ref || job.workflow_sha }}",
    ).replace("Require a pinned", "Require a resolvable")

    CHECKOUT = (
        "      - name: Load assets\n"
        "        uses: actions/checkout@abc\n"
        "        with:\n"
        "          ref: ${{ inputs.workflows_ref }}\n"
    )
    # Same checkout, written as a one-line flow mapping — the shape that walked
    # past a `ref:`-at-line-start anchor while the hole stayed wide open.
    FLOW_CHECKOUT = (
        "      - name: Load assets\n"
        "        uses: actions/checkout@abc\n"
        '        with: {repository: Comfy-Org/github-workflows, ref: "${{ inputs.workflows_ref }}"}\n'
    )

    @staticmethod
    def _wrap(*jobs):
        """The step blocks as a whole workflow, one job each."""
        text = "name: F\non:\n  workflow_call:\njobs:\n"
        for i, steps in enumerate(jobs):
            text += "  job%d:\n    runs-on: ubuntu-latest\n    steps:\n%s" % (i, steps)
        return text

    def _jobs(self, *jobs):
        return cwp.find_unguarded_ref_checkouts(self._wrap(*jobs).split("\n"))

    def test_guarded_checkout_passes(self):
        self.assertEqual(self._jobs(self.GUARD + self.CHECKOUT), [])

    def test_unguarded_checkout_is_reported(self):
        self.assertEqual(len(self._jobs(self.CHECKOUT)), 1)

    def test_a_guard_after_the_checkout_does_not_count(self):
        self.assertEqual(len(self._jobs(self.CHECKOUT + self.GUARD)), 1)

    def test_a_guard_in_another_job_does_not_count(self):
        # Jobs run independently — job A's guard protects nothing in job B.
        self.assertEqual(len(self._jobs(self.GUARD, self.CHECKOUT)), 1)

    def test_each_job_is_judged_on_its_own_guard(self):
        self.assertEqual(self._jobs(self.GUARD + self.CHECKOUT, self.GUARD + self.CHECKOUT), [])

    # A step that RECEIVES the ref but never tests it. Keying the guard on its
    # `env:` binding alone let this mark the whole job guarded, so every later
    # checkout passed unexamined — the lint's own subject, failing silently.
    DECOY = (
        "      - name: Print the ref\n"
        "        env:\n"
        "          WORKFLOWS_REF: ${{ inputs.workflows_ref }}\n"
        '        run: echo "$WORKFLOWS_REF"\n'
    )

    # The NEAR match: an emptiness test and a non-zero exit are both present,
    # but they are about an unrelated variable and an unrelated condition.
    # "a `-z` somewhere, an `exit` somewhere" passed this, and it is a likelier
    # accident than the bare decoy above — arg validation next to a clone.
    NEAR_MISS = (
        "      - name: Clone at the ref\n"
        "        env:\n"
        "          WORKFLOWS_REF: ${{ inputs.workflows_ref }}\n"
        "        run: |\n"
        '          if [ -z "$UNRELATED" ]; then\n'
        '            echo "unrelated value is empty"\n'
        "          fi\n"
        '          if [ "$UNRELATED" = "blocked" ]; then\n'
        "            exit 1\n"
        "          fi\n"
    )
    # Tests the RIGHT variable, but only warns — the exit belongs to a later,
    # separate branch, so an empty ref still reaches the checkout.
    NO_EXIT_IN_BRANCH = (
        "      - name: Warn only\n"
        "        env:\n"
        "          WORKFLOWS_REF: ${{ inputs.workflows_ref }}\n"
        "        run: |\n"
        '          if [ -z "$WORKFLOWS_REF" ]; then\n'
        '            echo "::warning::no ref"\n'
        "          fi\n"
        '          if [ "$OTHER" = "x" ]; then\n'
        "            exit 1\n"
        "          fi\n"
    )

    def test_a_step_that_only_handles_the_ref_is_not_a_guard(self):
        self.assertEqual(len(self._jobs(self.DECOY + self.CHECKOUT)), 1)

    def test_an_unrelated_test_and_an_unrelated_exit_are_not_a_guard(self):
        self.assertEqual(len(self._jobs(self.NEAR_MISS + self.CHECKOUT)), 1)

    def test_the_exit_must_be_in_the_empty_branch(self):
        self.assertEqual(len(self._jobs(self.NO_EXIT_IN_BRANCH + self.CHECKOUT)), 1)

    def test_a_one_line_empty_test_counts(self):
        one_liner = (
            "      - name: Require a pinned workflows_ref\n"
            "        env:\n"
            "          WORKFLOWS_REF: ${{ inputs.workflows_ref }}\n"
            '        run: [ -z "$WORKFLOWS_REF" ] && exit 1\n'
        )
        self.assertEqual(self._jobs(one_liner + self.CHECKOUT), [])

    def test_a_compound_condition_is_not_a_guard(self):
        # `-z "$REF" && OTHER = blocked` contains the emptiness test but does
        # not fail for EVERY empty ref: empty + `OTHER` unset falls straight
        # through to the checkout. A text lint cannot evaluate shell, so an
        # ANDed condition is rejected as ambiguous rather than trusted.
        compound = self.GUARD.replace(
            'if [ -z "$REF" ]; then',
            'if [ -z "$REF" ] && [ "$OTHER" = "blocked" ]; then',
        )
        self.assertEqual(len(self._jobs(compound + self.CHECKOUT)), 1)

    def test_a_single_line_if_then_exit_counts(self):
        one_line_if = (
            "      - name: Require a pinned workflows_ref\n"
            "        env:\n"
            "          WORKFLOWS_REF: ${{ inputs.workflows_ref }}\n"
            '        run: if [ -z "$WORKFLOWS_REF" ]; then exit 1; fi\n'
        )
        self.assertEqual(self._jobs(one_line_if + self.CHECKOUT), [])

    def test_an_inline_exit_after_the_branch_closes_is_not_a_guard(self):
        # `then echo "missing"; fi; exit 1` — the branch does not exit, so an
        # empty ref walks on to the checkout; the trailing exit answers to
        # nothing. The multiline path already stopped at `fi`; the inline one
        # had quietly stopped agreeing with it.
        after_fi = (
            "      - name: Decoy\n"
            "        env:\n"
            "          WORKFLOWS_REF: ${{ inputs.workflows_ref }}\n"
            '        run: if [ -z "$WORKFLOWS_REF" ]; then echo "missing"; fi; exit 1\n'
        )
        self.assertEqual(len(self._jobs(after_fi + self.CHECKOUT)), 1)

    def _run_step(self, script):
        return (
            "      - name: S\n"
            "        env:\n"
            "          WORKFLOWS_REF: ${{ inputs.workflows_ref }}\n"
            "        run: %s\n" % script
        )

    def _guard_with(self, extra_keys):
        return self.GUARD.replace(
            "      - name: Require a pinned workflows_ref\n",
            "      - name: Require a pinned workflows_ref\n" + extra_keys,
        )

    def test_a_skippable_guard_step_is_not_a_guard(self):
        # A step-level `if:` can skip the guard outright while the checkout
        # still runs. The shell inside is impeccable and guards nothing.
        step = self._guard_with("        if: github.event_name == 'push'\n")
        self.assertEqual(len(self._jobs(step + self.CHECKOUT)), 1)

    def test_a_continue_on_error_guard_is_not_a_guard(self):
        # `exit 1` that does not fail the job: the checkout runs regardless.
        step = self._guard_with("        continue-on-error: true\n")
        self.assertEqual(len(self._jobs(step + self.CHECKOUT)), 1)
        # …and an expression is not evaluable here, so it disqualifies too.
        expr = self._guard_with("        continue-on-error: ${{ inputs.soft }}\n")
        self.assertEqual(len(self._jobs(expr + self.CHECKOUT)), 1)

    def test_continue_on_error_false_is_still_a_guard(self):
        step = self._guard_with("        continue-on-error: false\n")
        self.assertEqual(self._jobs(step + self.CHECKOUT), [])

    def test_a_comment_only_continue_on_error_line_reads_its_continuation(self):
        # `continue-on-error:` with nothing (or only a comment) after the
        # colon is not "unset" — YAML lets the real scalar continue on the
        # next, more-indented line, and Actions reads THAT as the value.
        # Stopping at the colon would treat the comment-only line as an
        # absent key (same as `false`) while the job actually gets
        # `continue-on-error: true` from the continuation.
        true_continuation = self._guard_with(
            "        continue-on-error: # temporarily tolerated\n"
            "          true\n"
        )
        self.assertEqual(len(self._jobs(true_continuation + self.CHECKOUT)), 1)
        # …but a `false` continuation is still a real guard.
        false_continuation = self._guard_with(
            "        continue-on-error:\n"
            "          false\n"
        )
        self.assertEqual(self._jobs(false_continuation + self.CHECKOUT), [])

    def test_a_conditional_exit_inside_the_branch_is_not_a_guard(self):
        # The branch is entered on an empty ref but only exits if `$X` matches,
        # so the empty ref still reaches the checkout. The multiline path
        # already required a bare `exit`; the inline path had not.
        step = self._run_step(
            'if [ -z "$WORKFLOWS_REF" ]; then [ "$X" = y ] && exit 1; fi'
        )
        self.assertEqual(len(self._jobs(step + self.CHECKOUT)), 1)

    def test_a_nested_conditional_exit_is_not_a_guard_in_a_script_block(self):
        # The multiline twin of the test above: the empty-ref branch is
        # entered, but its only `exit` sits inside an inner `if`, so an empty
        # ref still reaches the checkout. One rule, two paths.
        step = (
            "      - name: S\n"
            "        env:\n"
            "          WORKFLOWS_REF: ${{ inputs.workflows_ref }}\n"
            "        run: |\n"
            '          if [ -z "$WORKFLOWS_REF" ]; then\n'
            '            if [ "$X" = y ]; then\n'
            "              exit 1\n"
            "            fi\n"
            "          fi\n"
        )
        self.assertEqual(len(self._jobs(step + self.CHECKOUT)), 1)

    def test_an_exit_after_a_nested_block_still_counts(self):
        # …but nesting must not blind the scan to the branch's own exit.
        step = (
            "      - name: S\n"
            "        env:\n"
            "          WORKFLOWS_REF: ${{ inputs.workflows_ref }}\n"
            "        run: |\n"
            '          if [ -z "$WORKFLOWS_REF" ]; then\n'
            '            if [ "$X" = y ]; then\n'
            '              echo "note"\n'
            "            fi\n"
            "            exit 1\n"
            "          fi\n"
        )
        self.assertEqual(self._jobs(step + self.CHECKOUT), [])

    def test_the_one_liner_exit_must_be_what_the_and_reaches(self):
        # `… && echo warn; … && exit 1` — the `&&` reaches only the echo.
        step = self._run_step(
            '[ -z "$WORKFLOWS_REF" ] && echo warn; [ "$X" = y ] && exit 1'
        )
        self.assertEqual(len(self._jobs(step + self.CHECKOUT)), 1)

    def test_the_guard_may_test_a_variable_derived_from_the_ref(self):
        # The real guard tests `$REF`, assigned from `$WORKFLOWS_REF` — but the
        # hop has to actually carry the value.
        self.assertEqual(self._jobs(self.GUARD + self.CHECKOUT), [])
        unrelated_hop = self.GUARD.replace(
            "REF=\"$(printf '%s' \"$WORKFLOWS_REF\" | tr -d '[:space:]')\"",
            'REF="$(cat /etc/hostname)"',
        )
        self.assertEqual(len(self._jobs(unrelated_hop + self.CHECKOUT)), 1)

    def test_a_decoy_before_the_real_guard_still_passes(self):
        # The decoy must not POISON a job that does guard — only fail to excuse
        # one that does not.
        self.assertEqual(self._jobs(self.DECOY + self.GUARD + self.CHECKOUT), [])

    def test_a_job_level_env_hoist_is_not_a_guard(self):
        # Hoisting the ref to a job-level `env:` is the natural refactor once
        # several steps want it. It binds the value but rejects nothing, and it
        # is not a step at all — so the checkout below it is unguarded, and the
        # `ref: ${{ env.WORKFLOWS_REF }}` spelling must still read as a ref use.
        text = (
            "name: F\non:\n  workflow_call:\njobs:\n"
            "  job0:\n    runs-on: ubuntu-latest\n"
            "    env:\n      WORKFLOWS_REF: ${{ inputs.workflows_ref }}\n"
            "    steps:\n"
            "      - name: Load assets\n"
            "        uses: actions/checkout@abc\n"
            "        with:\n"
            "          ref: ${{ env.WORKFLOWS_REF }}\n"
        )
        self.assertEqual(len(cwp.find_unguarded_ref_checkouts(text.split("\n"))), 1)

    ALIASED_CHECKOUT = (
        "      - name: Load assets\n"
        "        uses: actions/checkout@abc\n"
        "        with:\n"
        "          ref: ${{ env.WORKFLOWS_REF }}\n"
    )

    def test_an_aliased_checkout_still_needs_a_guard_in_its_own_job(self):
        # The guard in job A binds the name; job B checks out at it with no
        # guard of its own. Reading only `inputs.` made job B's checkout vanish.
        self.assertEqual(len(self._jobs(self.GUARD, self.ALIASED_CHECKOUT)), 1)

    def test_an_aliased_checkout_behind_the_guard_passes(self):
        self.assertEqual(self._jobs(self.GUARD + self.ALIASED_CHECKOUT), [])

    def test_only_env_blocks_bind_an_alias(self):
        # The checkout's own `ref:` is a mapping key bound to the input too.
        # Collecting it as an alias would make `env.ref`/`$ref` anywhere read
        # as the input.
        lines = (self.GUARD + self.CHECKOUT).split("\n")
        self.assertEqual(cwp.env_aliases(lines), frozenset({"WORKFLOWS_REF"}))

    def test_an_unbound_env_name_is_not_a_ref_use(self):
        # Nothing binds this name to the input, so it is an unrelated variable
        # — demanding a guard for it would fail a compliant workflow.
        self.assertEqual(self._jobs(self.ALIASED_CHECKOUT), [])

    def test_a_flow_mapping_checkout_is_not_an_escape_hatch(self):
        # `with: {…, ref: …}` on one line is the same unguarded checkout, and
        # anchoring on `ref:` at line start reported nothing at all for it.
        self.assertEqual(len(self._jobs(self.FLOW_CHECKOUT)), 1)

    def test_a_guarded_flow_mapping_checkout_passes(self):
        self.assertEqual(self._jobs(self.GUARD + self.FLOW_CHECKOUT), [])

    def test_a_sibling_flow_entry_is_not_read_as_the_ref(self):
        # `ref:` is pinned to a literal here; the input feeds a DIFFERENT key.
        # Matching greedily across the whole line would call this a ref use and
        # fail a workflow that never checks out at the input.
        step = (
            "      - name: Not a ref checkout\n"
            '        with: {ref: v1, path: "${{ inputs.workflows_ref }}"}\n'
        )
        self.assertEqual(self._jobs(step), [])

    # The same checkout again, with the value on the FOLLOWING line. Neither
    # same-line pattern sees an `inputs.` on the `ref:` line, so before the
    # continuation scan these reported nothing for a job with no guard at all.
    FOLDED_CHECKOUT = (
        "      - name: Load assets\n"
        "        uses: actions/checkout@abc\n"
        "        with:\n"
        "          ref: >-\n"
        "            ${{ inputs.workflows_ref }}\n"
    )
    LITERAL_CHECKOUT = FOLDED_CHECKOUT.replace("ref: >-", "ref: |")
    PLAIN_CHECKOUT = FOLDED_CHECKOUT.replace("ref: >-", "ref:")
    # …and the same two with a comment where the value would sit. A comment does
    # not end the mapping value: both still take the ref from the line below.
    COMMENTED_CHECKOUT = FOLDED_CHECKOUT.replace("ref: >-", "ref:  # the pinned ref")
    COMMENTED_FOLDED_CHECKOUT = FOLDED_CHECKOUT.replace("ref: >-", "ref: >-  # pinned")

    def test_a_folded_scalar_checkout_is_not_an_escape_hatch(self):
        self.assertEqual(len(self._jobs(self.FOLDED_CHECKOUT)), 1)

    def test_a_literal_scalar_checkout_is_not_an_escape_hatch(self):
        self.assertEqual(len(self._jobs(self.LITERAL_CHECKOUT)), 1)

    def test_a_plain_multiline_checkout_is_not_an_escape_hatch(self):
        self.assertEqual(len(self._jobs(self.PLAIN_CHECKOUT)), 1)

    def test_a_commented_ref_key_checkout_is_not_an_escape_hatch(self):
        # `ref:  # pinned` with the value below is a working checkout, but the
        # comment left the key line looking like an ordinary finished scalar, so
        # the continuation scan never opened and the job read as having no
        # checkout at all — silently, since `_consumes_input` missed it too.
        self.assertEqual(len(self._jobs(self.COMMENTED_CHECKOUT)), 1)

    def test_a_comment_after_a_block_header_is_not_an_escape_hatch(self):
        # YAML allows a comment after `|`/`>`; the value still follows below.
        self.assertEqual(len(self._jobs(self.COMMENTED_FOLDED_CHECKOUT)), 1)

    def test_a_guarded_multiline_checkout_passes(self):
        self.assertEqual(self._jobs(self.GUARD + self.FOLDED_CHECKOUT), [])

    def test_a_guarded_commented_checkout_passes(self):
        self.assertEqual(self._jobs(self.GUARD + self.COMMENTED_CHECKOUT), [])

    def test_a_trailing_comment_on_the_guard_still_counts_as_the_guard(self):
        # The guard is doing its job; refusing to see it would fail a compliant
        # workflow, which is the opposite of the flow-form guard's trade-off.
        guard = self.GUARD.replace(
            "WORKFLOWS_REF: ${{ inputs.workflows_ref }}",
            "WORKFLOWS_REF: ${{ inputs.workflows_ref }}  # the pinned ref",
        )
        self.assertEqual(self._jobs(guard + self.CHECKOUT), [])

    def test_a_multiline_ref_pinned_to_a_literal_is_not_a_use(self):
        # The window a `ref:` key opens is not itself a finding: this checkout
        # never names the input, and failing it would fail a compliant workflow.
        step = (
            "      - name: Literal ref\n"
            "        with:\n"
            "          ref: >-\n"
            "            main\n"
        )
        self.assertEqual(self._jobs(step), [])

    # A quoted scalar that opens WITH text and closes on a later line. The
    # runtime folds it to the one-line `${{ inputs.workflows_ref }}`, but the
    # key line names no input and the quote does not end the line, so neither
    # the same-line pattern nor the end-of-line opener saw a checkout at all.
    QUOTED_SPLIT_CHECKOUT = FOLDED_CHECKOUT.replace(
        "ref: >-\n            ${{ inputs.workflows_ref }}",
        'ref: "${{\n            inputs.workflows_ref }}"',
    )

    def test_a_quoted_scalar_split_after_its_opening_text_is_not_an_escape_hatch(self):
        self.assertEqual(len(self._jobs(self.QUOTED_SPLIT_CHECKOUT)), 1)
        self.assertEqual(
            len(self._jobs(self.QUOTED_SPLIT_CHECKOUT.replace('"', "'"))), 1
        )

    def test_a_guarded_split_quoted_checkout_passes(self):
        self.assertEqual(self._jobs(self.GUARD + self.QUOTED_SPLIT_CHECKOUT), [])

    def test_a_hash_inside_a_split_quoted_ref_is_content(self):
        # The continuation is still inside the quoted scalar, so its ` #` is
        # part of the value and must not comment the input mention out.
        step = self.QUOTED_SPLIT_CHECKOUT.replace(
            'inputs.workflows_ref }}"', 'x # inputs.workflows_ref }}"'
        )
        self.assertEqual(len(self._jobs(step)), 1)

    def test_a_split_quoted_ref_naming_no_input_is_not_a_use(self):
        step = self.QUOTED_SPLIT_CHECKOUT.replace("inputs.workflows_ref", "main")
        self.assertEqual(self._jobs(step), [])

    def test_a_quoted_ref_closed_on_its_own_line_opens_no_window(self):
        # `ref: "main"` is finished; the input on the next, shallower key is
        # someone else's value and must not be blamed on the ref.
        step = (
            "      - name: Literal ref\n"
            "        with:\n"
            '          ref: "main"\n'
            "        env:\n"
            "          X: ${{ inputs.workflows_ref }}\n"
        )
        self.assertEqual(self._jobs(step), [])

    def test_the_input_after_a_ref_scalar_closes_is_not_attributed_to_it(self):
        # `ref:` is pinned; the input feeds a LATER, shallower key. Running the
        # continuation scan past the scalar's end would blame it on the `ref:`.
        step = (
            "      - name: Literal ref\n"
            "        with:\n"
            "          ref: >-\n"
            "            main\n"
            "          path: ${{ inputs.workflows_ref }}\n"
        )
        self.assertEqual(self._jobs(step), [])

    def test_a_job_workflow_sha_fallback_still_needs_a_guard(self):
        # BE-8077, correcting BE-4169. The fallback can never resolve to a
        # MUTABLE ref -- which is what still earns it the `default: ''`
        # carve-out in `check_dir` -- but it is NOT self-sufficient:
        # `job.workflow_sha` needs runner v2.334.0+ and expands to '' on
        # anything older, which checkout reads as the default branch. So the
        # fallback answers mutability, the guard answers emptiness, and the
        # lint requires BOTH. Exempting the fallback from the guard check made
        # groom.yml's seven guard steps deletable with this lint still green.
        checkout = (
            "      - name: Load assets\n"
            "        uses: actions/checkout@abc\n"
            "        with:\n"
            "          ref: ${{ inputs.workflows_ref || job.workflow_sha }}\n"
        )
        self.assertEqual(len(self._jobs(checkout)), 1)

    def test_a_guarded_job_workflow_sha_fallback_passes(self):
        # The other direction: groom.yml's real shape -- the fallback checkout
        # WITH its guard -- must stay clean. That guard binds the ref as
        # `${{ inputs.workflows_ref || job.workflow_sha }}` too, and matching
        # only the bare `${{ inputs.workflows_ref }}` binding meant this shape
        # was never recognized as a guard at all.
        checkout = (
            "      - name: Load assets\n"
            "        uses: actions/checkout@abc\n"
            "        with:\n"
            "          ref: ${{ inputs.workflows_ref || job.workflow_sha }}\n"
        )
        self.assertEqual(self._jobs(self.GUARD_WITH_FALLBACK + checkout), [])

    def test_a_fallback_guard_does_not_cover_a_bare_input_checkout(self):
        # The strength rule, and the reason `_GUARD_BINDING_RE` records WHICH
        # expression each guard validated. This guard proves only that
        # `inputs.workflows_ref || job.workflow_sha` is non-empty. With the
        # input omitted it passes on `job.workflow_sha` while the bare
        # `ref: ${{ inputs.workflows_ref }}` below still receives '' and
        # checkout takes the default branch — so it must NOT mark the job
        # guarded for that checkout. Treating any recognized binding as blanket
        # job-wide coverage put a live hole behind a green lint.
        self.assertEqual(len(self._jobs(self.GUARD_WITH_FALLBACK + self.CHECKOUT)), 1)

    def test_a_bare_guard_covers_a_fallback_checkout(self):
        # The permitted direction: a guard on the bare input proves the INPUT
        # itself is non-empty, which is strictly stronger than what a fallback
        # checkout needs.
        checkout = (
            "      - name: Load assets\n"
            "        uses: actions/checkout@abc\n"
            "        with:\n"
            "          ref: ${{ inputs.workflows_ref || job.workflow_sha }}\n"
        )
        self.assertEqual(self._jobs(self.GUARD + checkout), [])

    def test_a_third_operand_defeats_the_fallback_exemption(self):
        # The fallback regex is anchored to the CLOSE of the interpolation on
        # purpose. Unanchored it read "contains the fallback" rather than "IS
        # the fallback", so this expression -- which resolves to the MUTABLE
        # default branch in precisely the pre-v2.334.0 case the fallback exists
        # for -- was blessed by the very lint meant to catch it. It is reported
        # here because it is unguarded; `test_a_third_operand_is_not_a_self_pin`
        # covers the `default: ''` carve-out half.
        checkout = (
            "      - name: Load assets\n"
            "        uses: actions/checkout@abc\n"
            "        with:\n"
            "          ref: ${{ inputs.workflows_ref || job.workflow_sha || 'main' }}\n"
        )
        self.assertEqual(len(self._jobs(checkout)), 1)

    def test_a_leading_operand_defeats_the_fallback_exemption(self):
        # The anchor has to hold at BOTH ends. Anchoring only the tail still let
        # an operand in FRONT through, and that one resolves to whatever the
        # leading operand names — a branch, a tag, another input — which the
        # runtime guard cannot catch, since a guard proves non-emptiness, not
        # immutability.
        checkout = (
            "      - name: Load assets\n"
            "        uses: actions/checkout@abc\n"
            "        with:\n"
            "          ref: ${{ inputs.override || inputs.workflows_ref || job.workflow_sha }}\n"
        )
        self.assertEqual(len(self._jobs(checkout)), 1)

    def test_an_env_alias_of_the_fallback_binding_is_still_a_ref_use(self):
        # `_ENV_ALIAS_RE` has to know the same spellings `_GUARD_BINDING_RE`
        # does. Hoisting the binding to a shared `env:` and checking out at
        # `ref: ${{ env.WORKFLOWS_REF }}` is exactly the refactor the alias
        # machinery exists to survive; if it registers no alias, `is_ref_use`
        # stops seeing those checkouts and the file drops to zero coverage
        # while reporting nothing at all.
        self.assertEqual(
            cwp.env_aliases(
                "env:\n"
                "  WORKFLOWS_REF: ${{ inputs.workflows_ref || job.workflow_sha }}\n".split("\n")
            ),
            frozenset({"WORKFLOWS_REF"}),
        )

    def test_an_env_alias_NEVER_earns_the_self_pin_exemption(self):
        # Carrying the binding's strength to `ref: ${{ env.NAME }}` was tried
        # and reverted: `env:` is scoped per step and per job and it SHADOWS,
        # while these scans are file-wide, so a file-wide "names bound to the
        # fallback" set granted the exemption at checkouts the binding never
        # reaches. This fixture is the counterexample that killed it — the
        # binding lives in the GUARD step's `env:`, which is invisible to the
        # sibling checkout step at run time, so `${{ env.WORKFLOWS_REF }}`
        # expands to '' and takes the default branch. It scored a guarded
        # self-pin. An alias is judged BARE now, so a fallback guard does not
        # cover it and the checkout is reported.
        aliased = (
            "      - name: Load assets\n"
            "        uses: actions/checkout@abc\n"
            "        with:\n"
            "          ref: ${{ env.WORKFLOWS_REF }}\n"
        )
        lines = self._wrap(self.GUARD_WITH_FALLBACK + aliased).split("\n")
        self.assertFalse(
            any(fb for _, fb, _, _ in cwp.ref_checkouts(lines)),
            "an env alias must never read as a self-pin",
        )
        self.assertEqual(len(self._jobs(self.GUARD_WITH_FALLBACK + aliased)), 1)
        # …and a BARE guard, which proves the input itself is non-empty, does
        # cover it. That is the correct answer, and the one left standing.
        self.assertEqual(self._jobs(self.GUARD + aliased), [])

    def test_a_mutable_step_local_binding_inherits_nothing(self):
        # The other direction of the same file-wide bug: a step-local
        # `WORKFLOWS_REF: ${{ inputs.workflows_ref || 'main' }}` inherited
        # fallback strength from any OTHER step binding that name strictly.
        # cursor-review.yml binds `WORKFLOWS_REF` both ways today, so this was
        # not hypothetical.
        mixed = (
            "      - name: Load assets\n"
            "        uses: actions/checkout@abc\n"
            "        env:\n"
            "          WORKFLOWS_REF: ${{ inputs.workflows_ref || 'main' }}\n"
            "        with:\n"
            "          ref: ${{ env.WORKFLOWS_REF }}\n"
        )
        lines = self._wrap(self.GUARD_WITH_FALLBACK + mixed).split("\n")
        self.assertFalse(any(fb for _, fb, _, _ in cwp.ref_checkouts(lines)))
        self.assertEqual(len(self._jobs(self.GUARD_WITH_FALLBACK + mixed)), 1)

    def test_the_leading_operand_has_to_reach_the_input(self):
        # A guard proves the INPUT is non-empty; it says nothing about an
        # expression that never reaches the input. GitHub's `||` returns the
        # first TRUTHY operand, so both of these mention the input (so they are
        # ref uses, and clear the guard) while resolving to a mutable ref on
        # every runner. The literal form needs no second input declaration at
        # all — which is why the earlier "unreachable without another input"
        # deferral of this was wrong.
        for ref in ("${{ 'main' || inputs.workflows_ref }}",
                    "${{ inputs.override || inputs.workflows_ref }}"):
            checkout = (
                "      - name: Load assets\n"
                "        uses: actions/checkout@abc\n"
                "        with:\n"
                "          ref: %s\n" % ref
            )
            self.assertEqual(len(self._jobs(self.GUARD + checkout)), 1, ref)

    def test_the_leading_operand_check_accepts_the_legitimate_shapes(self):
        # The narrowing must not fire on anything this module works to accept.
        for ref in ("${{ inputs.workflows_ref }}",
                    "${{ inputs.workflows_ref || job.workflow_sha }}",
                    "${{ env.WORKFLOWS_REF }}"):
            checkout = (
                "      - name: Load assets\n"
                "        uses: actions/checkout@abc\n"
                "        with:\n"
                "          ref: %s\n" % ref
            )
            self.assertEqual(self._jobs(self.GUARD + checkout), [], ref)

    def test_a_quoted_sibling_cannot_plant_a_decoy_ref(self):
        # The flow matcher `search`es mid-line, so its `[{,]` boundary can be
        # met by a comma INSIDE a quoted scalar — planting a `ref:` that scores
        # the line a self-pin while the real `ref:` on it is bare, which buys
        # the weaker fallback-guard requirement and the `default: ''` carve-out.
        decoy = (
            "  with: {ref: '${{ inputs.workflows_ref }}', "
            "path: \"x, ref: ${{ inputs.workflows_ref || job.workflow_sha }}, y\"}"
        )
        self.assertFalse(cwp._pins_to_job_workflow_sha(decoy))
        # …while a real flow-form fallback, quoted or not, still counts.
        self.assertTrue(cwp._pins_to_job_workflow_sha(
            "  with: {repository: a/b, ref: ${{ inputs.workflows_ref || job.workflow_sha }}}"
        ))
        self.assertTrue(cwp._pins_to_job_workflow_sha(
            "  with: {ref: '${{ inputs.workflows_ref || job.workflow_sha }}', x: 1}"
        ))

    def test_a_comment_does_not_bind_an_env_alias(self):
        # The widened `_ENV_ALIAS_RE` matches any value mentioning the input, so
        # unstripped it became the one place in the module reading a comment as
        # code — failing a compliant workflow whose unrelated `env:` value
        # merely mentions the input in prose.
        lines = self._wrap(
            "      - name: x\n"
            "        env:\n"
            "          GROOM_ASSETS: _groom_assets  # checked out at inputs.workflows_ref\n"
            "        run: echo hi\n"
        ).split("\n")
        self.assertEqual(cwp.env_aliases(lines), frozenset())

    def test_an_unrecognized_env_binding_still_counts_as_reaching_the_input(self):
        # Enumerating blessed spellings made an unrecognized one fail OPEN:
        # `WORKFLOWS_REF: ${{ inputs.workflows_ref || 'main' }}` registered no
        # alias, so `ref: ${{ env.WORKFLOWS_REF }}` read as no ref use at all
        # and that checkout — carrying the exact mutable fallback this lint
        # exists to catch — left the lint entirely.
        lines = (
            "jobs:\n"
            "  check:\n"
            "    steps:\n"
            "      - name: Load assets\n"
            "        env:\n"
            "          WORKFLOWS_REF: ${{ inputs.workflows_ref || 'main' }}\n"
            "        uses: actions/checkout@abc\n"
            "        with:\n"
            "          ref: ${{ env.WORKFLOWS_REF }}\n"
        ).split("\n")
        self.assertEqual(cwp.env_aliases(lines), frozenset({"WORKFLOWS_REF"}))
        self.assertEqual(len(cwp.find_unguarded_ref_checkouts(lines)), 1)

    # ── Three spellings that left a checkout invisible to BOTH halves of the
    # lint until BE-8146, each failing OPEN: the `_CONSUMES_*` backstop only
    # runs when `defaults is None` (the input DECLARATION was unparseable), so
    # a well-formed workflow using any of them got a green lint with an
    # unguarded checkout. ──

    # 1. The flow-style `env:` — the same binding as the block form, on one
    # line. `_ENV_KEY_RE` requires `env:` to END its line, so nothing bound.
    FLOW_ENV_CHECKOUT = (
        "      - name: Load assets\n"
        "        uses: actions/checkout@abc\n"
        "        env: {WORKFLOWS_REF: \"${{ inputs.workflows_ref }}\"}\n"
        "        with:\n"
        "          ref: ${{ env.WORKFLOWS_REF }}\n"
    )
    # 2a. Index access on `inputs` — documented Actions expression syntax, and
    # interchangeable with the property access every mention regex knew.
    BRACKET_INPUT_CHECKOUT = (
        "      - name: Load assets\n"
        "        uses: actions/checkout@abc\n"
        "        with:\n"
        "          ref: ${{ inputs[\'workflows_ref\'] }}\n"
    )
    # 2b. …and on `env`, so a bound alias was unreachable the same way.
    BRACKET_ENV_CHECKOUT = (
        "      - name: Load assets\n"
        "        uses: actions/checkout@abc\n"
        "        env:\n"
        "          WORKFLOWS_REF: ${{ inputs.workflows_ref }}\n"
        "        with:\n"
        "          ref: ${{ env[\'WORKFLOWS_REF\'] }}\n"
    )
    # 3. One alias into another. The scan followed exactly one hop, so `REF`
    # never registered and `ref: ${{ env.REF }}` read as no ref use at all.
    ALIAS_CHAIN_CHECKOUT = (
        "      - name: Load assets\n"
        "        uses: actions/checkout@abc\n"
        "        env:\n"
        "          BASE: ${{ inputs.workflows_ref }}\n"
        "          REF: ${{ env.BASE }}\n"
        "        with:\n"
        "          ref: ${{ env.REF }}\n"
    )

    def test_a_flow_form_env_alias_is_not_an_escape_hatch(self):
        self.assertEqual(len(self._jobs(self.FLOW_ENV_CHECKOUT)), 1)

    def test_a_guarded_flow_form_env_alias_checkout_passes(self):
        self.assertEqual(self._jobs(self.GUARD + self.FLOW_ENV_CHECKOUT), [])

    def test_a_flow_form_env_binds_the_alias(self):
        self.assertEqual(
            cwp.env_aliases(self._wrap(self.FLOW_ENV_CHECKOUT).split("\n")),
            frozenset({"WORKFLOWS_REF"}),
        )

    def test_a_bracket_input_checkout_is_not_an_escape_hatch(self):
        self.assertEqual(len(self._jobs(self.BRACKET_INPUT_CHECKOUT)), 1)

    def test_a_guarded_bracket_input_checkout_passes(self):
        self.assertEqual(self._jobs(self.GUARD + self.BRACKET_INPUT_CHECKOUT), [])

    def test_a_bracket_env_alias_checkout_is_not_an_escape_hatch(self):
        self.assertEqual(len(self._jobs(self.BRACKET_ENV_CHECKOUT)), 1)

    def test_a_guarded_bracket_env_alias_checkout_passes(self):
        self.assertEqual(self._jobs(self.GUARD + self.BRACKET_ENV_CHECKOUT), [])

    def test_an_env_alias_chain_is_followed_to_the_input(self):
        self.assertEqual(len(self._jobs(self.ALIAS_CHAIN_CHECKOUT)), 1)

    def test_a_guarded_env_alias_chain_checkout_passes(self):
        self.assertEqual(self._jobs(self.GUARD + self.ALIAS_CHAIN_CHECKOUT), [])

    def test_an_env_alias_chain_binds_every_name_on_it(self):
        # BOTH names, not just the far end: a `ref:` may reach in at either hop.
        self.assertEqual(
            cwp.env_aliases(self._wrap(self.ALIAS_CHAIN_CHECKOUT).split("\n")),
            frozenset({"BASE", "REF"}),
        )

    def test_a_bracket_spelled_flow_env_binds_through_the_chain(self):
        # All three widenings at once — the flow form, index access on both
        # `inputs` and `env`, and a hop between two aliases.
        steps = (
            "      - name: Load assets\n"
            "        uses: actions/checkout@abc\n"
            "        env: {BASE: \"${{ inputs[\'workflows_ref\'] }}\", REF: \"${{ env[\'BASE\'] }}\"}\n"
            "        with:\n"
            "          ref: ${{ env[\'REF\'] }}\n"
        )
        self.assertEqual(
            cwp.env_aliases(self._wrap(steps).split("\n")), frozenset({"BASE", "REF"})
        )
        self.assertEqual(len(self._jobs(steps)), 1)
        self.assertEqual(self._jobs(self.GUARD + steps), [])

    # ── …and the boundaries those three widenings must not cross. ──

    def test_whitespace_inside_the_index_brackets_is_the_same_access(self):
        # Actions allows whitespace around an index expression, so this is the
        # same access as `inputs['workflows_ref']` — and a near-miss that read
        # as no ref use at all is the exact failure mode BE-8146 is about.
        steps = (
            "      - name: Load assets\n"
            "        uses: actions/checkout@abc\n"
            "        env:\n"
            "          WORKFLOWS_REF: ${{ inputs[ \'workflows_ref\' ] }}\n"
            "        with:\n"
            "          ref: ${{ env[ \'WORKFLOWS_REF\' ] }}\n"
        )
        self.assertEqual(
            cwp.env_aliases(self._wrap(steps).split("\n")), frozenset({"WORKFLOWS_REF"})
        )
        self.assertEqual(len(self._jobs(steps)), 1)
        self.assertEqual(self._jobs(self.GUARD + steps), [])

    def test_a_flow_env_binding_an_unrelated_name_binds_no_alias(self):
        # Demanding a guard for an unrelated variable fails a compliant
        # workflow — the one cost of over-approximating, so it stays bounded.
        lines = self._wrap(
            "      - name: x\n"
            "        env: {GROOM_ASSETS: _groom_assets, RUNNER: ubuntu}\n"
            "        run: echo hi\n"
        ).split("\n")
        self.assertEqual(cwp.env_aliases(lines), frozenset())

    def test_a_quoted_decoy_in_a_flow_env_is_not_its_own_entry(self):
        # The entry boundary has to be real YAML punctuation: this `,` is
        # string content, so `EVIL` is not a second entry and binds no alias
        # of its own. `SAFE` still binds, though — Actions interpolates
        # `${{ … }}` wherever it sits inside a string scalar, quoted or not,
        # so SAFE's actual value DOES embed the resolved input at runtime,
        # and demanding a guard for it is the safe direction. Same quote
        # tracking the flow self-pin matcher's `_outside_quotes` applies.
        lines = self._wrap(
            "      - name: x\n"
            "        env: {SAFE: \'a, EVIL: ${{ inputs.workflows_ref }}\'}\n"
            "        run: echo hi\n"
        ).split("\n")
        self.assertEqual(cwp.env_aliases(lines), frozenset({"SAFE"}))

    def test_a_wrapped_flow_env_mapping_still_binds(self):
        # A flow mapping is not required to close on the line that opens it.
        # `_ENV_KEY_RE` used to reject a line ending in `{` (so the block-body
        # walk never ran) while the single-line flow scan found no entries on
        # a key line that was just `env: {` — the same silent fail-open one
        # newline from the shape already closed.
        lines = self._wrap(
            "      - name: x\n"
            "        env: {\n"
            "          WORKFLOWS_REF: \"${{ inputs.workflows_ref }}\"\n"
            "        }\n"
            "        run: echo hi\n"
        ).split("\n")
        self.assertEqual(cwp.env_aliases(lines), frozenset({"WORKFLOWS_REF"}))
        checkout = (
            "      - name: Load assets\n"
            "        uses: actions/checkout@abc\n"
            "        env: {\n"
            "          WORKFLOWS_REF: \"${{ inputs.workflows_ref }}\"\n"
            "        }\n"
            "        with:\n"
            "          ref: ${{ env.WORKFLOWS_REF }}\n"
        )
        self.assertEqual(len(self._jobs(checkout)), 1)
        self.assertEqual(self._jobs(self.GUARD + checkout), [])

    def test_a_quoted_comment_marker_in_an_earlier_flow_entry_does_not_truncate(self):
        # `_strip_comment` used to run on the WHOLE flow line before any
        # per-entry scan, and its regex was not quote-aware, so a `#` inside
        # an EARLIER entry's own quoted value truncated every entry after it
        # — a blast radius the block form (one entry per line) never had.
        lines = self._wrap(
            "      - name: x\n"
            '        env: {MSG: "a # b", WORKFLOWS_REF: "${{ inputs.workflows_ref }}"}\n'
            "        run: echo hi\n"
        ).split("\n")
        self.assertEqual(cwp.env_aliases(lines), frozenset({"WORKFLOWS_REF"}))

    def test_an_interpolation_preceding_the_mention_does_not_defeat_the_bind(self):
        # The old `[^,}]*` value bound stopped at the FIRST `,` or `}`, so any
        # nested expression call (`format('{0}', …)`, braces of its own) or a
        # second interpolation ahead of the mention (two `${{ … }}` in one
        # quoted value) kept the bind from ever reaching it. The structural
        # walk tracks quotes and `{}` depth together instead, so a `,`/`}`
        # inside the quoted value is not a boundary at all.
        for value in (
            "\"${{ format('{0}', inputs.workflows_ref) }}\"",
            '"${{ inputs.dir }}/${{ inputs.workflows_ref }}"',
        ):
            lines = self._wrap(
                "      - name: x\n"
                "        env: {REF: %s}\n"
                "        run: echo hi\n" % value
            ).split("\n")
            self.assertEqual(cwp.env_aliases(lines), frozenset({"REF"}), value)

    def test_a_hyphenated_env_key_binds_through_the_bracket_spelling(self):
        # `env.WORKFLOWS-REF` is not valid property-access syntax, so a
        # hyphenated name can ONLY be read back through the bracket form —
        # which means the alias scan has to accept a hyphen in the NAME it
        # binds, not just in the reference reading it back.
        lines = self._wrap(
            "      - name: x\n"
            "        env:\n"
            "          WORKFLOWS-REF: ${{ inputs.workflows_ref }}\n"
            "        run: echo hi\n"
        ).split("\n")
        self.assertEqual(cwp.env_aliases(lines), frozenset({"WORKFLOWS-REF"}))
        checkout = (
            "      - name: Load assets\n"
            "        uses: actions/checkout@abc\n"
            "        env:\n"
            "          WORKFLOWS-REF: ${{ inputs.workflows_ref }}\n"
            "        with:\n"
            "          ref: ${{ env['WORKFLOWS-REF'] }}\n"
        )
        self.assertEqual(len(self._jobs(checkout)), 1)
        self.assertEqual(self._jobs(self.GUARD + checkout), [])

    def test_an_env_alias_value_on_the_next_line_still_binds(self):
        # The block alias scan required the mention on the KEY's own line,
        # unlike `ref:` (`_REF_KEY_OPEN_RE`) or the `_CONSUMES_*` backstop,
        # which both already follow a value onto the line below.
        lines = self._wrap(
            "      - name: x\n"
            "        env:\n"
            "          WORKFLOWS_REF: >-\n"
            "            ${{ inputs.workflows_ref }}\n"
            "        run: echo hi\n"
        ).split("\n")
        self.assertEqual(cwp.env_aliases(lines), frozenset({"WORKFLOWS_REF"}))

    def test_a_list_marker_env_key_still_binds(self):
        # A step's FIRST key carries the `- ` list marker on its own line
        # (`- env:` / `- env: {…}`), which sits where `_ENV_KEY_RE` and
        # `_ENV_FLOW_KEY_RE` expected `env` itself — so a step written this
        # way bound no alias at all, block or flow.
        block = self._wrap(
            "      - env:\n"
            "          WORKFLOWS_REF: ${{ inputs.workflows_ref }}\n"
            "        run: echo hi\n"
        ).split("\n")
        self.assertEqual(cwp.env_aliases(block), frozenset({"WORKFLOWS_REF"}))
        flow = self._wrap(
            "      - env: {WORKFLOWS_REF: \"${{ inputs.workflows_ref }}\"}\n"
            "        run: echo hi\n"
        ).split("\n")
        self.assertEqual(cwp.env_aliases(flow), frozenset({"WORKFLOWS_REF"}))

    def test_whitespace_before_the_input_bracket_is_the_same_access(self):
        # The index alternative required `[` adjacent to `inputs`, but Actions
        # tolerates whitespace between tokens generally — `inputs ['x']` is
        # the same access as the tight spelling.
        checkout = (
            "      - name: Load assets\n"
            "        uses: actions/checkout@abc\n"
            "        with:\n"
            "          ref: ${{ inputs [\'workflows_ref\'] }}\n"
        )
        self.assertEqual(len(self._jobs(checkout)), 1)
        self.assertEqual(self._jobs(self.GUARD + checkout), [])

    def test_the_yaml_escaped_bracket_spelling_is_the_same_access(self):
        # A single-quoted YAML value escapes its inner `'` by doubling it, so
        # `ref: '${{ inputs[''workflows_ref''] }}'` — the whole value forced
        # single-quoted — decodes to the exact same bracket access as the
        # unescaped spelling and must read the same way.
        checkout = (
            "      - name: Load assets\n"
            "        uses: actions/checkout@abc\n"
            "        with:\n"
            "          ref: '${{ inputs[''workflows_ref''] }}'\n"
        )
        self.assertEqual(len(self._jobs(checkout)), 1)
        self.assertEqual(self._jobs(self.GUARD + checkout), [])

    def test_an_env_chain_not_rooted_at_the_input_is_not_a_ref_use(self):
        # A two-hop chain off a DIFFERENT input reaches nothing this lint
        # covers. The fixpoint must not treat "chained" as "reaching".
        steps = (
            "      - name: Load assets\n"
            "        uses: actions/checkout@abc\n"
            "        env:\n"
            "          BASE: ${{ inputs.some_other }}\n"
            "          REF: ${{ env.BASE }}\n"
            "        with:\n"
            "          ref: ${{ env.REF }}\n"
        )
        self.assertEqual(cwp.env_aliases(self._wrap(steps).split("\n")), frozenset())
        self.assertEqual(self._jobs(steps), [])

    def test_an_unrooted_cyclic_env_chain_terminates_and_binds_nothing(self):
        # Two names defined in terms of each other and neither rooted at the
        # input. Since neither mentions the input at all, `env_aliases` returns
        # at its `if not names` guard before the fixpoint loop runs — a cheaper
        # way to bind nothing than exercising the loop, and covered on its own
        # by `test_an_env_chain_not_rooted_at_the_input_is_not_a_ref_use`.
        lines = self._wrap(
            "      - name: x\n"
            "        env:\n"
            "          A: ${{ env.B }}\n"
            "          B: ${{ env.A }}\n"
            "        run: echo hi\n"
        ).split("\n")
        self.assertEqual(cwp.env_aliases(lines), frozenset())

    def test_a_cyclic_env_chain_rooted_at_the_input_terminates(self):
        # A cycle that DOES reach the input on its first hop, so the fixpoint
        # loop actually runs: `A` binds directly, then each pass tries to grow
        # from `env.<name already known>` — `B` reaches via `env.A`, `C` via
        # `env.B`, and a further pass finds nothing new (`A` is already bound,
        # and `B`'s own `env.C` closes the cycle without adding a name), so it
        # converges instead of spinning.
        lines = self._wrap(
            "      - name: x\n"
            "        env:\n"
            "          A: ${{ inputs.workflows_ref }}\n"
            "          B: ${{ env.A }}${{ env.C }}\n"
            "          C: ${{ env.B }}\n"
            "        run: echo hi\n"
        ).split("\n")
        self.assertEqual(cwp.env_aliases(lines), frozenset({"A", "B", "C"}))

    def test_the_guard_side_is_NOT_widened_to_the_flow_form(self):
        # Deliberately asymmetric: widening a ref USE can only DEMAND a guard,
        # but widening GUARD recognition would EXCUSE a checkout. A flow-form
        # guard binding therefore still reads as ABSENT — which fails closed,
        # reporting the checkout it precedes rather than passing it.
        flow_guard = (
            "      - name: Require a pinned workflows_ref\n"
            "        env: {WORKFLOWS_REF: \"${{ inputs.workflows_ref }}\"}\n"
            "        run: |\n"
            '          if [ -z "$WORKFLOWS_REF" ]; then\n'
            "            exit 1\n"
            "          fi\n"
        )
        self.assertEqual(len(self._jobs(flow_guard + self.CHECKOUT)), 1)

    def test_bracket_access_NEVER_earns_the_self_pin_exemption(self):
        # `_FALLBACK_RES` is an EXEMPTION, so widening it is the unsafe
        # direction and it stays literal/dot-form. The bracket spelling is a
        # ref use (it reaches the input) but not a recognized self-pin, so it
        # gets the plain "no empty-ref guard" report — loud, not excused.
        checkout = (
            "      - name: Load assets\n"
            "        uses: actions/checkout@abc\n"
            "        with:\n"
            "          ref: ${{ inputs[\'workflows_ref\'] || job.workflow_sha }}\n"
        )
        self.assertFalse(
            cwp._pins_to_job_workflow_sha("          ref: ${{ inputs[\'workflows_ref\'] || job.workflow_sha }}")
        )
        self.assertEqual(
            cwp.unguarded_ref_checkouts(self._wrap(checkout).split("\n")),
            [(11, False, False)],
        )

    def test_prose_and_sed_mentions_of_either_spelling_are_not_a_use(self):
        # The backstop\'s whole point is telling "not applicable" apart from "I
        # could not read this", so it stays anchored to the WHOLE value in both
        # accessor spellings — the test workflow\'s own shell fixtures name the
        # input in prose and in a `sed` script, and neither is a use.
        self.assertFalse(
            cwp._consumes_input(
                "# the workflow is checked out at inputs[\'workflows_ref\'] by the caller\n"
                "jobs:\n"
                "  j:\n"
                "    steps:\n"
                "      - run: sed -i \"s/inputs[\'workflows_ref\']/x/\" f.yml\n"
                "      - run: echo \"reads inputs.workflows_ref, but only in prose\"\n"
            )
        )

    def test_the_fallback_must_be_the_WHOLE_ref_value(self):
        # Anchoring `${{` … `}}` bounds the INTERPOLATION, not the YAML value.
        # Each of these still scored as a self-pin — earning the weaker
        # fallback-guard requirement and the `default: ''` carve-out while
        # resolving to a mutable ref, or to a sibling entry's value.
        for ref in (
            "refs/heads/${{ inputs.workflows_ref || job.workflow_sha }}",
            "${{ inputs.override }}${{ inputs.workflows_ref || job.workflow_sha }}",
        ):
            checkout = (
                "      - name: Load assets\n"
                "        uses: actions/checkout@abc\n"
                "        with:\n"
                "          ref: %s\n" % ref
            )
            lines = self._wrap(checkout).split("\n")
            self.assertFalse(any(fb for _, fb, _, _ in cwp.ref_checkouts(lines)), ref)
            self.assertEqual(len(self._jobs(checkout)), 1, ref)

    def test_a_flow_sibling_carrying_the_fallback_is_not_the_ref(self):
        # `_REF_USE_FLOW_RE` bounds its value at the entry boundary so a sibling
        # cannot be misread as the ref; the self-pin matcher needs the same
        # boundary, or the sibling's fallback exempts a bare `ref:`.
        checkout = (
            "      - name: Load assets\n"
            "        uses: actions/checkout@abc\n"
            "        with: {ref: '${{ inputs.workflows_ref }}', "
            "x: '${{ inputs.workflows_ref || job.workflow_sha }}'}\n"
        )
        lines = self._wrap(checkout).split("\n")
        self.assertFalse(any(fb for _, fb, _, _ in cwp.ref_checkouts(lines)))
        self.assertEqual(len(self._jobs(checkout)), 1)

    def test_a_block_scalar_self_pin_is_seen_by_the_carve_out_scan(self):
        # The per-line scan could not see this spelling: the key line carries no
        # expression and the continuation line carries no `ref:` key, so no
        # single line satisfied both tests. The file self-pins, but lost the
        # carve-out and got BE-5546's "delete the default" while its checkouts
        # got BE-8077's "the fallback IS recognized" — contradictory advice.
        checkout = (
            "      - name: Load assets\n"
            "        uses: actions/checkout@abc\n"
            "        with:\n"
            "          ref: >-\n"
            "            ${{ inputs.workflows_ref || job.workflow_sha }}\n"
        )
        lines = self._wrap(checkout).split("\n")
        self.assertTrue(any(fb for _, fb, _, _ in cwp.ref_checkouts(lines)))

    def test_the_github_job_workflow_sha_spelling_is_no_longer_exempt(self):
        # BE-8077: `github.job_workflow_sha` is an OIDC token claim, NOT a
        # `github` context property, so Actions expands it to '' and checkout
        # reads `ref: ''` as this repo's default branch. The old regex blessed
        # exactly that. It must now be flagged like any other unguarded
        # checkout, so the mistake cannot be reintroduced with the lint green.
        checkout = (
            "      - name: Load assets\n"
            "        uses: actions/checkout@abc\n"
            "        with:\n"
            "          ref: ${{ inputs.workflows_ref || github.job_workflow_sha }}\n"
        )
        self.assertEqual(len(self._jobs(checkout)), 1)

    def test_a_plain_fallback_to_a_branch_is_still_unguarded(self):
        # Only the LITERAL `job.workflow_sha` fallback is exempt — a fallback to
        # anything else (here, a floating branch) is the same `default: main`
        # hole wearing a different hat and must still trip.
        checkout = (
            "      - name: Load assets\n"
            "        uses: actions/checkout@abc\n"
            "        with:\n"
            "          ref: ${{ inputs.workflows_ref || 'main' }}\n"
        )
        self.assertEqual(len(self._jobs(checkout)), 1)

    def test_a_length_and_charset_guard_counts(self):
        # pr-risk.yml's shape: no `-z` anywhere, but `${#VAR} -ne 40` alone
        # already rejects an empty ref (length 0 != 40) regardless of what it
        # is OR'd with — an OR only ever widens the set of rejected values.
        guard = (
            "      - name: Enforce workflows_ref pin contract\n"
            "        env:\n"
            "          WORKFLOWS_REF: ${{ inputs.workflows_ref }}\n"
            "        run: |\n"
            "          if [[ ${#WORKFLOWS_REF} -ne 40 || \"$WORKFLOWS_REF\" == *[!0-9a-f]* ]]; then\n"
            "            exit 1\n"
            "          fi\n"
        )
        self.assertEqual(self._jobs(guard + self.CHECKOUT), [])

    def test_a_charset_only_condition_is_not_a_length_guard(self):
        # The OR-widening exception is for a branch that ALONE guarantees
        # rejection of empty. A charset-only test (no length check at all)
        # never does — an empty string trivially has no disallowed characters
        # — so this must still trip.
        guard = (
            "      - name: Bad guard\n"
            "        env:\n"
            "          WORKFLOWS_REF: ${{ inputs.workflows_ref }}\n"
            "        run: |\n"
            "          if [[ \"$WORKFLOWS_REF\" == *[!0-9a-f]* ]]; then\n"
            "            exit 1\n"
            "          fi\n"
        )
        self.assertEqual(len(self._jobs(guard + self.CHECKOUT)), 1)

    def test_a_marker_line_continue_on_error_disqualifies_the_guard(self):
        # `- continue-on-error: true` declares the key at the step's key column
        # via the list marker, where the disqualifier scan used to miss it — a
        # step that never fails scored as a hard guard while its `exit 1`
        # guards nothing, and the checkout ran anyway (BE-8221).
        never_fail = self.GUARD.replace(
            "      - name: Require a pinned workflows_ref\n",
            "      - continue-on-error: true\n"
            "        name: Require a pinned workflows_ref\n",
            1,
        )
        self.assertNotEqual(never_fail, self.GUARD, "fixture drifted")
        self.assertEqual(len(self._jobs(never_fail + self.CHECKOUT)), 1)

    def test_a_marker_line_if_disqualifies_the_guard(self):
        # `- if: …` can skip the guard outright for some events while the
        # checkout still runs — same marker-line blind spot as above.
        conditional = self.GUARD.replace(
            "      - name: Require a pinned workflows_ref\n",
            "      - if: github.event_name == 'push'\n"
            "        name: Require a pinned workflows_ref\n",
            1,
        )
        self.assertNotEqual(conditional, self.GUARD, "fixture drifted")
        self.assertEqual(len(self._jobs(conditional + self.CHECKOUT)), 1)

    def test_a_marker_line_one_line_guard_still_counts(self):
        # The one-line guard written marker-first: normalizing the marker line
        # for the DISQUALIFIER scan alone left the guard-detection scan reading
        # the raw `- run: …`, whose `- ` defeats the run-prefix strip — a real
        # guard missed, and its checkout reported with the remedy the author
        # already has.
        one_liner = (
            '      - run: [ -z "$WORKFLOWS_REF" ] && exit 1\n'
            "        env:\n"
            "          WORKFLOWS_REF: ${{ inputs.workflows_ref }}\n"
        )
        self.assertEqual(self._jobs(one_liner + self.CHECKOUT), [])

    def test_a_matrix_include_item_is_not_a_guard_step(self):
        # A `strategy.matrix.include` entry is a list item that can carry the
        # binding's exact shape plus a guard-shaped script — it is DATA, no
        # shell ever runs it. Resolving it to step bounds scores it as a hard
        # guard and blesses every later unguarded checkout in the job.
        text = self._wrap(self.CHECKOUT).replace(
            "    runs-on: ubuntu-latest\n",
            "    runs-on: ubuntu-latest\n"
            "    strategy:\n"
            "      matrix:\n"
            "        include:\n"
            "          - name: probe\n"
            "            WORKFLOWS_REF: ${{ inputs.workflows_ref }}\n"
            "            script: |\n"
            '              if [ -z "$WORKFLOWS_REF" ]; then\n'
            "                exit 1\n"
            "              fi\n",
        )
        self.assertEqual(len(cwp.find_unguarded_ref_checkouts(text.split("\n"))), 1)

    def test_guard_shaped_text_in_a_run_scalar_is_not_a_guard(self):
        # Step-shaped YAML inside a `run: |` block scalar — a heredoc writing a
        # workflow file — is text, not a step. Its `- …` line must not resolve
        # to step bounds and credit the "guard" it quotes.
        heredoc = (
            "      - name: Write a workflow fixture\n"
            "        run: |\n"
            "          cat > wf.yml <<'EOF'\n"
            "          - name: guard\n"
            "            env:\n"
            "              WORKFLOWS_REF: ${{ inputs.workflows_ref }}\n"
            '            run: [ -z "$WORKFLOWS_REF" ] && exit 1\n'
            "          EOF\n"
        )
        self.assertEqual(len(self._jobs(heredoc + self.CHECKOUT)), 1)

    def test_a_marker_line_env_block_still_binds_an_alias(self):
        # `- env:` written marker-first must feed alias discovery too — it is
        # the one path deciding whether a later `ref: ${{ env.<name> }}` is
        # SEEN as a ref use at all, so missing the spelling is a silent pass,
        # not a loud one.
        steps = (
            "      - env:\n"
            "          ASSET_REF: ${{ inputs.workflows_ref }}\n"
            "        run: echo ok\n"
            "      - name: Load assets\n"
            "        uses: actions/checkout@abc\n"
            "        with:\n"
            "          ref: ${{ env.ASSET_REF }}\n"
        )
        self.assertEqual(len(self._jobs(steps)), 1)

    def test_a_marker_line_env_block_binds_only_its_own_members(self):
        # The block's boundary is the KEY's column: the step's other keys sit
        # one level shallower than the env members, and reading them as block
        # members would bind `ref` itself as an alias — making `env.ref` and
        # `$ref` anywhere read as the input.
        steps = (
            "      - env:\n"
            "          ASSET_REF: ${{ inputs.workflows_ref }}\n"
            "        uses: actions/checkout@abc\n"
            "        with:\n"
            "          ref: ${{ inputs.workflows_ref }}\n"
        )
        lines = self._wrap(steps).split("\n")
        self.assertEqual(cwp.env_aliases(lines), frozenset({"ASSET_REF"}))

    def test_a_dash_with_only_trailing_whitespace_declares_no_key(self):
        # `- ` followed by nothing is the bare marker wearing trailing
        # whitespace: no key sits on it, so there is no marker width to
        # recover — the contract `_marker_width` documents for the bare `-`.
        self.assertEqual(cwp._marker_width("      - "), 0)
        self.assertEqual(cwp._marker_width("      -"), 0)

    # ------------------------------------------------------------------
    # The resolve-then-consume shape (BE-8130). A job that must NEVER fail
    # cannot run the fail-closed guard — an `exit 1` would fail it — so it
    # resolves the ref in a warn-only step and the checkout consumes that
    # step's OUTPUT. The `ref:` then names no input, which is how this whole
    # family of checkouts fell out of the lint: nothing but a hand-written
    # `if:` stood between an unresolvable ref and a silent default-branch
    # checkout of the scripts the job EXECUTES.
    # ------------------------------------------------------------------

    RESOLVER = (
        "      - name: Resolve the asset ref\n"
        "        id: resolve_ref\n"
        "        continue-on-error: true\n"
        "        env:\n"
        "          WORKFLOWS_REF: ${{ inputs.workflows_ref || job.workflow_sha }}\n"
        "        run: |\n"
        '          REF="$WORKFLOWS_REF"\n'
        '          if [ -z "$REF" ]; then\n'
        '            echo "::warning::could not resolve a usable ref"\n'
        "          fi\n"
        '          echo "ref=$REF" >> "$GITHUB_OUTPUT"\n'
    )
    # The same resolver WITHOUT `continue-on-error:`, rejecting the empty value
    # itself. It IS a real guard — and since BE-8221 that buys its consumers
    # nothing: the guard proves the step rejects an empty INPUT, not that the
    # value it writes to $GITHUB_OUTPUT is non-empty. Consumers still need
    # their own exact `if:`.
    HARD_RESOLVER = RESOLVER.replace("        continue-on-error: true\n", "").replace(
        '            echo "::warning::could not resolve a usable ref"\n',
        "            exit 1\n",
    )
    # A step that produces an output from something entirely unrelated to the
    # input. Nothing here is this lint's business.
    UNRELATED_RESOLVER = (
        "      - name: Resolve the tool version\n"
        "        id: resolve_ref\n"
        "        run: |\n"
        '          echo "ref=$(git rev-parse HEAD)" >> "$GITHUB_OUTPUT"\n'
    )

    @staticmethod
    def _step_output_checkout(extra_keys="", ref=None):
        return (
            "      - name: Load assets\n"
            + extra_keys
            + "        uses: actions/checkout@abc\n"
            "        with:\n"
            "          ref: %s\n" % (ref or "${{ steps.resolve_ref.outputs.ref }}")
        )

    EXACT_IF = "        if: steps.resolve_ref.outputs.ref != ''\n"

    def test_a_step_output_checkout_with_the_exact_if_passes(self):
        steps = self.RESOLVER + self._step_output_checkout(self.EXACT_IF)
        self.assertEqual(self._jobs(steps), [])
        # …and it IS covered, rather than passing by being invisible — the
        # regression this ticket exists to close.
        sites = cwp.ref_checkouts(self._wrap(steps).split("\n"))
        self.assertEqual(
            [(fb, guarded, via) for _, fb, guarded, via in sites],
            [(False, True, True)],
            "the checkout must be a COVERED site, not an invisible one",
        )

    def test_a_step_output_checkout_with_no_if_is_reported(self):
        self.assertEqual(len(self._jobs(self.RESOLVER + self._step_output_checkout())), 1)

    def test_an_or_widened_if_does_not_cover_a_step_output_checkout(self):
        # `... != '' || always()` runs the checkout PRECISELY when the output is
        # empty. It reads like a superset of the right condition and is the
        # exact inverse of one.
        widened = "        if: steps.resolve_ref.outputs.ref != '' || always()\n"
        self.assertEqual(len(self._jobs(self.RESOLVER + self._step_output_checkout(widened))), 1)

    def test_an_if_on_a_different_output_does_not_cover_the_checkout(self):
        other = "        if: steps.resolve_ref.outputs.status != ''\n"
        self.assertEqual(len(self._jobs(self.RESOLVER + self._step_output_checkout(other))), 1)

    def test_a_wrapped_if_expression_passes(self):
        wrapped = "        if: ${{ steps.resolve_ref.outputs.ref != '' }}\n"
        self.assertEqual(self._jobs(self.RESOLVER + self._step_output_checkout(wrapped)), [])

    def test_a_quoted_if_scalar_passes(self):
        # `if: "…"` is the same condition wearing YAML quotes. Failing it would
        # fail a consumer that has the remedy already applied.
        quoted = '        if: "steps.resolve_ref.outputs.ref != \'\'"\n'
        self.assertEqual(self._jobs(self.RESOLVER + self._step_output_checkout(quoted)), [])

    def test_a_quoted_wrapped_if_scalar_passes(self):
        # The two spellings compose: a `${{ … }}` wrapper inside the quotes.
        quoted = '        if: "${{ steps.resolve_ref.outputs.ref != \'\' }}"\n'
        self.assertEqual(self._jobs(self.RESOLVER + self._step_output_checkout(quoted)), [])

    def test_unwrapping_quotes_does_not_widen_the_match(self):
        # The strip removes exactly one leading and one trailing `"`, and the
        # comparison after it is still character-exact — an OR-widened
        # condition stays refused when it is written as a quoted scalar.
        widened = '        if: "steps.resolve_ref.outputs.ref != \'\' || always()"\n'
        self.assertEqual(len(self._jobs(self.RESOLVER + self._step_output_checkout(widened))), 1)

    def test_the_id_on_the_list_marker_line_registers_the_resolver(self):
        # `_binding_step_id` normalizes the marker so `- id: …` is read at the
        # step's key column. Without it the resolver registers under no id, and
        # the consuming checkout drops out of coverage — silently, since an
        # unresolvable `<id>` is treated as "not this lint's subject".
        marker_resolver = self.RESOLVER.replace(
            "      - name: Resolve the asset ref\n        id: resolve_ref\n",
            "      - id: resolve_ref\n        name: Resolve the asset ref\n",
            1,
        )
        self.assertNotEqual(marker_resolver, self.RESOLVER, "fixture drifted")
        sites = cwp.ref_checkouts(
            self._wrap(marker_resolver + self._step_output_checkout(self.EXACT_IF)).split("\n")
        )
        self.assertEqual(
            [(fb, guarded, via) for _, fb, guarded, via in sites], [(False, True, True)]
        )

    def test_the_if_on_the_list_marker_line_is_seen(self):
        # `- if: …` puts the condition at the step's key column via the list
        # marker. Missing it reports a correctly guarded checkout as unguarded —
        # `_binding_step_id` already normalizes the same marker to read an `id:`.
        marker = (
            "      - if: steps.resolve_ref.outputs.ref != ''\n"
            "        uses: actions/checkout@abc\n"
            "        with:\n"
            "          ref: ${{ steps.resolve_ref.outputs.ref }}\n"
        )
        self.assertEqual(self._jobs(self.RESOLVER + marker), [])

    def test_a_tightly_spaced_if_passes(self):
        # `a!=''` is valid Actions, and failing it would fail the very remedy
        # the lint asks for.
        tight = "        if: steps.resolve_ref.outputs.ref!=''\n"
        self.assertEqual(self._jobs(self.RESOLVER + self._step_output_checkout(tight)), [])

    def test_normalizing_the_spacing_does_not_widen_the_match(self):
        # Spacing is normalized; nothing else is. An OR-widened condition and an
        # `if:` naming a different output stay refused however they are spaced.
        for cond in (
            "        if: steps.resolve_ref.outputs.ref!=''||always()\n",
            "        if: steps.resolve_ref.outputs.status!=''\n",
        ):
            with self.subTest(cond=cond.strip()):
                self.assertEqual(
                    len(self._jobs(self.RESOLVER + self._step_output_checkout(cond))), 1
                )

    def test_a_job_level_if_does_not_cover_a_step_output_checkout(self):
        # A job-level `if:` skips the RESOLVER too, so it can never tell an
        # empty output from a populated one. Only the consuming step's own
        # `if:` can.
        text = self._wrap(self.RESOLVER + self._step_output_checkout()).replace(
            "  job0:\n    runs-on: ubuntu-latest\n",
            "  job0:\n    if: steps.resolve_ref.outputs.ref != ''\n    runs-on: ubuntu-latest\n",
        )
        self.assertEqual(len(cwp.find_unguarded_ref_checkouts(text.split("\n"))), 1)

    def test_a_hard_guard_resolver_does_not_exempt_its_consumer(self):
        # Flipped by BE-8221. The resolver's guard proves it rejects an empty
        # INPUT — nothing about the value it writes to $GITHUB_OUTPUT: a
        # sanitize-to-'' branch after the guard, a dropped or renamed output
        # write, or a consumer naming an output the step never sets all still
        # hand checkout ''. Coverage comes from the consumer's own exact `if:`,
        # full stop.
        self.assertEqual(len(self._jobs(self.HARD_RESOLVER + self._step_output_checkout())), 1)

    def test_a_marker_line_continue_on_error_resolver_is_not_a_guard_step(self):
        # The resolver spelling of the marker-line blind spot. Redundant as a
        # coverage test now the resolver exemption is gone (BE-8221), so it
        # pins `is_guard_step` DIRECTLY: `- continue-on-error: true` as the
        # step's first key must disqualify it exactly as the key does on a
        # line of its own.
        marker_resolver = self.HARD_RESOLVER.replace(
            "      - name: Resolve the asset ref\n",
            "      - continue-on-error: true\n"
            "        name: Resolve the asset ref\n",
            1,
        )
        self.assertNotEqual(marker_resolver, self.HARD_RESOLVER, "fixture drifted")
        lines = self._wrap(marker_resolver + self._step_output_checkout()).split("\n")
        binding = next(i for i, l in enumerate(lines) if "WORKFLOWS_REF:" in l)
        self.assertFalse(cwp.is_guard_step(lines, binding))

    # The same marker-first spelling at YAML's OTHER legal separation width.
    # A `- ` marker may be followed by any run of spaces, and the step's keys
    # then align at dash+3 rather than dash+2. Reading the marker as exactly
    # two columns leaves the rewritten key one column deep, where the
    # `_indent(line) != key_indent` scan skips it again — the same hole, open
    # to anyone who types one extra space.
    WIDE_MARKER_RESOLVER = (
        "      -  continue-on-error: true\n"
        "         name: Resolve the asset ref\n"
        "         id: resolve_ref\n"
        "         env:\n"
        "           WORKFLOWS_REF: ${{ inputs.workflows_ref || job.workflow_sha }}\n"
        "         run: |\n"
        '           REF="$WORKFLOWS_REF"\n'
        '           if [ -z "$REF" ]; then\n'
        "             exit 1\n"
        "           fi\n"
        '           echo "ref=$REF" >> "$GITHUB_OUTPUT"\n'
    )

    def test_a_wide_marker_continue_on_error_step_is_not_a_guard_step(self):
        lines = self._wrap(self.WIDE_MARKER_RESOLVER + self._step_output_checkout()).split("\n")
        binding = next(i for i, l in enumerate(lines) if "WORKFLOWS_REF:" in l)
        self.assertFalse(cwp.is_guard_step(lines, binding))

    def test_a_wide_marker_step_still_registers_its_id(self):
        # The marker rewrite feeds `_binding_step_id` too: at dash+3 the id
        # must still be read at the step's real key column, or the step is not
        # registered as a resolver and its consumer drops out of coverage
        # entirely — a silent pass, not a loud one.
        wide = self.WIDE_MARKER_RESOLVER.replace(
            "      -  continue-on-error: true\n         name: Resolve the asset ref\n",
            "      -  id: resolve_ref\n         name: Resolve the asset ref\n",
        ).replace("         id: resolve_ref\n", "", 1)
        self.assertNotIn("continue-on-error", wide)
        lines = self._wrap(wide + self._step_output_checkout()).split("\n")
        binding = next(i for i, l in enumerate(lines) if "WORKFLOWS_REF:" in l)
        self.assertEqual(cwp._binding_step_id(lines, binding), "resolve_ref")
        self.assertEqual(len(cwp.find_unguarded_ref_checkouts(lines)), 1)

    # The remedy itself, written marker-first. With the resolver exemption gone
    # this `if:` is the ONLY route to coverage, so failing to see it reports a
    # checkout that is in fact guarded — and tells the author to add the very
    # line they already have.
    MARKER_IF_CHECKOUT = (
        "      - if: steps.resolve_ref.outputs.ref != ''\n"
        "        name: Load assets\n"
        "        uses: actions/checkout@abc\n"
        "        with:\n"
        "          ref: ${{ steps.resolve_ref.outputs.ref }}\n"
    )

    def test_a_marker_line_if_covers_a_step_output_checkout(self):
        self.assertEqual(self._jobs(self.RESOLVER + self.MARKER_IF_CHECKOUT), [])

    def test_a_marker_line_if_is_still_matched_exactly(self):
        # Normalizing the marker line must not relax WHAT is matched there: an
        # OR-widened condition stays refused in the marker-first spelling too.
        widened = self.MARKER_IF_CHECKOUT.replace(
            "!= ''\n", "!= '' || always()\n"
        )
        self.assertEqual(len(self._jobs(self.RESOLVER + widened)), 1)

    # A resolver whose FIRST key is the binding block itself. `_step_bounds`
    # used to read the step's key column off the marker's physical indent,
    # find no `- ` line shallower than it, and answer None — so the id was
    # never registered and the consumer's `ref: ${{ steps.<id>.outputs.ref }}`
    # passed the lint unreported. Fails OPEN, unlike the guard path.
    MARKER_ENV_RESOLVER = (
        "      - env:\n"
        "          WORKFLOWS_REF: ${{ inputs.workflows_ref || job.workflow_sha }}\n"
        "        id: resolve_ref\n"
        "        continue-on-error: true\n"
        "        run: |\n"
        '          REF="$WORKFLOWS_REF"\n'
        '          echo "ref=$REF" >> "$GITHUB_OUTPUT"\n'
    )

    # The third marker spelling: a bare `-` with every key on the lines below
    # it. No key sits on the marker, so nothing needs normalizing — but the
    # step-start scan has to recognize it as opening a step at all.
    BARE_MARKER_RESOLVER = "      -\n" + RESOLVER.replace("      - name:", "        name:", 1)

    def test_a_bare_marker_resolver_is_registered(self):
        self.assertIn("      -\n        name:", self.BARE_MARKER_RESOLVER)
        lines = self._wrap(self.BARE_MARKER_RESOLVER + self._step_output_checkout()).split("\n")
        binding = next(i for i, l in enumerate(lines) if "WORKFLOWS_REF:" in l)
        self.assertEqual(cwp._binding_step_id(lines, binding), "resolve_ref")
        self.assertEqual(len(cwp.find_unguarded_ref_checkouts(lines)), 1)

    def test_a_bare_marker_consumer_is_covered_by_its_if(self):
        consumer = "      -\n" + self._step_output_checkout(self.EXACT_IF).replace(
            "      - name:", "        name:", 1
        )
        self.assertEqual(self._jobs(self.RESOLVER + consumer), [])

    def test_a_marker_line_env_resolver_is_registered(self):
        lines = self._wrap(self.MARKER_ENV_RESOLVER + self._step_output_checkout()).split("\n")
        binding = next(i for i, l in enumerate(lines) if "WORKFLOWS_REF:" in l)
        self.assertEqual(cwp._binding_step_id(lines, binding), "resolve_ref")
        self.assertEqual(len(cwp.find_unguarded_ref_checkouts(lines)), 1)

    def test_a_marker_line_env_resolver_with_the_exact_if_passes(self):
        self.assertEqual(
            self._jobs(self.MARKER_ENV_RESOLVER + self._step_output_checkout(self.EXACT_IF)),
            [],
        )

    # The binding in FLOW form. While a fail-closed resolver could stand in
    # for the consumer's `if:`, reading this as absent failed loudly; with
    # that exemption gone, absence means the step is never registered and its
    # consumer is never judged at all — a silent pass.
    FLOW_ENV_RESOLVER = (
        "      - name: Resolve the asset ref\n"
        "        id: resolve_ref\n"
        "        continue-on-error: true\n"
        '        env: {WORKFLOWS_REF: "${{ inputs.workflows_ref || job.workflow_sha }}"}\n'
        "        run: |\n"
        '          echo "ref=$WORKFLOWS_REF" >> "$GITHUB_OUTPUT"\n'
    )
    # …and flow form ON the marker line — both marker-first spellings of the
    # same registration path.
    MARKER_FLOW_ENV_RESOLVER = (
        '      - env: {WORKFLOWS_REF: "${{ inputs.workflows_ref || job.workflow_sha }}"}\n'
        "        id: resolve_ref\n"
        "        continue-on-error: true\n"
        "        run: |\n"
        '          echo "ref=$WORKFLOWS_REF" >> "$GITHUB_OUTPUT"\n'
    )

    def test_a_flow_env_resolver_is_registered(self):
        lines = self._wrap(self.FLOW_ENV_RESOLVER + self._step_output_checkout()).split("\n")
        binding = next(i for i, l in enumerate(lines) if "WORKFLOWS_REF:" in l)
        self.assertEqual(cwp._binding_step_id(lines, binding), "resolve_ref")
        self.assertEqual(len(cwp.find_unguarded_ref_checkouts(lines)), 1)

    def test_a_flow_env_resolver_with_the_exact_if_passes(self):
        self.assertEqual(
            self._jobs(self.FLOW_ENV_RESOLVER + self._step_output_checkout(self.EXACT_IF)), []
        )

    def test_a_marker_line_flow_env_resolver_is_registered(self):
        lines = self._wrap(
            self.MARKER_FLOW_ENV_RESOLVER + self._step_output_checkout()
        ).split("\n")
        binding = next(i for i, l in enumerate(lines) if "WORKFLOWS_REF:" in l)
        self.assertEqual(cwp._binding_step_id(lines, binding), "resolve_ref")
        self.assertEqual(len(cwp.find_unguarded_ref_checkouts(lines)), 1)

    # The consumer whose FIRST key is the flow `with:`, written on the marker.
    MARKER_FLOW_CONSUMER = (
        '      - with: {ref: "${{ steps.resolve_ref.outputs.ref }}"}\n'
        "        uses: actions/checkout@abc\n"
    )

    def test_a_marker_first_flow_consumer_is_not_credited_with_a_neighbours_if(self):
        # The never-fail idiom's first checkout carries the exact `if:`; a
        # second checkout written marker-first must not inherit it. The old
        # shifted-retry path stopped its backward scan at the PREVIOUS step's
        # marker and answered with bounds covering that step — whose `if:` is
        # exactly the shape this consumer lacks.
        steps = (
            self.RESOLVER
            + self._step_output_checkout(self.EXACT_IF)
            + self.MARKER_FLOW_CONSUMER
        )
        self.assertEqual(len(self._jobs(steps)), 1)

    def test_a_marker_first_flow_consumer_with_its_own_if_passes(self):
        covered = self.MARKER_FLOW_CONSUMER.replace(
            "        uses: actions/checkout@abc\n",
            self.EXACT_IF + "        uses: actions/checkout@abc\n",
        )
        self.assertEqual(self._jobs(self.RESOLVER + covered), [])

    def test_a_hard_guard_resolver_with_the_exact_if_passes(self):
        # The sound composition still works: the consumer's own `if:` covers
        # the checkout regardless of what the resolver does or does not guard.
        self.assertEqual(
            self._jobs(self.HARD_RESOLVER + self._step_output_checkout(self.EXACT_IF)), []
        )

    # A resolver that guards the INPUT and then sanitizes a malformed ref to
    # '' — the exact shape cursor-review.yml's `resolve_ref` step uses. The
    # guard is real, and the output can still be empty.
    SANITIZING_RESOLVER = HARD_RESOLVER.replace(
        '          echo "ref=$REF" >> "$GITHUB_OUTPUT"\n',
        "          case \"$REF\" in *[!A-Za-z0-9._/@+-]*) REF='' ;; esac\n"
        '          echo "ref=$REF" >> "$GITHUB_OUTPUT"\n',
    )

    def test_a_sanitizing_resolver_does_not_exempt_its_consumer(self):
        self.assertNotEqual(self.SANITIZING_RESOLVER, self.HARD_RESOLVER, "fixture drifted")
        # The step IS a hard guard — which is exactly why the old exemption
        # would have covered this consumer while the sanitize branch emits ''.
        lines = self._wrap(self.SANITIZING_RESOLVER + self._step_output_checkout()).split("\n")
        binding = next(i for i, l in enumerate(lines) if "WORKFLOWS_REF:" in l)
        self.assertTrue(cwp.is_guard_step(lines, binding))
        self.assertEqual(len(cwp.find_unguarded_ref_checkouts(lines)), 1)

    # A hard-guard resolver whose $GITHUB_OUTPUT write was dropped: the
    # consumer's `steps.<id>.outputs.<name>` is then guaranteed ''.
    NO_OUTPUT_RESOLVER = HARD_RESOLVER.replace(
        '          echo "ref=$REF" >> "$GITHUB_OUTPUT"\n', ""
    )

    def test_a_resolver_that_never_writes_the_output_does_not_exempt(self):
        self.assertNotEqual(self.NO_OUTPUT_RESOLVER, self.HARD_RESOLVER, "fixture drifted")
        self.assertEqual(
            len(self._jobs(self.NO_OUTPUT_RESOLVER + self._step_output_checkout())), 1
        )

    def test_a_resolver_in_another_job_is_not_reachable(self):
        # Jobs run independently: `steps.resolve_ref` in job B names job B's
        # step, not job A's — and job B declares no step under that id, so the
        # output is '' at runtime and the checkout takes the default branch
        # unconditionally. REPORTED, deliberately (BE-8215): this used to be
        # silently dropped as "not this lint's subject", which is fail-open on
        # exactly the runtime behavior the lint exists to close.
        sites = cwp.ref_checkouts(
            self._wrap(self.RESOLVER, self._step_output_checkout()).split("\n")
        )
        self.assertEqual(
            [(fb, guarded, via) for _, fb, guarded, via in sites],
            [(False, False, "dangling")],
        )

    def test_a_resolver_below_its_consumer_is_never_credited(self):
        # Declared after the checkout it would cover, so it cannot have run
        # first: at runtime the output is guaranteed '' and the checkout takes
        # the DEFAULT BRANCH — the exact `if:` on it tests the same dangling
        # output, so it cannot save the site either. Reported as the ONE
        # unguarded dangling site (BE-8215); the per-job step-id pre-scan is
        # what tells this apart from the legitimately out-of-scope case (an
        # EARLIER step that exists and never touches `workflows_ref`).
        sites = cwp.ref_checkouts(
            self._wrap(self._step_output_checkout(self.EXACT_IF) + self.RESOLVER).split("\n")
        )
        self.assertEqual(
            [(fb, guarded, via) for _, fb, guarded, via in sites],
            [(False, False, "dangling")],
        )

    def test_a_typoed_step_id_is_reported_dangling(self):
        # The resolver is present and correct — the checkout just misspells
        # its id, so the expression is '' at runtime and checkout takes the
        # default branch. The old early return dropped this silently.
        steps = self.RESOLVER + self._step_output_checkout(
            ref="${{ steps.resolve_reff.outputs.ref }}"
        )
        sites = cwp.ref_checkouts(self._wrap(steps).split("\n"))
        self.assertEqual(
            [(fb, guarded, via) for _, fb, guarded, via in sites],
            [(False, False, "dangling")],
        )
        # `'dangling'` is truthy on purpose — the unguarded projection needs
        # no change to report it.
        self.assertEqual(len(self._jobs(steps)), 1)

    def test_a_dangling_step_id_gets_its_own_error_text(self):
        # The BE-8130 remedies (add the exact `if:`, make the resolver
        # fail-closed) do not fix a nonexistent id — the dangling case needs
        # its own message naming the id as the thing to fix.
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        text = _reusable(PINNED).replace(
            "    steps:\n      - run: echo hi\n",
            "    steps:\n"
            + self.RESOLVER
            + self._step_output_checkout(ref="${{ steps.resolve_reff.outputs.ref }}"),
        )
        with open(os.path.join(tmp, "w.yml"), "w", encoding="utf-8") as f:
            f.write(text)
        errors, _, _, _ = cwp.check_dir(tmp, exempt=frozenset())
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("BE-8215", errors[0])
        self.assertIn("DEFAULT BRANCH", errors[0])
        self.assertIn("Fix the step id", errors[0])
        for be8130_remedy in ("BE-8130", "non-empty", "fail-closed", "never-fail"):
            self.assertNotIn(be8130_remedy, errors[0])

    def test_a_steps_own_id_does_not_excuse_its_own_ref(self):
        # The checkout step's own `id:` is the referenced one — but during the
        # step's `with:` evaluation its output does not exist yet, so the
        # expression is '' at runtime. The pre-scan boundary is the consuming
        # STEP's first line, not the `ref:` line, precisely so this id cannot
        # count as "declared before".
        steps = (
            "      - name: Load assets\n"
            "        id: resolve_ref\n"
            "        uses: actions/checkout@abc\n"
            "        with:\n"
            "          ref: ${{ steps.resolve_ref.outputs.ref }}\n"
        )
        sites = cwp.ref_checkouts(self._wrap(steps).split("\n"))
        self.assertEqual(
            [(fb, guarded, via) for _, fb, guarded, via in sites],
            [(False, False, "dangling")],
        )

    def test_a_flow_style_resolver_consumer_is_not_reported_dangling(self):
        # `- {id: resolve_ref, …}` is the flow spelling of a step id. A
        # flow-form guard BINDING deliberately reads as absent, so this step
        # can never be credited as a resolver — but the id EXISTS, and the
        # pre-scan must see it or a compliant workflow's consumer would be a
        # false dangling FAILURE rather than the old out-of-scope drop.
        flow_resolver = (
            "      - {id: resolve_ref, run: echo hi}\n"
        )
        sites = cwp.ref_checkouts(
            self._wrap(flow_resolver + self._step_output_checkout()).split("\n")
        )
        self.assertEqual(sites, [])

    # ------------------------------------------------------------------
    # One PHYSICAL line, two questions (BE-9098). A step written as a single
    # flow mapping can bind `WORKFLOWS_REF` in its `env:` AND read a step
    # output in its `with: {ref: …}`. The binding arm used to be terminal for
    # that line, so the checkout was never judged at all: no verdict, no
    # drop, and — because nothing was dropped — no BE-9045 `::warning`
    # either. The block `env:` binding cannot reach here (`_GUARD_BINDING_RE`
    # is line-anchored, so it can never share a line with `ref:`).
    # ------------------------------------------------------------------

    FLOW_BINDING_AND_REF = (
        "      - {uses: actions/checkout@abc,"
        " env: {WORKFLOWS_REF: \"${{ inputs.workflows_ref }}\"},"
        ' with: {ref: "${{ steps.%s.outputs.ref }}"}}\n'
    )

    def test_a_flow_step_with_binding_and_step_output_ref_is_judged(self):
        # The typo'd id names no step declared before this one, so the
        # expression is '' at runtime and checkout takes the DEFAULT BRANCH —
        # the same verdict the identical line minus the `env:` binding has
        # always earned.
        steps = self.FLOW_BINDING_AND_REF % "resolve_reff"
        lines = self._wrap(steps).split("\n")
        lineno = next(i for i, row in enumerate(lines, 1) if "steps.resolve_reff" in row)
        self.assertEqual(
            cwp.unguarded_ref_checkouts(lines), [(lineno, False, "dangling")]
        )
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        text = _reusable(PINNED).replace(
            "    steps:\n      - run: echo hi\n", "    steps:\n" + steps
        )
        with open(os.path.join(tmp, "w.yml"), "w", encoding="utf-8") as f:
            f.write(text)
        errors, _, _, _ = cwp.check_dir(tmp, exempt=frozenset())
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("steps.resolve_reff", errors[0])
        self.assertIn("BE-8215", errors[0])

    def test_a_flow_step_binding_does_not_swallow_a_covered_consumer(self):
        # A REAL earlier resolver, so the site is judged on its merits rather
        # than as a dangling id: `via_step_output` is `True`, which is what
        # proves the binding arm no longer swallows the line.
        #
        # It is nonetheless reported UNGUARDED, and deliberately so: the `if:`
        # rides INSIDE the flow mapping, and `_skips_on_empty_output` reads
        # only a step-level `if:` key of its own — bare, quoted and
        # `${{ … }}`-wrapped alike, all three checked. That is the same
        # fail-closed stance the flow-form BINDING takes one arm up: a flow
        # spelling may DEMAND a guard, never excuse one. And the remedy is one
        # line rather than a rewrite — lifting the `if:` out to its own key IS
        # coverage with `env:`/`with:` left as flow maps, asserted below so
        # the report never hardens into "this shape simply cannot pass".
        resolver = (
            "      - id: resolve_ref\n"
            "        env:\n"
            "          WORKFLOWS_REF: ${{ inputs.workflows_ref }}\n"
            '        run: echo "ref=x" >> "$GITHUB_OUTPUT"\n'
        )
        consumer = (self.FLOW_BINDING_AND_REF % "resolve_ref").replace(
            "{uses: actions/checkout@abc,",
            "{uses: actions/checkout@abc,"
            " if: \"${{ steps.resolve_ref.outputs.ref != '' }}\",",
        )
        lines = self._wrap(resolver + consumer).split("\n")
        lineno = next(i for i, row in enumerate(lines, 1) if "with: {ref:" in row)
        self.assertEqual(cwp.ref_checkouts(lines), [(lineno, False, False, True)])
        # The remedy, spelled out: same step, `if:` promoted to its own key.
        covered = (
            "      - if: steps.resolve_ref.outputs.ref != ''\n"
            "        uses: actions/checkout@abc\n"
            '        env: {WORKFLOWS_REF: "${{ inputs.workflows_ref }}"}\n'
            '        with: {ref: "${{ steps.resolve_ref.outputs.ref }}"}\n'
        )
        self.assertEqual(
            cwp.unguarded_ref_checkouts(self._wrap(resolver + covered).split("\n")), []
        )

    def test_a_self_referencing_flow_step_is_dangling(self):
        # The step's own `id:` is the referenced one, but during its `with:`
        # evaluation that output does not exist yet, so the expression is ''
        # at runtime and checkout takes the DEFAULT BRANCH.
        #
        # What holds the verdict is ORDER, not the reach of `_binding_step_id`
        # (BE-9099): the site is recorded BEFORE this line registers its own
        # id, so it is judged on `step_ids`, whose arm compares the declaring
        # line against the CONSUMING step's first line — and a step's own id
        # never precedes itself. The `resolvers` arm has no such ordering
        # check, so registering first would route this down it and judge the
        # step on an `if:` instead.
        steps = (self.FLOW_BINDING_AND_REF % "resolve_ref").replace(
            "{uses: actions/checkout@abc,", "{id: resolve_ref, uses: actions/checkout@abc,"
        )
        lines = self._wrap(steps).split("\n")
        self.assertEqual(
            [(fb, guarded, via) for _, fb, guarded, via in cwp.ref_checkouts(lines)],
            [(False, False, "dangling")],
        )

    def test_a_flow_form_resolver_registers_and_its_consumer_is_judged(self):
        # The RESOLVER side of the same hole (BE-9099). `_GUARD_BINDING_FLOW_RE`
        # already read this binding, but the block `id:` pattern cannot match a
        # line opening `{`, so the step registered no id — and an unregistered
        # resolver fails SILENTLY: the consumer's `ref:` reads as "a real
        # earlier step this lint has no claim on" and is dropped with no
        # verdict, switching the BE-8130/BE-8221 requirement off for it.
        resolver = (
            '      - {id: r, env: {WORKFLOWS_REF: "${{ inputs.workflows_ref }}"},'
            ' run: echo "ref=x" >> "$GITHUB_OUTPUT"}\n'
        )
        consumer = (
            "      - uses: actions/checkout@abc\n"
            "        with:\n"
            "          ref: ${{ steps.r.outputs.ref }}\n"
        )
        lines = self._wrap(resolver + consumer).split("\n")
        lineno = next(i for i, row in enumerate(lines, 1) if "steps.r.outputs" in row)
        # Judged, and judged UNGUARDED — the consumer carries no `if:`. The
        # block-form resolver has always earned exactly this verdict.
        self.assertEqual(cwp.ref_checkouts(lines), [(lineno, False, False, True)])
        # ...and the one accepted remedy clears it, so the flow spelling is not
        # a shape that simply cannot pass.
        covered = "      - if: steps.r.outputs.ref != ''\n" + consumer.replace(
            "      - uses:", "        uses:"
        )
        self.assertEqual(
            cwp.unguarded_ref_checkouts(self._wrap(resolver + covered).split("\n")), []
        )

    def test_a_flow_resolver_spanning_two_lines_registers_its_real_id(self):
        # The same BE-9099 drop, one spelling over: a flow mapping broken
        # across physical lines leaves the `}` on `_STEP_ID_RE`'s `\S+`, so
        # `_binding_step_id` registered `r}` while the pre-scan read `r`. An
        # id no consumer can name registers nothing usable, and the consumer
        # is dropped with no verdict — silently, exactly as an unregistered
        # resolver is. Both readers now narrow through `_step_id_value`.
        resolver = (
            '      - {env: {WORKFLOWS_REF: "${{ inputs.workflows_ref }}"},\n'
            "        id: r}\n"
        )
        consumer = (
            "      - uses: actions/checkout@abc\n"
            "        with:\n"
            "          ref: ${{ steps.r.outputs.ref }}\n"
        )
        lines = self._wrap(resolver + consumer).split("\n")
        lineno = next(i for i, row in enumerate(lines, 1) if "steps.r.outputs" in row)
        self.assertEqual(cwp.ref_checkouts(lines), [(lineno, False, False, True)])

    def test_a_flow_with_id_input_does_not_register_a_phantom_resolver(self):
        # The over-collection hazard the BE-9099 widening had to dodge: read on
        # a BLOCK key line the flow `id:` pattern also matches `with: {id: x}`,
        # where `id` is an action INPUT and no step is declared at all. That
        # would register a phantom resolver and judge its "consumer" on an
        # `if:` — a false failure — instead of the dangling verdict the absent
        # step earns. Gated to lines where a flow mapping actually OPENS.
        steps = (
            "      - name: Not a resolver\n"
            "        uses: org/act@abc\n"
            "        env:\n"
            "          WORKFLOWS_REF: ${{ inputs.workflows_ref }}\n"
            "        with: {id: phantom}\n"
            "      - uses: actions/checkout@abc\n"
            "        with:\n"
            "          ref: ${{ steps.phantom.outputs.ref }}\n"
        )
        lines = self._wrap(steps).split("\n")
        lineno = next(i for i, row in enumerate(lines, 1) if "steps.phantom" in row)
        self.assertEqual(
            cwp.ref_checkouts(lines), [(lineno, False, False, "dangling")]
        )

    # ------------------------------------------------------------------
    # The pre-scan reads step ITEMS, not lines (BE-8254). Anything shaped
    # like an `id:` key used to register as a declared step id at any nesting
    # depth, and a phantom id silences the dangling verdict outright: the
    # consumer resolves to an "earlier step out of scope" that does not exist.
    # ------------------------------------------------------------------

    WITH_INPUT_NAMED_ID = (
        "      - name: Run a tool\n"
        "        uses: some/action@abc\n"
        "        with:\n"
        "          id: resolve_ref\n"
    )
    # A `run:` block scalar emitting fixture YAML — the shape `cursor-review.yml`
    # and `groom.yml` write heavily. The emitted line is not workflow structure
    # at all, but it is indented like it and it starts with `id:`.
    HEREDOC_STEP_ID = (
        "      - name: Write a fixture workflow\n"
        "        run: |\n"
        "          cat <<'EOF' > fixture.yml\n"
        "          steps:\n"
        "            - id: resolve_ref\n"
        "              run: echo hi\n"
        "          EOF\n"
    )
    HEREDOC_QUOTED_STEP_ID = HEREDOC_STEP_ID.replace(
        "            - id: resolve_ref\n", '            "id": resolve_ref\n'
    )

    def _dangling_states(self, steps):
        return [
            (fb, guarded, via)
            for _, fb, guarded, via in cwp.ref_checkouts(self._wrap(steps).split("\n"))
        ]

    def test_an_action_input_named_id_declares_no_step(self):
        # `with:` members sit DEEPER than the step's own keys, so reading the
        # id at the item's key column excludes this structurally. Registering
        # it turned the consumer below into a silent pass.
        steps = self.WITH_INPUT_NAMED_ID + self._step_output_checkout()
        self.assertEqual(self._dangling_states(steps), [(False, False, "dangling")])
        self.assertEqual(len(self._jobs(steps)), 1)

    def test_a_heredoc_emitted_step_id_declares_no_step(self):
        # YAML puts a block scalar's content deeper than the key that opens
        # it, so script TEXT can never land on the item's key column — and the
        # scalar is skipped outright besides.
        steps = self.HEREDOC_STEP_ID + self._step_output_checkout()
        self.assertEqual(self._dangling_states(steps), [(False, False, "dangling")])

    def test_a_heredoc_emitted_quoted_step_id_declares_no_step(self):
        # The quoted spelling `"id": x` matches the same block pattern.
        self.assertIn('"id": resolve_ref', self.HEREDOC_QUOTED_STEP_ID)
        steps = self.HEREDOC_QUOTED_STEP_ID + self._step_output_checkout()
        self.assertEqual(self._dangling_states(steps), [(False, False, "dangling")])

    def test_a_real_id_beside_a_phantom_one_is_still_registered(self):
        # The narrowing must not cost the ids that ARE declared: the same job
        # carries a phantom `with:` input AND a real resolver, and the
        # consumer of the real one stays the old out-of-scope drop.
        steps = (
            self.WITH_INPUT_NAMED_ID + self.RESOLVER + self._step_output_checkout(self.EXACT_IF)
        )
        self.assertEqual(self._dangling_states(steps), [(False, True, True)])

    def test_a_marker_line_id_is_read_at_the_items_key_column(self):
        # `- id: x` declares the id at the step's key column, where `_indent`
        # reads the MARKER's column instead — so the marker-line spelling is
        # normalized back into view exactly as `_binding_step_id` does. The
        # step binds nothing, so it is never registered as a resolver and the
        # pre-scan is the ONLY thing standing between a compliant workflow and
        # a false dangling failure. (`WIDE_MARKER_RESOLVER` cannot pin this:
        # it binds the input, so `resolvers` answers before the pre-scan is
        # ever consulted.)
        for marker in ("      - id: resolve_ref\n", "      -  id: resolve_ref\n"):
            unrelated = marker + " " * (len(marker) - len(marker.lstrip(" ")) + 2) + "run: echo hi\n"
            sites = cwp.ref_checkouts(
                self._wrap(unrelated + self._step_output_checkout()).split("\n")
            )
            self.assertEqual(sites, [], unrelated)

    def test_a_multi_line_flow_step_still_registers_its_id(self):
        # `- {uses: x,` / `id: y}` — a flow mapping spanning physical lines.
        # The id sits on a CONTINUATION line, at no particular column, so it
        # is read by tracking the unbalanced `{` the marker line opened. An id
        # missed here is a compliant workflow reported as a false failure.
        multi_line_flow = (
            "      - {uses: some/action@abc,\n"
            "         id: resolve_ref}\n"
        )
        sites = cwp.ref_checkouts(
            self._wrap(multi_line_flow + self._step_output_checkout()).split("\n")
        )
        self.assertEqual(sites, [])

    def test_a_flow_input_named_id_declares_no_step_in_either_spelling(self):
        # The flow pattern is read only where a flow mapping actually OPENS —
        # a marker line whose value begins `{`. Read on every marker line it
        # also matches `- with: {id: x}`, where `id` is an action INPUT; read
        # on every line it matches the member spelling below as well.
        member = "      - uses: some/action@abc\n        with: {id: resolve_ref}\n"
        marker = "      - with: {id: resolve_ref}\n        uses: some/action@abc\n"
        for step in (member, marker):
            steps = step + self._step_output_checkout()
            self.assertEqual(
                self._dangling_states(steps), [(False, False, "dangling")], step
            )

    # ------------------------------------------------------------------
    # Shapes the item walk must read the SAME as a line scan did (BE-8254
    # review). Every one of these is the costly direction: an id the pre-scan
    # misses turns a compliant workflow into a false `dangling` FAILURE, or
    # escapes the job outright and drops a real finding.
    # ------------------------------------------------------------------

    @staticmethod
    def _job0_step_ids(job_text):
        lines = ("name: F\non:\n  workflow_call:\njobs:\n" + job_text).split("\n")
        start = next(i for i, line in enumerate(lines) if line.startswith("  job0:"))
        return cwp._job_step_ids(lines, start, 2)

    @staticmethod
    def _job0(steps):
        return "  job0:\n    runs-on: ubuntu-latest\n    steps:\n" + steps

    def test_an_indentless_steps_sequence_is_walked_not_escaped(self):
        # YAML lets a block sequence sit at its KEY's own column — `steps:`
        # and `- uses: …` both at 4 — and Actions accepts it. Reading only the
        # strictly-deeper body finds no `- ` item there, so the walk escapes a
        # job it can read perfectly well and silently drops every dangling
        # verdict in it. This is neither of the two shapes the escape is for.
        job = self._job0(
            "    - uses: actions/checkout@abc\n"
            "      id: checkout\n"
            "    - id: resolve_ref\n"
            "      run: echo hi\n"
        )
        self.assertEqual(set(self._job0_step_ids(job)), {"checkout", "resolve_ref"})

    def test_an_indentless_steps_sequence_still_reports_a_dangling_ref(self):
        # End to end, with a TYPO'D consumer: escaping this job would report
        # nothing at all.
        job = self._job0(
            "    - id: resolve_ref\n"
            "      run: echo hi\n"
            "    - uses: actions/checkout@abc\n"
            "      with:\n"
            '        ref: "${{ steps.resolve_reff.outputs.ref }}"\n'
        )
        lines = ("name: F\non:\n  workflow_call:\njobs:\n" + job).split("\n")
        self.assertEqual(
            [(fb, guarded, via) for _, fb, guarded, via in cwp.ref_checkouts(lines)],
            [(False, False, "dangling")],
        )

    def test_an_indentless_sequence_ends_at_its_siblings_column(self):
        # In the indentless style `steps:`'s SIBLING keys share the marker
        # column, so a non-item line there closes the sequence. Walking past
        # it reads a later job-level key's members as step ids — over-
        # collection, which silences the dangling verdict.
        job = self._job0(
            "    - id: resolve_ref\n"
            "      run: echo hi\n"
            "    env:\n"
            "      id: phantom\n"
        )
        self.assertEqual(set(self._job0_step_ids(job)), {"resolve_ref"})

    def test_a_brace_inside_a_quoted_scalar_does_not_wedge_flow_mode_open(self):
        # `text.count("{")` counts braces that are string CONTENT. Left open,
        # flow mode reads every later line column-agnostically: the heredoc
        # below registers `phantom` (the exact shape this change removes) and
        # the real block item `- id: resolve_ref` matches neither pattern
        # there, so it is lost.
        job = self._job0(
            '      - {id: flow_step, run: "echo {"}\n'
            "      - id: resolve_ref\n"
            "        run: |\n"
            "          echo '- id: phantom'\n"
        )
        self.assertEqual(set(self._job0_step_ids(job)), {"flow_step", "resolve_ref"})

    def test_a_quoted_close_brace_does_not_end_flow_mode_early(self):
        # The mirror miscount: a `}` inside a quoted scalar closing the
        # mapping a character early drops the ids after it.
        job = self._job0(
            '      - {name: "}", id: flow_step,\n'
            "         uses: some/action@abc}\n"
        )
        self.assertEqual(set(self._job0_step_ids(job)), {"flow_step"})

    def test_a_quoted_brace_on_a_flow_CONTINUATION_line_is_counted_the_same(self):
        # The continuation update runs the same count, so it needs the same
        # quote-awareness: one `{` of string content here leaves the mapping
        # "open" past its real `}`, and the next block item's `- id: later`
        # matches neither pattern the flow branch applies, so it is lost and
        # its consumer becomes a false `dangling` failure.
        job = self._job0(
            "      - {uses: some/action@abc,\n"
            '         run: "echo {",\n'
            "         id: resolve_ref}\n"
            "      - id: later\n"
            "        run: echo hi\n"
        )
        self.assertEqual(set(self._job0_step_ids(job)), {"resolve_ref", "later"})

    def test_an_unbalanced_flow_mapping_cannot_leak_into_the_next_job(self):
        # Quote-awareness narrows the miscount but cannot remove it, so the
        # walk's own bound stays load-bearing: an open `flow_depth` suppresses
        # the dedent break only DOWN TO the job, never past it.
        job = self._job0('      - {id: resolve_ref, run: echo "{"\n') + (
            "  job1:\n    runs-on: ubuntu-latest\n    steps:\n"
            "      - name: A step of somebody else's job\n"
            "        id: other_job_step\n"
        )
        self.assertEqual(set(self._job0_step_ids(job)), {"resolve_ref"})

    def test_a_comment_only_marker_line_leaves_the_key_column_to_the_members(self):
        # `-   # set up` declares no key: YAML puts that item's keys on the
        # lines below at the usual marker width. Measuring the marker on the
        # RAW line locks `key_column` to the comment's column instead, and the
        # item's real `id:` is then never read.
        job = self._job0(
            "      -   # set up the ref\n"
            "        id: resolve_ref\n"
            "        run: echo hi\n"
        )
        self.assertEqual(set(self._job0_step_ids(job)), {"resolve_ref"})

    def test_a_bare_marker_whose_item_is_a_flow_mapping_registers_its_id(self):
        # A bare `-` whose item is written flow-style on the line BELOW it.
        # Read with the block pattern alone, a line opening `{` matches
        # nothing and the id is lost.
        job = self._job0("      -\n        {id: resolve_ref, run: echo hi}\n")
        self.assertEqual(set(self._job0_step_ids(job)), {"resolve_ref"})

    def test_a_dedented_flow_continuation_does_not_end_the_walk(self):
        # YAML constrains no indentation inside `{ … }`, so a continuation
        # line may sit BELOW the dash column. Breaking there ends the whole
        # pre-scan and loses every id after it.
        job = self._job0(
            "      - {uses: some/action@abc,\n"
            "    id: resolve_ref}\n"
            "      - id: later\n"
            "        run: echo hi\n"
        )
        self.assertEqual(set(self._job0_step_ids(job)), {"resolve_ref", "later"})

    def test_a_nested_flow_input_named_id_is_a_tolerated_residual(self):
        # PINNED, not fixed. `_STEP_ID_FLOW_RE` is a flat pattern with no
        # nesting awareness, so inside a flow mapping the `with: {id: x}`
        # spelling this change closes for the BLOCK form still registers.
        # Over-collection merely reproduces the pre-BE-8215 fail-open drop for
        # that one site; narrowing it wrong manufactures a false FAILURE. No
        # occurrence exists in this tree (0 of 69 jobs).
        job = self._job0("      - {uses: some/action@abc, with: {id: resolve_ref}}\n")
        self.assertEqual(set(self._job0_step_ids(job)), {"resolve_ref"})

    def test_a_comma_inside_a_quoted_scalar_declares_no_flow_step(self):
        # `_STEP_ID_FLOW_RE` asks only for a preceding `[{,]`, and a comma
        # that is string CONTENT meets it. Ungated, this registers `phantom`
        # and SILENCES the dangling verdict on a genuine finding — the same
        # phantom the block-form fixes above exclude, through another door.
        job = self._job0('      - {run: "build, id: phantom", uses: org/act@abc}\n')
        self.assertEqual(set(self._job0_step_ids(job)), set())

    def test_the_quoted_comma_decoy_is_refused_on_a_continuation_line(self):
        # The same decoy on the CONTINUATION line of a multi-line flow
        # mapping, which is collected by a second call site.
        job = self._job0(
            "      - {uses: org/act@abc,\n"
            '         name: "a, id: phantom"}\n'
        )
        self.assertEqual(set(self._job0_step_ids(job)), set())

    def test_the_quoted_comma_decoy_is_refused_after_a_bare_dash(self):
        # And on the third site: a bare `-` whose flow mapping opens on the
        # line below it.
        job = self._job0(
            "      -\n"
            '        {run: "x, id: phantom", uses: org/act@abc}\n'
        )
        self.assertEqual(set(self._job0_step_ids(job)), set())

    def test_the_quote_guard_never_drops_a_real_flow_id(self):
        # The guard NARROWS: a real `id:` is only ever reached across an
        # unquoted `{` or `,`, so every legitimate spelling — including one
        # sitting after a quoted scalar that itself contains a comma, and one
        # whose double-quoted neighbour carries an apostrophe — survives it.
        # Under-collection is the costly direction: it manufactures a false
        # `dangling` FAILURE on a compliant workflow.
        for steps, expected in (
            ("      - {id: real, uses: org/act@abc}\n", "real"),
            ('      - {name: "a, b", id: real, uses: org/act@abc}\n', "real"),
            ("      - {'id': real, uses: org/act@abc}\n", "real"),
            ('      - {name: "it\'s fine", id: real}\n', "real"),
            ("      - {uses: org/act@abc,\n         id: real}\n", "real"),
            ("      -\n        {id: real, run: echo hi}\n", "real"),
        ):
            with self.subTest(steps=steps):
                job = self._job0(steps)
                self.assertEqual(set(self._job0_step_ids(job)), {expected})

    def test_an_apostrophe_in_a_PLAIN_scalar_opens_no_quote(self):
        # YAML forbids a quote only as a plain scalar's FIRST character, so
        # `don't` is a legal plain scalar and this a legal step. Opening quote
        # state on it swallows the rest of the line: the real entry-separating
        # comma before `id:` reads as string CONTENT, `real` is never
        # registered — a false `dangling` FAILURE on a compliant workflow —
        # and the closing `}` goes uncounted, so `flow_depth` wedges open and
        # the later block item is lost too.
        job = self._job0(
            "      - {name: don't, id: real}\n"
            "      - id: later\n"
            "        run: echo hi\n"
        )
        self.assertEqual(set(self._job0_step_ids(job)), {"real", "later"})

    def test_a_plain_scalar_quote_does_not_wedge_the_brace_count(self):
        # The same rule at the OTHER site the miscount reaches: with the
        # apostrophe read as an opener the closing `}` sits "inside a string",
        # the delta comes back +1, and flow mode stays open for the rest of
        # the job.
        self.assertEqual(cwp._flow_brace_delta("- {name: don't, id: real}"), 0)
        self.assertEqual(cwp._flow_brace_delta("- {name: it's {x}, id: real}"), 0)

    def test_strip_comment_keeps_the_WEAKER_reading_on_purpose(self):
        # `_strip_comment` shares the scan but NOT the scalar-start rule, and
        # the split is deliberate: it is asked about every physical line,
        # `run: |` script bodies included, where `echo "PR #${n}"` is literal
        # text. Strictly that `"` follows the plain word `echo` and opens
        # nothing, so the ` #` would read as a comment and truncate a line
        # whose tail may carry the very `ref:` this lint exists to find.
        self.assertEqual(
            cwp._strip_comment('echo "Updated PR #${existing}"'),
            'echo "Updated PR #${existing}"',
        )
        # A `#` inside a genuinely quoted scalar is content under either rule.
        self.assertEqual(cwp._strip_comment('MSG: "a # b"'), 'MSG: "a # b"')
        # And a real trailing comment is still stripped.
        self.assertEqual(cwp._strip_comment("uses: org/act@abc  # v1"), "uses: org/act@abc")

    def test_the_strict_rule_is_still_what_the_structural_readers_use(self):
        # The split must not quietly revert the fix: the flow readers keep the
        # scalar-start rule, so a plain-scalar apostrophe opens nothing there.
        self.assertTrue(cwp._outside_quotes("{name: don't, id: real}", 12))
        self.assertEqual(cwp._flow_brace_delta("- {name: don't, id: real}"), 0)

    def test_a_QUOTED_step_id_value_survives_the_quote_refusal(self):
        # The refusal is on a STRAY quote — the tail of a scalar opened on an
        # earlier line — not on a legitimately quoted value. `id: 'real'` is
        # an ordinary spelling in both styles, and dropping it is the
        # false-`dangling` direction.
        for steps in (
            "      - {id: 'real', uses: org/act@abc}\n",
            '      - {id: "real", uses: org/act@abc}\n',
            "      - uses: org/act@abc\n        id: 'real'\n",
            '      - uses: org/act@abc\n        id: "real"\n',
            "      - {uses: org/act@abc,\n         id: 'real'}\n",
        ):
            with self.subTest(steps=steps):
                self.assertEqual(set(self._job0_step_ids(self._job0(steps))), {"real"})

    def test_the_scalar_start_rule_still_opens_every_real_quote(self):
        # The rule NARROWS, and the positions a quoted scalar really can start
        # at must all keep opening one — otherwise a decoy comma inside a
        # genuinely quoted scalar starts registering phantoms again.
        for text, pos_inside in (
            ('a: "x, y"', 5),        # after a `:` key separator
            ("- 'x, y'", 4),         # after a `- ` list marker
            ('{a: 1, b: "x, y"}', 12),  # after a `,` entry separator
            ('["x, y"]', 4),         # after a `[` flow-sequence indicator
            ('"x, y"', 3),           # at the start of the text
        ):
            with self.subTest(text=text):
                self.assertFalse(cwp._outside_quotes(text, pos_inside))

    def test_a_node_property_does_not_hide_a_flow_step(self):
        # `- &resolver {id: resolve_ref, …}` is valid YAML that Actions
        # accepts. With the gate testing for a `{` immediately after the
        # marker, the property defeats it and the item falls through to the
        # block pattern, which cannot match a line beginning `&resolver {` —
        # the id is lost and its consumer becomes a false `dangling` failure.
        for steps in (
            "      - &resolver {id: resolve_ref, run: echo hi}\n",
            "      - !!map {id: resolve_ref, run: echo hi}\n",
            "      - &resolver !!map {id: resolve_ref, run: echo hi}\n",
            "      -\n        &resolver {id: resolve_ref, run: echo hi}\n",
        ):
            with self.subTest(steps=steps):
                job = self._job0(steps + "      - id: later\n        run: echo hi\n")
                self.assertEqual(
                    set(self._job0_step_ids(job)), {"resolve_ref", "later"}
                )

    def test_a_node_property_does_not_widen_what_is_collected(self):
        # The skip is consulted ONLY to ask "does the content begin `{`", so
        # the marker-line narrowing it sits behind still holds: `- &a with:
        # {id: x}` declares an action INPUT named `id`, not a step.
        job = self._job0("      - &a with: {id: x}\n        uses: org/act@abc\n")
        self.assertEqual(set(self._job0_step_ids(job)), set())

    # ------------------------------------------------------------------
    # A quoted scalar spanning PHYSICAL LINES. Every reader here is per-line
    # (`_strip_comment` included, and it runs FIRST), so cross-line quote
    # state is out of scope module-wide rather than a gap in this walk — but
    # the two shapes that state would have gotten wrong are closed anyway,
    # from the value side, and the residue that survives is pinned so any
    # future change to it is a deliberate one.
    # ------------------------------------------------------------------

    def test_a_CROSS_LINE_quote_does_not_register_a_phantom(self):
        # `- {name: "a` / `  id: phantom", …}` — the continuation line looks
        # like a step id to a per-line reader, and registering it silences the
        # dangling verdict this whole change exists to restore. Refused from
        # the VALUE side instead of the quote-state side: no Actions step id
        # carries a `"`, so the tail of a scalar that opened above cannot pass
        # for one.
        job = self._job0(
            '      - {name: "a\n'
            '        id: phantom", run: echo hi}\n'
        )
        self.assertEqual(set(self._job0_step_ids(job)), set())

    def test_a_CROSS_LINE_quote_does_not_lose_a_real_id(self):
        # The mirror: the CLOSING quote of `- {name: "two` / `  lines", id:
        # real}` must not read as a fresh opener, which would hide the real
        # `id:` after it and make its consumer a false `dangling` failure.
        # A quote opens a scalar only where a NODE can start, and this one
        # follows the plain text `lines`.
        job = self._job0(
            '      - {name: "two\n'
            '        lines", id: real}\n'
        )
        self.assertEqual(set(self._job0_step_ids(job)), {"real"})

    def test_a_CROSS_LINE_quote_residue_is_pinned_not_claimed_closed(self):
        # What the value-side guard does NOT reach: a continuation line whose
        # id value carries no quote at all. This reader is still per-line, so
        # the shape over-collects — the TOLERATED direction (it silences one
        # site, exactly as a real earlier out-of-scope step does), and the one
        # a cross-line quote contract would have to close. 0 occurrences in
        # the tree.
        job = self._job0(
            '      - {name: "a\n'
            '        id: phantom, more": run}\n'
        )
        self.assertEqual(set(self._job0_step_ids(job)), {"phantom"})

    def test_a_quoted_comma_decoy_leaves_a_real_dangling_ref_loud(self):
        # End to end: the point of refusing the phantom is that the genuine
        # finding it was covering for is reported again.
        text = (
            "name: F\non:\n  workflow_call:\njobs:\n"
            + self._job0(
                '      - {run: "build, id: phantom", uses: org/act@abc}\n'
                "      - uses: actions/checkout@abc\n"
                "        with:\n"
                '          ref: "${{ steps.phantom.outputs.ref }}"\n'
            )
        )
        self.assertEqual(
            [(fb, guarded, via) for _, fb, guarded, via in cwp.ref_checkouts(text.split("\n"))],
            [(False, False, "dangling")],
        )

    # ------------------------------------------------------------------
    # The fail-loud escape: a `steps:` shape the item walk cannot read.
    # ------------------------------------------------------------------

    FLOW_STEPS_JOB = (
        "  job0:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps: [{id: resolve_ref, run: echo hi},"
        " {uses: actions/checkout@abc,"
        ' with: {ref: "${{ steps.resolve_reff.outputs.ref }}"}}]\n'
    )

    def test_a_fully_flow_steps_list_is_unknown_not_empty(self):
        # `steps: [ … ]` on the key line opens no item the walk can read, so
        # the pre-scan answers None — "unknown" — rather than an empty map
        # that would read as "this job declares no step at all".
        lines = ("name: F\non:\n  workflow_call:\njobs:\n" + self.FLOW_STEPS_JOB).split("\n")
        start = next(i for i, line in enumerate(lines) if line.startswith("  job0:"))
        self.assertIsNone(cwp._job_step_ids(lines, start, 2))

    def test_a_fully_flow_steps_list_yields_no_dangling_verdict(self):
        # …and the caller keeps the pre-BE-8215 fail-open drop for that job
        # rather than manufacturing a failure out of a pre-scan that could not
        # run. Note the id here is TYPO'D — with a working pre-scan this would
        # be dangling; unknown is not the same as absent.
        text = "name: F\non:\n  workflow_call:\njobs:\n" + self.FLOW_STEPS_JOB
        self.assertEqual(cwp.ref_checkouts(text.split("\n")), [])
        self.assertEqual(cwp.find_unguarded_ref_checkouts(text.split("\n")), [])

    def test_a_flow_steps_job_does_not_silence_its_siblings(self):
        # The escape is per JOB. A sibling job written normally is judged
        # exactly as before.
        sibling = "  job1:\n    runs-on: ubuntu-latest\n    steps:\n%s" % (
            self.RESOLVER + self._step_output_checkout(ref="${{ steps.resolve_reff.outputs.ref }}")
        )
        text = "name: F\non:\n  workflow_call:\njobs:\n" + self.FLOW_STEPS_JOB + sibling
        self.assertEqual(
            [(fb, guarded, via) for _, fb, guarded, via in cwp.ref_checkouts(text.split("\n"))],
            [(False, False, "dangling")],
        )

    def test_a_caller_job_with_no_steps_is_empty_not_unknown(self):
        # A reusable-workflow CALLER job (`uses:` + `with:`) declares no step
        # at all, so `steps.<id>.outputs.<out>` in its `with:` is `''` at
        # runtime exactly as a typo'd id is — the empty map, not "unknown".
        # Answering "unknown" here would drop a real finding on the one job
        # shape that cannot possibly have the step.
        text = (
            "name: F\non:\n  workflow_call:\njobs:\n"
            "  job0:\n"
            "    uses: org/repo/.github/workflows/w.yml@abc\n"
            "    with:\n"
            '      ref: "${{ steps.resolve_ref.outputs.ref }}"\n'
        )
        lines = text.split("\n")
        start = next(i for i, line in enumerate(lines) if line.startswith("  job0:"))
        self.assertEqual(cwp._job_step_ids(lines, start, 2), {})
        self.assertEqual(
            [(fb, guarded, via) for _, fb, guarded, via in cwp.ref_checkouts(lines)],
            [(False, False, "dangling")],
        )

    def test_a_steps_key_with_no_list_item_is_unknown(self):
        # `steps:` present but opening no `- ` item — a shape with no reading,
        # answered the same way.
        lines = (
            "name: F\non:\n  workflow_call:\njobs:\n"
            "  job0:\n    runs-on: ubuntu-latest\n    steps:\n      not-an-item: true\n"
        ).split("\n")
        start = next(i for i, line in enumerate(lines) if line.startswith("  job0:"))
        self.assertIsNone(cwp._job_step_ids(lines, start, 2))

    def test_a_trailing_or_fallback_after_a_covered_output_passes(self):
        # `${{ steps.resolve_ref.outputs.ref || 'main' }}` under the exact
        # `if:`: the checkout only runs when the output is non-empty, so the
        # `|| 'main'` arm is unreachable dead code. Before BE-8215 this
        # spelling matched nothing anywhere and recorded nothing.
        fallback = "${{ steps.resolve_ref.outputs.ref || 'main' }}"
        steps = self.RESOLVER + self._step_output_checkout(self.EXACT_IF, ref=fallback)
        self.assertEqual(self._jobs(steps), [])
        # …and it IS a covered site, not an invisible one.
        sites = cwp.ref_checkouts(self._wrap(steps).split("\n"))
        self.assertEqual(
            [(fb, guarded, via) for _, fb, guarded, via in sites],
            [(False, True, True)],
        )

    def test_a_trailing_or_fallback_without_the_if_is_reported(self):
        # Without the `if:` the fallback IS reachable — the empty case
        # resolves to a mutable branch instead of skipping the checkout.
        fallback = "${{ steps.resolve_ref.outputs.ref || 'main' }}"
        self.assertEqual(
            len(self._jobs(self.RESOLVER + self._step_output_checkout(ref=fallback))), 1
        )

    def test_a_leading_literal_operand_is_reported_even_with_the_if(self):
        # GitHub's `||` returns the first truthy operand, so `'main'` wins on
        # EVERY runner — the output is never consulted and the exact `if:`
        # guards a value the checkout does not use. Same rule as the input
        # side's `_leading_operand_reaches_input`.
        leading = "${{ 'main' || steps.resolve_ref.outputs.ref }}"
        self.assertEqual(
            len(self._jobs(self.RESOLVER + self._step_output_checkout(self.EXACT_IF, ref=leading))),
            1,
        )

    def test_the_or_spellings_behave_the_same_in_flow_form(self):
        flow = (
            "      - name: Load assets\n"
            "%s"
            "        uses: actions/checkout@abc\n"
            '        with: {repository: a/b, ref: "${{ %s }}"}\n'
        )
        trailing = "steps.resolve_ref.outputs.ref || 'main'"
        leading = "'main' || steps.resolve_ref.outputs.ref"
        self.assertEqual(self._jobs(self.RESOLVER + flow % (self.EXACT_IF, trailing)), [])
        self.assertEqual(len(self._jobs(self.RESOLVER + flow % ("", trailing))), 1)
        self.assertEqual(len(self._jobs(self.RESOLVER + flow % (self.EXACT_IF, leading))), 1)

    def test_the_or_spellings_behave_the_same_in_continuation_form(self):
        scalar = (
            "      - name: Load assets\n"
            "%s"
            "        uses: actions/checkout@abc\n"
            "        with:\n"
            "          ref: >-\n"
            "            ${{ %s }}\n"
        )
        trailing = "steps.resolve_ref.outputs.ref || 'main'"
        leading = "'main' || steps.resolve_ref.outputs.ref"
        self.assertEqual(self._jobs(self.RESOLVER + scalar % (self.EXACT_IF, trailing)), [])
        self.assertEqual(len(self._jobs(self.RESOLVER + scalar % ("", trailing))), 1)
        self.assertEqual(len(self._jobs(self.RESOLVER + scalar % (self.EXACT_IF, leading))), 1)

    def test_a_step_output_unrelated_to_the_input_is_not_a_ref_use(self):
        # The producing step binds no `workflows_ref`, so this checkout has
        # nothing to do with the pin contract. Demanding an empty-ref `if:` of
        # it would fail workflows this lint has no claim on. The id EXISTS and
        # precedes the consumer — which is what separates this from the
        # dangling case the pre-scan now reports (BE-8215).
        sites = cwp.ref_checkouts(
            self._wrap(self.UNRELATED_RESOLVER + self._step_output_checkout()).split("\n")
        )
        self.assertEqual(sites, [])

    def test_the_flow_form_of_a_step_output_checkout_behaves_the_same(self):
        # The whole `with:` on one line — the same one-line bypass the input
        # spellings already bar.
        flow = (
            "      - name: Load assets\n"
            "%s"
            "        uses: actions/checkout@abc\n"
            '        with: {repository: Comfy-Org/github-workflows, ref: "${{ steps.resolve_ref.outputs.ref }}"}\n'
        )
        self.assertEqual(self._jobs(self.RESOLVER + flow % self.EXACT_IF), [])
        self.assertEqual(len(self._jobs(self.RESOLVER + flow % "")), 1)

    def test_a_quoted_sibling_cannot_plant_a_decoy_step_output(self):
        # The twin of `test_a_quoted_sibling_cannot_plant_a_decoy_ref`, on this
        # path. A comma inside a quoted sibling meets the flow matcher's `[{,]`
        # boundary, so the leftmost match can be a `ref:` that is string
        # CONTENT — naming a step no `resolvers` entry holds, which drops the
        # real checkout out of coverage entirely. Keep looking past it.
        line = (
            "        with: {path: \"x, ref: ${{ steps.other.outputs.ref }}\", "
            "ref: \"${{ steps.resolve_ref.outputs.ref }}\"}"
        )
        self.assertEqual(cwp.steps_output_ref(line), ("resolve_ref", "ref"))
        # …and a real flow-form step output is still read, decoy or not.
        plain = "        with: {repository: a/b, ref: ${{ steps.r.outputs.ref }}}"
        self.assertEqual(cwp.steps_output_ref(plain), ("r", "ref"))

    def test_the_block_scalar_form_of_a_step_output_checkout_behaves_the_same(self):
        # `ref: >-` leaves the key line with no expression on it and the
        # expression line with no `ref:` key — the vertical spelling of the
        # same bypass.
        scalar = (
            "      - name: Load assets\n"
            "%s"
            "        uses: actions/checkout@abc\n"
            "        with:\n"
            "          ref: >-\n"
            "            ${{ steps.resolve_ref.outputs.ref }}\n"
        )
        self.assertEqual(self._jobs(self.RESOLVER + scalar % self.EXACT_IF), [])
        unguarded = self._jobs(self.RESOLVER + scalar % "")
        self.assertEqual(len(unguarded), 1)
        # Reported at the `ref:` KEY line, which is the checkout a reader looks
        # for — not at the continuation line the parser matched.
        text = self._wrap(self.RESOLVER + scalar % "").split("\n")
        self.assertEqual(text[unguarded[0] - 1].strip(), "ref: >-")

    def test_a_bracket_spelled_resolver_still_registers(self):
        # `_GUARD_BINDING_RE` is also the ONLY thing that registers a step in
        # `resolvers` (via `_binding_step_id`), so a bracket-spelled binding
        # that it fails to recognize does not just go unreported as a guard —
        # it drops the step out of `resolvers` entirely, and every checkout
        # resolved from its output then vanishes from `found` rather than
        # being reported. The bracket spelling is the exact same value as the
        # dot form, so it must register the resolver just the same.
        bracket_resolver = self.RESOLVER.replace(
            "inputs.workflows_ref", "inputs['workflows_ref']"
        )
        self.assertEqual(len(self._jobs(bracket_resolver + self._step_output_checkout())), 1)
        self.assertEqual(
            self._jobs(bracket_resolver + self._step_output_checkout(self.EXACT_IF)), []
        )

    def test_a_bracket_spelled_step_output_checkout_is_recognized(self):
        # `steps_output_ref` had the same dot-only gap on its OWN accessor:
        # `steps['id'].outputs['out']` is the identical access to the dot
        # form, and reading it as no step output at all silently dropped the
        # checkout from the lint rather than reporting it.
        bracket_ref = "${{ steps['resolve_ref'].outputs['ref'] }}"
        self.assertEqual(
            len(self._jobs(self.RESOLVER + self._step_output_checkout(ref=bracket_ref))), 1
        )
        self.assertEqual(
            self._jobs(
                self.RESOLVER + self._step_output_checkout(self.EXACT_IF, ref=bracket_ref)
            ),
            [],
        )

    def test_an_unguarded_step_output_checkout_gets_its_own_remedy(self):
        # "Copy the guard step in ahead of it" is the wrong advice here — the
        # never-fail job cannot run one. A lint that fails with a remedy that
        # does not apply is a lint people learn to route around.
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        text = _reusable(PINNED).replace(
            "    steps:\n      - run: echo hi\n",
            "    steps:\n" + self.RESOLVER + self._step_output_checkout(),
        )
        with open(os.path.join(tmp, "w.yml"), "w", encoding="utf-8") as f:
            f.write(text)
        errors, _, _, _ = cwp.check_dir(tmp, exempt=frozenset())
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("BE-8130", errors[0])
        self.assertIn("resolved from a step", errors[0])
        self.assertNotIn("BE-5546", errors[0])

    def test_a_job_level_outputs_ref_is_not_a_checkout(self):
        # The walk asks EVERY line of the job about step-output refs, so a
        # job-level `outputs:` mapping — which conventionally sits ABOVE
        # `steps:`, and therefore above every step id — reaches
        # `_record_steps_output` looking exactly like a dangling consumer.
        # It is not a checkout at all, and hard-failing a compliant workflow
        # with an error naming a mutable checkout ref is worse than the drop
        # the dangling check replaced. `_is_ref_input` is the gate.
        text = (
            "name: F\non:\n  workflow_call:\njobs:\n"
            "  job0:\n"
            "    runs-on: ubuntu-latest\n"
            "    outputs:\n"
            "      ref: ${{ steps.resolve_ref.outputs.ref }}\n"
            "    steps:\n" + self.RESOLVER
        )
        self.assertEqual(cwp.ref_checkouts(text.split("\n")), [])

    def test_a_ref_line_inside_a_run_heredoc_is_not_a_checkout(self):
        # A `run:` script emitting fixture YAML — a shape this repo itself
        # uses. The line is inside a step, so the `- ` list item resolves and
        # the step-bounds question cannot tell it apart from an input; the
        # ENCLOSING KEY can, and it is `run:`, not `with:`.
        heredoc = (
            "      - name: Write a fixture\n"
            "        run: |\n"
            "          cat <<'EOF' > f.yml\n"
            "          ref: ${{ steps.resolve_ref.outputs.ref }}\n"
            "          EOF\n"
        )
        self.assertEqual(cwp.ref_checkouts(self._wrap(heredoc).split("\n")), [])

    def test_an_unrelated_earlier_step_stays_out_of_scope_in_any_operand_position(self):
        # `ref: ${{ inputs.pr_sha || steps.detect.outputs.sha }}` resolved from
        # a REAL earlier step that never touches `workflows_ref` is an ordinary
        # checkout this lint has no claim on. Judging operand order ahead of
        # scope hoisted it past the out-of-scope return and hard-failed it as
        # 'non-leading' — printing an error that demands an operand reorder,
        # which changes the workflow's runtime semantics.
        ref = "${{ inputs.pr_sha || steps.resolve_ref.outputs.ref }}"
        steps = self.UNRELATED_RESOLVER + self._step_output_checkout(ref=ref)
        self.assertEqual(cwp.ref_checkouts(self._wrap(steps).split("\n")), [])
        # …and the same step in the LEADING position is equally out of scope,
        # which is the behavior that was already correct.
        self.assertEqual(
            cwp.ref_checkouts(
                self._wrap(
                    self.UNRELATED_RESOLVER + self._step_output_checkout()
                ).split("\n")
            ),
            [],
        )

    def test_a_dangling_id_behind_a_leading_operand_is_reported_non_leading(self):
        # Scope moved ahead of operand order; the ordering between DANGLING and
        # 'non-leading' did not. With the leading operand winning, the output is
        # never consulted, so "no such step" is a red herring and fixing the id
        # changes nothing — operand order is the only fix, and it needs the
        # message that says so.
        ref = "${{ 'main' || steps.resolve_reff.outputs.ref }}"
        sites = cwp.ref_checkouts(
            self._wrap(self.RESOLVER + self._step_output_checkout(ref=ref)).split("\n")
        )
        self.assertEqual(
            [(fb, guarded, via) for _, fb, guarded, via in sites],
            [(False, False, "non-leading")],
        )

    def test_a_with_key_on_the_list_marker_line_is_seen(self):
        # Mapping keys are unordered, so `- with:` ahead of `uses:` is valid
        # Actions YAML — and the marker holds the key column, exactly as it does
        # for `- id:` and `- if:`. Without the same normalization the `with:`
        # gate answers False and the site is DROPPED, turning a fail-closed
        # report into a silent fail-open on the shape the lint exists to close.
        marker = (
            "      - with:\n"
            "          ref: ${{ steps.resolve_ref.outputs.ref }}\n"
            "        uses: actions/checkout@abc\n"
        )
        self.assertEqual(len(self._jobs(self.RESOLVER + marker)), 1)
        # …and it is a real COVERED site once the exact `if:` is on it, rather
        # than passing by being invisible.
        guarded_marker = marker + self.EXACT_IF
        sites = cwp.ref_checkouts(self._wrap(self.RESOLVER + guarded_marker).split("\n"))
        self.assertEqual(
            [(fb, guarded, via) for _, fb, guarded, via in sites], [(False, True, True)]
        )

    def test_a_heredoc_emitting_a_whole_step_is_not_a_checkout(self):
        # The flat heredoc is not the only shape: a script emitting a STEP puts
        # the word `with:` at exactly the indent the enclosing-key walk stops
        # on, so indentation alone reads script output as an action input and
        # hard-fails a compliant workflow. The open `|` block scalar is what
        # tells the two apart. Both the block and the flow spelling.
        for emitted in (
            "          with:\n            ref: ${{ steps.resolve_ref.outputs.ref }}\n",
            '          - uses: actions/checkout@abc\n'
            '            with: {ref: "${{ steps.resolve_ref.outputs.ref }}"}\n',
        ):
            with self.subTest(emitted=emitted.strip()):
                heredoc = (
                    "      - name: Write a fixture\n"
                    "        run: |\n"
                    "          cat <<'EOF' > f.yml\n" + emitted + "          EOF\n"
                )
                self.assertEqual(cwp.ref_checkouts(self._wrap(heredoc).split("\n")), [])
        # The scalar CLOSES: a real checkout after the heredoc is still judged.
        after = (
            "      - name: Write a fixture\n"
            "        run: |\n"
            "          cat <<'EOF' > f.yml\n"
            "          with:\n"
            "          EOF\n"
        ) + self._step_output_checkout()
        self.assertEqual(len(self._jobs(self.RESOLVER + after)), 1)

    def test_a_folded_ref_split_across_lines_is_read_as_one_expression(self):
        # `ref: >-` folds its continuation lines into ONE value, so an
        # expression split across two of them is at runtime exactly the
        # mutable-fallback ref BE-8215 closed. Matching each physical line on
        # its own left a single newline enough to hide it.
        split = (
            "      - name: Load assets\n"
            "        uses: actions/checkout@abc\n"
            "        with:\n"
            "          ref: >-\n"
            "            ${{ steps.resolve_ref.outputs.ref ||\n"
            "            'main' }}\n"
        )
        self.assertEqual(len(self._jobs(self.RESOLVER + split)), 1)
        # Reported at the `ref:` KEY line — the checkout the reader must find —
        # as the single-line continuation spelling already is.
        text = self._wrap(self.RESOLVER + split).split("\n")
        self.assertEqual(text[cwp.find_unguarded_ref_checkouts(text)[0] - 1].strip(), "ref: >-")
        # …and the exact `if:` on the consuming step still covers it.
        guarded = split.replace(
            "      - name: Load assets\n",
            "      - name: Load assets\n" + self.EXACT_IF,
            1,
        )
        self.assertEqual(self._jobs(self.RESOLVER + guarded), [])

    def test_a_folded_unparseable_ref_split_across_lines_is_still_refused(self):
        # The fold-join (BE-8220) and the two-tier reader (BE-8253) compose: a
        # spelling this reader can never parse — a TRAILING `&&` — split
        # across the same two physical lines the fold test above joins into a
        # PARSEABLE expression. The single first line alone reads `UNPARSED`
        # (an unclosed `${{`), which must not be finalized early — that would
        # report the site before the second line is ever read, and on a
        # single-continuation-line scalar it would also just be correct by
        # accident. Joining confirms the interpolation is genuinely
        # unparseable rather than merely incomplete, and refuses it — the
        # fold must never let a site that used to record nothing regress to
        # silently recording nothing again.
        split = (
            "      - name: Load assets\n"
            "        uses: actions/checkout@abc\n"
            "        with:\n"
            "          ref: >-\n"
            "            ${{ steps.resolve_ref.outputs.ref &&\n"
            "            'main' }}\n"
        )
        self.assertEqual(self._states(self.RESOLVER + split), [(False, False, "unparsed")])

    def test_a_fallback_containing_a_comma_is_still_read(self):
        # `[^,}]` is the FLOW form's entry boundary and nothing else's.
        # Carrying it into the block form dropped every fallback holding a
        # comma out of the lint entirely — the same fail-open the `||` reader
        # exists to close.
        ref = "${{ steps.resolve_ref.outputs.ref || 'a,b' }}"
        self.assertEqual(
            self._jobs(self.RESOLVER + self._step_output_checkout(self.EXACT_IF, ref=ref)), []
        )
        self.assertEqual(
            len(self._jobs(self.RESOLVER + self._step_output_checkout(ref=ref))), 1
        )
        # The flow form keeps `[^,}]`, so its entry boundary still holds: a
        # sibling entry after the comma is not swallowed into the ref's value.
        line = '        with: {ref: "${{ steps.r.outputs.ref }}", repository: a/b}'
        self.assertEqual(cwp.steps_output_ref(line), ("r", "ref"))

    def test_a_falsey_leading_operand_still_reaches_the_output(self):
        # `||` returns the first TRUTHY operand, not the first operand, so a
        # leading `false`/`''` falls THROUGH to the step output — the value is
        # exactly the one the guard covers. Reading `leading` as "nothing
        # precedes the output" failed these with a message no edit can satisfy.
        for lead in ("false", "''", '""', "0", "null", "'' || false"):
            with self.subTest(lead=lead):
                ref = "${{ %s || steps.resolve_ref.outputs.ref }}" % lead
                self.assertEqual(
                    self._jobs(
                        self.RESOLVER + self._step_output_checkout(self.EXACT_IF, ref=ref)
                    ),
                    [],
                    lead,
                )
        # …and a TRUTHY leading operand is still unguarded unconditionally.
        truthy = "${{ 'main' || steps.resolve_ref.outputs.ref }}"
        self.assertEqual(
            len(self._jobs(self.RESOLVER + self._step_output_checkout(self.EXACT_IF, ref=truthy))),
            1,
        )

    def _one_error(self, steps):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        text = _reusable(PINNED).replace(
            "    steps:\n      - run: echo hi\n", "    steps:\n" + steps
        )
        with open(os.path.join(tmp, "w.yml"), "w", encoding="utf-8") as f:
            f.write(text)
        errors, _, _, _ = cwp.check_dir(tmp, exempt=frozenset())
        self.assertEqual(len(errors), 1, errors)
        return errors[0]

    def test_a_non_leading_operand_gets_its_own_error_text(self):
        # The BE-8130 remedies are worse than useless here: both harden a value
        # this ref never resolves to, so applying the printed advice leaves CI
        # red with the identical error and never names operand order — the one
        # thing that fixes it. Nor is it the dangling case: the step EXISTS.
        ref = "${{ 'main' || steps.resolve_ref.outputs.ref }}"
        error = self._one_error(
            self.RESOLVER + self._step_output_checkout(self.EXACT_IF, ref=ref)
        )
        self.assertIn("does not START with it", error)
        self.assertIn("steps.resolve_ref.outputs.ref", error)
        for wrong_remedy in ("BE-8130", "fail-closed", "never-fail", "no step"):
            self.assertNotIn(wrong_remedy, error)

    def test_a_non_or_leading_operator_is_reported_the_same_way(self):
        # `A && steps.x.outputs.ref` resolves to the output when `A` is truthy
        # and to '' when it is not — an unguarded path either way, and one no
        # `if:` on this step closes. The message must not claim `||`.
        ref = "${{ github.event_name && steps.resolve_ref.outputs.ref }}"
        error = self._one_error(
            self.RESOLVER + self._step_output_checkout(self.EXACT_IF, ref=ref)
        )
        self.assertIn("does not START with it", error)

    def test_the_dangling_error_names_the_actual_step_id(self):
        # A typo'd id is the first cause the message lists, so the annotation
        # has to show which id it read — the placeholders leave the reader
        # hunting for it.
        error = self._one_error(
            self.RESOLVER
            + self._step_output_checkout(ref="${{ steps.resolve_reff.outputs.ref }}")
        )
        self.assertIn("steps.resolve_reff.outputs.ref", error)
        self.assertIn("no step `resolve_reff` precedes", error)
        self.assertNotIn("<id>", error)

    # ------------------------------------------------------------------
    # The two-tier reader (BE-8253). One regex per line gives one verdict per
    # line, so a `ref:` value the strict reader cannot chew — or one holding
    # SEVERAL step outputs — used to record no site and pass the lint, while
    # the identical workflow spelled bare failed. Loose tier: does the
    # comment-stripped VALUE mention a step output at all? Strict tier: every
    # interpolation, every top-level `||` operand. Loose > strict = refuse.
    # ------------------------------------------------------------------

    def _states(self, steps):
        """(uses_fallback, guarded, via_step_output) for each site in one job."""
        return [
            (fb, guarded, via)
            for _, fb, guarded, via in cwp.ref_checkouts(self._wrap(steps).split("\n"))
        ]

    def _second_resolver(self, base, step_id="lookup"):
        """`base` re-declared under a second id, so a two-operand `ref:` can
        have each of its operands covered — or not — independently."""
        other = base.replace("id: resolve_ref", "id: %s" % step_id).replace(
            "Resolve the asset ref", "Resolve the %s ref" % step_id
        )
        self.assertNotEqual(other, base, "fixture drifted")
        return other

    def test_a_fallback_containing_a_brace_is_refused_not_skipped(self):
        # The `}` in `{0}` breaks the fallback stretch, so the strict reader
        # matches nothing and the line used to leave the lint entirely — a
        # confirmed fail-open through the real CLI: this workflow passed while
        # the identical one spelled bare failed.
        ref = "${{ steps.resolve_ref.outputs.ref || format('refs/heads/{0}', 'main') }}"
        steps = self.RESOLVER + self._step_output_checkout(self.EXACT_IF, ref=ref)
        self.assertEqual(self._states(steps), [(False, False, "unparsed")])
        self.assertEqual(len(self._jobs(steps)), 1)

    def test_a_parenthesized_operand_is_refused_not_skipped(self):
        # `)` after the output name matches neither `||` nor `}}`, so the
        # strict reader stops — but the value plainly reads a step output.
        ref = "${{ (steps.resolve_ref.outputs.ref) || 'main' }}"
        steps = self.RESOLVER + self._step_output_checkout(self.EXACT_IF, ref=ref)
        self.assertEqual(self._states(steps), [(False, False, "unparsed")])

    def test_an_and_operator_after_the_output_is_refused_not_skipped(self):
        # Only `||` is read. `&&` is wrong in BOTH arms at runtime — truthy
        # checks out at the mutable literal, falsey resolves to '' and checkout
        # takes the default branch — so the one shape that must NOT happen is
        # the lint staying quiet about it.
        ref = "${{ steps.resolve_ref.outputs.ref && 'main' }}"
        steps = self.RESOLVER + self._step_output_checkout(self.EXACT_IF, ref=ref)
        self.assertEqual(self._states(steps), [(False, False, "unparsed")])

    def test_the_flow_and_continuation_spellings_refuse_it_too(self):
        # Same treatment on all three spellings — a shape refused in the block
        # form and skipped in the other two is the one-line bypass again.
        expr = "${{ steps.resolve_ref.outputs.ref && 'main' }}"
        flow = (
            "      - name: Load assets\n"
            "        uses: actions/checkout@abc\n"
            '        with: {repository: Comfy-Org/github-workflows, ref: "%s"}\n' % expr
        )
        scalar = (
            "      - name: Load assets\n"
            "        uses: actions/checkout@abc\n"
            "        with:\n"
            "          ref: >-\n"
            "            %s\n" % expr
        )
        for spelling, steps in (("flow", flow), ("block scalar", scalar)):
            with self.subTest(spelling=spelling):
                self.assertEqual(
                    self._states(self.RESOLVER + steps), [(False, False, "unparsed")]
                )

    def test_a_flow_fallback_holding_a_comma_is_refused_not_skipped(self):
        # `[^,}]` is the flow form's entry boundary, so a fallback carrying a
        # comma is a spelling the strict reader genuinely cannot read there.
        # The block form reads it (`test_a_fallback_containing_a_comma_is_still_read`);
        # the flow form now REFUSES it instead of dropping the site.
        flow = (
            "      - name: Load assets\n"
            "        uses: actions/checkout@abc\n"
            '        with: {ref: "${{ steps.resolve_ref.outputs.ref || \'a,b\' }}"}\n'
        )
        self.assertEqual(
            self._states(self.RESOLVER + flow), [(False, False, "unparsed")]
        )

    def test_a_commented_out_step_output_is_not_a_ref_use(self):
        # The loose tier is permissive by design, so it MUST run on the
        # comment-stripped value only — prose naming a step output is not a
        # checkout resolved from one, and reporting it would fail a workflow
        # whose `ref:` is a plain literal.
        checkout = self._step_output_checkout(ref="main  # was ${{ steps.resolve_ref.outputs.ref }}")
        self.assertEqual(self._states(self.RESOLVER + checkout), [])

    def test_an_unparseable_expression_outside_a_with_input_is_not_a_checkout(self):
        # The `_is_ref_input` gate, on the new state. The walk asks EVERY line
        # of the job, so a job-level `outputs:` mapping and a `run:` heredoc
        # emitting fixture YAML both reach the recorder — and hard-failing a
        # compliant workflow with an error naming a checkout where there is no
        # checkout is the false-CI-failure channel BE-8215 already had to close
        # once. Do not reopen it.
        expr = "${{ steps.resolve_ref.outputs.ref && 'main' }}"
        heredoc = (
            "      - name: Write a fixture\n"
            "        run: |\n"
            "          cat <<'EOF' > f.yml\n"
            "          ref: %s\n"
            "          EOF\n" % expr
        )
        self.assertEqual(self._states(self.RESOLVER + heredoc), [])
        job_outputs = (
            "name: F\non:\n  workflow_call:\njobs:\n"
            "  job0:\n"
            "    runs-on: ubuntu-latest\n"
            "    outputs:\n"
            "      ref: %s\n"
            "    steps:\n" % expr
        ) + self.RESOLVER
        self.assertEqual(cwp.ref_checkouts(job_outputs.split("\n")), [])

    def test_a_quoted_decoy_does_not_hide_an_unparseable_real_ref(self):
        # The decoy discipline and the new state compose: a `ref:` planted in
        # string content meets the flow matcher's `[{,]` boundary, and stopping
        # there would drop the REAL ref — which is itself unparseable — out of
        # coverage entirely.
        flow = (
            "      - name: Load assets\n"
            "        uses: actions/checkout@abc\n"
            '        with: {path: "x, ref: ${{ steps.decoy.outputs.ref }}", '
            'ref: "${{ (steps.resolve_ref.outputs.ref) || \'main\' }}"}\n'
        )
        self.assertEqual(
            self._states(self.RESOLVER + flow), [(False, False, "unparsed")]
        )

    def test_both_concatenated_interpolations_are_judged(self):
        # The block pattern's greedy `.*` landed on the LAST interpolation, so
        # the first was never judged at all. Both feed the ref's value, so both
        # must be covered independently — and since BE-8221 a resolver's own
        # guard strength buys its consumer nothing, hard or soft: coverage is
        # ONLY the consuming step's own exact `if:`. `both_hard` proves the
        # first half — two fail-closed resolvers and no `if:` at all still
        # report the site, because neither resolver's strength is examined.
        ref = "${{ steps.lookup.outputs.ref }}${{ steps.resolve_ref.outputs.ref }}"
        checkout = self._step_output_checkout(ref='"%s"' % ref)
        both_hard = self._second_resolver(self.HARD_RESOLVER) + self.HARD_RESOLVER
        self.assertEqual(len(self._jobs(both_hard + checkout)), 1)
        # A step carries only one `if:`, so covering the SECOND operand's
        # exact condition — the one the old single-operand reader judged —
        # still fails: the first operand is untouched, and one covered
        # sibling does not excuse the other (BE-8253).
        covered_second = self._second_resolver(self.RESOLVER) + self.HARD_RESOLVER
        steps = covered_second + self._step_output_checkout(
            self.EXACT_IF, ref='"%s"' % ref
        )
        self.assertEqual(len(self._jobs(steps)), 1)
        # …and only the first: the same failure, from the other side.
        covered_first = self._second_resolver(self.HARD_RESOLVER) + self.RESOLVER
        first_if = "        if: steps.lookup.outputs.ref != ''\n"
        steps = covered_first + self._step_output_checkout(first_if, ref='"%s"' % ref)
        self.assertEqual(len(self._jobs(steps)), 1)

    def test_every_or_operand_must_be_covered(self):
        # `A || B` with both operands step outputs: the fallback stretch
        # swallowed `B`, so a covered `A` passed the whole site while `B`
        # reached an output nothing had judged. An unresolved `A` really is
        # falsey, so `B` really can be the ref.
        ref = "${{ steps.lookup.outputs.ref || steps.resolve_ref.outputs.ref }}"
        checkout = self._step_output_checkout(ref=ref)
        both_hard = self._second_resolver(self.HARD_RESOLVER) + self.HARD_RESOLVER
        # A fail-closed resolver on EITHER operand still buys nothing without
        # the consuming step's own `if:` (BE-8221) — the site is reported.
        self.assertEqual(len(self._jobs(both_hard + checkout)), 1)
        # Leading operand's resolver is hard, trailing one's is soft — neither
        # matters without an `if:`, so the site still fails either way.
        lead_hard = self._second_resolver(self.HARD_RESOLVER) + self.RESOLVER
        self.assertEqual(len(self._jobs(lead_hard + checkout)), 1)
        # …and the trailing operand is not reported `'non-leading'` merely for
        # sitting behind another step output: `||` falls through an empty one.
        self.assertEqual(self._states(lead_hard + checkout), [(False, False, True)])
        # Covering only the TRAILING operand's exact `if:` still fails: the
        # leading one is untouched, and a covered sibling excuses nothing.
        steps = lead_hard + self._step_output_checkout(self.EXACT_IF, ref=ref)
        self.assertEqual(len(self._jobs(steps)), 1)

    def test_a_second_operand_naming_no_step_is_dangling(self):
        # The exhaustive reader sees the operand the old one swallowed, so a
        # typo in it is caught rather than excused by a covered leading operand.
        ref = "${{ steps.resolve_ref.outputs.ref || steps.resolve_reff.outputs.ref }}"
        steps = self.HARD_RESOLVER + self._step_output_checkout(ref=ref)
        self.assertEqual(self._states(steps), [(False, False, "dangling")])

    def test_the_supported_spellings_are_unchanged(self):
        # No behavior change for a parsed single-operand site — the whole point
        # of leaving the strict reader alone and adding a tier around it.
        for ref in (
            "${{ steps.resolve_ref.outputs.ref }}",
            "${{ steps.resolve_ref.outputs.ref || 'main' }}",
            "${{ steps.resolve_ref.outputs.ref || 'a,b' }}",
        ):
            with self.subTest(ref=ref):
                self.assertEqual(
                    self._states(
                        self.RESOLVER + self._step_output_checkout(self.EXACT_IF, ref=ref)
                    ),
                    [(False, True, True)],
                    ref,
                )
                self.assertEqual(
                    self._states(self.RESOLVER + self._step_output_checkout(ref=ref)),
                    [(False, False, True)],
                    ref,
                )

    def test_a_continuation_value_with_a_literal_prefix_is_a_site(self):
        # The block form always read a value that only CONTAINS the
        # interpolation (`ref: refs/heads/${{ … }}`); the continuation form used
        # to demand the whole line BE one, so the same concatenation under
        # `ref: >-` recorded no site. Reading the value the same way in all
        # three spellings is the point — a checkout is not less of a checkout
        # for having a prefix, and the value still resolves through the output.
        scalar = (
            "      - name: Load assets\n"
            "%s"
            "        uses: actions/checkout@abc\n"
            "        with:\n"
            "          ref: >-\n"
            "            refs/heads/${{ steps.resolve_ref.outputs.ref }}\n"
        )
        self.assertEqual(
            self._states(self.RESOLVER + scalar % ""), [(False, False, True)]
        )
        self.assertEqual(self._jobs(self.RESOLVER + scalar % self.EXACT_IF), [])

    def test_an_unparsed_ref_gets_its_own_error_text(self):
        # Every other remedy presumes a parse — an id to name, an operand order
        # to fix, an `if:` on a known output — so printing one of those here
        # sends the author to edit something the lint never read. The message
        # has to name the SUPPORTED spellings instead.
        ref = "${{ steps.resolve_ref.outputs.ref || format('refs/heads/{0}', 'main') }}"
        error = self._one_error(
            self.RESOLVER + self._step_output_checkout(self.EXACT_IF, ref=ref)
        )
        self.assertIn("cannot parse", error)
        self.assertIn("BE-8253", error)
        self.assertIn(".github/workflow-pins/README.md", error)
        for wrong_remedy in ("BE-8130", "does not START with it", "no step `"):
            self.assertNotIn(wrong_remedy, error)

    def test_a_multi_operand_error_does_not_name_the_wrong_step(self):
        # The dangling message's whole value is naming the id to fix, and with
        # two operands only one of them is the offending one — naming the first
        # would send the author to edit a sibling that is fine. Step existence
        # is a property of the JOB, not the line, so the placeholders are the
        # honest answer there; a NON-leading operand is answerable from the
        # line itself, so that one is still named exactly.
        dangling = "${{ steps.resolve_ref.outputs.ref || steps.resolve_reff.outputs.ref }}"
        error = self._one_error(
            self.HARD_RESOLVER + self._step_output_checkout(ref=dangling)
        )
        self.assertIn("no step `<id>` precedes", error)
        self.assertNotIn("no step `resolve_ref` precedes", error)
        non_leading = "${{ steps.resolve_ref.outputs.ref || 'main' || steps.lookup.outputs.ref }}"
        error = self._one_error(
            self._second_resolver(self.HARD_RESOLVER)
            + self.HARD_RESOLVER
            + self._step_output_checkout(ref=non_leading)
        )
        self.assertIn("steps.lookup.outputs.ref", error)
        self.assertNotIn("<id>", error)

    def test_this_repos_own_workflows_guard_every_ref_checkout(self):
        root = os.path.normpath(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "workflows")
        )
        seen = 0
        for name in ("cursor-review.yml", "groom.yml", "agents-md-integrity.yml", "pr-size.yml"):
            with open(os.path.join(root, name), encoding="utf-8") as f:
                lines = f.read().split("\n")
            # Counted through `ref_checkouts`, not a per-line `is_ref_use` scan:
            # since BE-8130 a site can reach the ref through an earlier step's
            # output, which no single line can be asked about — the answer
            # depends on what the job walked past. A line scan would report the
            # ledger site as absent while the lint covers it, which is exactly
            # the drift this count exists to catch.
            sites = cwp.ref_checkouts(lines)
            self.assertTrue(sites, "%s: no ref checkout found — fixture drifted" % name)
            seen += len(sites)
            self.assertEqual(cwp.find_unguarded_ref_checkouts(lines), [], name)
        self.assertEqual(
            seen,
            16,
            "expected the 12 guarded sites BE-5546 fixed + pr-size.yml's (BE-5858) "
            "+ cursor-review.yml's preflight (hard guard) site picked up merging "
            "main + cursor-review.yml's diff-size job's check-pr-size-tool "
            "checkout, also picked up merging main (BE-5546 added its guard "
            "here) + cursor-review.yml's ledger checkout, which RE-ENTERED "
            "coverage in BE-8130: it reads `ref: ${{ steps.resolve_ref.outputs.ref "
            "}}` and names no input, and the detector now follows the `ref:` "
            "through that step output to the step binding `workflows_ref` (it had "
            "dropped out in BE-8077, when the resolve-then-consume shape landed). "
            "groom.yml's 7 sites name the input directly, via the "
            "`|| job.workflow_sha` fallback — and are clean because each one "
            "carries its guard, which BE-8077 made load-bearing rather than "
            "exempt.",
        )

    WORKFLOWS_DIR = os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "workflows")
    )

    def _workflow(self, name):
        with open(os.path.join(self.WORKFLOWS_DIR, name), encoding="utf-8") as f:
            return f.read().split("\n")

    @staticmethod
    def _enclosing_step(lines, idx):
        """The lines of the `- name:` step containing `idx`."""
        start = idx
        while start >= 0 and not lines[start].lstrip().startswith("- name:"):
            start -= 1
        assert start >= 0, "line %d is not inside a step" % idx
        indent = len(lines[start]) - len(lines[start].lstrip())
        end = start + 1
        while end < len(lines):
            stripped = lines[end].strip()
            if stripped and len(lines[end]) - len(lines[end].lstrip()) <= indent:
                break
            end += 1
        return lines[start:end]

    def test_the_ledger_resolver_carries_the_shape_check(self):
        # The consumer's `if:` is LINT-COVERED since BE-8130: the detector
        # follows the ledger checkout's `ref:` through
        # `steps.resolve_ref.outputs.ref` back to the step binding the input,
        # and requires exactly that non-empty `if:` (an OR-widened one such as
        # `... != '' || always()` runs the checkout precisely when the output is
        # empty, and is refused). So the assertion loop that used to pin it by
        # hand is gone — `check_workflow_pins.py` itself fails now if the `if:`
        # is deleted or widened.
        #
        # The resolver's SHAPE is not lint-covered, and that is what remains
        # here: the lint proves emptiness-skip, never charset policy. This is
        # the cursor-review twin of `test_every_groom_guard_carries_the_shape_check`
        # below.
        #
        # The resolving step must REJECT rather than sanitize: it emits the
        # empty string for anything not ref-shaped, under a pinned byte locale.
        #
        # (That the checkout still READS `steps.resolve_ref.outputs.ref` needs no
        # assertion here either: the count above would drop back to 15 if it
        # stopped, since the site would leave the detector's coverage.)
        #
        # Scoped to that step, not to the whole file. Matched against the file
        # body, any step in it satisfied these — and the file already carries
        # six other hand-copied guards, so the natural consistency sweep that
        # adds `export LC_ALL=C` to them would let someone delete it from
        # `resolve_ref` with the only test covering that step still green.
        lines = self._workflow("cursor-review.yml")
        resolvers = [i for i, line in enumerate(lines) if line.strip() == "id: resolve_ref"]
        self.assertEqual(len(resolvers), 1, "expected exactly one resolve_ref step")
        resolver = [s.strip() for s in self._enclosing_step(lines, resolvers[0])]
        self.assertIn("*[!A-Za-z0-9._/@+-]*) REF='' ;;", resolver)
        self.assertIn("export LC_ALL=C", resolver)
        # …and it must stay NEVER-FAIL, which the lint also cannot assert.
        # Nothing in the lint has an opinion on `continue-on-error:` here —
        # since BE-8221 the consumer's own `if:` is the only coverage route, so
        # it stays green either way. Deleting `continue-on-error: true` and
        # exiting non-zero on an unresolvable ref would therefore pass silently
        # while making this job able to FAIL — and a failing ledger job takes
        # the whole review matrix down with it, which is the one thing the job
        # is built never to do.
        self.assertIn("continue-on-error: true", resolver)
        # …and it must resolve from the BE-8077 fallback. Every assertion above
        # still passed with the binding swapped for `|| 'main'`, which is the
        # mutable-ref hole BE-8077 exists to close — and the lint still cannot
        # see it: `_GUARD_BINDING_RE` recognizes only the bare input and the
        # `|| job.workflow_sha` fallback, so a resolver binding `|| 'main'`
        # registers as no resolver at all and its consumer drops out of coverage
        # as "not this lint's subject". This assertion is what covers that for
        # cursor-review.yml.
        self.assertIn(
            "WORKFLOWS_REF: ${{ inputs.workflows_ref || job.workflow_sha }}",
            resolver,
            "resolve_ref no longer resolves from the job.workflow_sha fallback",
        )

    def test_every_groom_guard_carries_the_shape_check(self):
        # The `case` block is the only thing closing the Unicode-whitespace hole
        # (the `-z` above it runs on an ASCII-only stripped copy), and
        # `is_guard_step` only requires an emptiness test plus an exit — so
        # deleting all seven `case` blocks left the pin lint AND this whole suite
        # green. cursor-review.yml's twin is pinned by the test above; this is
        # the matching cover for groom.yml.
        lines = self._workflow("groom.yml")
        starts = [
            i
            for i, line in enumerate(lines)
            if line.strip() == "- name: Require a resolvable workflows_ref"
        ]
        self.assertEqual(len(starts), 7, "expected 7 groom guard steps")

        bodies = []
        for idx in starts:
            step = self._enclosing_step(lines, idx)
            text = "\n".join(step)
            self.assertIn("export LC_ALL=C", text)
            self.assertIn('REF="$(printf \'%s\' "$WORKFLOWS_REF" | tr -d \'[:space:]\')"', text)
            self.assertIn('if [ -z "$REF" ]; then', text)
            self.assertIn('case "$WORKFLOWS_REF" in', text)
            self.assertIn("*[!A-Za-z0-9._/@+-]*)", text)
            self.assertEqual(text.count("exit 1"), 2, "both branches must exit")
            bodies.append(text)

        # Seven hand-copied guards drift; the guarantee is only as strong as the
        # weakest copy, so require them byte-identical.
        self.assertEqual(len(set(bodies)), 1, "the 7 groom guard steps have drifted apart")

    # ------------------------------------------------------------------
    # Trailing comments (BE-9129). The per-line arm selection used to run on
    # the RAW physical line, and `_REF_USE_BLOCK_RE`'s `.*` spans a trailing
    # `#` comment — so a `ref: ${{ steps.r.outputs.ref }}  # from
    # inputs.workflows_ref` took the INPUT-use arm and a correctly
    # `if:`-guarded checkout was reported unguarded, while a comment carrying
    # `{WORKFLOWS_REF: "${{ … }}"}` registered a resolver that does not exist.
    # Both directions are the arm selection reading prose as config.
    # ------------------------------------------------------------------

    # The minimal resolve-then-consume pair, deliberately smaller than
    # `RESOLVER`: the comment, not the guard shape, is what is under test.
    COMMENT_RESOLVER = (
        "      - name: Resolve the asset ref\n"
        "        id: r\n"
        "        env:\n"
        "          WORKFLOWS_REF: ${{ inputs.workflows_ref }}\n"
        '        run: echo "ref=x" >> "$GITHUB_OUTPUT"\n'
    )
    COMMENTED_CONSUMER = (
        "      - name: Load assets\n"
        "        if: steps.r.outputs.ref != ''\n"
        "        uses: actions/checkout@abc\n"
        "        with:\n"
        "          ref: ${{ steps.r.outputs.ref }}  # resolved from inputs.workflows_ref\n"
    )
    # The same consumer with the value on its own line — the block-scalar /
    # plain multi-line spelling, judged on the CONTINUATION line, which had the
    # identical raw-line bug in `mention_re.search`.
    COMMENTED_CONSUMER_CONT = (
        "      - name: Load assets\n"
        "        if: steps.r.outputs.ref != ''\n"
        "        uses: actions/checkout@abc\n"
        "        with:\n"
        "          ref:\n"
        "            ${{ steps.r.outputs.ref }}  # resolved from inputs.workflows_ref\n"
    )

    def test_an_explanatory_comment_does_not_unguard_a_step_output_checkout(self):
        # Before the fix: (False, False, False) and one unguarded site — the
        # comment alone flipped a compliant workflow red.
        steps = self.COMMENT_RESOLVER + self.COMMENTED_CONSUMER
        self.assertEqual(self._states(steps), [(False, True, True)])
        self.assertEqual(self._jobs(steps), [])

    def test_an_explanatory_comment_on_a_continuation_line_does_not_unguard(self):
        steps = self.COMMENT_RESOLVER + self.COMMENTED_CONSUMER_CONT
        self.assertEqual(self._states(steps), [(False, True, True)])
        self.assertEqual(self._jobs(steps), [])

    def test_a_comment_that_looks_like_a_flow_binding_registers_no_resolver(self):
        # `_GUARD_BINDING_FLOW_RE` searches unanchored, so a binding spelled
        # inside a COMMENT used to register `d` as a resolver of the input —
        # which pulled its consumer into scope as a covered-by-`if:` site and
        # then reported it for having no `if:`. The comment configures nothing;
        # the step must be as invisible as it is without it.
        decoy = (
            '      - name: resolve  # {WORKFLOWS_REF: "${{ inputs.workflows_ref }}"}\n'
            "        id: d\n"
            "        run: echo ok\n"
            "      - name: Load assets\n"
            "        uses: actions/checkout@abc\n"
            "        with:\n"
            "          ref: ${{ steps.d.outputs.ref }}\n"
        )
        # Before the fix: [(False, False, True)] — an unguarded site invented
        # out of a comment.
        self.assertEqual(self._states(decoy), [])
        self.assertEqual(self._jobs(decoy), [])
        # …and identical to the same steps with the comment deleted, which is
        # the whole claim: the comment changes no verdict.
        plain = decoy.replace(
            '  # {WORKFLOWS_REF: "${{ inputs.workflows_ref }}"}', ""
        )
        self.assertNotEqual(plain, decoy, "fixture drifted")
        self.assertEqual(self._states(plain), [])

    def test_a_literal_ref_with_the_input_only_in_a_comment_is_not_a_ref_use(self):
        # `ref: main` checks out a branch, not the input. The mention lives
        # entirely past the `#`.
        steps = (
            "      - name: Load assets\n"
            "        uses: actions/checkout@abc\n"
            "        with:\n"
            "          ref: main  # {WORKFLOWS_REF: ${{ inputs.workflows_ref }}}\n"
        )
        self.assertEqual(self._states(steps), [])
        self.assertEqual(self._jobs(steps), [])

    def test_a_real_binding_still_guards_when_it_carries_a_comment(self):
        # Fail-closed direction: stripping must not cost a REAL guard its
        # binding. `_GUARD_BINDING_RE` already tolerated a trailing comment, so
        # this pins that the stripped-text arm selection keeps it that way.
        guard = self.GUARD.replace(
            "WORKFLOWS_REF: ${{ inputs.workflows_ref }}",
            "WORKFLOWS_REF: ${{ inputs.workflows_ref }}  # bound",
        )
        self.assertNotEqual(guard, self.GUARD, "fixture drifted")
        steps = guard + self.CHECKOUT
        self.assertEqual(self._states(steps), [(False, True, False)])
        self.assertEqual(self._jobs(steps), [])

    def test_an_unguarded_input_checkout_with_a_comment_is_still_reported(self):
        # The other fail-closed direction, both spellings: a comment must not
        # buy an unguarded checkout its way OUT of the lint either.
        same_line = (
            "      - name: Load assets\n"
            "        uses: actions/checkout@abc\n"
            "        with:\n"
            "          ref: ${{ inputs.workflows_ref }}  # note\n"
        )
        self.assertEqual(len(self._jobs(same_line)), 1)
        continuation = (
            "      - name: Load assets\n"
            "        uses: actions/checkout@abc\n"
            "        with:\n"
            "          ref:\n"
            "            ${{ inputs.workflows_ref }}  # note\n"
        )
        self.assertEqual(len(self._jobs(continuation)), 1)

    # ------------------------------------------------------------------
    # …and the two places a `#` is NOT a comment, where stripping would move
    # the lint fail-OPEN — the one direction it may never move. Both were
    # reported by the review panel on the first cut of BE-9129, and both are
    # pinned here against the RAW-line reading they restore.
    # ------------------------------------------------------------------

    def test_a_hash_inside_a_block_scalar_body_is_content_not_a_comment(self):
        # A `|`/`>` body is literal text: YAML opens no comment there, so the
        # whole folded line reaches the runtime and this expression resolves to
        # the input. Stripping at the ` #` hid the mention and the checkout
        # left the lint entirely.
        steps = (
            "      - name: Load assets\n"
            "        uses: actions/checkout@abc\n"
            "        with:\n"
            "          ref: >-\n"
            "            ${{ 'foo\n"
            "            bar # baz' && inputs.workflows_ref }}\n"
        )
        self.assertEqual(len(self._jobs(steps)), 1)

    def test_a_quoted_multi_line_ref_scalar_keeps_a_hash_in_its_body(self):
        # Same rule for the other non-plain opener: inside a `ref: "` scalar
        # continued below, the `#` is string content.
        steps = (
            "      - name: Load assets\n"
            "        uses: actions/checkout@abc\n"
            "        with:\n"
            '          ref: "\n'
            '            ${{ format(\'a#b\') != \'\' && inputs.workflows_ref }}"\n'
        )
        self.assertEqual(len(self._jobs(steps)), 1)

    def test_a_plain_multi_line_ref_scalar_still_strips_its_comment(self):
        # The complement, so the gate above cannot be read as "never strip a
        # continuation": the bare `ref:` spelling IS ended by a ` #`, and that
        # is the spelling `COMMENTED_CONSUMER_CONT` exercises. Asserted here on
        # the fail-closed side too — an unguarded input use with a trailing
        # comment is still reported.
        steps = (
            "      - name: Load assets\n"
            "        uses: actions/checkout@abc\n"
            "        with:\n"
            "          ref:\n"
            "            ${{ inputs.workflows_ref }}  # note\n"
        )
        self.assertEqual(len(self._jobs(steps)), 1)

    def test_a_quote_opened_on_the_previous_line_suspends_the_strip(self):
        # `_strip_comment` restarts its quote scan on every physical line, so a
        # flow mapping split across lines has its closing `"` read as an
        # opener-less ` #` — truncating the entry that carries the checkout.
        # The line the one above left mid-scalar is judged RAW instead.
        steps = (
            '      - {name: "foo\n'
            '        bar # baz", uses: actions/checkout@abc, '
            'with: {ref: "${{ inputs.workflows_ref }}"}}\n'
        )
        self.assertEqual(len(self._jobs(steps)), 1)
        # The single-line spelling of the same step agrees — the split is not
        # what decides the verdict.
        one_line = (
            '      - {name: "foo bar # baz", uses: actions/checkout@abc, '
            'with: {ref: "${{ inputs.workflows_ref }}"}}\n'
        )
        self.assertEqual(len(self._jobs(one_line)), 1)

    def test_a_run_body_line_mentioning_the_input_records_no_site(self):
        # `_strip_comment` deliberately OVER-protects a line it cannot prove is
        # YAML (see its docstring): inside a `run: |` body, `# PR #${n}` is
        # literal shell, not a comment. Unchanged by this fix in either
        # direction — no site before, no site after.
        steps = (
            "      - name: Say\n"
            "        run: |\n"
            '          echo "ref: ${{ inputs.workflows_ref }} # PR #${n}"\n'
        )
        self.assertEqual(self._states(steps), [])
        self.assertEqual(self._jobs(steps), [])



class CheckDirTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _write(self, name, text):
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as f:
            f.write(text)

    def test_clean_dir_passes(self):
        self._write("good.yml", _reusable(PINNED))
        self._write("unrelated.yml", "name: F\non: [push]\njobs: {}\n")
        errors, checked, exempt_ok, _ = cwp.check_dir(self.dir, exempt=frozenset())
        self.assertEqual(errors, [])
        self.assertEqual(checked, ["good.yml"])
        self.assertEqual(exempt_ok, [])

    def test_defaulted_dir_fails_with_an_annotation(self):
        self._write("bad.yml", _reusable(DEFAULTED))
        errors, checked, _, _ = cwp.check_dir(self.dir, exempt=frozenset())
        self.assertEqual(len(errors), 1, errors)
        self.assertTrue(errors[0].startswith("::error file="), errors[0])
        self.assertIn("bad.yml", errors[0])
        self.assertIn("BE-5546", errors[0])
        self.assertEqual(checked, ["bad.yml"])

    def test_a_job_workflow_sha_default_is_tolerated(self):
        # BE-4169: `default: ''` paired with `inputs.workflows_ref ||
        # job.workflow_sha` at the checkout is not the `default: main` hole —
        # the fallback can never be mutable. See groom.yml. The guard below is
        # what BE-8077 additionally requires: the carve-out is about the ref
        # not being MUTABLE, and the guard is what makes it non-EMPTY.
        text = (
            "name: Fixture\n"
            "on:\n"
            "  workflow_call:\n"
            "    inputs:\n"
            "      workflows_ref:\n"
            "        type: string\n"
            "        required: false\n"
            "        default: ''\n"
            "jobs:\n"
            "  check:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - name: Require a resolvable workflows_ref\n"
            "        env:\n"
            "          WORKFLOWS_REF: ${{ inputs.workflows_ref || job.workflow_sha }}\n"
            "        run: |\n"
            '          if [ -z "$WORKFLOWS_REF" ]; then\n'
            '            echo "::error::empty"\n'
            "            exit 1\n"
            "          fi\n"
            "      - name: Load assets\n"
            "        uses: actions/checkout@abc\n"
            "        with:\n"
            "          ref: ${{ inputs.workflows_ref || job.workflow_sha }}\n"
        )
        self._write("groom-like.yml", text)
        errors, checked, _, _ = cwp.check_dir(self.dir, exempt=frozenset())
        self.assertEqual(errors, [], errors)
        self.assertEqual(checked, ["groom-like.yml"])

    def test_a_third_operand_is_not_a_self_pin(self):
        # The `default: ''` carve-out half of the same anchoring bug. This
        # fixture LOOKS like the groom shape and is not one: with both leading
        # operands empty it checks out `main`, so the empty default is the
        # BE-5546 mutable-default hole after all. The unanchored regex read the
        # substring and waved it through.
        text = (
            "name: Fixture\n"
            "on:\n"
            "  workflow_call:\n"
            "    inputs:\n"
            "      workflows_ref:\n"
            "        type: string\n"
            "        required: false\n"
            "        default: ''\n"
            "jobs:\n"
            "  check:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - name: Load assets\n"
            "        uses: actions/checkout@abc\n"
            "        with:\n"
            "          ref: ${{ inputs.workflows_ref || job.workflow_sha || 'main' }}\n"
        )
        self._write("sneaky.yml", text)
        errors, checked, _, _ = cwp.check_dir(self.dir, exempt=frozenset())
        self.assertTrue(any("BE-5546" in e and "default:" in e for e in errors), errors)
        self.assertEqual(checked, ["sneaky.yml"])

    def test_a_comment_naming_the_fallback_is_not_a_self_pin(self):
        # `check_dir` asks "does ANY ref checkout self-pin?", so prose merely
        # NAMING the expression -- which this repo's workflows do at length --
        # must not buy the `default: ''` carve-out for a file whose checkout
        # never uses the fallback. Two mechanisms hold this now (the scan is
        # scoped to checkouts, AND comments are stripped from what it reads);
        # the trailing-comment test below is the one that pins the second.
        text = (
            "name: Fixture\n"
            "on:\n"
            "  workflow_call:\n"
            "    inputs:\n"
            "      workflows_ref:\n"
            "        type: string\n"
            "        required: false\n"
            "        default: ''\n"
            "jobs:\n"
            "  check:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - name: Load assets\n"
            "        uses: actions/checkout@abc\n"
            "        with:\n"
            # A COMPLETE `${{ … }}` — spelled without the opening `${{` the
            # fixture cannot exercise the matcher at all — both on a whole-line
            # comment and trailing a real `ref:` line.
            "          # we could have used ${{ inputs.workflows_ref || job.workflow_sha }}\n"
            "          ref: ${{ inputs.workflows_ref }}  # not ${{ inputs.workflows_ref || job.workflow_sha }}\n"
        )
        self._write("commented.yml", text)
        errors, checked, _, _ = cwp.check_dir(self.dir, exempt=frozenset())
        self.assertTrue(any("BE-5546" in e and "default:" in e for e in errors), errors)
        self.assertEqual(checked, ["commented.yml"])

    def test_a_trailing_comment_does_not_hide_a_REAL_self_pin(self):
        # The other direction, and the one that actually pins the stripping:
        # the matcher anchors the fallback to the whole YAML value, so an
        # unstripped `# pinned` sits between the expression and the end of the
        # line and the genuine self-pin stops being recognized — costing a
        # compliant file its `default: ''` carve-out.
        text = (
            "name: Fixture\n"
            "on:\n"
            "  workflow_call:\n"
            "    inputs:\n"
            "      workflows_ref:\n"
            "        type: string\n"
            "        required: false\n"
            "        default: ''\n"
            "jobs:\n"
            "  check:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - name: Require a resolvable workflows_ref\n"
            "        env:\n"
            "          WORKFLOWS_REF: ${{ inputs.workflows_ref || job.workflow_sha }}\n"
            "        run: |\n"
            "          if [ -z \"$WORKFLOWS_REF\" ]; then\n"
            "            exit 1\n"
            "          fi\n"
            "      - name: Load assets\n"
            "        uses: actions/checkout@abc\n"
            "        with:\n"
            "          ref: ${{ inputs.workflows_ref || job.workflow_sha }}  # pinned\n"
        )
        self._write("pinned.yml", text)
        errors, checked, _, _ = cwp.check_dir(self.dir, exempt=frozenset())
        self.assertEqual(errors, [])
        self.assertEqual(checked, ["pinned.yml"])

    def test_a_block_scalar_self_pin_keeps_the_default_carve_out(self):
        # `check_dir`'s carve-out asks the PARSER, not each line in isolation.
        # A per-line scan cannot see this spelling — the key line carries no
        # expression, the continuation line carries no `ref:` key — so the file
        # lost the carve-out and got BE-5546's "delete the default" while its
        # checkouts got BE-8077's "the fallback IS recognized": a false failure
        # carrying contradictory advice.
        text = (
            "name: Fixture\n"
            "on:\n"
            "  workflow_call:\n"
            "    inputs:\n"
            "      workflows_ref:\n"
            "        type: string\n"
            "        required: false\n"
            "        default: ''\n"
            "jobs:\n"
            "  check:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - name: Require a resolvable workflows_ref\n"
            "        env:\n"
            "          WORKFLOWS_REF: ${{ inputs.workflows_ref || job.workflow_sha }}\n"
            "        run: |\n"
            "          if [ -z \"$WORKFLOWS_REF\" ]; then\n"
            "            exit 1\n"
            "          fi\n"
            "      - name: Load assets\n"
            "        uses: actions/checkout@abc\n"
            "        with:\n"
            "          ref: >-\n"
            "            ${{ inputs.workflows_ref || job.workflow_sha }}\n"
        )
        self._write("folded.yml", text)
        errors, checked, _, _ = cwp.check_dir(self.dir, exempt=frozenset())
        self.assertEqual(errors, [])
        self.assertEqual(checked, ["folded.yml"])


    def test_the_github_job_workflow_sha_default_is_no_longer_tolerated(self):
        # BE-8077: the same fixture spelled the OLD way is the `default: main`
        # hole in disguise — `github.job_workflow_sha` expands to '' on every
        # runner, so an omitted input self-pins to the default branch. Both
        # halves must fire: the tolerated-default carve-out and the
        # unguarded-checkout exemption.
        text = (
            "name: Fixture\n"
            "on:\n"
            "  workflow_call:\n"
            "    inputs:\n"
            "      workflows_ref:\n"
            "        type: string\n"
            "        required: false\n"
            "        default: ''\n"
            "jobs:\n"
            "  check:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - name: Load assets\n"
            "        uses: actions/checkout@abc\n"
            "        with:\n"
            "          ref: ${{ inputs.workflows_ref || github.job_workflow_sha }}\n"
        )
        self._write("stale-spelling.yml", text)
        errors, checked, _, _ = cwp.check_dir(self.dir, exempt=frozenset())
        self.assertEqual(len(errors), 2, errors)
        self.assertEqual(checked, ["stale-spelling.yml"])

    def test_a_default_without_the_fallback_is_still_reported(self):
        # The exemption is conditional on the fallback actually being present:
        # `default: ''` alone, with an unfallback'd checkout, is the ordinary
        # `default: main` hole (just spelled with an empty string) and must
        # still fail both checks.
        text = (
            "name: Fixture\n"
            "on:\n"
            "  workflow_call:\n"
            "    inputs:\n"
            "      workflows_ref:\n"
            "        type: string\n"
            "        required: false\n"
            "        default: ''\n"
            "jobs:\n"
            "  check:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - name: Load assets\n"
            "        uses: actions/checkout@abc\n"
            "        with:\n"
            "          ref: ${{ inputs.workflows_ref }}\n"
        )
        self._write("bad.yml", text)
        errors, checked, _, _ = cwp.check_dir(self.dir, exempt=frozenset())
        self.assertEqual(len(errors), 2, errors)

    def test_exempt_workflow_is_tolerated(self):
        self._write("legacy.yml", _reusable(DEFAULTED))
        errors, checked, exempt_ok, _ = cwp.check_dir(self.dir, exempt=frozenset({"legacy.yml"}))
        self.assertEqual(errors, [])
        self.assertEqual(exempt_ok, ["legacy.yml"])
        self.assertEqual(checked, ["legacy.yml"])

    def test_stale_exemption_fails_so_the_list_drains(self):
        self._write("legacy.yml", _reusable(PINNED))
        errors, _, exempt_ok, _ = cwp.check_dir(self.dir, exempt=frozenset({"legacy.yml"}))
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("KNOWN_EXEMPT", errors[0])
        self.assertEqual(exempt_ok, [])

    def test_a_lost_declaration_is_an_error_not_a_silent_skip(self):
        # The file plainly USES the input, so a declaration must exist. If the
        # text parser cannot find it, the file is uncovered — which must look
        # different from "not applicable", not identical to it.
        self._write(
            "unparseable.yml",
            "name: F\n"
            "on:\n"
            "  workflow_call:\n"
            "jobs:\n"
            "  j:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - uses: actions/checkout@abc\n"
            "        with:\n"
            "          ref: ${{ inputs.workflows_ref }}\n",
        )
        errors, checked, _, _ = cwp.check_dir(self.dir, exempt=frozenset())
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("NOT covering this file", errors[0])
        self.assertEqual(checked, [])

    def test_a_lost_declaration_is_caught_through_a_flow_mapping_use(self):
        # Same uncovered file, with its only use written in flow style. Before
        # the flow patterns this returned zero errors — the loudest failure the
        # checker has, silenced by a pair of braces.
        self._write(
            "unparseable.yml",
            "name: F\n"
            "on:\n"
            "  workflow_call:\n"
            "jobs:\n"
            "  j:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - uses: actions/checkout@abc\n"
            '        with: {repository: Comfy-Org/github-workflows, ref: "${{ inputs.workflows_ref }}"}\n',
        )
        errors, checked, _, _ = cwp.check_dir(self.dir, exempt=frozenset())
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("NOT covering this file", errors[0])
        self.assertEqual(checked, [])

    def test_a_lost_declaration_is_caught_through_a_bracket_use(self):
        # Backstop parity for index access (BE-8146): a file whose only use is
        # `inputs['workflows_ref']` was a silent skip, so a lost declaration
        # there looked exactly like "not applicable" — the one thing this error
        # exists to prevent.
        self._write(
            "unparseable.yml",
            "name: F\n"
            "on:\n"
            "  workflow_call:\n"
            "jobs:\n"
            "  j:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - uses: actions/checkout@abc\n"
            "        with:\n"
            "          ref: ${{ inputs['workflows_ref'] }}\n",
        )
        errors, checked, _, _ = cwp.check_dir(self.dir, exempt=frozenset())
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("NOT covering this file", errors[0])
        self.assertEqual(checked, [])

    def test_a_lost_declaration_is_caught_through_a_block_scalar_use(self):
        # And once more with the value on the next line — the third spelling of
        # the same use, which has to stay just as loud as the other two.
        self._write(
            "unparseable.yml",
            "name: F\n"
            "on:\n"
            "  workflow_call:\n"
            "jobs:\n"
            "  j:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - uses: actions/checkout@abc\n"
            "        with:\n"
            "          ref: >-\n"
            "            ${{ inputs.workflows_ref }}\n",
        )
        errors, checked, _, _ = cwp.check_dir(self.dir, exempt=frozenset())
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("NOT covering this file", errors[0])
        self.assertEqual(checked, [])

    def test_an_unguarded_block_scalar_checkout_fails_the_lint(self):
        # End to end: a declared, default-free workflow whose only checkout is
        # written vertically and unguarded still exits non-zero.
        self._write(
            "leaky.yml",
            _reusable(PINNED).replace(
                "      - run: echo hi\n",
                "      - uses: actions/checkout@abc\n"
                "        with:\n"
                "          ref: >-\n"
                "            ${{ inputs.workflows_ref }}\n",
            ),
        )
        errors, checked, _, _ = cwp.check_dir(self.dir, exempt=frozenset())
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("no empty-ref guard", errors[0])
        self.assertEqual(checked, ["leaky.yml"])

    def test_a_lost_declaration_is_caught_through_a_commented_ref_key(self):
        # The fourth spelling: a comment where the value would go. This one was
        # doubly silent — invisible to the guard scan AND to this backstop, so a
        # whole uncovered file passed clean rather than failing loudly.
        self._write(
            "unparseable.yml",
            "name: F\n"
            "on:\n"
            "  workflow_call:\n"
            "jobs:\n"
            "  j:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - uses: actions/checkout@abc\n"
            "        with:\n"
            "          ref:  # the pinned ref\n"
            "            ${{ inputs.workflows_ref }}\n",
        )
        errors, checked, _, _ = cwp.check_dir(self.dir, exempt=frozenset())
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("NOT covering this file", errors[0])
        self.assertEqual(checked, [])

    def test_an_unguarded_commented_ref_checkout_fails_the_lint(self):
        self._write(
            "leaky.yml",
            _reusable(PINNED).replace(
                "      - run: echo hi\n",
                "      - uses: actions/checkout@abc\n"
                "        with:\n"
                "          ref:  # the pinned ref\n"
                "            ${{ inputs.workflows_ref }}\n",
            ),
        )
        errors, checked, _, _ = cwp.check_dir(self.dir, exempt=frozenset())
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("no empty-ref guard", errors[0])
        self.assertEqual(checked, ["leaky.yml"])

    def test_an_unrelated_workflow_is_still_a_silent_skip(self):
        self._write("unrelated.yml", "name: F\non: [push]\njobs: {}\n")
        errors, checked, _, _ = cwp.check_dir(self.dir, exempt=frozenset())
        self.assertEqual((errors, checked), ([], []))

    def test_an_unguarded_ref_checkout_fails_the_lint(self):
        self._write(
            "leaky.yml",
            _reusable(PINNED).replace(
                "      - run: echo hi\n",
                "      - uses: actions/checkout@abc\n"
                "        with:\n"
                "          ref: ${{ inputs.workflows_ref }}\n",
            ),
        )
        errors, checked, _, _ = cwp.check_dir(self.dir, exempt=frozenset())
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("no empty-ref guard", errors[0])
        self.assertEqual(checked, ["leaky.yml"])

    def test_an_exempt_workflow_is_not_held_to_the_guard(self):
        # It still has its default, so an omitted input never reaches checkout
        # as '' — the guard only becomes required when the default goes.
        self._write(
            "legacy.yml",
            _reusable(DEFAULTED).replace(
                "      - run: echo hi\n",
                "      - uses: actions/checkout@abc\n"
                "        with:\n"
                "          ref: ${{ inputs.workflows_ref }}\n",
            ),
        )
        errors, _, exempt_ok, _ = cwp.check_dir(self.dir, exempt=frozenset({"legacy.yml"}))
        self.assertEqual(errors, [])
        self.assertEqual(exempt_ok, ["legacy.yml"])

    def test_an_exemption_for_a_missing_file_fails(self):
        # Rename or delete the workflow and the entry would otherwise survive
        # forever, pre-exempting whatever later reuses the filename.
        self._write("good.yml", _reusable(PINNED))
        errors, _, _, _ = cwp.check_dir(self.dir, exempt=frozenset({"renamed-away.yml"}))
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("renamed-away.yml", errors[0])
        self.assertIn("KNOWN_EXEMPT", errors[0])

    def test_an_exemption_for_a_workflow_that_dropped_the_input_fails(self):
        self._write("legacy.yml", "name: F\non: [push]\njobs: {}\n")
        errors, _, _, _ = cwp.check_dir(self.dir, exempt=frozenset({"legacy.yml"}))
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("KNOWN_EXEMPT", errors[0])

    def test_the_real_known_exempt_list_is_not_stale(self):
        # KNOWN_EXEMPT is checked against the real tree by the default run too;
        # this pins it so a rename cannot quietly widen the exemption.
        root = os.path.normpath(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "workflows")
        )
        errors, checked, exempt_ok, _ = cwp.check_dir(root)
        self.assertEqual(errors, [], errors)
        self.assertEqual(sorted(exempt_ok), sorted(cwp.KNOWN_EXEMPT))

    def test_this_repos_own_workflows_pass(self):
        # The real forcing function: the checked-in tree must stay clean.
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "workflows")
        errors, checked, _, notices = cwp.check_dir(os.path.normpath(root))
        self.assertEqual(errors, [], errors)
        # …and FULLY judged: every `steps:` in this tree is a bare block
        # sequence, so a warning here is a regression in the BE-9045 collector
        # (or a job that really did stop being judged), never a finding.
        self.assertEqual(notices, [], notices)
        for name in ("cursor-review.yml", "groom.yml", "agents-md-integrity.yml", "pr-size.yml"):
            self.assertIn(name, checked)

    # ------------------------------------------------------------------
    # BE-9045: the BE-8254 fail-open is announced, never enforced.
    # ------------------------------------------------------------------

    # `on: workflow_call` with a `workflows_ref` input, so the file is CHECKED
    # at all — `check_dir` skips a file whose input it cannot find.
    _ESCAPED_HEAD = (
        "name: Escaped\n"
        "on:\n"
        "  workflow_call:\n"
        "    inputs:\n"
        "      workflows_ref:\n"
        "        type: string\n"
        "        required: true\n"
        "jobs:\n"
    )

    def test_an_escaped_steps_job_with_a_dropped_site_emits_a_warning(self):
        # The whole point: the drop stays a drop (no error, exit 0), but it is
        # now NAMED — the job, its `steps:` line, and how many checkouts went
        # unjudged.
        text = self._ESCAPED_HEAD + GuardCoverageTests.FLOW_STEPS_JOB
        self._write("escaped.yml", text)
        errors, checked, _, notices = cwp.check_dir(self.dir, exempt=frozenset())
        self.assertEqual(errors, [], errors)
        self.assertEqual(checked, ["escaped.yml"])
        self.assertEqual(len(notices), 1, notices)
        self.assertTrue(notices[0].startswith("::warning file="), notices[0])
        self.assertIn("job `job0`", notices[0])
        self.assertIn("BE-9045", notices[0])
        rows = text.split("\n")
        steps_line = next(
            i for i, row in enumerate(rows) if row.lstrip().startswith("steps:")
        ) + 1
        self.assertIn("line=%d" % steps_line, notices[0])

    def test_a_flow_step_binding_in_an_escaped_steps_job_is_counted_as_dropped(self):
        # The BE-9098 line — one flow mapping binding `WORKFLOWS_REF` AND
        # reading a step output — inside a `steps: [ … ]` job. It used to take
        # the binding arm and stop there, which cost it BOTH the verdict and
        # the drop: nothing reached `_record_steps_output`, so the BE-9045
        # collector recorded nothing and this job's lost coverage was silent.
        # Now it is counted, and the run stays green (a drop is lost coverage,
        # never a finding).
        text = self._ESCAPED_HEAD + (
            "  job0:\n"
            "    runs-on: ubuntu-latest\n"
            '    steps: [{uses: actions/checkout@abc,'
            ' env: {WORKFLOWS_REF: "${{ inputs.workflows_ref }}"},'
            ' with: {ref: "${{ steps.resolve_reff.outputs.ref }}"}}]\n'
        )
        self._write("escaped.yml", text)
        errors, checked, _, notices = cwp.check_dir(self.dir, exempt=frozenset())
        self.assertEqual(errors, [], errors)
        self.assertEqual(checked, ["escaped.yml"])
        self.assertEqual(len(notices), 1, notices)
        self.assertTrue(notices[0].startswith("::warning file="), notices[0])
        self.assertIn("job `job0`", notices[0])
        self.assertIn("BE-9045", notices[0])
        self.assertIn("could NOT run for 1 `ref:` site(s)", notices[0])
        rows = text.split("\n")
        steps_line = next(
            i for i, row in enumerate(rows) if row.lstrip().startswith("steps:")
        ) + 1
        self.assertIn("line=%d" % steps_line, notices[0])

    def test_an_escaped_steps_job_with_no_consumer_emits_nothing(self):
        # The same unreadable `steps:` shape, with nothing reading a step
        # output: `_record_steps_output`'s drop branch is never reached, so
        # this job lost no coverage and must stay silent. A warning per
        # flow-`steps:` job would be noise, not signal.
        self._write(
            "quiet.yml",
            self._ESCAPED_HEAD
            + "  job0:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps: [{uses: actions/checkout@abc, with: {ref: main}}]\n",
        )
        errors, _, _, notices = cwp.check_dir(self.dir, exempt=frozenset())
        self.assertEqual(errors, [], errors)
        self.assertEqual(notices, [])

    def test_a_readable_steps_job_emits_no_notice(self):
        # The control: the identical typo'd id under a block-sequence `steps:`
        # is JUDGED — a dangling error, and no warning, because nothing was
        # skipped.
        self._write(
            "readable.yml",
            self._ESCAPED_HEAD
            + "  job0:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - id: resolve_ref\n"
            "        run: echo hi\n"
            "      - uses: actions/checkout@abc\n"
            "        with:\n"
            '          ref: "${{ steps.resolve_reff.outputs.ref }}"\n',
        )
        errors, _, _, notices = cwp.check_dir(self.dir, exempt=frozenset())
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("BE-8215", errors[0])
        self.assertEqual(notices, [])

    def test_a_warning_counts_the_sites_the_job_lost(self):
        # The count is per `ref:` SITE, which is per LINE — the unit
        # `_record_steps_output` itself works in (at most one `found` entry per
        # call). A flow sequence broken over two physical lines therefore
        # counts two, and the message names the unit so the number cannot be
        # misread as a step count.
        self._write(
            "two.yml",
            self._ESCAPED_HEAD
            + "  job0:\n"
            "    runs-on: ubuntu-latest\n"
            '    steps: [{uses: actions/checkout@abc, with: {ref: "${{ steps.t1.outputs.ref }}"}},\n'
            '            {uses: actions/checkout@abc, with: {ref: "${{ steps.t2.outputs.ref }}"}}]\n',
        )
        errors, _, _, notices = cwp.check_dir(self.dir, exempt=frozenset())
        self.assertEqual(errors, [], errors)
        self.assertEqual(len(notices), 1, notices)
        self.assertIn("could NOT run for 2 `ref:` site(s)", notices[0])
        # …and it says so as LOST COVERAGE, not as a finding against those
        # refs: the escape makes a dangling id and a real earlier out-of-scope
        # step indistinguishable, and the readable path drops the second one
        # silently too.
        self.assertIn("lost coverage, not a finding", notices[0])

    def test_notices_never_fail_the_run(self):
        # The CLI half, end to end: the warning reaches stdout (so it lands in
        # the PR annotations) and the process still exits 0. Asserting it on
        # `check_dir` alone would leave `main` free to reintroduce the failure.
        self._write(
            "escaped.yml", self._ESCAPED_HEAD + GuardCoverageTests.FLOW_STEPS_JOB
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = cwp.main(["--workflows-dir", self.dir])
        self.assertEqual(status, 0, out.getvalue())
        self.assertIn("::warning", out.getvalue())
        self.assertIn("every JUDGED ref checkout guarded", out.getvalue())

    def test_only_a_site_left_with_no_verdict_is_recorded(self):
        # The collector's contract, at the unit: record a site the escape left
        # with NO `found` entry, and nothing else. Both halves matter.
        #
        # (a) Every operand swallowed -> one record, and ONE, not one per
        #     operand: a site is a LINE here, the unit `_record_steps_output`
        #     itself works in.
        found, dropped = [], []
        lines = [
            "jobs:",
            "  job0:",
            "    steps: &anchored",
            "      - uses: actions/checkout@abc",
            "        with:",
            '          ref: "${{ steps.a.outputs.r || steps.b.outputs.r }}"',
        ]
        cwp._record_steps_output(
            found, lines, 5,
            [("a", "r", True), ("b", "r", True)],
            set(), None, drop=(1, 2, dropped),
        )
        self.assertEqual(found, [])
        self.assertEqual(dropped, [(1, 2, 5)])

        # (b) A sibling operand naming a tracked resolver still earns the site
        #     a `found` entry — it WAS judged, by the operand this reader could
        #     read — so recording it too would let one `ref:` line carry an
        #     `::error` and a `::warning` saying it went unjudged.
        found, dropped = [], []
        cwp._record_steps_output(
            found, lines, 5,
            [("a", "r", True), ("b", "r", False)],
            {"b"}, None, drop=(1, 2, dropped),
        )
        self.assertEqual(len(found), 1, found)
        self.assertEqual(dropped, [])
        # No end-to-end fixture for (b) on purpose: `_step_bounds` refuses the
        # same `steps:` shapes `_job_step_ids` does, so no resolver registers
        # inside an escaped job today and `resolvers` is empty there. The
        # guard keeps the count honest if those two readers ever diverge —
        # they are separate walks over the same key.

    def test_an_anchor_on_the_job_key_line_is_not_printed_as_the_job_name(self):
        # `  job0: &common` is one of the three escape shapes the message
        # itself enumerates, so it is a ROUTINE way to reach this annotation —
        # and trimming only a trailing `:` printed "job `job0: &common`" in
        # the one annotation whose whole job is to name the job.
        self._write(
            "anchor.yml",
            self._ESCAPED_HEAD
            + "  job0: &common\n"
            "    runs-on: ubuntu-latest\n"
            '    steps: [{uses: actions/checkout@abc, with: {ref: "${{ steps.t1.outputs.ref }}"}}]\n',
        )
        _, _, _, notices = cwp.check_dir(self.dir, exempt=frozenset())
        self.assertEqual(len(notices), 1, notices)
        self.assertIn("job `job0`:", notices[0])
        self.assertNotIn("&common", notices[0])

    def test_a_quoted_job_key_keeps_its_own_colon(self):
        # The cut is at the key's own MAPPING colon, found on the shared
        # quote-aware scan — not at the first `:` on the line, which would
        # slice a quoted key in half.
        self._write(
            "quoted.yml",
            self._ESCAPED_HEAD
            + '  "deploy: prod":\n'
            "    runs-on: ubuntu-latest\n"
            '    steps: [{uses: actions/checkout@abc, with: {ref: "${{ steps.t1.outputs.ref }}"}}]\n',
        )
        _, _, _, notices = cwp.check_dir(self.dir, exempt=frozenset())
        self.assertEqual(len(notices), 1, notices)
        self.assertIn("job `deploy: prod`:", notices[0])

    def test_annotation_values_are_escaped(self):
        # `path`/`name` come from a directory listing and git permits newlines
        # and commas in a filename. Unescaped, a crafted name walks out of
        # `file=` — or, since notices print AHEAD of the errors, starts a line
        # with `::stop-commands::` and suppresses the error annotations after
        # it. The escape is a no-op for every real workflow filename.
        self.assertEqual(cwp._ann_msg("plain.yml"), "plain.yml")
        self.assertEqual(cwp._ann_prop(".github/workflows/a.yml"), ".github/workflows/a.yml")
        self.assertEqual(
            cwp._ann_msg("a\n::stop-commands::b"), "a%0A::stop-commands::b"
        )
        self.assertEqual(cwp._ann_prop("a,b:c%d"), "a%2Cb%3Ac%25d")
        # …and the emitter actually uses them.
        self._write(
            "esc,ape.yml",
            self._ESCAPED_HEAD + GuardCoverageTests.FLOW_STEPS_JOB,
        )
        _, _, _, notices = cwp.check_dir(self.dir, exempt=frozenset())
        self.assertEqual(len(notices), 1, notices)
        self.assertIn("esc%2Cape.yml,line=", notices[0])

    def test_the_drop_collector_cannot_travel_without_its_job(self):
        # `_record_steps_output`'s out-parameter is ONE bundle, not three
        # independently-defaulting arguments: passing the list alone used to
        # collect `(None, None, idx)`, and `lines[None]` is an exit-1
        # `TypeError` out of the one path whose entire purpose is non-fatal.
        import inspect

        params = list(inspect.signature(cwp._record_steps_output).parameters)
        self.assertNotIn("job_start", params)
        self.assertNotIn("job_indent", params)
        self.assertIn("drop", params)
        # And the coordinates that DO arrive are always real ones.
        lines = (
            self._ESCAPED_HEAD + GuardCoverageTests.FLOW_STEPS_JOB
        ).split("\n")
        dropped = []
        cwp.ref_checkouts(lines, dropped=dropped)
        self.assertTrue(dropped)
        for job_start, job_indent, idx in dropped:
            self.assertIsInstance(job_start, int)
            self.assertIsInstance(job_indent, int)
            self.assertIsInstance(idx, int)


if __name__ == "__main__":
    unittest.main(verbosity=2)
