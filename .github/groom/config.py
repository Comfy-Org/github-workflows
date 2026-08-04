#!/usr/bin/env python3
"""Resolve groom's OPERATIONAL knobs from a JSON Actions variable (BE-5227).

Every groom knob used to be a `with:` input on the caller, so retuning one —
raise the builder PR ceiling, narrow the scan to a package, pause filing —
meant editing a workflow file in the consumer repo, opening a PR, and waiting
for review. `interval_days` already escaped that via the `GROOM_INTERVAL_DAYS`
Actions variable (BE-4004); this module generalizes that one-off into a single
`GROOM_CONFIG` JSON variable covering every operational knob.

Precedence, highest first:

  1. `workflow_dispatch` inputs  — a one-run override an operator types in the UI
  2. `GROOM_CONFIG` (this file)  — the live, no-PR config layer
  3. the caller's `with:` inputs — the reviewed default committed in the repo
  4. the reusable's own defaults

so an existing caller that sets nothing keeps behaving EXACTLY as before, and a
repo opts into variable-driven config purely by setting the variable.

WHAT THIS DELIBERATELY CANNOT SET (`_LOCKED_KEYS`)
--------------------------------------------------
An Actions variable is editable by anyone with repo write — the same principals
who could edit the workflow file — but editing it BYPASSES PR REVIEW, and unlike
a commit it leaves no diff on a branch. So the knobs that define the auto-builder's
security boundary stay in the reviewed workflow file and are IGNORED here, loudly:

  * `builder`       — flipping the auto-builder on is a decision to let an agent
                      author code in this repo. That belongs in a reviewed commit.
  * `sink`, `bot_app_id`, `workflows_ref`, `config` — identity, the pinned
                      asset ref, and this input itself: pinning is the
                      reproducibility contract, not an operational dial.

WHAT `_OPERATIONAL_KEYS` CAN DO — INCLUDING THE ONE KNOB THAT WIDENS (BE-6345)
------------------------------------------------------------------------------
Nearly every operational knob can only make groom scan less, propose less, or
file less. `pr_size_limit` is the ONE exception, and it is deliberate: raising it
by variable admits a LARGER machine-written patch into a PR that a lower ceiling
would have bailed to an issue. So this layer is not a narrow-only layer, and the
bypass-PR-review property described above genuinely applies to that knob.

Why that is accepted rather than compensated for with a clamp:

  * `pr_size_limit` is NOT the control that stops a machine-written patch from
    landing. Builder PRs are opened by the bot with the `cursor-review` label,
    run full CI + cursor-review, and are NEVER auto-merged — groom.yml opens
    them with a plain `gh pr create` and contains no `gh pr merge` and no
    auto-merge enablement anywhere. A human approves and merges every one.
  * The size limit is therefore a convenience backstop: it keeps an
    unreviewably-large diff out of the review queue in the first place. Raising
    it means a bigger diff REACHES A HUMAN, not that a bigger diff lands
    unreviewed.
  * The alternative was worse in practice — one shared default for every caller,
    or a reviewed commit per repo for a value operators retune the same way they
    retune every other dial. That friction is what this module exists to remove.

Because a variable-set value leaves no diff and no branch, the run record is the
only place the effective ceiling is visible: the gate job echoes the whole
resolved knob set (`Resolved knobs: …`), `pr_size_limit` included, on every run.

A typo must not silently disable PR-opening, which is why the coercion is
`nonneg_int` rather than the `numeric_string` its neighbour `max_prs` uses —
see `_OPERATIONAL_KEYS` for that reasoning.

The lock applies to the VARIABLE and `config` layers, not to the caller-defaults
layer, which is the reviewed workflow file itself — the thing the lock protects.
So a locked key arriving in `--defaults-json` passes through untouched
(`coerce_layer(..., trusted=True)`); rejecting it there would strip a reviewed
`with:` value out of the resolved set and fire the bypass warning on every normal
run, teaching operators to ignore the one annotation that flags a real attempt.

FAIL-OPEN, ALWAYS
-----------------
This runs before the finder, i.e. before anything has been paid for, but a
malformed variable must never take groom offline: a repo that typos its JSON
should get a WARNING and the previous (`with:`) behavior, not a red run every
day until someone notices. So every failure path here degrades to the caller's
defaults and exits 0. The one thing it will not do is silently accept a value it
could not parse — each drop emits a `::warning::` naming the key.

Output is ONE compact JSON object on a single line, written to stdout for the
workflow to stash in `$GITHUB_OUTPUT` as `resolved`. Consumers read individual
knobs with `fromJSON(needs.gate.outputs.resolved).max_prs`. One output beats
eleven because job outputs are strings: multi-line values (`themes`,
`scope_desc`) would each need heredoc delimiter plumbing, and every new knob
would mean another output declaration in `gate` plus another `env:` line in
every consuming job.

CLI:

    python3 .github/groom/config.py \
        --defaults-json "$DEFAULTS" \
        --config-json "$GROOM_CONFIG" \
        [--dispatch-json "$DISPATCH_INPUTS"]
"""

