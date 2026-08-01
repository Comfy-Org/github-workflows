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

    def _jobs(self, *jobs):
        text = "name: F\non:\n  workflow_call:\njobs:\n"
        for i, steps in enumerate(jobs):
            text += "  job%d:\n    runs-on: ubuntu-latest\n    steps:\n%s" % (i, steps)
        return cwp.find_unguarded_ref_checkouts(text.split("\n"))

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

    def test_this_repos_own_workflows_guard_every_ref_checkout(self):
        root = os.path.normpath(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "workflows")
        )
        seen = 0
        for name in ("cursor-review.yml", "groom.yml", "agents-md-integrity.yml"):
            with open(os.path.join(root, name), encoding="utf-8") as f:
                lines = f.read().split("\n")
            uses = [line for line in lines if cwp.is_ref_use(line)]
            self.assertTrue(uses, "%s: no ref checkout found — fixture drifted" % name)
            seen += len(uses)
            self.assertEqual(cwp.find_unguarded_ref_checkouts(lines), [], name)
        self.assertEqual(seen, 12, "expected the 12 guarded sites BE-5546 fixed")


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
        for name in ("cursor-review.yml", "groom.yml", "agents-md-integrity.yml"):
            self.assertIn(name, checked)


if __name__ == "__main__":
    unittest.main(verbosity=2)
