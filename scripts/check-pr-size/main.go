package main

import (
	"bufio"
	"bytes"
	"context"
	"flag"
	"fmt"
	"io"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"time"
)

const (
	defaultMaxLines    = 1000
	defaultBypassLabel = "oversized-ok"

	modeEnforce = "enforce"
	modeWarn    = "warn"

	// maxScanBytes bounds how much of a file we read looking for the generated
	// marker (which sits at the top). It caps memory/time so a PR cannot point a
	// "generated" path at an unbounded or blocking target and hang the job.
	maxScanBytes = 4 << 20 // 4 MiB
	// maxStderrBytes caps captured git stderr. cmd.Output() installs a 32 KiB
	// prefixSuffixSaver on stderr only when it is nil; runGitStdin sets its own
	// stderr writer, so it re-adds an equivalent cap to keep a git command that
	// floods stderr from buffering unbounded memory.
	maxStderrBytes = 32 << 10 // 32 KiB
)

func main() {
	base := flag.String("base", "", "base git ref to diff against (e.g. origin/main); required")
	head := flag.String("head", "HEAD", "head git ref of the PR")
	maxFlag := flag.Int("max", envInt("PR_SIZE_MAX_LINES", defaultMaxLines), "max non-generated changed lines allowed")
	bypassFlag := flag.Bool("bypass", envBool("PR_SIZE_BYPASS"), "bypass the cap (set when the bypass label is present)")
	modeFlag := flag.String("mode", envStr("PR_SIZE_MODE", modeEnforce), "'enforce' exits non-zero when over the cap; 'warn' reports without failing")
	bypassLabel := flag.String("bypass-label", envStr("PR_SIZE_BYPASS_LABEL", defaultBypassLabel), "PR label name the report offers as the bypass")
	extraLockfiles := flag.String("extra-lockfiles", os.Getenv("PR_SIZE_EXTRA_LOCKFILES"), "extra lockfile base names to exclude (whitespace/comma separated)")
	extraGlobs := flag.String("extra-generated-globs", os.Getenv("PR_SIZE_EXTRA_GENERATED_GLOBS"), "extra glob patterns treated as generated (whitespace/comma separated)")
	reviewedDiffOut := flag.String("reviewed-diff-out", "", "if set and the size check passes, also write the PR's unified diff with all generated-file sections excluded to this file (for a downstream review consumer, e.g. cursor-review); a failure writing it exits 2 so the consumer never pairs this run's counts with a missing/partial diff")
	diffExcludes := flag.String("diff-excludes", "", "extra git pathspecs (whitespace-separated, passed to git verbatim) applied ONLY when building --reviewed-diff-out; never affects the size verdict")
	markerFromBase := flag.Bool("marker-from-base", false, "read the Go generated-code marker from the BASE blob instead of the PR head, so a PR cannot self-exempt a file by adding the marker; files new in the PR then never match the marker (path- and attribute-based rules still apply), which is strictly conservative — set this when --reviewed-diff-out feeds a review that must not be evadable")
	ignoreComments := flag.Bool("ignore-comments", envBool("PR_SIZE_IGNORE_COMMENTS"), "exclude blank and comment-only changed lines from the counted total (heuristic, count-only; never affects generated classification or --reviewed-diff-out)")
	flag.Parse()

	if *base == "" {
		fmt.Fprintln(os.Stderr, "check-pr-size: --base is required")
		os.Exit(2)
	}
	if *modeFlag != modeEnforce && *modeFlag != modeWarn {
		fmt.Fprintf(os.Stderr, "check-pr-size: --mode must be %q or %q, got %q\n", modeEnforce, modeWarn, *modeFlag)
		os.Exit(2)
	}
	extras, err := ParseExtras(*extraLockfiles, *extraGlobs)
	if err != nil {
		fmt.Fprintf(os.Stderr, "check-pr-size: %v\n", err)
		os.Exit(2)
	}

	files, err := diffFiles(*base, *head)
	if err != nil {
		fmt.Fprintf(os.Stderr, "check-pr-size: %v\n", err)
		os.Exit(2)
	}

	// The linguist-generated attribute is read from the BASE ref, never the PR
	// head, so a PR cannot add `* linguist-generated=true` to .gitattributes and
	// shrink its own counted LoC. Two defense-in-depth conditions disable the
	// attribute path entirely (unless the bypass label is present): git too old
	// to honor `check-attr --source`, or the PR diff touching .gitattributes at
	// all. See attrGeneratedBatch / TouchesGitattributes.
	attr := attrPolicy{source: *base, useSource: checkAttrSourceSupported(*base)}
	attrModified := TouchesGitattributes(files)
	attr.trusted = attrTrusted(attr.useSource, attrModified, *bypassFlag)
	classify(files, *base, *head, attr, extras, *markerFromBase)

	// Discount blank/comment-only changed lines from the count (opt-in). Runs
	// after classify so it only ever inspects non-generated, non-binary files.
	if *ignoreComments {
		annotateDiscounts(files, *base, *head)
	}

	res := Evaluate(files, *maxFlag, *bypassFlag)
	if !res.OK && !attr.trusted {
		// The check is failing and we ignored linguist-generated exclusions; tell
		// the contributor every reason so that dropping one (e.g. the .gitattributes
		// edit) does not surprise them with the next (an old-git runner).
		var reasons []string
		if attrModified {
			reasons = append(reasons, "this PR modifies `.gitattributes`")
		}
		if !attr.useSource {
			reasons = append(reasons, "this CI runner's git is too old to read `linguist-generated` from the base ref")
		}
		if len(reasons) > 0 {
			res.Note = fmt.Sprintf("`linguist-generated` exclusions were not applied because %s. Add the `%s` label if this large change is intentional.", strings.Join(reasons, " and "), *bypassLabel)
		}
	}
	report(res, *modeFlag, *bypassLabel)
	writeGitHubOutputs(res)

	// Emit the reviewed diff for a downstream consumer (the cursor-review
	// workflow feeds it to the review panel + judge): the PR's full patch with
	// every generated file's section filtered out, reusing THIS classification
	// as the single source of truth for "what is codegen" — the count and the
	// reviewed diff come from one process and can never disagree. Written last;
	// a write failure exits 2 (after removing any partial file) so the consumer
	// sees a degraded run rather than pairing this run's counts with a
	// missing/truncated diff. Skipped when over the cap — nothing downstream
	// consumes a diff the gate is about to skip.
	if *reviewedDiffOut != "" {
		if res.OK {
			if err := writeReviewedDiff(*base, *head, strings.Fields(*diffExcludes), files, *reviewedDiffOut); err != nil {
				os.Remove(*reviewedDiffOut)
				fmt.Fprintf(os.Stderr, "check-pr-size: could not write --reviewed-diff-out %q: %v\n", *reviewedDiffOut, err)
				os.Exit(2)
			}
		} else {
			fmt.Fprintln(os.Stderr, "check-pr-size: over the cap — skipping --reviewed-diff-out")
		}
	}

	if shouldFail(res, *modeFlag) {
		os.Exit(1)
	}
}