import argparse
import json
import math
import re
import sys

# Knobs `GROOM_CONFIG` may set. The value is the coercion applied — see the
# `_coerce_*` helpers for why each knob is typed the way it is (several are
# deliberately kept as STRINGS because a downstream parser, not this module, is
# their normalization authority).
_OPERATIONAL_KEYS = {
    "dry_run": "bool",
    "volume_gate": "bool",
    # Stays a string: `build_select`'s Python block is the single parse/clamp
    # authority for max_prs (see the input's description in groom.yml), and
    # duplicating the clamp here would give two places to disagree.
    "max_prs": "numeric_string",
    "max_findings": "nonneg_int",
    # `nonneg_int`, NOT `numeric_string` like its neighbour `max_prs`, even
    # though both are "a number a downstream step parses". The downstream here is
    # `PR_SIZE_LIMIT=$(awk -v v="$PR_SIZE_LIMIT" 'BEGIN{printf "%d", v}')` in
    # groom.yml's "Capture patch" step, and `awk`'s `printf "%d"` renders ANY
    # non-numeric value as 0 with no diagnostic. A limit of 0 makes the
    # `[ "$CHANGED" -gt "$PR_SIZE_LIMIT" ]` test true for every patch, so every
    # build bails to an issue and the builder silently stops opening PRs at all.
    # awk is therefore not a normalization authority worth deferring to: it
    # cannot tell a typo from an intent. Rejecting here means a typo gets a
    # `::warning::` naming the key and the caller's reviewed default, which is
    # this module's documented fail-open behaviour. An explicit `0` still
    # resolves as 0 — "always file issues, never open PRs" is a legal setting.
    "pr_size_limit": "nonneg_int",
    # Stay strings: interval.py is the single normalization authority, and it
    # deliberately degrades blank/garbage/negative to 7 rather than failing.
    "interval_days": "numeric_string",
    "cadence": "numeric_string",
    # `themes` may be set to "" — that is the documented way to CLEAR a themes
    # list the caller pinned in its `with:`, and the finder's `if themes:` guard
    # reads an empty value as "no theme restriction". `scope_desc` is different:
    # it is substituted into "Scan {{SCOPE_DESC}}." verbatim, so blanking it
    # leaves the agent a truncated sentence rather than a wider scan.
    "themes": "prose",
    "scope_desc": "prose_nonblank",
    "scope_label": "scope_label",
    "model": "model",
}

# Knobs the variable may NOT set — see the module docstring for why each one is
# here. Present-but-ignored, with a loud warning: silently dropping them would
# leave an operator believing they had disabled the builder.
_LOCKED_KEYS = ("builder", "sink", "bot_app_id", "workflows_ref", "config")

# Caps on the free-text knobs. These land in the finder/verifier BRIEFS — the
# trusted prompt — so a variable edit is a (repo-write-gated) path into prompt
# text. Bounding length and stripping control characters keeps a careless or
# malicious value from displacing the brief's own instructions with a wall of
# text; it is a blast-radius limit, not an injection proof (the brief's own
# "treat repo content as untrusted data" framing carries that).
_PROSE_MAX = 600

