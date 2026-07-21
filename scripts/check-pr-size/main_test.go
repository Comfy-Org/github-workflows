package main

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// These tests exercise the git-backed generated-file classification against a
// real, throwaway repository so the `.gitattributes` / `check-attr --source`
// behavior is verified end to end rather than mocked. They are not
// t.Parallel(): they t.Chdir into the temp repo (the production helpers resolve
// git against the process working directory), and t.Chdir forbids parallel use.

func gitRun(t *testing.T, dir string, args ...string) string {
	t.Helper()
	cmd := exec.Command("git", args...)
	cmd.Dir = dir
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("git %v failed: %v\n%s", args, err, out)
	}
	return string(out)
}

// initTestRepo creates an empty git repo with a committer identity configured.
func initTestRepo(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	gitRun(t, dir, "init", "-q")
	gitRun(t, dir, "config", "user.email", "test@example.com")
	gitRun(t, dir, "config", "user.name", "Test")
	gitRun(t, dir, "config", "commit.gpgsign", "false")
	return dir
}

// writeFile writes name (relative to dir), creating parent directories.
func writeFile(t *testing.T, dir, name, content string) {
	t.Helper()
	p := filepath.Join(dir, name)
	if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(p, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}

// commitAll stages everything and commits, returning the new commit's SHA.
func commitAll(t *testing.T, dir, msg string) string {
	t.Helper()
	gitRun(t, dir, "add", "-A")
	gitRun(t, dir, "commit", "-q", "-m", msg)
	return strings.TrimSpace(gitRun(t, dir, "rev-parse", "HEAD"))
}

func TestTouchesGitattributes(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name  string
		files []FileChange
		want  bool
	}{
		{"root gitattributes", []FileChange{{Path: ".gitattributes"}}, true},
		{"nested gitattributes", []FileChange{{Path: "vendor/.gitattributes"}}, true},
		{"no gitattributes", []FileChange{{Path: "main.go"}, {Path: "dir/x.go"}}, false},
		{"lookalike suffix is not a match", []FileChange{{Path: "my.gitattributes"}}, false},
		{"empty", nil, false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()
			if got := TouchesGitattributes(tt.files); got != tt.want {
				t.Errorf("TouchesGitattributes(%+v) = %v, want %v", tt.files, got, tt.want)
			}
		})
	}
}

// TestAttrGeneratedSourceIsolatesBase proves attrGeneratedBatch reads
// .gitattributes from the base tree (useSource=true), not the working tree (the
// PR head). It checks both directions: a base rule the head removed is still
// honored via --source, and a rule only the head adds is ignored via --source
// but WOULD have been honored reading the working tree.
func TestAttrGeneratedSourceIsolatesBase(t *testing.T) {
	// Direction 1: base HAS the rule, head REMOVED it.
	t.Run("base rule honored despite head removal", func(t *testing.T) {
		dir := initTestRepo(t)
		writeFile(t, dir, ".gitattributes", "foo.go linguist-generated=true\n")
		writeFile(t, dir, "foo.go", "package foo\n")
		base := commitAll(t, dir, "base with rule")
		writeFile(t, dir, ".gitattributes", "") // head drops the rule
		commitAll(t, dir, "head drops rule")    // working tree now at head
		t.Chdir(dir)

		if !checkAttrSourceSupported(base) {
			t.Skip("git too old for check-attr --source")
		}
		if !attrGeneratedBatch([]string{"foo.go"}, base, true)["foo.go"] {
			t.Error("attrGeneratedBatch should read the base rule via --source")
		}
		// Reading the working tree (head) must NOT see the removed rule.
		if attrGeneratedBatch([]string{"foo.go"}, base, false)["foo.go"] {
			t.Error("attrGeneratedBatch without --source read the head tree, expected no rule")
		}
	})

	// Direction 2: base has NO rule, head ADDED one (the attack). --source must
	// ignore it; reading the working tree would (unsafely) honor it.
	t.Run("head-added rule ignored via source", func(t *testing.T) {
		dir := initTestRepo(t)
		writeFile(t, dir, "foo.go", "package foo\n")
		base := commitAll(t, dir, "base no rule")
		writeFile(t, dir, ".gitattributes", "*.go linguist-generated=true\n")
		commitAll(t, dir, "head adds rule") // working tree now at head
		t.Chdir(dir)

		if !checkAttrSourceSupported(base) {
			t.Skip("git too old for check-attr --source")
		}
		if attrGeneratedBatch([]string{"foo.go"}, base, true)["foo.go"] {
			t.Error("attrGeneratedBatch via --source must not see the head-introduced rule")
		}
		if !attrGeneratedBatch([]string{"foo.go"}, base, false)["foo.go"] {
			t.Error("sanity: reading the working tree should see the head rule (the vulnerability)")
		}
	})
}