// writeReviewedDiff streams `git diff base...head` (plus any caller-supplied
// verbatim exclude pathspecs) through FilterPatch, writing the patch minus
// every generated file's section to outPath. The generated set is passed to
// git by FILTERING ITS OUTPUT, never as per-file argv pathspecs, so an
// unbounded number of generated files (they cost nothing against the cap)
// cannot overflow ARG_MAX and fail the diff. core.quotePath=false keeps
// non-ASCII paths verbatim in the headers so they match the numstat-derived
// classification; a path git still quotes (control chars, quotes) simply won't
// match and its section is kept — reviewed, never hidden.
func writeReviewedDiff(base, head string, diffExcludes []string, files []FileChange, outPath string) error {
	gen := make(map[string]bool)
	for _, f := range files {
		if f.Generated {
			gen[f.Path] = true
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), gitTimeout)
	defer cancel()
	args := append([]string{"-c", "core.quotePath=false", "diff", base + "..." + head, "--", "."}, diffExcludes...)
	cmd := exec.CommandContext(ctx, "git", args...)
	cmd.WaitDelay = gitWaitDelay
	errBuf := &capWriter{cap: maxStderrBytes}
	cmd.Stderr = errBuf
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return err
	}
	out, err := os.Create(outPath)
	if err != nil {
		return err
	}
	defer out.Close()
	if err := cmd.Start(); err != nil {
		return err
	}
	bw := bufio.NewWriter(out)
	kept, dropped, ferr := FilterPatch(stdout, bw, func(p string) bool { return gen[p] })
	if ferr != nil {
		_ = cmd.Process.Kill()
		_ = cmd.Wait()
		return ferr
	}
	if err := cmd.Wait(); err != nil {
		if ctx.Err() == context.DeadlineExceeded {
			err = fmt.Errorf("git %v timed out after %s: %w", args, gitTimeout, ctx.Err())
		}
		if s := strings.TrimSpace(errBuf.String()); s != "" {
			err = fmt.Errorf("%w: %s", err, s)
		}
		return err
	}
	if err := bw.Flush(); err != nil {
		return err
	}
	if err := out.Close(); err != nil {
		return err
	}
	fmt.Fprintf(os.Stderr, "check-pr-size: reviewed diff written to %s (%d file section(s) kept, %d generated section(s) excluded)\n", outPath, kept, dropped)
	return nil
}

