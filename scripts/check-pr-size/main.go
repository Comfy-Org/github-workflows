package main

import (
	"flag"
	"fmt"
	"io"
	"os"
	"os/exec"
	"strconv"
	"strings"
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
	// all. See attrGenerated / TouchesGitattributes.
	attr := attrPolicy{source: *base, useSource: checkAttrSourceSupported(*base)}
	attrModified := TouchesGitattributes(files)
	attr.trusted = attrTrusted(attr.useSource, attrModified, *bypassFlag)
	classify(files, *base, *head, attr, extras)

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

	if shouldFail(res, *modeFlag) {
		os.Exit(1)
	}
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
func classify(files []FileChange, base, head string, attr attrPolicy, extras Extras) {
	for i := range files {
		f := &files[i]
		if f.Binary {
			continue
		}
		if IsLockfile(f.Path) || extras.Generated(f.Path) ||
			(attr.trusted && attrGenerated(f.Path, attr.source, attr.useSource)) ||
			contentGenerated(f.Path, base, head) {
			f.Generated = true
		}
	}
}

// attrGenerated reports whether .gitattributes marks the path linguist-generated.
// When useSource is set, the attribute rules are read from source (the base ref)
// via `git check-attr --source`, so .gitattributes rules introduced by the PR
// head are never consulted — a PR cannot mark arbitrary hand-written files
// generated to escape the size count. `--source` needs git >= 2.40 (GitHub
// runners ship newer); on older git useSource is false and the attribute path is
// left untrusted (see attrPolicy) rather than reading the PR checkout.
func attrGenerated(path, source string, useSource bool) bool {
	args := []string{"check-attr", "linguist-generated"}
	if useSource {
		args = append(args, "--source", source)
	}
	args = append(args, "--", path)
	out, err := runGit(args...)
	if err != nil {
		return false
	}
	// Format: "<path>: linguist-generated: <value>". git reports "true" for an
	// explicit "=true" and "set" for a bare attribute; both mean generated.
	s := strings.TrimSpace(out)
	return strings.HasSuffix(s, ": true") || strings.HasSuffix(s, ": set")
}

// checkAttrSourceSupported reports whether the installed git honors
// `check-attr --source` (added in git 2.40). Older git rejects the flag as an
// unknown option and exits non-zero, so we probe once rather than parse the
// version string. A false result forces the attribute path onto its untrusted
// fallback (see attrPolicy). The probe path need not exist; check-attr resolves
// rules for arbitrary path strings.
func checkAttrSourceSupported(source string) bool {
	_, err := runGit("check-attr", "--source", source, "linguist-generated", "--", ".gitattributes")
	return err == nil
}

// contentGenerated reports whether a .go file carries Go's canonical generated
// marker before its package clause. Only .go files are consulted: the marker's
// anti-gaming guarantee relies on the package-clause gate (see IsGeneratedContent),
// which non-Go files lack, so a contributor cannot exclude a large hand-written
// non-Go file by pasting the marker at its top. Working-tree reads never follow a
// symlink and are capped at maxScanBytes, so a PR cannot point a "generated" path
// at an unbounded/blocking target (e.g. /dev/zero) to hang or OOM the job.
func contentGenerated(path, base, head string) bool {
	if !strings.HasSuffix(path, ".go") {
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
		if f.Generated || f.Changed() == 0 {
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

func runGit(args ...string) (string, error) {
	cmd := exec.Command("git", args...)
	out, err := cmd.Output()
	if err != nil {
		return "", err
	}
	return string(out), nil
}

// runGitCapped runs git and reads at most maxBytes of its stdout, killing the
// process rather than blocking on Wait() if it had more to write. Used for
// reads of ref-addressed blobs (e.g. `git show <ref>:<path>`) whose size we
// don't control, so a single oversized blob can't spike job memory the way an
// unbounded cmd.Output() would.
func runGitCapped(maxBytes int64, args ...string) ([]byte, error) {
	cmd := exec.Command("git", args...)
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