// TestAttrGeneratedBatchMultiplePaths proves the single-pass batch resolves each
// path independently against the base ref: a matched path is reported generated,
// an unmatched one is absent from the map, and an empty input never shells out.
func TestAttrGeneratedBatchMultiplePaths(t *testing.T) {
	dir := initTestRepo(t)
	writeFile(t, dir, ".gitattributes", "gen/*.go linguist-generated=true\n")
	writeFile(t, dir, "gen/a.go", "package gen\n")
	writeFile(t, dir, "gen/b.go", "package gen\n")
	writeFile(t, dir, "hand.go", "package main\n")
	base := commitAll(t, dir, "base with rule")
	t.Chdir(dir)

	if !checkAttrSourceSupported(base) {
		t.Skip("git too old for check-attr --source")
	}
	got := attrGeneratedBatch([]string{"gen/a.go", "gen/b.go", "hand.go"}, base, true)
	if !got["gen/a.go"] || !got["gen/b.go"] {
		t.Errorf("matched paths should be generated, got %v", got)
	}
	if got["hand.go"] {
		t.Errorf("unmatched path should not be generated, got %v", got)
	}
	if len(attrGeneratedBatch(nil, base, true)) != 0 {
		t.Error("empty input should return an empty map")
	}
}

// TestIsUnknownFlagError checks that only git's unrecognized-option wording is
// treated as the "unsupported --source flag" signal, and unrelated git errors
// (e.g. a bad ref) are not — the core BE-3247 distinction.
func TestIsUnknownFlagError(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name   string
		stderr string
		want   bool
	}{
		{"long option wording", "error: unknown option `source'\nusage: git check-attr ...", true},
		{"short switch wording", "error: unknown switch `s'\n", true},
		{"mixed case tolerated", "ERROR: Unknown Option `source'", true},
		{"unrelated fatal is not a flag error", "fatal: no-such-ref: not a valid tree-ish source\n", false},
		{"empty stderr", "", false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()
			if got := isUnknownFlagError(tt.stderr); got != tt.want {
				t.Errorf("isUnknownFlagError(%q) = %v, want %v", tt.stderr, got, tt.want)
			}
		})
	}
}

// TestCheckAttrSourceSupported proves the BE-3247 fix end to end against real
// git: a valid probe reports supported, and a probe that fails for a reason
// OTHER than an unknown --source flag (here an unresolvable base ref) is NOT
// misreported as unsupported — which would otherwise drop legit base exclusions
// and emit the misleading "git is too old" note.
func TestCheckAttrSourceSupported(t *testing.T) {
	dir := initTestRepo(t)
	writeFile(t, dir, "foo.go", "package foo\n")
	commitAll(t, dir, "base")
	t.Chdir(dir)

	if !checkAttrSourceSupported("HEAD") {
		t.Skip("git too old for check-attr --source")
	}
	// An unresolvable source ref makes git fatal with "not a valid tree-ish
	// source" (not an unknown-flag error), so --source must stay trusted.
	if !checkAttrSourceSupported("this-ref-does-not-exist") {
		t.Error("checkAttrSourceSupported must stay true when the probe fails for a reason other than an unknown --source flag")
	}
}

