#!/usr/bin/env bash
# grade-pr-risk.sh — deterministic PR risk grader for CI (the reusable pr-risk.yml workflow).
#
# Grades ONE pull request into a risk tier R0 (safest) .. R3 (riskiest), or refuses with
# `unknown` when an input could not be read. The tier is advisory: this script only COMPUTES;
# the workflow around it leaves the label. Nothing here gates, blocks, comments, or merges.
#
# EXTRACTED from the fleet's offline corpus grader (BE-5507), which remains the backfill
# tool. This copy is the CANONICAL grader for the CI path.
# Three pieces were inlined or dropped to make it self-contained:
#   * the shared actor-identity jq (logins / classify_login) is inlined from grade-collect.sh
#     (BE-5030) — if you change it here, change it there too; the jq is small on purpose
#   * gh-lib.sh (fleet API budget accounting) is dropped — CI runs use the job's GITHUB_TOKEN
#   * ledger/report modes are dropped — CI grades one open PR; the corpus tooling stays home
# The risk map and runbook registry are the SAME versioned artifacts; every graded record
# carries map_version + registry_version, so fleet-graded and CI-graded records are comparable
# and a map revision can be replayed against either corpus.
#
# DETERMINISTIC BY CONSTRUCTION: `gh` + `jq` only. No model call, no LLM anywhere in the path.
#
# ── grade = worst(path_floor, provenance, reversibility) ───────────────────────────────────
# Each axis INDEPENDENTLY proposes a tier and the WORST one wins. That is the whole safety
# property: an axis can only ever move a PR into a RISKIER lane. No axis can pull a PR safer
# than another axis put it, so a mis-modelled runbook cannot buy its way past the path map,
# and an unknown on any axis cannot be averaged away.
#
#   AXIS 1 — PATH FLOOR. Reads the VERSIONED map (risk-map.v0.json). In the reusable workflow
#     the map ships in Comfy-Org/github-workflows and is checked out at the caller's pinned
#     ref, never from the graded PR — so a PR cannot edit the rules that judge it. The floor
#     is the WORST tier over every rule any changed path matches.
#   AXIS 2 — PROVENANCE. runbook / agent-supervised / human / external. A PR whose identity
#     matches a runbook but whose DIFF SHAPE does not is not a runbook, and falls back to its
#     underlying class. `external` (fork / first-time contributor) is R3 on provenance alone.
#   AXIS 3 — REVERSIBILITY. Single clean revert? Mutates persistent state or deletes data?
#     Did tests covering the touched lines actually run? Answered from the changed-path list
#     + change types + the PR's own check rollup.
#
# ── THE UNKNOWN CONTRACT ────────────────────────────────────────────────────────────────────
# An unreadable input yields `unknown` and is REPORTED — never a confident tier. Every axis
# returns {tier, status, reason} and a `status: unknown` makes `tier` null. An overall grade
# with ANY unknown axis is `tier: null, status: unknown` — it is NOT silently graded off the
# axes that did resolve, because a PR whose file list we could not read is exactly the PR
# that might touch auth. The workflow labels these `risk:ungraded`, never a tier.
#
#   ./grade-pr-risk.sh --repo my-org/my-repo --pr 123       # grade one PR (open or terminal)
#   ./grade-pr-risk.sh --stdin                              # scorecard JSONL in, graded out
#                                                           # (the no-network test surface)
#
# Exit: 0 = graded ok. 1 = graded, grade is unknown (reported). 2 = usage/setup error.
#       3 = the PR itself was unreadable — NOTHING was graded (this is NOT "no risk").
#
# Deliberately bash (shebang), not zsh — CI runners and the test suite both exercise bash.

set -uo pipefail   # no -e: faults are collected and reported, not fatal mid-run

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

REPO=""
RISK_MAP="${PR_RISK_MAP:-$SKILL_DIR/risk-map.v0.json}"
RUNBOOKS="${PR_RISK_RUNBOOKS:-$SKILL_DIR/runbook-registry.v0.json}"
FLEET_LOGINS="${PR_RISK_FLEET_LOGINS:-mattmillerai}"
BOT_LOGINS="${PR_RISK_BOT_LOGINS:-github-actions,dependabot,renovate,coderabbitai,cursor,comfy-pr-bot,web-flow}"
SELF_CONTEXT="${PR_RISK_SELF_CONTEXT:-}"
SELF_RUN_ID="${PR_RISK_SELF_RUN_ID:-}"
# How many 100-context pages of the check rollup to drain before giving up and leaving the PR
# UNKNOWN. 30 pages = 3000 checks, the same ceiling GitHub puts on the REST changed-files
# endpoint this script already accepts, and ~29x the largest rollup measured on Comfy-Org/cloud
# (103). It is a runaway bound, not a policy: a rollup past it stays ungraded rather than being
# graded off the pages that fit.
MAX_ROLLUP_PAGES="${PR_RISK_MAX_ROLLUP_PAGES:-30}"
PR_NUM=""
MODE=""

log()  { printf '[grade-pr-risk] %s\n' "$*" >&2; }
warn() { printf '[grade-pr-risk] WARN %s\n' "$*" >&2; }
die()  { printf '[grade-pr-risk] ERROR %s\n' "$*" >&2; exit 2; }

usage() {
  cat >&2 <<'USAGE'
usage: grade-pr-risk.sh (--repo owner/name --pr N | --stdin) [options]
  --repo owner/name    repo being graded (required with --pr)
  --pr N               grade ONE pr live (works on an OPEN pr) and print the graded record
  --stdin              grade scorecard-shaped JSONL records from stdin (no network; tests)
  --map FILE           risk map           (default risk-map.v0.json beside this script)
  --runbooks FILE      runbook registry   (default runbook-registry.v0.json beside this script)
  --fleet-logins CSV   GitHub logins that are supervised agents (default mattmillerai)
  --bot-logins CSV     extra logins treated as bots
  --self-context NAME  when grading FROM CI: the calling workflow's own name. The check
                       rollup is then computed from individual contexts EXCLUDING that
                       workflow's runs — the grading job is itself part of the rollup it
                       reads, so the raw rollup can never be SUCCESS while it runs. Also
                       emits checks_pending_excl_self so a caller can wait for CI to settle.
  --self-run-id ID     PREFERRED over --self-context: exclude by workflow-RUN id
                       (github.run_id) instead of display name, so a same-named workflow
                       elsewhere is not excluded. A FAILING check is never excluded either
                       way — self-exclusion may only ever hide our own pending run.
exit: 0 ok | 1 graded but unknown | 2 usage/setup | 3 PR unreadable (nothing graded)
USAGE
}

