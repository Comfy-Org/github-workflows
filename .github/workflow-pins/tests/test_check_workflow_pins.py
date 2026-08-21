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
    # itself. It is a real guard, so its consumers need no `if:` at all.
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

    def test_a_hard_guard_resolver_covers_its_consumer_without_an_if(self):
        # Nothing empty can leave a resolver that exits non-zero on it, so the
        # consumer needs no `if:`. (The never-fail idiom cannot take this route:
        # `continue-on-error: true` means the `exit 1` does not fail the job and
        # the checkout runs anyway — which `is_guard_step` already refuses.)
        self.assertEqual(self._jobs(self.HARD_RESOLVER + self._step_output_checkout()), [])

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
        errors, _, _ = cwp.check_dir(tmp, exempt=frozenset())
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
        errors, _, _ = cwp.check_dir(tmp, exempt=frozenset())
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
        errors, _, _ = cwp.check_dir(tmp, exempt=frozenset())
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
        # must be covered — a fail-closed resolver each, since one step can
        # carry only one exact `if:`.
        ref = "${{ steps.lookup.outputs.ref }}${{ steps.resolve_ref.outputs.ref }}"
        checkout = self._step_output_checkout(ref='"%s"' % ref)
        both_hard = self._second_resolver(self.HARD_RESOLVER) + self.HARD_RESOLVER
        self.assertEqual(self._jobs(both_hard + checkout), [])
        # Cover only the SECOND — the one the old reader judged — and the site
        # must still fail on the first.
        one_hard = self._second_resolver(self.RESOLVER) + self.HARD_RESOLVER
        self.assertEqual(len(self._jobs(one_hard + checkout)), 1)
        # …and only the first: the same failure, from the other side.
        other_hard = self._second_resolver(self.HARD_RESOLVER) + self.RESOLVER
        self.assertEqual(len(self._jobs(other_hard + checkout)), 1)

    def test_every_or_operand_must_be_covered(self):
        # `A || B` with both operands step outputs: the fallback stretch
        # swallowed `B`, so a covered `A` passed the whole site while `B`
        # reached an output nothing had judged. An unresolved `A` really is
        # falsey, so `B` really can be the ref.
        ref = "${{ steps.lookup.outputs.ref || steps.resolve_ref.outputs.ref }}"
        checkout = self._step_output_checkout(ref=ref)
        both_hard = self._second_resolver(self.HARD_RESOLVER) + self.HARD_RESOLVER
        self.assertEqual(self._jobs(both_hard + checkout), [])
        # Leading operand covered, trailing one not — the shape that used to
        # pass. A covered sibling excuses nothing.
        lead_hard = self._second_resolver(self.HARD_RESOLVER) + self.RESOLVER
        self.assertEqual(len(self._jobs(lead_hard + checkout)), 1)
        # …and the trailing operand is not reported `'non-leading'` merely for
        # sitting behind another step output: `||` falls through an empty one.
        self.assertEqual(self._states(lead_hard + checkout), [(False, False, True)])

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
        # The lint takes a fail-closed resolver as an ALTERNATIVE to the
        # consumer's `if:`, so deleting `continue-on-error: true` here and
        # exiting non-zero on an unresolvable ref would keep the lint green
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
        errors, checked, exempt_ok = cwp.check_dir(self.dir, exempt=frozenset())
        self.assertEqual(errors, [])
        self.assertEqual(checked, ["good.yml"])
        self.assertEqual(exempt_ok, [])

    def test_defaulted_dir_fails_with_an_annotation(self):
        self._write("bad.yml", _reusable(DEFAULTED))
        errors, checked, _ = cwp.check_dir(self.dir, exempt=frozenset())
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
        errors, checked, _ = cwp.check_dir(self.dir, exempt=frozenset())
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
        errors, checked, _ = cwp.check_dir(self.dir, exempt=frozenset())
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
        errors, checked, _ = cwp.check_dir(self.dir, exempt=frozenset())
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
        errors, checked, _ = cwp.check_dir(self.dir, exempt=frozenset())
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
        errors, checked, _ = cwp.check_dir(self.dir, exempt=frozenset())
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
        errors, checked, _ = cwp.check_dir(self.dir, exempt=frozenset())
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
        errors, checked, _ = cwp.check_dir(self.dir, exempt=frozenset())
        self.assertEqual(len(errors), 2, errors)

    def test_exempt_workflow_is_tolerated(self):
        self._write("legacy.yml", _reusable(DEFAULTED))
        errors, checked, exempt_ok = cwp.check_dir(self.dir, exempt=frozenset({"legacy.yml"}))
        self.assertEqual(errors, [])
        self.assertEqual(exempt_ok, ["legacy.yml"])
        self.assertEqual(checked, ["legacy.yml"])

    def test_stale_exemption_fails_so_the_list_drains(self):
        self._write("legacy.yml", _reusable(PINNED))
        errors, _, exempt_ok = cwp.check_dir(self.dir, exempt=frozenset({"legacy.yml"}))
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
        errors, checked, _ = cwp.check_dir(self.dir, exempt=frozenset())
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
        errors, checked, _ = cwp.check_dir(self.dir, exempt=frozenset())
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
        errors, checked, _ = cwp.check_dir(self.dir, exempt=frozenset())
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
        errors, checked, _ = cwp.check_dir(self.dir, exempt=frozenset())
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
        errors, checked, _ = cwp.check_dir(self.dir, exempt=frozenset())
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
        errors, checked, _ = cwp.check_dir(self.dir, exempt=frozenset())
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("no empty-ref guard", errors[0])
        self.assertEqual(checked, ["leaky.yml"])

    def test_an_unrelated_workflow_is_still_a_silent_skip(self):
        self._write("unrelated.yml", "name: F\non: [push]\njobs: {}\n")
        errors, checked, _ = cwp.check_dir(self.dir, exempt=frozenset())
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
        errors, checked, _ = cwp.check_dir(self.dir, exempt=frozenset())
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
        errors, _, exempt_ok = cwp.check_dir(self.dir, exempt=frozenset({"legacy.yml"}))
        self.assertEqual(errors, [])
        self.assertEqual(exempt_ok, ["legacy.yml"])

    def test_an_exemption_for_a_missing_file_fails(self):
        # Rename or delete the workflow and the entry would otherwise survive
        # forever, pre-exempting whatever later reuses the filename.
        self._write("good.yml", _reusable(PINNED))
        errors, _, _ = cwp.check_dir(self.dir, exempt=frozenset({"renamed-away.yml"}))
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("renamed-away.yml", errors[0])
        self.assertIn("KNOWN_EXEMPT", errors[0])

    def test_an_exemption_for_a_workflow_that_dropped_the_input_fails(self):
        self._write("legacy.yml", "name: F\non: [push]\njobs: {}\n")
        errors, _, _ = cwp.check_dir(self.dir, exempt=frozenset({"legacy.yml"}))
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("KNOWN_EXEMPT", errors[0])

    def test_the_real_known_exempt_list_is_not_stale(self):
        # KNOWN_EXEMPT is checked against the real tree by the default run too;
        # this pins it so a rename cannot quietly widen the exemption.
        root = os.path.normpath(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "workflows")
        )
        errors, checked, exempt_ok = cwp.check_dir(root)
        self.assertEqual(errors, [], errors)
        self.assertEqual(sorted(exempt_ok), sorted(cwp.KNOWN_EXEMPT))

    def test_this_repos_own_workflows_pass(self):
        # The real forcing function: the checked-in tree must stay clean.
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "workflows")
        errors, checked, _ = cwp.check_dir(os.path.normpath(root))
        self.assertEqual(errors, [], errors)
        for name in ("cursor-review.yml", "groom.yml", "agents-md-integrity.yml", "pr-size.yml"):
            self.assertIn(name, checked)


if __name__ == "__main__":
    unittest.main(verbosity=2)