// TestCapWriter checks that captured git stderr is bounded: writes past the cap
// are dropped (never buffered), while each Write still reports its full length so
// the child process never observes a short write.
func TestCapWriter(t *testing.T) {
	t.Parallel()
	w := &capWriter{cap: 4}
	if n, err := w.Write([]byte("ab")); n != 2 || err != nil {
		t.Fatalf("Write(ab) = (%d, %v), want (2, nil)", n, err)
	}
	// Straddles the cap: only "cd" fits, but the writer must report all 4 bytes
	// written so exec does not treat it as an error.
	if n, err := w.Write([]byte("cdef")); n != 4 || err != nil {
		t.Fatalf("Write(cdef) = (%d, %v), want (4, nil)", n, err)
	}
	// Fully past the cap: dropped entirely, still reported as fully written.
	if n, err := w.Write([]byte("gh")); n != 2 || err != nil {
		t.Fatalf("Write(gh) = (%d, %v), want (2, nil)", n, err)
	}
	if got := w.String(); got != "abcd" {
		t.Errorf("capWriter retained %q, want %q", got, "abcd")
	}
}

// TestClassifyPRAddedGitattributesDoesNotReduceCount is the anti-gaming
// regression: a PR that adds `*.go linguist-generated=true` to .gitattributes
// must not shrink the counted lines of its hand-written .go changes.
func TestClassifyPRAddedGitattributesDoesNotReduceCount(t *testing.T) {
	dir := initTestRepo(t)
	writeFile(t, dir, "hand.go", "package main\n\nfunc F() {}\n")
	base := commitAll(t, dir, "base")

	// PR head: sneak in an attribute rule AND a large hand-written change.
	writeFile(t, dir, ".gitattributes", "*.go linguist-generated=true\n")
	writeFile(t, dir, "hand.go", "package main\n\n"+strings.Repeat("// padding line\n", 40)+"func F() {}\n")
	head := commitAll(t, dir, "head")
	t.Chdir(dir)

	files, err := diffFiles(base, head)
	if err != nil {
		t.Fatalf("diffFiles: %v", err)
	}
	attr := attrPolicy{source: base, useSource: checkAttrSourceSupported(base)}
	attr.trusted = attrTrusted(attr.useSource, TouchesGitattributes(files), false)
	classify(files, base, head, attr, Extras{})

	for _, f := range files {
		if f.Path == "hand.go" && f.Generated {
			t.Error("hand.go was excluded by a PR-introduced .gitattributes rule")
		}
	}
	res := Evaluate(files, 1000, false)
	if res.Counted == 0 {
		t.Errorf("counted lines should include hand.go's changes, got %d", res.Counted)
	}
}

// TestClassifyHonorsLegitBaseGitattributes proves the base-only reading does
// not break legitimate attribute-based exclusion: a rule already present in the
// base ref (and not modified by the PR) still excludes a matching file.
func TestClassifyHonorsLegitBaseGitattributes(t *testing.T) {
	dir := initTestRepo(t)
	writeFile(t, dir, ".gitattributes", "generated/** linguist-generated=true\n")
	writeFile(t, dir, "generated/api.go", "package generated\n")
	writeFile(t, dir, "hand.go", "package main\n")
	base := commitAll(t, dir, "base")

	// PR head changes only source files, NOT .gitattributes.
	writeFile(t, dir, "generated/api.go", "package generated\n\n"+strings.Repeat("// gen line\n", 30))
	writeFile(t, dir, "hand.go", "package main\n\n"+strings.Repeat("// hand line\n", 5))
	head := commitAll(t, dir, "head")
	t.Chdir(dir)

	if !checkAttrSourceSupported(base) {
		t.Skip("git too old for check-attr --source")
	}
	files, err := diffFiles(base, head)
	if err != nil {
		t.Fatalf("diffFiles: %v", err)
	}
	attr := attrPolicy{source: base, useSource: true}
	attr.trusted = !TouchesGitattributes(files)
	if !attr.trusted {
		t.Fatal("attribute path should be trusted (PR does not touch .gitattributes)")
	}
	classify(files, base, head, attr, Extras{})

	var genExcluded, handCounted bool
	for _, f := range files {
		if f.Path == "generated/api.go" && f.Generated {
			genExcluded = true
		}
		if f.Path == "hand.go" && !f.Generated {
			handCounted = true
		}
	}
	if !genExcluded {
		t.Error("generated/api.go should be excluded by the legit base .gitattributes rule")
	}
	if !handCounted {
		t.Error("hand.go should still be counted")
	}
}