// shouldFail reports whether the process should exit non-zero: over the cap
// (and not bypassed) in enforce mode. Warn mode never fails — it reports and
// lets the workflow's comment job surface the overage.
func shouldFail(res Result, mode string) bool {
	return !res.OK && mode == modeEnforce
}

// diffFiles runs `git diff --numstat` for the PR's net changes (three-dot: from
// the merge-base of base and head, to head) and parses the result.
func diffFiles(base, head string) ([]FileChange, error) {
	out, err := runGit("diff", "--numstat", "-z", base+"..."+head)
	if err != nil {
		return nil, fmt.Errorf("git diff failed: %w", err)
	}
	return ParseNumstat(strings.NewReader(out))
}

// annotateDiscounts sets FileChange.Discounted for each non-generated, non-binary
// file to the number of its changed lines that are blank or comment-only, so the
// counted total reflects significant lines. Count-only: it never changes which
// files are generated, nor any diff a consumer builds from --generated-out.
//
// Best-effort by design: on any git/parse failure it leaves Discounted at 0, so
// that file counts raw — a failure can only make the cap STRICTER, never sneak a
// large change under it. Restricts the patch to the counted paths, so a large
// generated diff is never fetched or parsed.
func annotateDiscounts(files []FileChange, base, head string) {
	idx := make(map[string]*FileChange, len(files))
	paths := make([]string, 0, len(files))
	for i := range files {
		f := &files[i]
		if f.Generated || f.Binary {
			continue
		}
		paths = append(paths, f.Path)
		idx[f.Path] = f
	}
	if len(paths) == 0 {
		return
	}
	patch, err := runGitDiffPatch(base, head, paths)
	if err != nil {
		fmt.Fprintf(os.Stderr, "check-pr-size: comment discounting skipped (git diff failed): %v\n", err)
		return
	}
	discounts, err := ParseDiscounts(strings.NewReader(patch))
	if err != nil {
		fmt.Fprintf(os.Stderr, "check-pr-size: comment discounting skipped (parse failed): %v\n", err)
		return
	}
	for p, d := range discounts {
		f, ok := idx[p]
		if !ok {
			continue // header path git quoted, or otherwise unmatched — count raw
		}
		if d > f.Changed() {
			d = f.Changed() // clamp: never discount more than the file changed
		}
		f.Discounted = d
	}
}

// runGitDiffPatch returns the unified-diff patch for the PR's net changes (three
// dot) restricted to the given paths. core.quotePath=false keeps non-ASCII paths
// unquoted so ParseDiscounts can read them back; the :(literal) pathspec magic
// disables wildcards so a filename with glob metacharacters is matched literally.
func runGitDiffPatch(base, head string, paths []string) (string, error) {
	args := []string{"-c", "core.quotePath=false", "diff", base + "..." + head, "--"}
	for _, p := range paths {
		args = append(args, ":(literal)"+p)
	}
	return runGit(args...)
}

// attrPolicy controls how the linguist-generated git attribute is consulted.
// source is the tree-ish whose .gitattributes rules are read (the base ref);
// useSource is whether the installed git honors `check-attr --source` (git
// >=2.40); trusted is whether attribute-based exclusion may be applied at all
// (false disables it, so a PR-introduced rule cannot shrink the count).
type attrPolicy struct {
	source    string
	useSource bool
	trusted   bool
}