# A model id is passed as the VALUE of `claude --model`, so it cannot become a
# separate flag — but keep it to the shape a model id actually has rather than
# forwarding arbitrary bytes into the agent invocation. Leading `-` is refused
# explicitly: it is the one shape that would be worth attempting.
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# Printable-only: strip C0/C1 controls (including newlines) from prose that gets
# spliced into a single-line JSON output and then into a prompt. A newline in
# `scope_desc` could otherwise fabricate a new instruction line in the brief.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


_WARN_ECHO_MAX = 120


def _shown(value) -> str:
    """`repr(value)` for a warning, bounded.

    Warnings echo the offending value so the operator can spot their typo, but
    the value is variable-controlled and can be enormous (a 4300-digit integer
    literal, a 600+-char prose blob), and this repo's run logs are PUBLIC. Cap it
    at a length that still identifies the value.
    """
    text = repr(value)
    if len(text) > _WARN_ECHO_MAX:
        return f"{text[:_WARN_ECHO_MAX]}… ({len(text)} chars)"
    return text


def _warn(message: str) -> None:
    """Emit a GitHub Actions warning annotation on stderr.

    stderr, not stdout: stdout carries the resolved JSON and nothing else.
    """
    print(f"::warning::groom config: {message}", file=sys.stderr)