// TestClassifyAppliesExtras proves the per-repo extras exclude matching files
// without any git attribute or content marker involved. Non-.go paths are used
// so contentGenerated never consults git, and attr.trusted is false so the
// attribute path is skipped — classify needs no repo.
func TestClassifyAppliesExtras(t *testing.T) {
	t.Parallel()
	extras, err := ParseExtras("Gemfile.lock", "*.gen.ts web/snapshots/**")
	if err != nil {
		t.Fatalf("ParseExtras: %v", err)
	}
	files := []FileChange{
		{Path: "app/Gemfile.lock", Added: 500},
		{Path: "web/src/api.gen.ts", Added: 800},
		{Path: "web/snapshots/a/b.snap", Added: 900},
		{Path: "web/src/hand.ts", Added: 40},
	}
	classify(files, "", "", attrPolicy{}, extras)

	wantGenerated := map[string]bool{
		"app/Gemfile.lock":       true,
		"web/src/api.gen.ts":     true,
		"web/snapshots/a/b.snap": true,
		"web/src/hand.ts":        false,
	}
	for _, f := range files {
		if f.Generated != wantGenerated[f.Path] {
			t.Errorf("%s: Generated = %v, want %v", f.Path, f.Generated, wantGenerated[f.Path])
		}
	}
	res := Evaluate(files, 1000, false)
	if res.Counted != 40 {
		t.Errorf("Counted = %d, want 40", res.Counted)
	}
	if res.Generated != 2200 {
		t.Errorf("Generated = %d, want 2200", res.Generated)
	}
}

// TestAttrTrusted pins the base-only invariant: attribute exclusions are trusted
// only when git can read attributes from the base ref (useSource), and the bypass
// label may lift the .gitattributes-touched gate but must NEVER re-enable
// head-controlled reading on an old-git runner (useSource=false).
func TestAttrTrusted(t *testing.T) {
	tests := []struct {
		name         string
		useSource    bool
		attrModified bool
		bypass       bool
		want         bool
	}{
		{"modern git, clean", true, false, false, true},
		{"modern git, gitattributes edited", true, true, false, false},
		{"modern git, gitattributes edited, bypass overrides gate", true, true, true, true},
		{"modern git, clean, bypass", true, false, true, true},
		{"old git never trusted", false, false, false, false},
		// The regression: bypass must not re-enable head reading on old git.
		{"old git, bypass does NOT re-enable head reading", false, false, true, false},
		{"old git, gitattributes edited, bypass", false, true, true, false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := attrTrusted(tt.useSource, tt.attrModified, tt.bypass); got != tt.want {
				t.Errorf("attrTrusted(useSource=%v, attrModified=%v, bypass=%v) = %v, want %v",
					tt.useSource, tt.attrModified, tt.bypass, got, tt.want)
			}
		})
	}
}

// TestShouldFail pins the mode contract: enforce fails on overage, warn never
// fails, and a bypassed result never fails in either mode.
func TestShouldFail(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name string
		res  Result
		mode string
		want bool
	}{
		{"enforce over cap fails", Result{OK: false}, modeEnforce, true},
		{"enforce under cap passes", Result{OK: true}, modeEnforce, false},
		{"warn over cap does not fail", Result{OK: false}, modeWarn, false},
		{"warn under cap does not fail", Result{OK: true}, modeWarn, false},
		{"enforce bypassed does not fail", Result{OK: true, Bypassed: true}, modeEnforce, false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()
			if got := shouldFail(tt.res, tt.mode); got != tt.want {
				t.Errorf("shouldFail(%+v, %q) = %v, want %v", tt.res, tt.mode, got, tt.want)
			}
		})
	}
}