// attrTrusted decides whether linguist-generated attribute exclusions may be
// applied. It always requires useSource, so attributes are only ever read from
// the base ref via `--source` and never from the PR-head working tree. The
// bypass label may override the .gitattributes-touched gate, but must NOT
// re-enable head-controlled reading on a runner too old for `--source` — doing
// so would make the reported excluded/counted numbers head-manipulable,
// breaking the base-only invariant even though the check passes on bypass.
func attrTrusted(useSource, attrModified, bypass bool) bool {
	return useSource && (!attrModified || bypass)
}

// classify sets FileChange.Generated for each file using, in order: the
// lockfile name lists (built-in + extras), the caller-supplied generated globs,
// the linguist-generated git attribute (read from the base ref per attr), and
// the canonical Go generated marker in the file's content.
//
// Binary files are classified by the path- and attribute-based rules too (none
// of those read content), so a binary lockfile or a binary matching an extra
// glob still leaves a --reviewed-diff-out consumer's diff; only the content
// marker read is skipped for them. Their Changed() is 0 either way, so the
// size verdict is untouched.
//
// The linguist-generated attribute is resolved for every path in a SINGLE
// `git check-attr` pass (attrGeneratedBatch) rather than one subprocess per
// file, so a large PR pays constant process-creation cost instead of O(N).
func classify(files []FileChange, base, head string, attr attrPolicy, extras Extras, markerFromBase bool) {
	var attrGen map[string]bool
	if attr.trusted {
		paths := make([]string, 0, len(files))
		for i := range files {
			paths = append(paths, files[i].Path)
		}
		attrGen = attrGeneratedBatch(paths, attr.source, attr.useSource)
	}
	for i := range files {
		f := &files[i]
		if IsLockfile(f.Path) || extras.Generated(f.Path) ||
			attrGen[f.Path] ||
			(!f.Binary && contentGenerated(f.Path, base, head, markerFromBase)) {
			f.Generated = true
		}
	}
}

// attrGeneratedBatch reports, per path, whether .gitattributes marks it
// linguist-generated, resolving every path in ONE `git check-attr` pass (paths
// fed on stdin) instead of one subprocess per file. The returned map contains an
// entry only for paths git reported as generated ("true" for an explicit "=true"
// or "set" for a bare attribute); callers treat an absent path as not-generated,
// matching the old err→false fallback.
//
// When useSource is set the rules are read from source (the base ref) via
// `--source`, so .gitattributes rules introduced by the PR head are never
// consulted — a PR cannot mark arbitrary hand-written files generated to escape
// the size count. `--source` needs git >= 2.40 (GitHub runners ship newer); on
// older git useSource is false and `--source` is omitted (the caller only
// invokes this on a runner where the attribute path is trusted; see attrPolicy).
func attrGeneratedBatch(paths []string, source string, useSource bool) map[string]bool {
	result := make(map[string]bool, len(paths))
	if len(paths) == 0 {
		return result
	}
	// `-z` uses NUL as the separator for both stdin paths and output fields, so a
	// path containing spaces/newlines (already possible from the -z numstat that
	// produced these) round-trips safely.
	args := []string{"check-attr", "-z"}
	if useSource {
		args = append(args, "--source", source)
	}
	args = append(args, "--stdin", "linguist-generated")

	var stdin bytes.Buffer
	for _, p := range paths {
		stdin.WriteString(p)
		stdin.WriteByte(0)
	}
	out, _, err := runGitStdin(stdin.Bytes(), args...)
	if err != nil {
		return result
	}
	// `-z` output is a flat NUL-separated stream of (path, attribute, value)
	// triples, with a trailing NUL after the final value (so Split yields an
	// empty tail element the triple loop ignores).
	fields := strings.Split(out, "\x00")
	for i := 0; i+2 < len(fields); i += 3 {
		if v := fields[i+2]; v == "true" || v == "set" {
			result[fields[i]] = true
		}
	}
	return result
}