def _coerce_bool(key, value):
    """JSON `true`/`false`, or the strings "true"/"false" (any case).

    Strings are accepted because a caller may forward a `workflow_dispatch`
    input, which GitHub always delivers as a string, through the same merge.
    Anything else (1, "yes", null) is DROPPED rather than guessed at: silently
    reading "yes" as false would quietly turn filing back on.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    _warn(f"{key}={_shown(value)} is not a boolean — ignoring it (using the caller's value).")
    return None


def _coerce_numeric_string(key, value):
    """A number kept AS A STRING for a downstream parser to normalize.

    `max_prs`, `interval_days` and `cadence` each have an existing, documented
    parser that owns clamping and fail-open degradation. This only checks the
    value is numeric at all and hands the canonical string on, so the resolved
    JSON never carries a shape those parsers would choke on.

    "Numeric at all" means FINITE and NON-NEGATIVE, not merely `float()`-able.
    `inf`/`nan` (and the bare `Infinity`/`NaN` literals Python's `json` accepts)
    parse as floats but degrade downstream in opposite, silent directions —
    `max_prs: "inf"` builds zero PRs while `interval_days: "nan"` reverts to the
    weekly default — so the operator would get the inverse of what they set with
    no annotation naming the key. `interval.py`'s `parse_interval_days` already
    applies the same `math.isfinite`/`< 0` guard; rejecting here means the
    warning names the key instead of the value vanishing into a clamp.
    """
    if isinstance(value, bool):  # bool is an int subclass — reject before the number check
        _warn(f"{key}={_shown(value)} is a boolean, expected a number — ignoring it.")
        return None
    if isinstance(value, (int, float)):
        text = str(value)
    elif isinstance(value, str) and value.strip():
        text = value.strip()
    else:
        _warn(f"{key}={_shown(value)} is not a number — ignoring it.")
        return None
    try:
        parsed = float(text)
    except (ValueError, OverflowError):
        # OverflowError: an int literal past ~1e308 is a valid JSON number but
        # has no float, so `float()` raises rather than returning inf.
        _warn(f"{key}={_shown(value)} is not a number — ignoring it.")
        return None
    if not math.isfinite(parsed):
        _warn(f"{key}={_shown(value)} is not a finite number in range — ignoring it.")
        return None
    if parsed < 0:
        _warn(f"{key}={_shown(value)} must be non-negative — ignoring it.")
        return None
    return text


def _coerce_nonneg_int(key, value):
    """A non-negative integer, floored.

    `max_findings` slices the filing list (`[:cap]`); a negative value would
    make that slice count from the END and file the wrong subset, so refuse it
    here rather than let it through to the file job. `pr_size_limit` is compared
    with `-gt` after an `awk printf "%d"` that turns anything unparseable into a
    silent 0, which would bail every build to an issue — so its typos have to be
    caught here too, and both go to the caller's reviewed default instead.

    The float is validated BEFORE it is floored, in that order deliberately:

    * `int(float(x))` raises `OverflowError` — an `ArithmeticError`, NOT caught
      by `(TypeError, ValueError)` — for `inf`, `"inf"`, the bare `Infinity`
      literal `json` accepts, `1e999`, and any 310+-digit integer. An uncaught
      traceback here would exit non-zero and break this module's central
      FAIL-OPEN contract, turning the gate red on every daily tick.
    * `int()` truncates TOWARD ZERO, so flooring first would turn `-0.5` into a
      `0` that sails past the `< 0` guard — reading a clearly invalid value as
      "file nothing" instead of keeping the caller's cap.
    """
    if isinstance(value, bool):
        _warn(f"{key}={_shown(value)} is a boolean, expected a number — ignoring it.")
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        _warn(f"{key}={_shown(value)} is not a number — ignoring it.")
        return None
    if not math.isfinite(parsed):
        _warn(f"{key}={_shown(value)} is not a finite number in range — ignoring it.")
        return None
    if parsed < 0:
        _warn(f"{key}={_shown(value)} must be non-negative — ignoring it.")
        return None
    return int(parsed)


def _coerce_prose(key, value):
    """Free text for the briefs: controls stripped, whitespace collapsed, capped.

    An empty result is returned as `""`, not `None`: for `themes` that is the
    documented way to clear a list the caller pinned. Knobs where a blank value
    is unusable rather than meaningful use `_coerce_prose_nonblank`.
    """
    if not isinstance(value, str):
        _warn(f"{key}={_shown(value)} is not a string — ignoring it.")
        return None
    cleaned = " ".join(_CONTROL_RE.sub(" ", value).split())
    if len(cleaned) > _PROSE_MAX:
        _warn(f"{key} is {len(cleaned)} chars (limit {_PROSE_MAX}) — truncating.")
        cleaned = cleaned[:_PROSE_MAX].rstrip()
    return cleaned


def _coerce_prose_nonblank(key, value):
    """Prose whose blank form is unusable, so blank means "keep the caller's".

    `scope_desc` is substituted verbatim into the finder brief's "Scan
    {{SCOPE_DESC}}." sentence, so an empty or whitespace-only value does not
    widen the scan — it hands the agent a truncated sentence with no scope at
    all. Every other coercer signals "unusable, keep the caller's value" by
    returning `None`; do the same here rather than letting `coerce_layer`'s
    `is not None` check write the empty string over a working default.
    """
    cleaned = _coerce_prose(key, value)
    if cleaned is None:
        return None
    if not cleaned:
        _warn(f"{key}={_shown(value)} is empty — ignoring it (using the caller's value).")
        return None
    return cleaned


def _coerce_scope_label(key, value):
    """The scope label, which is ALSO the dedup signature's second field.

    Changing it re-keys every signature, so the entire ledger stops matching and
    groom re-files its whole backlog once. That is occasionally what you want
    (a genuinely re-scoped scan) and never what you want by accident, so it is
    settable but always announced.
    """
    cleaned = _coerce_prose(key, value)
    if cleaned is None:
        return None
    if not re.fullmatch(r"[A-Za-z0-9._/\-]{1,80}", cleaned):
        _warn(f"{key}={_shown(value)} must be 1-80 chars of [A-Za-z0-9._/-] — ignoring it.")
        return None
    return cleaned


def _coerce_model(key, value):
    if not isinstance(value, str) or not _MODEL_RE.match(value.strip()):
        _warn(f"{key}={_shown(value)} is not a valid model id — ignoring it.")
        return None
    return value.strip()


_COERCERS = {
    "bool": _coerce_bool,
    "numeric_string": _coerce_numeric_string,
    "nonneg_int": _coerce_nonneg_int,
    "prose": _coerce_prose,
    "prose_nonblank": _coerce_prose_nonblank,
    "scope_label": _coerce_scope_label,
    "model": _coerce_model,
}


def parse_layer(raw, *, label):
    """Parse one JSON config layer into a dict, or {} — never raising.

    A blank value is the overwhelmingly common case (no variable set / a
    schedule event carrying no dispatch inputs) and is NOT worth a warning.
    Anything non-blank that is not a JSON object IS warned about, because
    someone meant it to take effect.

    The `except` is deliberately broad (as in `interval.py`'s `evaluate`) rather
    than the obvious `json.JSONDecodeError`: `json.loads` also raises a plain
    `ValueError` for an integer literal past CPython's 4300-digit int/str
    conversion limit and `RecursionError` for a deeply nested array — both fit
    easily inside a 48 KB Actions variable, and either one crashing the gate
    would break the fail-open contract this whole module exists to keep.
    """
    if raw is None or not str(raw).strip():
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        _warn(f"{label} is not valid JSON — ignoring it entirely and using the caller's values.")
        return {}
    if not isinstance(parsed, dict):
        _warn(f"{label} must be a JSON object, got {type(parsed).__name__} — ignoring it.")
        return {}
    return parsed


def coerce_layer(layer, *, label, trusted=False):
    """Validate/normalize one parsed layer down to accepted operational knobs.

    `trusted=True` marks the caller-defaults layer, which IS the workflow file —
    the thing `_LOCKED_KEYS` protects, not a bypass of it. Rejecting a locked key
    there would strip a reviewed `with:` value out of `resolved` and fire the
    "part of the reviewed security boundary" warning on every normal run,
    desensitizing operators to the one annotation meant to flag a real bypass
    attempt. Locked keys pass through untouched (there is no coercer for them);
    only the variable/`config` layers are gated.
    """
    out = {}
    for key, value in layer.items():
        if key in _LOCKED_KEYS:
            if trusted:
                if value is not None:  # explicit null = "don't set this knob"
                    out[key] = value
                continue
            _warn(
                f"{label} sets {key!r}, which cannot be configured by variable — it is part of "
                "the reviewed security boundary and must be changed in the workflow file. Ignoring it."
            )
            continue
        kind = _OPERATIONAL_KEYS.get(key)
        if kind is None:
            _warn(f"{label} has unknown key {key!r} — ignoring it (typo?).")
            continue
        if value is None:  # explicit null = "don't override this knob"
            continue
        coerced = _COERCERS[kind](key, value)
        if coerced is not None:
            out[key] = coerced
    return out


def resolve(*, defaults, config_raw, dispatch_raw=None):
    """Merge the layers into the final knob set. Highest precedence last."""
    resolved = dict(defaults)
    layers = (
        (config_raw, "GROOM_CONFIG"),
        (dispatch_raw, "workflow_dispatch inputs"),
    )
    for raw, label in layers:
        overrides = coerce_layer(parse_layer(raw, label=label), label=label)
        for key, value in overrides.items():
            if key == "scope_label" and value != resolved.get("scope_label"):
                _warn(
                    f"{label} changes scope_label from {_shown(resolved.get('scope_label'))} to {_shown(value)}. "
                    "The scope label is part of every dedup signature, so EVERY existing ledger "
                    "entry stops matching and this run will re-propose findings already filed "
                    "under the old label. Intended only for a genuine re-scope."
                )
            resolved[key] = value
    return resolved


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Resolve groom's operational knobs from GROOM_CONFIG over the caller's defaults."
    )
    parser.add_argument(
        "--defaults-json",
        required=True,
        help="JSON object of the caller's `with:` values (the reviewed baseline).",
    )
    parser.add_argument(
        "--config-json",
        default="",
        help="Contents of the GROOM_CONFIG Actions variable (may be empty).",
    )
    parser.add_argument(
        "--dispatch-json",
        default="",
        help="JSON object of workflow_dispatch inputs, if any (highest precedence).",
    )
    args = parser.parse_args(argv)

    # The defaults layer comes from the workflow itself, so a parse failure here
    # is OUR bug, not a user typo — but still degrade rather than abort: an empty
    # baseline plus the reusable's input defaults is a working groom run.
    defaults = coerce_layer(parse_layer(args.defaults_json, label="caller defaults"),
                            label="caller defaults", trusted=True)
    resolved = resolve(
        defaults=defaults,
        config_raw=args.config_json,
        dispatch_raw=args.dispatch_json,
    )
    # Compact + no newlines: this is written verbatim as one `$GITHUB_OUTPUT` line.
    print(json.dumps(resolved, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
