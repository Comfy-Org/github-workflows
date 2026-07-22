#!/usr/bin/env python3
"""Recompute the reviewer expertise map (reviewers.yml) from git history.

Backs the reusable `refresh-reviewers.yml` workflow (BE-4114 spike → BE-4116).
Reads the caller repo's committed reviewers.yml (the same config
`assign-reviewers.yml` consumes at runtime), walks the default branch's git
history over a decaying window, scores each collaborator's expertise per rule
bucket, and surgically rewrites ONLY the `reviewers: [...]` / `default_pool:
[...]` lists — every comment and all other bytes are preserved, because the
config's comments are its documentation.

Signal model (validated against cloud history in the BE-4114 spike):
  - per commit, per rule bucket touched by >=1 surviving file:
      score[bucket][login] += 0.5 ** (age_days / half_life_days)
      touches[bucket][login] += 1
  - line counts are intentionally unused (numstat is only the file list);
    recency-decayed commit touches aged out stale expertise correctly where
    line-weighted variants did not.

Exclusions:
  - bot authors (email matches `\\[bot\\]@` or noreply@argoproj.io),
  - generated/churn paths (codegen, vendored deps, lockfiles, mechanical
    version-bump files) plus caller-supplied EXTRA_EXCLUDE_PATHS regexes,
  - logins outside the repo's collaborator set (the exact eligibility test the
    runtime applies: `addAssignees` silently drops non-collaborators),
  - MAP_EXCLUDE logins (e.g. an operator whose commits are agent-authored).

Trust model: scoring reads unauthenticated git author metadata. Anyone with
push access to the default branch can forge another user's noreply email (the
commits API attributes by email too, so API resolution inherits the same
limit) or a future author date (clamped to full weight). That forger is by
definition already a collaborator, and the only output is a drift PR a human
reviews — so the exposure is accepted rather than "fixed" with signature
checks the underlying data can't support.

This script is a drift DETECTOR, never a live mutator: it emits the rewritten
config + a machine-readable report for the workflow's PR step and exits.
Environmental problems (missing config, unreachable API) are downgraded to a
clean no-op with a `::warning::` — same never-fail posture as
assign-reviewers.yml — while real bugs still raise.

Environment (all optional unless noted):
  GITHUB_REPOSITORY     owner/repo of the caller (required)
  GH_TOKEN              app token for the commits/collaborators API (required)
  DEFAULT_BRANCH        the caller's default branch (required)
  REVIEWER_CONFIG_PATH  path to reviewers.yml   (default .github/reviewers.yml)
  WINDOW_MONTHS         history window          (default 12)
  HALF_LIFE_DAYS        decay half-life         (default 90)
  TOP_K                 max experts per rule    (default 4)
  FLOOR                 min experts per rule    (default 2)
  MIN_TOUCHES           qualify: raw touches    (default 5)
  MIN_SCORE             qualify: decayed score  (default 1.5)
  FLOOR_MIN_TOUCHES     backfill touch floor    (default 2)
  MAP_EXCLUDE           whitespace-separated logins never placed in the map
  EXTRA_EXCLUDE_PATHS   newline-separated regexes appended to the built-ins
  RESULTS_DIR           where outputs land      (default $RUNNER_TEMP or .)
  GITHUB_API_URL        API base                (default https://api.github.com)
  GITHUB_OUTPUT         step-output file (written when present)

Outputs (in RESULTS_DIR): reviewers.new.yml, report.json, pr-body.md.
Step outputs: changed=true|false, new_config_path, report_path, pr_body_path.
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict

BOT_EMAIL_RX = re.compile(r"(\[bot\]@|noreply@argoproj\.io)", re.IGNORECASE)
NOREPLY_RX = re.compile(r"^(?:\d+\+)?([^@]+)@users\.noreply\.github\.com$", re.IGNORECASE)
RENAME_BRACE_RX = re.compile(r"\{[^}]* => ([^}]*)\}")

# Generated/churn paths whose commits carry no expertise signal. Regexes are
# re.search()'d against the (rename-normalized) path.
BUILTIN_EXCLUDE_PATHS = [
    r"(^|/)ent/(?!schema/)",  # ent codegen; hand-written schema/ still counts
    r"\.gen\.go$",
    r"\.pb\.go$",
    r"(^|/)vendor/",
    r"(^|/)(go\.sum|go\.work[^/]*\.sum)$",
    r"(^|/)(package-lock\.json|pnpm-lock\.yaml|yarn\.lock)$",
    r"\.lock$",
    # mechanical version-bump churn (e.g. "update staging template to 0.9.37")
    r"^infrastructure/dynamicconfig/[^/]+/config\.json$",
    r"^frontend-version\.json$",
]


# --- glob semantics (parity with assign-reviewers.yml's globToRegExp) --------

def glob_to_regexp(glob):
    """Port of assign-reviewers.yml's globToRegExp: `*` within a segment,
    `**` across segments (`**/` -> optional leading dirs), `?` one non-slash
    char. Full-string anchored. Must stay byte-for-byte semantics-equal to the
    JS original — the map is only correct if it is scored with the same
    matcher the runtime assigns with."""
    out = ""
    i = 0
    n = len(glob)
    while i < n:
        c = glob[i]
        if c == "*":
            if i + 1 < n and glob[i + 1] == "*":
                if i + 2 < n and glob[i + 2] == "/":
                    out += "(?:.*/)?"
                    i += 3
                    continue
                out += ".*"
                i += 2
                continue
            out += "[^/]*"
        elif c == "?":
            out += "[^/]"
        elif c in ".+^${}()|[]\\/":
            out += "\\" + c
        else:
            out += c
        i += 1
    return re.compile("^" + out + "$")


def matches_any(path, compiled_globs):
    return any(rx.match(path) for rx in compiled_globs)


# --- reviewers.yml parsing (parity with parseReviewerConfig) -----------------
#
# Mirrors assign-reviewers.yml's minimal parser (default_pool + rules[{paths,
# reviewers}], flow or block sequences, comment stripping) but ALSO records
# where each reviewers/default_pool list lives so the rewrite can touch only
# those bytes. Location shapes:
#   ("flow", line_idx)              — `reviewers: [a, b]` (also bare scalar)
#   ("block", [line_idx, ...], indent) — `- a` item lines

def _strip_comment(s):
    in_s = in_d = False
    for i, ch in enumerate(s):
        if ch == "'" and not in_d:
            in_s = not in_s
        elif ch == '"' and not in_s:
            in_d = not in_d
        elif ch == "#" and not in_s and not in_d and (i == 0 or s[i - 1].isspace()):
            return s[:i]
    return s


def _unquote(s):
    s = s.strip()
    if len(s) >= 2 and ((s[0] == '"' and s[-1] == '"') or (s[0] == "'" and s[-1] == "'")):
        return s[1:-1]
    return s


def _parse_flow(s):
    s = s.strip()
    if not s.startswith("["):
        return None
    end = s.find("]")
    inner = s[1:end] if end != -1 else s[1:]
    return [x for x in (_unquote(p) for p in inner.split(",")) if x]


def _indent_of(line):
    return len(line) - len(line.lstrip(" "))


def parse_reviewer_config(text):
    """Return (config, locations).

    config    = {"default_pool": [...], "rules": [{"paths": [...],
                 "reviewers": [...]}, ...]}          (parser-parity shape)
    locations = {"default_pool": loc-or-None,
                 "rules": [loc-or-None, ...]}        (reviewers-list positions)
    """
    raw_lines = text.split("\n")
    lines = [_strip_comment(l) for l in raw_lines]
    config = {"default_pool": [], "rules": []}
    locs = {"default_pool": None, "rules": []}

    i = 0
    n = len(lines)
    while i < n:
        raw = lines[i]
        line = raw.strip()
        if not line:
            i += 1
            continue
        if _indent_of(raw) == 0 and line.startswith("default_pool:"):
            rest = line[len("default_pool:"):].strip()
            flow = _parse_flow(rest)
            if flow is not None:
                config["default_pool"] = flow
                locs["default_pool"] = ("flow", i)
                i += 1
                continue
            i += 1
            items = []
            item_lines = []
            indent = 2
            while i < n:
                r = lines[i]
                if not r.strip():
                    i += 1
                    continue
                if _indent_of(r) == 0:
                    break
                t = r.strip()
                if t.startswith("- "):
                    items.append(_unquote(t[2:]))
                    item_lines.append(i)
                    indent = _indent_of(r)
                i += 1
            config["default_pool"] = items
            if item_lines:
                locs["default_pool"] = ("block", item_lines, indent)
            continue
        if _indent_of(raw) == 0 and line.startswith("rules:"):
            i += 1
            current = None
            cur_loc = None
            list_key = None
            rule_indent = -1

            def set_key(seg, line_idx):
                nonlocal list_key
                m = re.match(r"^(paths|reviewers):(.*)$", seg)
                if not m:
                    return
                key, val = m.group(1), m.group(2).strip()
                flow = _parse_flow(val)
                if flow is not None:
                    if current is not None:
                        current[key] = flow
                        if key == "reviewers":
                            cur_loc["reviewers"] = ("flow", line_idx)
                    list_key = None
                elif val == "":
                    list_key = key
                else:
                    if current is not None:
                        current[key] = [_unquote(val)]
                        if key == "reviewers":
                            # bare scalar — rewritten as a flow sequence
                            cur_loc["reviewers"] = ("flow", line_idx)
                    list_key = None

            while i < n:
                r = lines[i]
                if not r.strip():
                    i += 1
                    continue
                if _indent_of(r) == 0:
                    break
                ind = _indent_of(r)
                t = r.strip()
                is_dash = t == "-" or t.startswith("- ")
                if is_dash and (rule_indent == -1 or ind == rule_indent):
                    if rule_indent == -1:
                        rule_indent = ind
                    current = {"paths": [], "reviewers": []}
                    cur_loc = {"reviewers": None}
                    config["rules"].append(current)
                    locs["rules"].append(cur_loc)
                    list_key = None
                    after_dash = t[1:].strip()
                    if after_dash:
                        set_key(after_dash, i)
                elif is_dash and list_key and current is not None:
                    current[list_key].append(_unquote(t[1:].strip()))
                    if list_key == "reviewers":
                        if cur_loc["reviewers"] is None:
                            cur_loc["reviewers"] = ("block", [], ind)
                        if cur_loc["reviewers"][0] == "block":
                            cur_loc["reviewers"][1].append(i)
                else:
                    set_key(t, i)
                i += 1
            continue
        i += 1
    # locs["rules"] holds dicts internally; expose just the reviewers loc.
    locs["rules"] = [r["reviewers"] for r in locs["rules"]]
    return config, locs


# --- surgical rewrite --------------------------------------------------------

def _rewrite_flow_line(line, key, logins):
    """Replace only the `[...]` span on a flow line, preserving everything
    else (leading bytes, spacing, trailing comment). A bare-scalar value is
    converted to a flow sequence up to the trailing comment. Brackets are
    located in the comment-STRIPPED portion so a `[` inside a trailing
    comment can never be mistaken for the flow sequence."""
    new_list = "[" + ", ".join(logins) + "]"
    stripped = _strip_comment(line)
    key_pos = stripped.find(key)
    open_idx = stripped.find("[", key_pos)
    if open_idx != -1:
        close_idx = stripped.find("]", open_idx)
        end = close_idx + 1 if close_idx != -1 else len(stripped.rstrip())
        return line[:open_idx] + new_list + line[end:]
    # scalar form: `reviewers: alice  # note` -> replace the value span only
    key_end = key_pos + len(key)
    return line[:key_end] + " " + new_list + line[len(stripped.rstrip()):]


def rewrite_config(text, locs, rule_replacements, default_pool_replacement):
    """Rewrite ONLY the reviewer lists named in the replacement maps.

    rule_replacements: {rule_index: [logins]} — rules absent from the map keep
    their bytes untouched. default_pool_replacement: [logins] or None.
    Everything outside the replaced flow spans / block item lines — comments
    included — is preserved byte-for-byte."""
    lines = text.split("\n")
    # (loc, key, logins) for every list being replaced
    jobs = []
    if default_pool_replacement is not None and locs["default_pool"] is not None:
        jobs.append((locs["default_pool"], "default_pool:", default_pool_replacement))
    for idx, logins in rule_replacements.items():
        if idx < len(locs["rules"]) and locs["rules"][idx] is not None:
            jobs.append((locs["rules"][idx], "reviewers:", logins))

    drop = set()          # block item lines to remove
    insert_at = {}        # first block item line -> replacement item lines
    for loc, key, logins in jobs:
        if loc[0] == "flow":
            lines[loc[1]] = _rewrite_flow_line(lines[loc[1]], key, logins)
        else:
            _, item_lines, indent = loc
            drop.update(item_lines)
            insert_at[item_lines[0]] = [" " * indent + "- " + l for l in logins]

    out = []
    for i, line in enumerate(lines):
        if i in insert_at:
            out.extend(insert_at[i])
        if i in drop:
            continue
        out.append(line)
    return "\n".join(out)


# --- git log parsing ---------------------------------------------------------

def _git_unquote(path):
    """Decode git's C-style path quoting (core.quotePath: `\\t`, `\\"`,
    `\\\\`, `\\ooo` octal for non-ASCII bytes). Quoted output is ASCII, so
    decode escapes then reassemble the raw bytes as UTF-8; malformed input
    falls back to the bare inner string."""
    if not (len(path) >= 2 and path[0] == '"' and path[-1] == '"'):
        return path
    inner = path[1:-1]
    try:
        return (inner.encode("ascii", "backslashreplace")
                .decode("unicode_escape")
                .encode("latin-1").decode("utf-8", "replace"))
    except (UnicodeDecodeError, UnicodeEncodeError):
        return inner


def normalize_numstat_path(path):
    """Normalize a numstat path: decode git's quoting, resolve both rename
    syntaxes (`a/{old => new}/c` and whole-path `old => new`) to the NEW
    path. A filename legitimately containing ` => ` is indistinguishable from
    the whole-path rename form in numstat's line output — accepted ambiguity
    (vanishingly rare, and the cost is one file scored against the wrong
    bucket)."""
    path = _git_unquote(path)
    path = RENAME_BRACE_RX.sub(r"\1", path).replace("//", "/")
    if " => " in path:
        path = path.rsplit(" => ", 1)[1]
    return path


def parse_log(lines):
    """Parse `git log --format='@%H|%ad|%ae' --numstat --date=unix` output
    into (sha, timestamp, email, [normalized paths]) tuples."""
    commits = []
    cur = None
    for line in lines:
        line = line.rstrip("\n")
        if line.startswith("@") and line.count("|") >= 2:
            sha, ad, ae = line[1:].split("|", 2)
            try:
                ts = int(ad)
            except ValueError:
                cur = None
                continue
            cur = (sha, ts, ae, [])
            commits.append(cur)
        elif cur is not None and "\t" in line:
            parts = line.split("\t", 2)
            if len(parts) == 3:
                cur[3].append(normalize_numstat_path(parts[2]))
    return commits


def email_to_login(email):
    """Decode a GitHub noreply email (`login@` or `digits+login@`) to its
    login; None for anything else (those need the commits API). Login case is
    preserved — membership matching canonicalizes case-insensitively."""
    m = NOREPLY_RX.match(email.strip())
    return m.group(1) if m else None


# --- scoring -----------------------------------------------------------------

def decay_weight(age_days, half_life_days):
    return 0.5 ** (max(age_days, 0.0) / half_life_days)


def compute_scores(commits, rule_globs, exclude_rxs, now, half_life_days):
    """Score resolved commits against the rule buckets.

    commits: iterable of (login, ts, paths) — already bot-filtered, resolved,
    and membership-filtered. rule_globs: per-rule lists of compiled glob
    regexes. Returns (score, touches, overall, gap):
      score[i][login]   decayed per-rule score
      touches[i][login] raw per-rule commit touches
      overall[login]    whole-repo decayed score (any surviving file)
      gap[topdir][login] decayed score of commits touching >=1 file matching
                         NO rule, keyed by the file's top-two-level DIRECTORY
                         ("(root)" for top-level files). Accumulated once per
                         commit per key — same commit-touch semantics as the
                         rule scores, so the two columns stay comparable.
    """
    score = [defaultdict(float) for _ in rule_globs]
    touches = [defaultdict(int) for _ in rule_globs]
    overall = defaultdict(float)
    gap = defaultdict(lambda: defaultdict(float))
    for login, ts, paths in commits:
        w = decay_weight((now - ts) / 86400.0, half_life_days)
        touched = set()
        gap_keys = set()
        any_file = False
        for path in paths:
            if any(rx.search(path) for rx in exclude_rxs):
                continue
            any_file = True
            matched = False
            for i, globs in enumerate(rule_globs):
                if matches_any(path, globs):
                    touched.add(i)
                    matched = True
            if not matched:
                dirs = path.split("/")[:-1]
                gap_keys.add("/".join(dirs[:2]) if dirs else "(root)")
        if not any_file:
            continue
        overall[login] += w
        for key in gap_keys:
            gap[key][login] += w
        for i in touched:
            score[i][login] += w
            touches[i][login] += 1
    return score, touches, overall, gap


# --- selection ---------------------------------------------------------------

def select_for_rule(score_map, touch_map, top_k, floor, min_touches, min_score,
                    floor_min_touches):
    """Pick a rule's reviewers. Returns (picks, under_floor, starred) where
    `starred` marks floor-backfill picks (below the main thresholds). Ranked
    by score desc, login asc for determinism."""
    ranked = sorted(score_map.items(), key=lambda kv: (-kv[1], kv[0]))
    picks = [l for l, s in ranked
             if s >= min_score and touch_map.get(l, 0) >= min_touches][:top_k]
    starred = set()
    if len(picks) < floor:
        for l, _s in ranked:
            if len(picks) >= floor:
                break
            if l not in picks and touch_map.get(l, 0) >= floor_min_touches:
                picks.append(l)
                starred.add(l)
    return picks, len(picks) < floor, starred


def select_default_pool(overall, final_rule_lists, map_exclude, size=5,
                        max_anchored_rules=1):
    """Top-`size` whole-repo scorers, skipping anyone already anchoring more
    than `max_anchored_rules` rules (the #5448 anti-pile-on rationale) and
    MAP_EXCLUDE logins. `overall` is already collaborator-only."""
    anchored = defaultdict(int)
    for logins in final_rule_lists:
        for l in set(logins):
            anchored[l] += 1
    excluded = {x.lower() for x in map_exclude}
    ranked = sorted(overall.items(), key=lambda kv: (-kv[1], kv[0]))
    return [l for l, _s in ranked
            if anchored[l] <= max_anchored_rules and l.lower() not in excluded][:size]


# --- GitHub API --------------------------------------------------------------

# Sentinel distinct from None: "the API call FAILED" (timeout / 5xx / 403 /
# bad JSON) vs "the API answered and the answer is empty". Callers must never
# collapse the two — a transient failure that masquerades as "author has no
# linked account" silently drops that contributor's history and biases the
# proposal (the documented posture for environmental problems is a clean
# no-op, not a skewed map).
API_ERROR = object()


def gh_get(url, token):
    """GET a GitHub API URL; API_ERROR on any transport/HTTP failure so
    callers can tell an outage from a genuinely empty answer."""
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "User-Agent": "refresh-reviewers",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        print(f"::warning::GET {url.split('?')[0]} failed: {e}")
        return API_ERROR


def fetch_collaborators(api, repo, token):
    """All collaborator logins (paginated); None if the API is unavailable."""
    logins = set()
    page = 1
    while True:
        data = gh_get(f"{api}/repos/{repo}/collaborators?per_page=100&page={page}", token)
        if data is API_ERROR or not isinstance(data, list):
            return None
        for c in data:
            if isinstance(c, dict) and c.get("login"):
                logins.add(c["login"])
        if len(data) < 100:
            break
        page += 1
    return logins


def resolve_email_via_api(api, repo, token, sha):
    """Resolve a commit author email -> login via one commit fetch. Returns
    the login, None when the email has no linked GitHub account, or API_ERROR
    when the call itself failed (callers must not treat that as no-account)."""
    data = gh_get(f"{api}/repos/{repo}/commits/{sha}", token)
    if data is API_ERROR:
        return API_ERROR
    if not isinstance(data, dict):
        return None
    author = data.get("author") or {}
    return author.get("login") or None


# --- report / PR body --------------------------------------------------------

def md_code(s):
    """Wrap repo-derived text (paths, directory names) in a Markdown code
    span that cannot break out of its table cell: GFM honors `\\|` even
    inside code spans within tables, and a backtick would close the span so
    it is swapped for a plain apostrophe. Keeps a crafted filename from
    injecting rows/links/mentions into the bot-authored PR body."""
    return "`" + s.replace("|", "\\|").replace("`", "'") + "`"


def fmt_login_score(login, score_map, touch_map, starred=frozenset()):
    star = "\\*" if login in starred else ""
    if login in score_map:
        return f"{login}{star} ({score_map[login]:.1f}/{touch_map.get(login, 0)})"
    return login


def build_pr_body(report):
    """Render the drift-PR body from the report dict: per-rule before/after
    with scores/touches, unresolved-email count, taxonomy gap report, knobs."""
    k = report["knobs"]
    lines = [
        "## Reviewer expertise map refresh",
        "",
        "Recomputed `" + report["config_path"] + "` from `git log " +
        report["default_branch"] + "` — recency-decayed commit touches per "
        "rule bucket, collaborators only, bots and generated/churn paths "
        "excluded. Entries are `login (decayed score/raw touches)`; `*` marks "
        "a floor backfill below the main thresholds. This PR is the whole "
        "deliverable of the refresh engine: review the proposed map, edit "
        "freely, merge when it looks right.",
        "",
        "### Proposed map",
        "",
        "| rule | paths | before | after |",
        "|---|---|---|---|",
    ]
    for r in report["rules"]:
        paths = "<br>".join(md_code(p) for p in r["paths"]) or "—"
        before = ", ".join(r["before"]) or "—"
        if r["under_floor"]:
            after = "*(unchanged — fewer than floor qualify; runtime falls " \
                    "back to `default_pool` if the rule can't match)*"
        else:
            after = ", ".join(
                fmt_login_score(l, r["scores"], r["touches"], set(r["starred"]))
                for l in r["after"]) or "—"
        lines.append(f"| {r['index']} | {paths} | {before} | {after} |")
    dp = report["default_pool"]
    lines += [
        f"| default_pool | — | {', '.join(dp['before']) or '—'} | "
        + (", ".join(f"{l} ({dp['scores'].get(l, 0.0):.1f})" for l in dp["after"]) or "—")
        + " |",
        "",
        f"Commits dropped (author email unresolvable to a login): "
        f"**{report['unresolved_email_commits']}** · bot commits excluded: "
        f"{report['bot_commits_excluded']}",
        "",
    ]
    if report["gaps"]:
        lines += [
            "### Taxonomy gaps (report-only)",
            "",
            "Hottest top-level areas matched by NO rule glob — candidates for "
            "new rules (not auto-added):",
            "",
            "| area | decayed score | top contributors |",
            "|---|---|---|",
        ]
        for g in report["gaps"]:
            top = ", ".join(f"{t['login']} ({t['score']:.1f})" for t in g["top"])
            lines.append(f"| {md_code(g['dir'])} | {g['score']:.1f} | {top} |")
        lines.append("")
    lines += [
        "### Knobs",
        "",
        f"`window_months={k['window_months']}` `half_life_days={k['half_life_days']}` "
        f"`top_k={k['top_k']}` `floor={k['floor']}` `min_touches={k['min_touches']}` "
        f"`min_score={k['min_score']}` `floor_min_touches={k['floor_min_touches']}` "
        f"`map_exclude={' '.join(k['map_exclude']) or '(none)'}`",
    ]
    return "\n".join(lines) + "\n"


# --- env helpers -------------------------------------------------------------

def _env_int(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        print(f"::warning::{name} is not an integer — using {default}")
        return default


def _env_float(name, default):
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        print(f"::warning::{name} is not a number — using {default}")
        return default


def _env_pos_float(name, default):
    """_env_float, but the knob must be strictly positive — a zero half-life
    would divide by zero and a negative one would invert the decay (older
    commits gaining weight), so both fall back to the default with a warning
    instead of crashing the never-fail run."""
    v = _env_float(name, default)
    if v <= 0:
        print(f"::warning::{name} must be positive (got {v}) — using {default}")
        return float(default)
    return v


def write_outputs(outputs):
    out_file = os.environ.get("GITHUB_OUTPUT")
    if not out_file:
        return
    with open(out_file, "a", encoding="utf-8") as f:
        for key, val in outputs.items():
            f.write(f"{key}={val}\n")


def _noop_exit(reason):
    print(f"::warning::{reason} — nothing to refresh.")
    write_outputs({"changed": "false"})
    return 0


# --- main --------------------------------------------------------------------

def main():
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GH_TOKEN", "")
    branch = os.environ.get("DEFAULT_BRANCH", "")
    if not repo or not token or not branch:
        return _noop_exit("GITHUB_REPOSITORY / GH_TOKEN / DEFAULT_BRANCH must be set")
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    config_path = os.environ.get("REVIEWER_CONFIG_PATH") or ".github/reviewers.yml"
    knobs = {
        "window_months": _env_int("WINDOW_MONTHS", 12),
        "half_life_days": _env_pos_float("HALF_LIFE_DAYS", 90),
        "top_k": _env_int("TOP_K", 4),
        "floor": _env_int("FLOOR", 2),
        "min_touches": _env_int("MIN_TOUCHES", 5),
        "min_score": _env_float("MIN_SCORE", 1.5),
        "floor_min_touches": _env_int("FLOOR_MIN_TOUCHES", 2),
        "map_exclude": (os.environ.get("MAP_EXCLUDE") or "").split(),
    }
    results_dir = os.environ.get("RESULTS_DIR") or os.environ.get("RUNNER_TEMP") or "."
    os.makedirs(results_dir, exist_ok=True)

    # exclusion regexes (built-ins + caller extras; bad extras warn, not
    # fail). The extras are trusted caller workflow config — a caller can
    # already run arbitrary code in their own workflow, so a pathological
    # (catastrophic-backtracking) pattern only stalls the caller's own run
    # until the job timeout; no complexity bound is attempted here.
    exclude_rxs = [re.compile(rx) for rx in BUILTIN_EXCLUDE_PATHS]
    for rx in (os.environ.get("EXTRA_EXCLUDE_PATHS") or "").splitlines():
        rx = rx.strip()
        if not rx:
            continue
        try:
            exclude_rxs.append(re.compile(rx))
        except re.error as e:
            print(f"::warning::skipping invalid EXTRA_EXCLUDE_PATHS regex {rx!r}: {e}")

    # --- committed config (from the default branch, not the checkout ref) ---
    show = subprocess.run(
        ["git", "show", f"refs/remotes/origin/{branch}:{config_path}"],
        capture_output=True, text=True)
    if show.returncode != 0:
        return _noop_exit(f"could not read {config_path} on origin/{branch}")
    committed_text = show.stdout
    config, locs = parse_reviewer_config(committed_text)
    if not config["rules"] and not config["default_pool"]:
        return _noop_exit(f"{config_path} has no rules or default_pool")
    rule_globs = [[glob_to_regexp(g) for g in r.get("paths", [])]
                  for r in config["rules"]]

    # --- git history ---------------------------------------------------------
    log = subprocess.run(
        ["git", "log", f"refs/remotes/origin/{branch}",
         f"--since={knobs['window_months']} months ago", "--no-merges",
         "--date=unix", "--format=@%H|%ad|%ae", "--numstat"],
        capture_output=True, text=True, errors="replace")
    if log.returncode != 0:
        return _noop_exit(f"git log failed: {log.stderr.strip()[:200]}")
    raw_commits = parse_log(log.stdout.splitlines())
    if not raw_commits:
        return _noop_exit(f"no commits in the last {knobs['window_months']} months")

    # --- resolve emails -> logins -------------------------------------------
    bot_commits = 0
    unresolved_commits = 0
    email_login = {}   # lowercased email -> login or None
    email_sha = {}     # lowercased email -> a commit sha for API resolution
    for sha, _ts, email, _paths in raw_commits:
        e = email.strip().lower()
        if BOT_EMAIL_RX.search(e) or e in email_login or e in email_sha:
            continue
        login = email_to_login(email)
        if login is not None:
            email_login[e] = login
        else:
            email_sha.setdefault(e, sha)
    failed_lookups = 0
    for e, sha in email_sha.items():
        login = resolve_email_via_api(api, repo, token, sha)
        if login is API_ERROR:
            failed_lookups += 1
            login = None
        email_login[e] = login
    if failed_lookups:
        # A transient API failure (rate limit, 5xx, timeout) must not be
        # scored as "these authors have no linked account" — that would
        # silently drop their entire history and emit a biased proposal.
        # Environmental problem -> the documented clean no-op; the next
        # scheduled run retries with a fresh quota.
        return _noop_exit(
            f"email->login resolution failed for {failed_lookups} email(s)")

    # --- membership filter (collaborators = the runtime's eligibility) ------
    collaborators = fetch_collaborators(api, repo, token)
    if collaborators is None:
        return _noop_exit("collaborators API unavailable — cannot validate eligibility")
    # GitHub logins are case-insensitive but the noreply decode preserves the
    # email's casing — canonicalize to the collaborator list's casing so a
    # `123+DrJKL@` commit matches collaborator "DrJKL".
    canon = {l.lower(): l for l in collaborators}
    map_exclude = {x.lower() for x in knobs["map_exclude"]}

    resolved = []
    for _sha, ts, email, paths in raw_commits:
        e = email.strip().lower()
        if BOT_EMAIL_RX.search(e):
            bot_commits += 1
            continue
        login = email_login.get(e)
        if login is None:
            unresolved_commits += 1
            continue
        if login.lower() in map_exclude:
            continue
        login = canon.get(login.lower())
        if login is None:
            continue  # not a collaborator — the runtime couldn't assign them
        resolved.append((login, ts, paths))

    now = time.time()
    score, touches, overall, gap = compute_scores(
        resolved, rule_globs, exclude_rxs, now, knobs["half_life_days"])

    # --- per-rule selection --------------------------------------------------
    rule_reports = []
    replacements = {}
    final_lists = []
    for i, rule in enumerate(config["rules"]):
        picks, under_floor, starred = select_for_rule(
            score[i], touches[i], knobs["top_k"], knobs["floor"],
            knobs["min_touches"], knobs["min_score"], knobs["floor_min_touches"])
        before = list(rule.get("reviewers", []))
        if under_floor:
            # cold-start fallback: leave the committed reviewers; the runtime
            # already falls back to default_pool when a rule can't match.
            final_lists.append(before)
            after = before
        else:
            final_lists.append(picks)
            after = picks
            if picks != before:
                replacements[i] = picks
        rule_reports.append({
            "index": i,
            "paths": rule.get("paths", []),
            "before": before,
            "after": after,
            "changed": i in replacements,
            "under_floor": under_floor,
            "starred": sorted(starred),
            "scores": {l: round(score[i].get(l, 0.0), 2) for l in after},
            "touches": {l: touches[i].get(l, 0) for l in after},
        })

    # --- default pool --------------------------------------------------------
    dp_before = list(config["default_pool"])
    dp_after = select_default_pool(overall, final_lists, map_exclude)
    dp_replacement = None
    if locs["default_pool"] is None or not dp_after:
        # no default_pool line to rewrite, or nothing scored — report the
        # committed pool unchanged rather than advertising an inapplicable one
        dp_after = dp_before
    elif dp_after != dp_before:
        dp_replacement = dp_after

    # --- taxonomy gap report -------------------------------------------------
    gaps = []
    for d, per_login in sorted(gap.items(), key=lambda kv: -sum(kv[1].values()))[:10]:
        top3 = sorted(per_login.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
        gaps.append({
            "dir": d,
            "score": round(sum(per_login.values()), 2),
            "top": [{"login": l, "score": round(s, 2)} for l, s in top3],
        })

    # --- rewrite + outputs ---------------------------------------------------
    new_text = rewrite_config(committed_text, locs, replacements, dp_replacement)
    changed = new_text != committed_text

    report = {
        "repo": repo,
        "default_branch": branch,
        "config_path": config_path,
        "knobs": knobs,
        "changed": changed,
        "bot_commits_excluded": bot_commits,
        "unresolved_email_commits": unresolved_commits,
        "rules": rule_reports,
        "default_pool": {
            "before": dp_before,
            "after": dp_after,
            "changed": dp_replacement is not None,
            "scores": {l: round(overall.get(l, 0.0), 2) for l in dp_after},
        },
        "gaps": gaps,
    }

    new_config_path = os.path.join(results_dir, "reviewers.new.yml")
    report_path = os.path.join(results_dir, "report.json")
    pr_body_path = os.path.join(results_dir, "pr-body.md")
    with open(new_config_path, "w", encoding="utf-8") as f:
        f.write(new_text)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    with open(pr_body_path, "w", encoding="utf-8") as f:
        f.write(build_pr_body(report))
    write_outputs({
        "changed": "true" if changed else "false",
        "new_config_path": new_config_path,
        "report_path": report_path,
        "pr_body_path": pr_body_path,
    })
    print(f"drift={'yes' if changed else 'no'} "
          f"(rules changed: {sorted(replacements)}, "
          f"default_pool changed: {dp_replacement is not None}, "
          f"unresolved-email commits: {unresolved_commits})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