// checkAttrSourceSupported reports whether the installed git honors
// `check-attr --source` (added in git 2.40). Older git rejects the flag as an
// unknown option and exits non-zero, so we probe once rather than parse the
// version string. The probe path need not exist; check-attr resolves rules for
// arbitrary path strings.
//
// Only git's own "unknown flag" signature counts as unsupported. Any OTHER probe
// failure (a transient git error, an unreadable base ref) leaves --source
// trusted: reporting a modern-but-flaky git as "too old" would silently drop
// every base linguist-generated exclusion and emit a misleading "runner's git is
// too old" note (BE-3247). On such an unrelated failure the reads stay
// conservative anyway — attrGeneratedBatch's own --source call fails closed, so
// files are over-counted (never under-counted), and the size cap can only be too
// strict, never too loose.
func checkAttrSourceSupported(source string) bool {
	_, stderr, err := runGitStdin(nil, "check-attr", "--source", source, "linguist-generated", "--", ".gitattributes")
	if err == nil {
		return true
	}
	// A genuine unknown-flag rejection => --source truly unsupported. Anything
	// else is an unrelated failure and must not disable the base-ref attribute path.
	return !isUnknownFlagError(stderr)
}

// isUnknownFlagError reports whether git's stderr indicates it rejected an option
// as unrecognized. git's parse-options prints "unknown option" for long flags and
// "unknown switch" for short ones; matching is case-insensitive and substring-based
// so it tolerates the surrounding usage text and minor wording drift across git
// versions. The stderr comes from runGitStdin, which forces LC_ALL=C, so this
// only ever sees git's canonical English wording (BE-3247).
func isUnknownFlagError(stderr string) bool {
	s := strings.ToLower(stderr)
	return strings.Contains(s, "unknown option") || strings.Contains(s, "unknown switch")
}

// contentGenerated reports whether a .go file carries Go's canonical generated
// marker before its package clause. Only .go files are consulted: the marker's
// anti-gaming guarantee relies on the package-clause gate (see IsGeneratedContent),
// which non-Go files lack, so a contributor cannot exclude a large hand-written
// non-Go file by pasting the marker at its top. Working-tree reads never follow a
// symlink and are capped at maxScanBytes, so a PR cannot point a "generated" path
// at an unbounded/blocking target (e.g. /dev/zero) to hang or OOM the job.
//
// With markerFromBase set, ONLY the base blob is consulted — mirroring the
// base-ref invariant the linguist-generated attribute path already enforces.
// The head-content read is fine for a size cap (self-marking merely shrinks a
// count a maintainer can eyeball), but when the classification decides what a
// blocking review SEES, a head-honored marker would let a PR hide arbitrary
// code by prepending one comment line. Base-only means a file new in the PR
// never matches the marker (it is counted and reviewed — conservative); files
// generated at base stay excluded.
func contentGenerated(path, base, head string, markerFromBase bool) bool {
	if !strings.HasSuffix(path, ".go") {
		return false
	}
	if markerFromBase {
		if data, err := runGitCapped(maxScanBytes, "show", base+":"+path); err == nil {
			return IsGeneratedContent(data)
		}
		return false
	}
	if info, err := os.Lstat(path); err == nil {
		if info.Mode()&os.ModeSymlink != 0 || !info.Mode().IsRegular() {
			return false
		}
		f, err := os.Open(path)
		if err != nil {
			return false
		}
		defer f.Close()
		data, err := io.ReadAll(io.LimitReader(f, maxScanBytes))
		if err != nil {
			return false
		}
		return IsGeneratedContent(data)
	}
	// Not in the working tree (e.g. deleted): read the head/base blob so a
	// shrinking regen of a generated file is still classified as generated.
	// Capped at maxScanBytes just like the working-tree read above, so a
	// deleted path pointing at an oversized blob can't balloon job memory.
	for _, ref := range []string{head, base} {
		if data, err := runGitCapped(maxScanBytes, "show", ref+":"+path); err == nil {
			return IsGeneratedContent(data)
		}
	}
	return false
}