// TestRenderReport checks the mode- and label-sensitive report copy: warn mode
// shows the warning header and never the failure one, the bypass label name is
// the configured one, and the over-cap guidance appears whenever over.
func TestRenderReport(t *testing.T) {
	t.Parallel()
	over := Result{Counted: 1500, Max: 1000, OK: false, Files: []FileChange{{Path: "big.go", Added: 1500}}}
	under := Result{Counted: 10, Max: 1000, OK: true}
	bypassed := Result{Counted: 1500, Max: 1000, OK: true, Bypassed: true}

	t.Run("enforce over cap", func(t *testing.T) {
		t.Parallel()
		got := renderReport(over, modeEnforce, "huge-ok")
		for _, want := range []string{"❌ Failed", "over the 1000-line cap", "`huge-ok` label", "Largest counted files"} {
			if !strings.Contains(got, want) {
				t.Errorf("report missing %q:\n%s", want, got)
			}
		}
	})
	t.Run("warn over cap", func(t *testing.T) {
		t.Parallel()
		got := renderReport(over, modeWarn, "oversized-ok")
		if !strings.Contains(got, "⚠️ Over cap (warn-only)") {
			t.Errorf("warn report missing warn header:\n%s", got)
		}
		if strings.Contains(got, "❌ Failed") {
			t.Errorf("warn report must not claim failure:\n%s", got)
		}
		if !strings.Contains(got, "`warn` mode") {
			t.Errorf("warn report should explain warn mode:\n%s", got)
		}
	})
	t.Run("under cap", func(t *testing.T) {
		t.Parallel()
		got := renderReport(under, modeEnforce, "oversized-ok")
		if !strings.Contains(got, "✅ Passed") {
			t.Errorf("under-cap report missing pass header:\n%s", got)
		}
		if strings.Contains(got, "Options:") {
			t.Errorf("under-cap report must not include over-cap guidance:\n%s", got)
		}
	})
	t.Run("bypassed", func(t *testing.T) {
		t.Parallel()
		got := renderReport(bypassed, modeEnforce, "oversized-ok")
		for _, want := range []string{"✅ Passed", "Bypassed via `oversized-ok` label"} {
			if !strings.Contains(got, want) {
				t.Errorf("bypassed report missing %q:\n%s", want, got)
			}
		}
	})
}

// TestContentGeneratedDeletedFileReadsBlob proves the deleted-file fallback
// (the file no longer exists in the working tree, so contentGenerated falls
// back to reading the head/base git blob) still classifies correctly.
func TestContentGeneratedDeletedFileReadsBlob(t *testing.T) {
	dir := initTestRepo(t)
	writeFile(t, dir, "gen.go", "// Code generated by tool DO NOT EDIT.\npackage x\n")
	base := commitAll(t, dir, "add generated file")
	if err := os.Remove(filepath.Join(dir, "gen.go")); err != nil {
		t.Fatal(err)
	}
	head := commitAll(t, dir, "delete generated file")
	t.Chdir(dir)

	// path is relative, matching production: f.Path comes from `git diff
	// --numstat` (repo-root-relative), and the process cwd is the repo root.
	if !contentGenerated("gen.go", base, head) {
		t.Error("a deleted generated file should still classify as generated via the base blob fallback")
	}
}

// TestRunGitCappedLimitsBlobRead is the regression for the deleted-file
// fallback's DoS guard: contentGenerated's working-tree read is capped at
// maxScanBytes, and the git-blob fallback (for a path no longer in the
// working tree) must be capped the same way rather than buffering an
// unbounded blob via a plain cmd.Output().
func TestRunGitCappedLimitsBlobRead(t *testing.T) {
	dir := initTestRepo(t)
	writeFile(t, dir, "big.txt", strings.Repeat("x", 10_000))
	sha := commitAll(t, dir, "add big file")
	t.Chdir(dir)

	data, err := runGitCapped(100, "show", sha+":big.txt")
	if err != nil {
		t.Fatalf("runGitCapped: %v", err)
	}
	if len(data) != 100 {
		t.Errorf("len(data) = %d, want 100 (capped read)", len(data))
	}
}
