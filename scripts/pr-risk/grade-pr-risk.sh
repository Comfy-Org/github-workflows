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

    # ---- AXIS 1: PATH FLOOR ---------------------------------------------------------------
    # WORST over every rule any changed path matches. An R0 rule (docs, tests) can never cancel
    # an R3 rule (migrations) in the same PR — that is why this is a max, not a last-match-wins.
    | (if $r.changed_paths_status != "ok" or $paths == null
       then {tier:null, status:"unknown",
             reason:("changed-path list is " + ($r.changed_paths_status // "absent") + " — a PR whose files we cannot read is exactly the PR that might touch auth"),
             classes:null}
       else
         ([$M.path_rules[]? | . as $rule | select($plist | any(. as $p | $p | matches_any($rule.paths)))]) as $hit
         | {tier: (reduce $hit[] as $h ($DEF; worst(.; $h.tier))),
            status:"ok",
            reason: (if ($hit|length) == 0 then "no mapped path touched — floor \($DEF)"
                     else "matched " + ([$hit[] | "\(.class)=\(.tier)"] | join(", ")) end),
            classes: [$hit[] | .class]}
       end) as $A1

    # ---- AXIS 2: PROVENANCE ---------------------------------------------------------------
    # Identity first (server-attributed author login, classified by the shared resolver), then
    # the runbook shape assertion. A claimed runbook that fails its shape assertion is NOT a
    # runbook — provenance alone is never sufficient.
    | ($r.author // null) as $author
    | ($author | classify_login($fleetl; $botl)) as $cls
    | (($r.labels // []) | index("agent-coded") != null) as $agent_coded
    # `external` is decided from is_fork + author_association, and those arrive with a STATUS
    # twin — whether they were actually read has to be asked before they are believed. Reading
    # an un-collected `is_fork` as "not a fork" would make `external => R3` — the one provenance
    # class never routed unattended — silently unreachable. Unread is `unknown`, and `unknown`
    # refuses to grade the axis.
    | ($r.provenance_status // (if ($r | has("is_fork")) then "ok" else "absent" end)) as $pvst
    | (if $pvst != "ok" then "unknown"
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
                      else ($plist | length) > 0 and all($plist[]; matches_any($bk.permitted_paths // [])) end),
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
                     else "PR author did not resolve to a GitHub account — provenance is unattributable" end),
             provenance:null}
       else {tier: (($M.provenance_tiers // {})[$prov] // "R1"), status:"ok",
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
         | ([$paths[] | select(.change_type == "DELETED") | .path]) as $deleted
         | (($A1.classes // []) | any(. as $c | ($RV.delete_sensitive_classes // []) | index($c))) as $del_sensitive
         | ($plist | any(matches_any($M.flippable_flag_paths // []))) as $flag
         | ($plist | any(test("(_test\\.|\\.test\\.|\\.spec\\.|(^|/)tests?/|-test\\.sh$)"))) as $touched_test
         | ($r.checks_state // null) as $checks
         | (if ($irrev | length) > 0
              then {t:"R3", why:("touches " + ($irrev|join(", ")) + " — mutates persistent state or deletes data; reverting the code does not restore it")}
            elif (($deleted | length) > 0 and $del_sensitive)
              then {t:"R3", why:("deletes " + ($deleted|length|tostring) + " file(s) under a sensitive class — not a single clean revert")}
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
# would be an artifact of the measurement. With --self-context <caller workflow name> the
# rollup is therefore recomputed from the individual contexts, EXCLUDING check runs belonging
# to that workflow: any remaining failure => FAILURE, any remaining pending => PENDING, any
# remaining success => SUCCESS, nothing else on the commit => null (the honest "no CI" case).
# `checks_pending_excl_self` is emitted alongside so a CI caller can wait for the rest of the
# rollup to settle instead of labeling a snapshot of half-finished checks. More than 100
# contexts is `unknown`, never a truncated aggregate.
fetch_pr_record() { # <repo> <num> -> record JSON on stdout, rc 1 on an unreadable PR
  local repo="$1" num="$2" q resp
  # shellcheck disable=SC2016  # GraphQL: $vars are query variables
  q='query($owner:String!,$name:String!,$num:Int!){
  repository(owner:$owner,name:$name){ pullRequest(number:$num){
    number title state isDraft createdAt updatedAt closedAt mergedAt
    author{ login } authorAssociation baseRefName headRefName isCrossRepository
    additions deletions changedFiles
    labels(first:20){ nodes{ name } }
    commits(last:1){ nodes{ commit{ statusCheckRollup{ state
      contexts(first:100){ pageInfo{ hasNextPage } nodes{ __typename
        ... on CheckRun{ name status conclusion checkSuite{ workflowRun{ workflow{ name } } } }
        ... on StatusContext{ context state } } } } } } }
    files(first:100){ pageInfo{ hasNextPage } nodes{ path additions deletions changeType } }
  } } }'
  resp="$(gh api graphql -f query="$q" -F owner="${repo%%/*}" -F name="${repo##*/}" -F num="$num" 2>/dev/null)" || return 1
  jq -e '.data.repository.pullRequest.number != null' >/dev/null 2>&1 <<<"$resp" || return 1
  jq -c --arg repo "$repo" --arg self "$SELF_CONTEXT" '.data.repository.pullRequest
    | ([.labels.nodes[]? | .name] | sort) as $labels
    | (.commits.nodes[0].commit.statusCheckRollup) as $ro
    # Effective check state. Without --self-context ($self == ""): the raw rollup, the same
    # signal the offline corpus grader reads. With it: the self-excluding aggregate above.
    | (if ($self == "") or ($ro == null)
       then {state: ($ro.state // null), pending: false, status: "ok"}
       elif ($ro.contexts.pageInfo.hasNextPage // false)
       then {state: null, pending: false, status: "unknown"}
       else
         ([$ro.contexts.nodes[]
           | select(((.__typename == "CheckRun") and ((.checkSuite.workflowRun.workflow.name // "") == $self)) | not)]) as $ctx
         | (if any($ctx[]; (.__typename == "CheckRun" and ((.conclusion // "") | IN("FAILURE","TIMED_OUT","CANCELLED","ACTION_REQUIRED","STARTUP_FAILURE")))
                        or (.__typename == "StatusContext" and (.state | IN("ERROR","FAILURE"))))
            then "FAILURE"
            elif any($ctx[]; (.__typename == "CheckRun" and ((.status != "COMPLETED") or ((.conclusion // "") == "STALE")))
                          or (.__typename == "StatusContext" and (.state | IN("PENDING","EXPECTED"))))
            then "PENDING"
            elif ($ctx | length) > 0 then "SUCCESS"
            else null end) as $st
         | {state: $st, pending: ($st == "PENDING"), status: "ok"}
       end) as $checks
    | {schema_version:3, repo:$repo, pr:.number, title:.title, author:(.author.login // null),
       author_association:.authorAssociation, is_fork:(.isCrossRepository // false),
       labels:$labels, agent_coded:($labels | index("agent-coded") != null),
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
       changed_paths:(if (.files.pageInfo.hasNextPage // false) then null
                      else [.files.nodes[] | {path:.path, additions:.additions, deletions:.deletions, change_type:.changeType}] end),
       changed_paths_status:(if (.files.pageInfo.hasNextPage // false) then "unknown" else "ok" end),
       changed_paths_reason:(if (.files.pageInfo.hasNextPage // false) then "file list truncated at 100 files" else null end)}' <<<"$resp"
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
      -h|--help)       usage; exit 0 ;;
      *)               usage; die "unknown argument '$1'" ;;
    esac
  done

  [ -n "$MODE" ] || { usage; die "one of --pr or --stdin is required"; }
  command -v jq >/dev/null 2>&1 || die "jq not found on PATH"
  if [ "$MODE" = pr ]; then
    [[ "$REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die "bad --repo '$REPO' (want owner/name)"
    [[ "$PR_NUM" =~ ^[0-9]+$ ]] || die "bad --pr '$PR_NUM'"
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