// renderReport builds the markdown report for stdout, the step summary, and the
// sticky PR comment.
func renderReport(res Result, mode, bypassLabel string) string {
	var b strings.Builder
	status := "✅ Passed"
	if !res.OK {
		if mode == modeWarn {
			status = "⚠️ Over cap (warn-only)"
		} else {
			status = "❌ Failed"
		}
	}
	fmt.Fprintf(&b, "## %s — PR size check\n\n", status)
	fmt.Fprintf(&b, "- Changed lines counted (non-generated): **%d**\n", res.Counted)
	fmt.Fprintf(&b, "- Cap: **%d**\n", res.Max)
	fmt.Fprintf(&b, "- Excluded (generated/lockfiles): %d\n", res.Generated)
	if res.Discounted > 0 {
		fmt.Fprintf(&b, "- Excluded (blank/comment lines): %d\n", res.Discounted)
	}
	if res.Bypassed {
		fmt.Fprintf(&b, "- Bypassed via `%s` label ✅\n", bypassLabel)
	}
	if !res.OK {
		fmt.Fprintf(&b, "\n**This PR changes %d lines of hand-written code, over the %d-line cap.**\n\n", res.Counted, res.Max)
		if mode == modeWarn {
			b.WriteString("This check runs in `warn` mode, so it will not fail — but consider:\n")
		} else {
			b.WriteString("Options:\n")
		}
		b.WriteString("- Split it into smaller, independently reviewable PRs (stacked PRs help).\n")
		fmt.Fprintf(&b, "- If the size is justified, add the `%s` label to bypass this check.\n", bypassLabel)
	}
	if res.Note != "" {
		fmt.Fprintf(&b, "\n> %s\n", res.Note)
	}
	// Largest contributing files, for quick triage.
	shown := 0
	var top strings.Builder
	for _, f := range res.Files {
		if f.Generated || f.Counted() == 0 {
			continue
		}
		if shown == 0 {
			top.WriteString("\n<details><summary>Largest counted files</summary>\n\n")
		}
		fmt.Fprintf(&top, "- `%s` (+%d/-%d)\n", f.Path, f.Added, f.Deleted)
		shown++
		if shown >= 10 {
			break
		}
	}
	if shown > 0 {
		top.WriteString("\n</details>\n")
		b.WriteString(top.String())
	}
	return b.String()
}

func report(res Result, mode, bypassLabel string) {
	summary := renderReport(res, mode, bypassLabel)
	fmt.Println(summary)
	if path := os.Getenv("GITHUB_STEP_SUMMARY"); path != "" {
		if f, err := os.OpenFile(path, os.O_APPEND|os.O_WRONLY|os.O_CREATE, 0o644); err == nil {
			defer f.Close()
			fmt.Fprintln(f, summary)
		}
	}
}

// writeGitHubOutputs exposes the evaluation to later workflow steps via
// GITHUB_OUTPUT. over_cap drives the sticky-comment job, which must post on
// overage even in warn mode, where this process exits 0 and the job result
// alone cannot distinguish over from under. No-op outside GitHub Actions.
func writeGitHubOutputs(res Result) {
	path := os.Getenv("GITHUB_OUTPUT")
	if path == "" {
		return
	}
	f, err := os.OpenFile(path, os.O_APPEND|os.O_WRONLY|os.O_CREATE, 0o644)
	if err != nil {
		return
	}
	defer f.Close()
	fmt.Fprintf(f, "over_cap=%t\ncounted=%d\n", !res.OK, res.Counted)
}

// gitTimeout bounds every git invocation so a hung git (a wedged credential
// prompt, a stuck pack read, an unresponsive filter) cannot stall the job until
// the global CI runner timeout. It is deliberately generous — the heaviest call
// here is a single blob read — so a healthy git never trips it.
const gitTimeout = 60 * time.Second

// gitWaitDelay bounds how long Wait() may block after the timeout fires and the
// child is killed. exec.CommandContext only SIGKILLs the immediate git process;
// a descendant that inherited the stdout/stderr pipes (a diff/filter driver,
// textconv, pager, or credential helper) can hold them open, so the copy
// goroutines behind cmd.Output()/StdoutPipe never see EOF and Wait() would block
// past the deadline. WaitDelay (Go 1.20+) forces those pipes closed and Wait to
// return after the grace period, keeping the BE-3248 timeout robust.
const gitWaitDelay = 10 * time.Second

func runGit(args ...string) (string, error) {
	out, stderr, err := runGitStdin(nil, args...)
	// runGitStdin sets cmd.Stderr, so a returned *exec.ExitError no longer
	// carries git's stderr (Cmd.Output only populates ExitError.Stderr when
	// Stderr is nil). Fold the captured stderr back into the error so callers
	// like diffFiles keep git's diagnostics.
	if err != nil {
		if s := strings.TrimSpace(stderr); s != "" {
			err = fmt.Errorf("%w: %s", err, s)
		}
	}
	return out, err
}