# ---- the map ---------------------------------------------------------------------------------
# An unreadable map is fatal — grading against one would grade every PR R0. VALID JSON IS NOT
# ENOUGH: `{}` parses, so a syntactically-valid but STRUCTURALLY EMPTY map used to sail through
# upstream — `default_tier` fell back to R0, no path rule matched anything, and every PR graded
# R0. So the shape is checked too, and every tier STRING in the file is checked against the tier
# enum here rather than being tolerated downstream: `tier_rank` cannot rank a tier it does not
# know, and a fail-safe rank is a worse answer than a refusal at load time.
read_map() { # <file> <kind: map|runbooks> -> JSON on stdout, rc 0; rc 1 + reason on stderr
  local f="$1" kind="${2:-map}" raw shape
  [ -f "$f" ] || { echo "$kind $f not found" >&2; return 1; }
  raw="$(cat "$f" 2>/dev/null)" || { echo "cannot read $f" >&2; return 1; }
  jq -e . >/dev/null 2>&1 <<<"$raw" || { echo "$kind is not valid JSON" >&2; return 1; }
  if [ "$kind" = map ]; then
    # shellcheck disable=SC2016  # jq program: $vars belong to jq
    shape='
      def known: ["R0","R1","R2","R3"];
      if type != "object" then "not a JSON object"
      elif (.path_rules | type) != "array" then "path_rules is missing or not an array"
      elif (.path_rules | length) == 0 then "path_rules is EMPTY — an empty rule set would grade every PR R0"
      elif ([.path_rules[] | select((.class | type) != "string" or (.paths | type) != "array" or (.paths | length) == 0)] | length) > 0
        then "a path rule is missing a class or a non-empty paths list"
      elif ([.path_rules[] | .tier | select(IN(known[]) | not)] | length) > 0
        then "a path rule carries a tier outside \(known) — refusing to rank an unknown tier"
      elif (.provenance_tiers | type) != "object" then "provenance_tiers is missing or not an object"
      elif ([.provenance_tiers | to_entries[] | select(.key | startswith("_") | not) | .value | select(IN(known[]) | not)] | length) > 0
        then "provenance_tiers carries a tier outside \(known)"
      # EVERY provenance class must be MAPPED, not just well-typed. Checking only the values let
      # a map that OMITS `external` pass, and the lookup then fell back to a tier of its own
      # choosing — silently retiring the "external (fork / first-time contributor) is R3, no
      # exceptions" invariant that both the default map comment and the README promise. A class
      # nobody mapped is a routing decision nobody made, so it is refused at load time.
      # (No apostrophes in here: the whole shape program is a single-quoted shell string.)
      elif ((["runbook","agent-supervised","human","external"] - [.provenance_tiers | keys[]]) | length) > 0
        then "provenance_tiers is missing a class: \(["runbook","agent-supervised","human","external"] - [.provenance_tiers | keys[]]) — an unmapped class would be graded off a tier nobody chose"
      elif ((.reversibility // {}) | has("test_path_patterns")) and (((.reversibility // {}).test_path_patterns | type) != "array")
        then "reversibility.test_path_patterns is present but not an array"
      elif (.default_tier // "R0") as $d | ($d | IN(known[])) | not then "default_tier is outside \(known)"
      elif [(.reversibility // {}) | .no_green_checks_tier, .no_test_touched_tier, .clean_tier | select(. != null and (IN(known[]) | not))] | length > 0
        then "a reversibility tier is outside \(known)"
      else empty end'
  else
    # shellcheck disable=SC2016  # jq program: $vars belong to jq
    shape='
      if type != "object" then "not a JSON object"
      elif (.runbooks | type) != "array" then "runbooks is missing or not an array"
      elif ([.runbooks[] | select((.id | type) != "string" or (.identity | type) != "object" or (.shape | type) != "object")] | length) > 0
        then "a runbook entry is missing an id, an identity or a shape assertion"
      elif ([.runbooks[] | select(((.identity.logins // []) | type) != "array" or ((.identity.logins // []) | length) == 0)] | length) > 0
        then "a runbook identity has no logins — identity is the author login, so an entry without one can never assert"
      else empty end'
  fi
  local why; why="$(jq -r "$shape" <<<"$raw" 2>/dev/null)"
  [ -z "$why" ] || { echo "$kind is structurally invalid: $why" >&2; return 1; }
  printf '%s' "$raw"
}

# ---- shared actor-identity resolution (jq) ---------------------------------------------------
# INLINED from agent-work's grade-collect.sh (BE-5030), the one definition the fleet's graders
# share. Small on purpose; if you change it here, change it there. NEVER classify on
# commit.author.name/email — that string is `git config user.email` and is forgeable by the
# thing being graded. The PR author login is the resolution GitHub itself made.
# shellcheck disable=SC2016  # jq program: $vars belong to jq
IDENTITY_JQ='
    def logins: ascii_downcase | [splits("[,[:space:]]+")] | map(select(. != ""));
    # classify_login: the login STRING -> "unknown" | "bot" | "fleet" | "human". The bot test
    # (a `[bot]` suffix plus the caller-supplied bot list) runs BEFORE the fleet test so a bot
    # that also appears in $fleetl still reads as a bot.
    def classify_login($fleetl; $botl):
      if . == null or . == "" then "unknown"
      else (ascii_downcase) as $l
      | if ($l | endswith("[bot]")) or (($botl | index($l)) != null) then "bot"
        elif ($fleetl | index($l)) != null then "fleet"
        else "human" end end;
'

# ---- the grading jq program -------------------------------------------------------------------
# ONE jq program grades every record. Input: one scorecard-shaped record per line. Output: the
# same record + a `risk` block carrying the tier, the PER-AXIS tiers, the reason, AND the map
# version that produced them — so a later map revision can be REPLAYED against accumulated
# records rather than reconstructed.
grade_program() {
cat <<'JQ'
    # --- glob -> anchored regex. `**` crosses separators, `*` does not. -------------------
    # Staged via \u0002 / \u0001 placeholders rather than a lookbehind: the placeholder cannot appear in a path,
    # and staging it this way makes "** before *" unambiguous without relying on regex-engine
    # lookbehind support.
    def glob2re:
      gsub("(?<c>[.+?^$(){}|\\[\\]\\\\])"; "\\\(.c)")
      | gsub("\\*\\*/"; "\u0002") | gsub("\\*\\*"; "\u0001") | gsub("\\*"; "[^/]*")
      | gsub("\u0002"; "(?:.*/)?") | gsub("\u0001"; ".*")
      | "^" + . + "$";
    def matches_any($globs): . as $p | any($globs[]?; . as $g | ($p | test($g | glob2re)));
    # An UNRECOGNIZED tier ranks as the RISKIEST, never the safest. read_map already refuses a
    # map carrying one, so this is defence in depth — but the direction matters: defaulting to
    # R0 would let a typo'd or future tier silently DOWNGRADE a PR's grade, which inverts the
    # "unknown is never safe" contract in the one place it decides routing.
    def tier_rank: {"R0":0,"R1":1,"R2":2,"R3":3}[.] // 3;
    def worst($a; $b): if ($a | tier_rank) >= ($b | tier_rank) then $a else $b end;

    $map as $M | $rb as $RB
    | ($fleet | logins) as $fleetl | ($bots | logins) as $botl
    | ($M.default_tier // "R0") as $DEF
    | .  as $r
    | (.changed_paths) as $paths
    | ([$paths[]? | .path]) as $plist
    # EVERY path the diff touches, DESTINATION *and* ORIGIN. A RENAMED file is recorded under its
    # destination only, so matching `.path` alone let a rename out of a sensitive directory escape
    # the floor entirely: move `.github/workflows/deploy.yml` or an `auth/` file to an innocuous
    # name and the R3 rule that guards it never matches. The origin path is part of what the PR
    # did, so it is graded too.
    | ([$paths[]? | .path, (.previous_path // empty)] | unique) as $pall

    # ---- AXIS 1: PATH FLOOR ---------------------------------------------------------------
    # WORST over every rule any changed path matches. An R0 rule (docs, tests) can never cancel
    # an R3 rule (migrations) in the same PR — that is why this is a max, not a last-match-wins.
    | (if $r.changed_paths_status != "ok" or $paths == null
       then {tier:null, status:"unknown",
             reason:("changed-path list is " + ($r.changed_paths_status // "absent") + " — a PR whose files we cannot read is exactly the PR that might touch auth"),
             classes:null}
       else
         ([$M.path_rules[]? | . as $rule | select($pall | any(. as $p | $p | matches_any($rule.paths)))]) as $hit
         # PER-FILE FLOORS, for REPORTING ONLY. The same rules, matched one file at a time, so the
         # publish surfaces can say WHICH files put the floor where it is ("94% of this diff is
         # R0/R1; the 6% that makes it R3 is these two files") instead of printing an opaque tier.
         # This CANNOT move the grade: `worst` over these per-file floors is by construction the
         # same value as `worst` over every matched rule, because each file's floor already starts
         # at $DEF and every matched rule is some file's rule. tests/test_grade_pr_risk.sh pins
         # that equality so a future edit here cannot quietly become a second grading model.
         # Each file is matched on its DESTINATION *and* ORIGIN path, exactly as $pall is above —
         # a rename out of a guarded directory keeps that directory's tier on its row.
         | ([$paths[]? | . as $f
             | ([$f.path, ($f.previous_path // empty)]) as $fp
             | ([$M.path_rules[]? | . as $rule | select($fp | any(. as $p | $p | matches_any($rule.paths)))]) as $fh
             | {path: $f.path,
                previous_path: ($f.previous_path // null),
                additions: ($f.additions // 0),
                deletions: ($f.deletions // 0),
                change_type: ($f.change_type // null),
                tier: (reduce $fh[] as $h ($DEF; worst(.; $h.tier))),
                classes: [$fh[] | .class]}]) as $fl
         | {tier: (reduce $hit[] as $h ($DEF; worst(.; $h.tier))),
            status:"ok",
            reason: (if ($hit|length) == 0 then "no mapped path touched — floor \($DEF)"
                     else "matched " + ([$hit[] | "\(.class)=\(.tier)"] | join(", ")) end),
            classes: [$hit[] | .class],
            files: $fl}
       end) as $A1

    # ---- AXIS 2: PROVENANCE ---------------------------------------------------------------
    # Identity first (server-attributed author login, classified by the shared resolver), then
    # the runbook shape assertion. A claimed runbook that fails its shape assertion is NOT a
    # runbook — provenance alone is never sufficient.
    | ($r.author // null) as $author
    | ($author | classify_login($fleetl; $botl)) as $cls
    | (($r.labels // []) | index("agent-coded") != null) as $agent_coded
    # The label list has a STATUS TWIN for the same reason the file list does: `agent-coded` is
    # read from it, and a TRUNCATED label list answers "is this agent-coded?" with a confident no
    # it has not earned. A consumer map that splits agent-supervised from human would then grade
    # off a list nobody confirmed was complete.
    | ($r.labels_status // "ok") as $lbst
    # `external` is decided from is_fork + author_association, and those arrive with a STATUS
    # twin — whether they were actually read has to be asked before they are believed. Reading
    # an un-collected `is_fork` as "not a fork" would make `external => R3` — the one provenance
    # class never routed unattended — silently unreachable. Unread is `unknown`, and `unknown`
    # refuses to grade the axis.
    | ($r.provenance_status // (if ($r | has("is_fork")) then "ok" else "absent" end)) as $pvst
    | (if $pvst != "ok" or $lbst != "ok" then "unknown"
       elif ($r.is_fork // false) or (($r.author_association // "") | IN("FIRST_TIME_CONTRIBUTOR","FIRST_TIMER","NONE"))
       then "external"
       elif $agent_coded or $cls == "fleet" then "agent-supervised"
       elif $cls == "bot" then "runbook-candidate"
       elif $cls == "human" then "human"
       else "unknown" end) as $base_class
    # The shape assertion: identity match AND every changed path inside permitted_paths AND the
    # diff-shape bounds AND the title. Anything short of all four is a shape FAILURE, recorded.
    #
    # IDENTITY IS THE AUTHOR LOGIN, AND THE HEAD REF NARROWS IT — never the other way round.
    # This was `login_match or (has_patterns and head_ref_match)`, and because jq binds `and`
    # tighter than `or` that made a matching HEAD REF ALONE sufficient: anyone who names a
    # branch `.../generated-x` presents sdk-spec-push's identity without being its author. The
    # login test is therefore REQUIRED, and head_ref_patterns are an ADDITIONAL condition where
    # the producer declares them. The parentheses are load-bearing — do not let this collapse
    # back into a bare or/and chain.
    | ([$RB.runbooks[]? | . as $bk
        | select((($bk.identity.logins // []) | any(. as $l | ($author // "") | ascii_downcase == ($l | ascii_downcase)))
                 and ((($bk.identity.head_ref_patterns // []) | length) == 0
                      or (($r.head_ref // "") | matches_any($bk.identity.head_ref_patterns // []))))
        | {id: $bk.id, lane: $bk.lane, daily_cap: $bk.daily_cap,
           paths_ok: (if $r.changed_paths_status != "ok" then null
                      else ($pall | length) > 0 and all($pall[]; matches_any($bk.permitted_paths // [])) end),
           shape_ok: (($r.changed_files // 0) <= ($bk.shape.max_changed_files // 1e9)
                      and ($r.additions // 0) <= ($bk.shape.max_additions // 1e9)
                      and ($r.deletions // 0) <= ($bk.shape.max_deletions // 1e9)),
           title_ok: (($bk.shape.title_regex // null) as $tr
                      | if $tr == null then true else (($r.title // "") | test($tr)) end)}]) as $cand
    | ([$cand[] | select(.paths_ok == true and .shape_ok and .title_ok)] | first) as $rbk
    | ([$cand[] | select(.paths_ok != true or (.shape_ok | not) or (.title_ok | not))
        | "\(.id): paths=\(.paths_ok) shape=\(.shape_ok) title=\(.title_ok)"]) as $shape_failures
    # A matched runbook NEVER overrides `external`. An outside diff that also happens to assert
    # a runbook's shape is still an outside diff — letting a runbook match downgrade it would
    # hand any fork a route past the rule by imitating a known producer's shape.
    | (if $base_class == "external" then "external"
       elif $rbk != null then "runbook"
       elif $base_class == "runbook-candidate" then "human"   # a bot we do not have a runbook for is not trusted
       else $base_class end) as $prov
    | (if $prov == "unknown" or $author == null
       then {tier:null, status:"unknown",
             reason:(if $pvst != "ok"
                     then "fork / author-association were not collected (\($pvst)) — the `external` provenance class is un-decidable, and defaulting it to 'not a fork' would silently retire the external => R3 rule"
                     elif $lbst != "ok"
                     then "the PR label list is \($lbst) — `agent-coded` cannot be read off a truncated list, and reading it as absent would be a confident answer from a source nobody finished reading"
                     else "PR author did not resolve to a GitHub account — provenance is unattributable" end),
             provenance:null}
       # An UNMAPPED class falls back to the RISKIEST tier, never R1 — same direction as
       # tier_rank. read_map now REQUIRES all four classes, so this is defence in depth; the
       # direction is what matters, because defaulting to R1 is how a map that omitted `external`
       # used to grade a fork the same as a teammate.
       else {tier: (($M.provenance_tiers // {})[$prov] // "R3"), status:"ok",
             provenance: $prov,
             runbook: (if $rbk == null then null else $rbk.id end),
             runbook_lane: (if $rbk == null then null else $rbk.lane end),
             shape_failures: $shape_failures,
             reason: (if $rbk != null then "runbook \($rbk.id) — identity, permitted paths, diff shape and title all assert"
                      elif ($shape_failures | length) > 0 then "\($prov) — claimed a runbook identity but the shape assertion failed (" + ($shape_failures | join("; ")) + ")"
                      else $prov end)}
       end) as $A2

    # ---- AXIS 3: REVERSIBILITY ------------------------------------------------------------
    # Four questions, answered deterministically and in worsening order:
    #   mutates persistent state / deletes data?  -> R3 (reverting code does not restore state)
    #   deletes a file under a sensitive class?   -> R3 (not a single clean revert)
    #   did tests covering these lines actually run? no green rollup -> R2; green but no test
    #   file touched -> R1; green and a test touched -> R0.
    # `flag_gated` is RECORDED but never LOWERS a tier — an axis may only move riskier.
    | ($M.reversibility // {}) as $RV
    # Was the check rollup READ? `checks_status: ok` with a null `checks_state` is GitHub
    # genuinely reporting no rollup for this head (a repo with no CI) and IS gradeable — the
    # honest R2. A rollup that was never collected is not: reading it as "no green rollup"
    # would be a confident answer computed from a source nobody read.
    | ($r.checks_status // (if ($r | has("checks_state")) then "ok" else "absent" end)) as $ckst
    | (if $r.changed_paths_status != "ok" or $paths == null
       then {tier:null, status:"unknown", reason:"changed-path list is \($r.changed_paths_status // "absent") — reversibility is un-answerable without the paths"}
       elif $ckst != "ok"
       then {tier:null, status:"unknown", reason:"check rollup was not collected (\($ckst)) — 'did tests covering these lines run?' is un-answerable"}
       else
         ([$A1.classes[]? | select(. as $c | ($RV.irreversible_classes // []) | index($c))]) as $irrev
         # A RENAME removes the ORIGIN path as surely as a delete does, so renaming a file OUT of
         # a sensitive directory counts here too — otherwise `git mv auth/x.go misc/x.go` reads as
         # a clean revert.
         | (([$paths[] | select(.change_type == "DELETED") | .path]
             + [$paths[] | select(.change_type == "RENAMED") | (.previous_path // empty)]) | unique) as $deleted
         # Sensitive-class match over the DELETED paths ONLY. This was computed from $A1.classes —
         # the classes matched by ANY changed file — so a PR that merely MODIFIED an auth file
         # while deleting an unrelated README reported "deletes N file(s) under a sensitive class"
         # and pinned reversibility R3. The two sets have to be the same set for the sentence the
         # reason string prints to be true.
         | ([$M.path_rules[]? | . as $rule | select($deleted | any(. as $p | $p | matches_any($rule.paths))) | .class] | unique) as $del_classes
         | ($del_classes | any(. as $c | ($RV.delete_sensitive_classes // []) | index($c))) as $del_sensitive
         | ($pall | any(matches_any($M.flippable_flag_paths // []))) as $flag
         # "Did a test file change?" comes from the VERSIONED map when it says, and falls back to
         # the built-in regex when it does not. Hardcoding it meant a consumer could not fix it
         # with .github/risk.json — the one lever the workflow gives them — and the regex misses
         # `test_*.py`, `*_test.py`, `*Test.java` and `*_spec.rb`, so whole ecosystems could never
         # reach clean_tier and sat at R1 forever.
         # DESTINATION paths only ($plist, not $pall): renaming `x_test.go` to `x.go` REMOVES a
         # test, and matching the origin path would read that as "a test file changed" and let it
         # reach clean_tier. The path floor uses $pall because widening it there can only ever
         # grade RISKIER; widening it here would grade safer, which is the wrong direction.
         | (($RV.test_path_patterns // []) as $tp
            | if ($tp | length) > 0 then ($plist | any(matches_any($tp)))
              else ($plist | any(test("(_test\\.|\\.test\\.|\\.spec\\.|(^|/)tests?/|-test\\.sh$)"))) end) as $touched_test
         | ($r.checks_state // null) as $checks
         | (if ($irrev | length) > 0
              then {t:"R3", why:("touches " + ($irrev|join(", ")) + " — mutates persistent state or deletes data; reverting the code does not restore it")}
            elif (($deleted | length) > 0 and $del_sensitive)
              then {t:"R3", why:("removes " + ($deleted|length|tostring) + " file(s) under a sensitive class (" + ($del_classes|join(", ")) + ") — not a single clean revert")}
            elif $checks == null or $checks != "SUCCESS"
              then {t: ($RV.no_green_checks_tier // "R2"),
                    why:("no GREEN check rollup (" + ($checks // "absent") + ") — cannot answer whether tests covering these lines actually ran")}
            elif ($touched_test | not)
              then {t: ($RV.no_test_touched_tier // "R1"), why:"checks green but the diff touches no test file — nothing proves the suite covers THESE lines"}
            else {t: ($RV.clean_tier // "R0"), why:"single clean revert, no persistent-state mutation, checks green, tests touched"} end) as $d
         | {tier:$d.t, status:"ok", reason:$d.why, flag_gated:$flag, deleted_files:($deleted|length)}
       end) as $A3

    # ---- worst wins ------------------------------------------------------------------------
    # ANY unknown axis makes the OVERALL grade unknown. Grading off the axes that did resolve
    # would present a partially-read PR as a confident tier, which is the failure the unknown
    # contract exists to forbid.
    | ([$A1, $A2, $A3] | map(select(.status != "ok"))) as $unk
    | . + {risk: {
        map_version: ($M.map_version // "unknown"),
        registry_version: ($RB.registry_version // "unknown"),
        graded_at: $now,
        tier: (if ($unk|length) > 0 then null
               else (reduce [$A1.tier, $A2.tier, $A3.tier][] as $t ("R0"; worst(.; $t))) end),
        status: (if ($unk|length) > 0 then "unknown" else "ok" end),
        reason: (if ($unk|length) > 0
                 then "unknown: " + ([$unk[] | .reason] | join(" | "))
                 else "worst of path_floor=\($A1.tier), provenance=\($A2.tier), reversibility=\($A3.tier)" end),
        axes: {path_floor: $A1, provenance: $A2, reversibility: $A3}}}
JQ
}

# grade_stream <map-json> <runbooks-json> — stdin: scorecard records; stdout: graded records
grade_stream() {
  local map="$1" rb="$2"
  jq -c --argjson map "$map" --argjson rb "$rb" \
        --arg fleet "$FLEET_LOGINS" --arg bots "$BOT_LOGINS" --arg now "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        "$IDENTITY_JQ$(grade_program)"
}

# ---- one live PR ------------------------------------------------------------------------------
# Shaped into the same scorecard-like record the fleet's collector emits, so the SAME grading
# program grades a live PR here and a collected one offline; two graders would drift.
#
# THE GRADING JOB IS PART OF THE ROLLUP IT READS. When this script runs inside a workflow on
# the PR it is grading, its own check run is in progress, so the raw statusCheckRollup.state
# can never be SUCCESS at grade time — every CI-time grade would floor at R2 and the tiers
# would be an artifact of the measurement. With --self-run-id (preferred) or --self-context
# the rollup is therefore recomputed from the individual contexts, EXCLUDING our own check
# runs: any remaining pending => PENDING, any remaining SUCCESS => SUCCESS, remaining contexts
# that are all SKIPPED/NEUTRAL => NEUTRAL, nothing else on the commit => null (the honest
# "no CI" case). `checks_pending_excl_self` is emitted alongside so a CI caller can wait for
# the rest of the rollup to settle instead of labeling a snapshot of half-finished checks.
# More than 100 contexts is `unknown`, never a truncated aggregate.
#
# TWO RULES KEEP SELF-EXCLUSION FROM BECOMING A BLINDFOLD:
#   * A FAILURE is scanned over ALL contexts, self INCLUDED. Exclusion may only ever hide our
#     own PENDING; it must never be able to hide a red check. Matching on the workflow NAME
#     dropped every sibling job of a consumer that put the grading job inside its existing CI
#     workflow, so a FAILED test job vanished and the remaining green contexts aggregated to
#     SUCCESS — reversibility then graded R0/R1 on a red PR.
#   * SUCCESS requires at least one context that actually CONCLUDED SUCCESS. A rollup of
#     nothing but SKIPPED / NEUTRAL / null establishes nothing about whether tests ran, which
#     is the axis's whole question, so it aggregates to NEUTRAL and floors at R2.
# --self-run-id also makes the match EXACT (github.run_id), so a same-named workflow in the
# consumer repo is no longer excluded. A caller that still embeds the grading job inside a
# multi-job workflow has all of that run's siblings excluded and lands on the honest R2 floor
# rather than a false green — enroll pr-risk as its OWN workflow to grade off a full rollup.
fetch_pr_record() { # <repo> <num> -> record JSON on stdout, rc 1 on an unreadable PR
  # Token note: the checkSuite -> workflowRun traversal below is an ACTIONS resource, so the
  # job token needs `actions: read` on top of `pull-requests`/`checks`.
  #
  # Without it this is an UNREADABLE PR, not a partial record — and the mechanism is `gh`, not
  # GraphQL. GitHub does answer a forbidden field with `data` present and that field nulled,
  # plus an `errors` entry; but `gh api graphql` exits NON-ZERO whenever the response carries a
  # top-level `errors` array, however complete `data` is (verified: a two-alias query with one
  # bad alias returns data for the good one and still exits 1). So the `|| return 1` below fires
  # and the half-record never reaches jq. That matters: if a nulled `workflowRun` DID reach jq,
  # `is_self`'s `(.checkSuite.workflowRun.databaseId // -1)` fallback would match nothing,
  # self-exclusion would silently stop working, our own in-progress run would read PENDING
  # forever, and the PR would land a confident-looking R2 floor after burning the whole wait
  # budget. Rejecting the read outright is what keeps that from being reachable — so do NOT
  # "recover" partial data here by dropping the rc check.
  local repo="$1" num="$2" q qctx ctxsel resp files fstatus errf
  # The rollup's context selection, shared by the first read and the drain query below so the
  # two cannot drift. It is ONE string on purpose: `is_self` keys on
  # `checkSuite.workflowRun.databaseId` and `is_failing`/`is_pending` on `__typename` +
  # status/conclusion, so a drained page missing any of those fields would read as neither
  # ours nor failing — self-exclusion would quietly stop working past context 100.
  # shellcheck disable=SC2016  # GraphQL: $vars are query variables
  ctxsel='pageInfo{ hasNextPage endCursor } nodes{ __typename
        ... on CheckRun{ name status conclusion checkSuite{ workflowRun{ databaseId workflow{ name } } } }
        ... on StatusContext{ context state } }'
  # shellcheck disable=SC2016  # GraphQL: $vars are query variables
  q='query($owner:String!,$name:String!,$num:Int!){
  repository(owner:$owner,name:$name){ pullRequest(number:$num){
    number title state isDraft createdAt updatedAt closedAt mergedAt
    author{ login } authorAssociation baseRefName headRefName isCrossRepository
    additions deletions changedFiles
    labels(first:100){ pageInfo{ hasNextPage } nodes{ name } }
    commits(last:1){ nodes{ commit{ statusCheckRollup{ state
      contexts(first:100){ '"$ctxsel"' } } } } }
  } } }'
  # shellcheck disable=SC2016  # GraphQL: $vars are query variables
  qctx='query($owner:String!,$name:String!,$num:Int!,$cursor:String!){
  repository(owner:$owner,name:$name){ pullRequest(number:$num){
    commits(last:1){ nodes{ commit{ statusCheckRollup{
      contexts(first:100, after:$cursor){ '"$ctxsel"' } } } } }
  } } }'
  # SURFACE WHY THE READ FAILED. Discarding gh's stderr made a PERMANENTLY misconfigured token
  # look exactly like a transient blip: the caller's rc=3 handler retries four times with
  # backoff and then labels `ungraded`, and the one string that names the cause ("Resource not
  # accessible by integration" / FORBIDDEN, or a missing-scope message) never reached the log —
  # so the fix above (`actions: read`) was undiagnosable from a run. gh's own error text is safe
  # to print: it names fields and scopes, not PR content.
  errf="$(mktemp "${TMPDIR:-/tmp}/grade-pr-risk-fetch.XXXXXX")" || return 1
  resp="$(gh api graphql -f query="$q" -F owner="${repo%%/*}" -F name="${repo##*/}" -F num="$num" 2>"$errf")" || {
    warn "PR read failed for $repo#$num: $(tr '\n' ' ' < "$errf")"
    rm -f "$errf"; return 1
  }
  jq -e '.data.repository.pullRequest.number != null' >/dev/null 2>&1 <<<"$resp" \
    || { warn "PR read for $repo#$num returned no pullRequest — treating as unreadable"
         rm -f "$errf"; return 1; }

  # ---- drain the rollup's `contexts` connection ----------------------------------------------
  # GraphQL caps ANY connection page at 100, so the read above returns at most the first 100
  # checks plus `hasNextPage`. The grader refuses to grade a TRUNCATED rollup — a subset cannot
  # answer "did green checks covering these lines actually run?" — so without this loop every PR
  # with more than 100 checks landed `risk:ungraded`, and the busier a repo's CI the more of its
  # PRs went ungraded. That is the same shape as the `files(first:100)` bug fixed by moving the
  # changed-file list to REST `--paginate` (see the note below it); the rollup is the connection
  # that was left behind.
  #
  # ON FAILURE, LEAVE THE FLAG SET. A page read that errors, a cursor that does not advance, or a
  # rollup deeper than the page guard all `break` WITHOUT splicing, so the record keeps the
  # first page and its `hasNextPage: true` — which the unknown contract below turns into exactly
  # the ungraded outcome this path already produced. Never a partial rollup graded as whole, and
  # never the harsher "PR unreadable" bucket for a PR we did successfully read.
  local rollup='.data.repository.pullRequest.commits.nodes[0].commit.statusCheckRollup'
  local ctxnodes ctxmore ctxcur page pages=0
  ctxnodes="$(jq -c "[$rollup.contexts.nodes[]?]" <<<"$resp")"
  ctxmore="$(jq -r "$rollup.contexts.pageInfo.hasNextPage // false" <<<"$resp")"
  ctxcur="$(jq -r "$rollup.contexts.pageInfo.endCursor // \"\"" <<<"$resp")"
  while [ "$ctxmore" = true ] && [ -n "$ctxcur" ]; do
    pages=$((pages + 1))
    if [ "$pages" -gt "$MAX_ROLLUP_PAGES" ]; then
      warn "rollup for $repo#$num exceeds $((MAX_ROLLUP_PAGES * 100)) checks — grading it would mean grading a partial rollup, so it stays UNKNOWN"
      break
    fi
    page="$(gh api graphql -f query="$qctx" -F owner="${repo%%/*}" -F name="${repo##*/}" \
              -F num="$num" -F cursor="$ctxcur" 2>"$errf")" || {
      warn "rollup page $pages failed for $repo#$num: $(tr '\n' ' ' < "$errf")"
      break
    }
    ctxnodes="$(jq -c --argjson acc "$ctxnodes" "\$acc + [$rollup.contexts.nodes[]?]" <<<"$page")" || break
    ctxmore="$(jq -r "$rollup.contexts.pageInfo.hasNextPage // false" <<<"$page")"
    # A cursor that repeats would spin until the page guard; treat it as a drained-but-unsure
    # read and let the flag above decide.
    local prev="$ctxcur"
    ctxcur="$(jq -r "$rollup.contexts.pageInfo.endCursor // \"\"" <<<"$page")"
    [ "$ctxcur" != "$prev" ] || { warn "rollup cursor for $repo#$num did not advance — stopping"; break; }
  done
  # Splice ONLY when pagination actually ran AND drained. `pages -gt 0` is load-bearing beyond
  # saving work: on a PR with no checks at all `statusCheckRollup` is null, and assigning into a
  # null in jq CREATES the object — which would turn "no rollup" into "an empty rollup" and route
  # it away from the `$ro == null` branch that treats a checkless PR as readable.
  if [ "$pages" -gt 0 ] && [ "$ctxmore" = false ]; then
    resp="$(jq -c --argjson ctx "$ctxnodes" \
              "$rollup.contexts = {pageInfo: {hasNextPage: false, endCursor: null}, nodes: \$ctx}" \
              <<<"$resp")" || { warn "could not reassemble the drained rollup for $repo#$num"
                                rm -f "$errf"; return 1; }
  fi

  # ---- the changed-file list comes from REST, not GraphQL ------------------------------------
  # GraphQL's `files` connection cannot answer this axis. Two reasons, both structural:
  #   * PullRequestChangedFile has NO previous-path field (its whole field set is additions,
  #     changeType, deletions, path, viewerViewedState), so a RENAME is only ever visible under
  #     its DESTINATION — the origin path, which is what the sensitive-path floor needs, is
  #     simply not on offer.
  #   * `files(first:100)` capped the list at 100 and graded everything above it `unknown`,
  #     which put exactly the PRs a risk grade helps most (the 150-file ones) in the ungraded
  #     lane.
  # REST /pulls/{n}/files answers both: `previous_filename` carries the origin, and --paginate
  # walks every page. GitHub caps that endpoint at 3000 files; a short read is detected below
  # against GraphQL's own changedFiles count and reported `unknown` rather than graded.
  fstatus=ok
  files="$(gh api --paginate "repos/$repo/pulls/$num/files?per_page=100" \
             --jq '.[] | {path: .filename,
                          previous_path: (.previous_filename // null),
                          additions: .additions, deletions: .deletions,
                          change_type: ((.status // "") | ascii_upcase
                                        | if . == "REMOVED" then "DELETED" else . end)}' 2>"$errf" \
           | jq -sc '.')" || { files=null; fstatus=unreadable; }
  [ -n "$files" ] || { files=null; fstatus=unreadable; }
  # Same reason as the GraphQL read above: an unreadable file list drops the whole PR to
  # `changed_paths_status: unreadable` (the path floor goes unknown), and without gh's error text
  # the log says only "could not be read" — a 403 from a short `pull-requests` grant and a 502
  # are then the same message. This one is a WARN, not a return: the record is still emitted with
  # its status twin set, and the grader's unknown contract handles it from there.
  [ "$fstatus" = ok ] || warn "changed-file read failed for $repo#$num: $(tr '\n' ' ' < "$errf" | grep . || echo '(no error text — the read produced nothing)')"
  rm -f "$errf"

  jq -c --arg repo "$repo" --arg self "$SELF_CONTEXT" --arg selfrun "$SELF_RUN_ID" \
        --argjson files "$files" --arg fstatus "$fstatus" '
      def is_failing: (.__typename == "CheckRun" and ((.conclusion // "") | IN("FAILURE","TIMED_OUT","CANCELLED","ACTION_REQUIRED","STARTUP_FAILURE")))
                      or (.__typename == "StatusContext" and ((.state // "") | IN("ERROR","FAILURE")));
      def is_pending: (.__typename == "CheckRun" and ((.status != "COMPLETED") or ((.conclusion // "") == "STALE")))
                      or (.__typename == "StatusContext" and ((.state // "") | IN("PENDING","EXPECTED")));
      def is_success: (.__typename == "CheckRun" and ((.conclusion // "") == "SUCCESS"))
                      or (.__typename == "StatusContext" and ((.state // "") == "SUCCESS"));
      # Ours by RUN ID when the caller supplied one (exact), else by workflow display name.
      def is_self($self; $selfrun): .__typename == "CheckRun"
        and (if $selfrun != "" then ((.checkSuite.workflowRun.databaseId // -1) | tostring) == $selfrun
             else (($self != "") and ((.checkSuite.workflowRun.workflow.name // "") == $self)) end);
      .data.repository.pullRequest
    | ([.labels.nodes[]? | .name] | sort) as $labels
    | (.labels.pageInfo.hasNextPage // false) as $labels_trunc
    | (.commits.nodes[0].commit.statusCheckRollup) as $ro
    # Effective check state. Without a self selector: the raw rollup, the same signal the
    # offline corpus grader reads. With one: the self-excluding aggregate described above.
    | (if ($self == "" and $selfrun == "") or ($ro == null)
       then {state: ($ro.state // null), pending: false, status: "ok"}
       elif ($ro.contexts.pageInfo.hasNextPage // false)
       then {state: null, pending: false, status: "unknown"}
       else
         ([$ro.contexts.nodes[]]) as $all
         | ([$all[] | select(is_self($self; $selfrun) | not)]) as $ctx
         | (if   any($all[]; is_failing) then "FAILURE"
            elif any($ctx[]; is_pending) then "PENDING"
            elif any($ctx[]; is_success) then "SUCCESS"
            elif ($ctx | length) > 0     then "NEUTRAL"
            else null end) as $st
         | {state: $st, pending: ($st == "PENDING"), status: "ok"}
       end) as $checks
    # The changed-file read, with its status twin. A list SHORTER than changedFiles is a
    # truncated read, and a truncated read is `unknown` — never a floor computed from the
    # subset of files that happened to fit.
    | (if $fstatus != "ok" or $files == null
       then {list: null, status: "unreadable", reason: "the changed-file list could not be read from the pulls/{n}/files API"}
       elif ($files | length) < (.changedFiles // 0)
       then {list: null, status: "unknown",
             reason: "changed-file list is short (\($files | length) of \(.changedFiles)) — GitHub caps the files API at 3000 files"}
       else {list: $files, status: "ok", reason: null} end) as $fread
    | {schema_version:3, repo:$repo, pr:.number, title:.title, author:(.author.login // null),
       author_association:.authorAssociation, is_fork:(.isCrossRepository // false),
       labels:$labels, agent_coded:($labels | index("agent-coded") != null),
       labels_status:(if $labels_trunc then "unknown" else "ok" end),
       created_at:.createdAt, updated_at:.updatedAt, closed_at:.closedAt, merged_at:.mergedAt,
       base_ref:.baseRefName, head_ref:.headRefName, is_draft:.isDraft,
       additions:.additions, deletions:.deletions, changed_files:.changedFiles,
       # The status twins the fleet collector emits, so a live grade and a corpus grade of the
       # same PR read the same fields. checks_status is `unknown` only when the context list
       # was truncated; a null state is an answer from GitHub, not an un-asked question.
       checks_state:$checks.state,
       checks_status:$checks.status, provenance_status:"ok",
       checks_pending_excl_self:$checks.pending,
       outcome:(if .mergedAt != null then "merged" elif .state == "CLOSED" then "closed_unmerged" else "open" end),
       changed_paths:$fread.list,
       changed_paths_status:$fread.status,
       changed_paths_reason:$fread.reason}' <<<"$resp"
}

# ---- main --------------------------------------------------------------------------------------
main() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --repo)          REPO="${2:-}"; shift 2 || die "--repo needs a value" ;;
      --pr)            PR_NUM="${2:-}"; MODE="pr"; shift 2 || die "--pr needs a value" ;;
      --stdin)         MODE="stdin"; shift ;;
      --map)           RISK_MAP="${2:-}"; shift 2 || die "--map needs a value" ;;
      --runbooks)      RUNBOOKS="${2:-}"; shift 2 || die "--runbooks needs a value" ;;
      --fleet-logins)  FLEET_LOGINS="${2:-}"; shift 2 || die "--fleet-logins needs a value" ;;
      --bot-logins)    BOT_LOGINS="${2:-}"; shift 2 || die "--bot-logins needs a value" ;;
      --self-context)  SELF_CONTEXT="${2:-}"; shift 2 || die "--self-context needs a value" ;;
      --self-run-id)   SELF_RUN_ID="${2:-}"; shift 2 || die "--self-run-id needs a value" ;;
      -h|--help)       usage; exit 0 ;;
      *)               usage; die "unknown argument '$1'" ;;
    esac
  done

  [ -n "$MODE" ] || { usage; die "one of --pr or --stdin is required"; }
  command -v jq >/dev/null 2>&1 || die "jq not found on PATH"
  if [ "$MODE" = pr ]; then
    [[ "$REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die "bad --repo '$REPO' (want owner/name)"
    [[ "$PR_NUM" =~ ^[0-9]+$ ]] || die "bad --pr '$PR_NUM'"
    [ -z "$SELF_RUN_ID" ] || [[ "$SELF_RUN_ID" =~ ^[0-9]+$ ]] || die "bad --self-run-id '$SELF_RUN_ID' (want a numeric github.run_id)"
    command -v gh >/dev/null 2>&1 || die "gh not found on PATH"
  fi

  # The REASON is captured alongside the value in ONE call each: re-running read_map just to
  # collect its stderr would read the file twice, and the two reads could disagree. An unusable
  # map is fatal — grading against one would grade every PR R0.
  local map rb errf
  errf="$(mktemp "${TMPDIR:-/tmp}/grade-pr-risk-err.XXXXXX")" || die "mktemp failed"
  map="$(read_map "$RISK_MAP" map 2>"$errf")" \
    || die "risk map unusable ($RISK_MAP): $(tr '\n' ' ' < "$errf")— refusing to grade: an unusable map would grade every PR R0"
  rb="$(read_map "$RUNBOOKS" runbooks 2>"$errf")" \
    || die "runbook registry unusable ($RUNBOOKS): $(tr '\n' ' ' < "$errf")— refusing to grade"
  rm -f "$errf"

  case "$MODE" in
    pr)
      local rec
      rec="$(fetch_pr_record "$REPO" "$PR_NUM")" \
        || { warn "PR $REPO#$PR_NUM was UNREADABLE — nothing graded (this is NOT 'no risk')"; exit 3; }
      local graded; graded="$(printf '%s\n' "$rec" | grade_stream "$map" "$rb")"
      [ -n "$graded" ] || die "the grading pass produced nothing for $REPO#$PR_NUM — NOTHING was graded; this is not 'no risk'"
      jq . <<<"$graded"
      local st; st="$(jq -r '.risk.status' <<<"$graded")"
      [ "$st" = ok ] || { warn "grade is UNKNOWN for $REPO#$PR_NUM"; exit 1; }
      exit 0 ;;
    stdin)
      # Per-line tolerant read, same contract as the fleet's corpus path: one corrupt line
      # drops exactly that line, never every valid record after it.
      local tmp; tmp="$(mktemp -d "${TMPDIR:-/tmp}/grade-pr-risk.XXXXXX")" || die "mktemp failed"
      # shellcheck disable=SC2064
      trap "rm -rf '$tmp'" EXIT
      jq -R -c 'fromjson? | select(type == "object")' > "$tmp/in.jsonl" 2>/dev/null
      local kept; kept="$(wc -l < "$tmp/in.jsonl" | tr -d ' ')"
      [ "$kept" -gt 0 ] || { warn "stdin yielded no parseable records — NOTHING graded"; exit 3; }
      # A grading pass that FAILED must never read as a graded corpus: both the rc and the
      # output count are checked, so a pass that produced nothing can never report a clean run.
      if ! grade_stream "$map" "$rb" < "$tmp/in.jsonl" > "$tmp/graded.jsonl"; then
        die "the grading pass FAILED (jq returned non-zero) — NOTHING was graded"
      fi
      local produced; produced="$(wc -l < "$tmp/graded.jsonl" | tr -d ' ')"
      [ "$produced" -eq "$kept" ] || die "the grading pass produced $produced record(s) from $kept input(s) — refusing to report a partial pass as a clean one"
      cat "$tmp/graded.jsonl"
      local unknown; unknown="$(jq -s '[.[] | select(.risk.status != "ok")] | length' "$tmp/graded.jsonl")"
      [ "$unknown" -gt 0 ] && { warn "$unknown record(s) graded UNKNOWN — reported, never a confident tier"; exit 1; }
      exit 0 ;;
  esac
}

# Sourceable without side effects (the test suite sources this file to exercise the grader
# directly); only a direct invocation runs it.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  main "$@"
fi