// runGitStdin runs git with the given args and optional stdin, returning stdout
// and stderr separately. Three behaviors matter to callers:
//   - stderr is captured (a plain cmd.Output() discards it) and bounded to
//     maxStderrBytes, so checkAttrSourceSupported can tell an old git's
//     unknown-flag rejection from an unrelated failure (BE-3247).
//   - the call runs under a gitTimeout deadline via exec.CommandContext; when it
//     fires the child is killed and the timeout surfaces as an error, which
//     callers already treat as "git failed" rather than returning stale data
//     (BE-3248).
//   - stdin, when non-nil, is fed to git — used for `check-attr --stdin`.
//
// git is forced into the C locale so its parse-options diagnostics (localized
// via gettext under LANG/LC_MESSAGES) stay in the canonical English wording
// isUnknownFlagError matches; the porcelain output we parse (-z numstat, -z
// check-attr, blob contents) is locale-independent.
func runGitStdin(stdin []byte, args ...string) (stdout, stderr string, err error) {
	ctx, cancel := context.WithTimeout(context.Background(), gitTimeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, "git", args...)
	cmd.WaitDelay = gitWaitDelay
	cmd.Env = append(os.Environ(), "LC_ALL=C")
	if stdin != nil {
		cmd.Stdin = bytes.NewReader(stdin)
	}
	errBuf := &capWriter{cap: maxStderrBytes}
	cmd.Stderr = errBuf
	out, err := cmd.Output()
	if err != nil && ctx.Err() == context.DeadlineExceeded {
		err = fmt.Errorf("git %v timed out after %s: %w", args, gitTimeout, ctx.Err())
	}
	return string(out), errBuf.String(), err
}

// capWriter is an io.Writer that retains at most cap bytes, silently discarding
// the overflow while still reporting a full write so the child process never
// sees a short-write error. It re-adds the stderr cap that cmd.Output() would
// otherwise provide (see maxStderrBytes).
type capWriter struct {
	buf bytes.Buffer
	cap int
}

func (w *capWriter) Write(p []byte) (int, error) {
	if room := w.cap - w.buf.Len(); room > 0 {
		if len(p) > room {
			w.buf.Write(p[:room])
		} else {
			w.buf.Write(p)
		}
	}
	return len(p), nil
}

func (w *capWriter) String() string { return w.buf.String() }

// runGitCapped runs git and reads at most maxBytes of its stdout, killing the
// process rather than blocking on Wait() if it had more to write. Used for
// reads of ref-addressed blobs (e.g. `git show <ref>:<path>`) whose size we
// don't control, so a single oversized blob can't spike job memory the way an
// unbounded cmd.Output() would. Bounded by gitTimeout like every other git call
// (BE-3248), so a stuck blob read can't hang the job.
func runGitCapped(maxBytes int64, args ...string) ([]byte, error) {
	ctx, cancel := context.WithTimeout(context.Background(), gitTimeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, "git", args...)
	cmd.WaitDelay = gitWaitDelay
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return nil, err
	}
	if err := cmd.Start(); err != nil {
		return nil, err
	}
	data, readErr := io.ReadAll(io.LimitReader(stdout, maxBytes))
	if readErr != nil {
		_ = cmd.Process.Kill()
		_ = cmd.Wait()
		return nil, readErr
	}
	// Drain-probe for leftover output beyond the cap; if the process still has
	// more to write, kill it instead of letting Wait() block on a full pipe.
	extra := make([]byte, 1)
	n, _ := stdout.Read(extra)
	if n > 0 {
		_ = cmd.Process.Kill()
		_ = cmd.Wait()
		return data, nil
	}
	if err := cmd.Wait(); err != nil {
		// Wrap a timeout hit as %w of ctx.Err() so errors.Is(err,
		// context.DeadlineExceeded) works, mirroring runGitStdin (BE-3248).
		if ctx.Err() == context.DeadlineExceeded {
			err = fmt.Errorf("git %v timed out after %s: %w", args, gitTimeout, ctx.Err())
		}
		return nil, err
	}
	return data, nil
}

func envInt(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}

func envBool(key string) bool {
	b, _ := strconv.ParseBool(os.Getenv(key))
	return b
}

func envStr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}
