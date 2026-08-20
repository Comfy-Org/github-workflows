package main

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"unicode/utf8"
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

func TestResolveMergeBaseWithAdvancedBaseBranch(t *testing.T) {
	dir := initTestRepo(t)
	writeFile(t, dir, "shared.txt", "common\n")
	forkPoint := commitAll(t, dir, "fork point")

	gitRun(t, dir, "checkout", "-q", "-b", "pr")
	writeFile(t, dir, "pr.txt", "from pr\n")
	prHead := commitAll(t, dir, "pr change")

	gitRun(t, dir, "checkout", "-q", "-b", "main", forkPoint)
	writeFile(t, dir, "main.txt", "unrelated main change\n")
	mainTip := commitAll(t, dir, "main advances")
	t.Chdir(dir)

	got, err := resolveMergeBase(mainTip, prHead)
	if err != nil {
		t.Fatalf("resolveMergeBase: %v", err)
	}
	if got != forkPoint {
		t.Fatalf("resolveMergeBase(%s, %s) = %s, want fork point %s", mainTip, prHead, got, forkPoint)
	}

	files, err := diffFiles(got, prHead)
	if err != nil {
		t.Fatalf("diffFiles: %v", err)
	}
	if len(files) != 1 || files[0].Path != "pr.txt" {
		t.Fatalf("review diff = %+v, want only pr.txt", files)
	}
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
	classify(files, base, head, attr, Extras{}, false)

	for _, f := range files {
		if f.Path == "hand.go" && f.Generated {
			t.Error("hand.go was excluded by a PR-introduced .gitattributes rule")
		}
	}
	res := Evaluate(files, Policy{Max: 1000})
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
	classify(files, base, head, attr, Extras{}, false)

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

// TestClassifyRenameIntoAttrGeneratedTreeStillCounts is the attribute half of
// the rename anti-gaming guard (the path-based buckets are covered by
// TestRenameIntoExclusionBucketsStillCounts, which cannot reach this one — the
// attribute lookup needs a real repo). Renaming a hand-written file INTO a
// `linguist-generated` tree and editing it in the same commit must not hide the
// edit: classifying on the destination alone excluded it from both the size
// count and the --reviewed-diff-out patch fed to the review panel. A rename
// whose BOTH ends carry the attribute is still excluded, so genuine moves
// inside a generated tree are unaffected.
func TestClassifyRenameIntoAttrGeneratedTreeStillCounts(t *testing.T) {
	dir := initTestRepo(t)
	// The two base files must NOT be byte-identical: gen/keep2.json below is an
	// exact-blob rename of gen/keep.json, and if handwritten/x.json carried the
	// same bytes git's exact-rename phase could pair keep2 with the hand-written
	// source instead (neither basename matches, so the tie-break is hashmap
	// order and varies by git version), inverting both OldPath assertions.
	body := strings.Repeat("{\"k\": \"v\"}\n", 40)
	keepBody := strings.Repeat("{\"keep\": \"generated\"}\n", 25)
	writeFile(t, dir, ".gitattributes", "gen/** linguist-generated=true\n")
	writeFile(t, dir, "handwritten/x.json", body)
	writeFile(t, dir, "gen/keep.json", keepBody)
	base := commitAll(t, dir, "base")

	// PR head, touching NOTHING under .gitattributes: a hand-written file moves
	// into the generated tree carrying an injected edit, and a genuinely
	// generated file moves within that tree.
	gitRun(t, dir, "mv", "handwritten/x.json", "gen/x.json")
	writeFile(t, dir, "gen/x.json", body+"{\"injected\": \"marker\"}\n")
	gitRun(t, dir, "mv", "gen/keep.json", "gen/keep2.json")
	head := commitAll(t, dir, "head")
	t.Chdir(dir)

	// Probe AFTER the chdir: base is a SHA in this throwaway repo, so running it
	// from the outer repo would fail for "not a valid object" — which
	// checkAttrSourceSupported deliberately reports as supported — and the skip
	// could never fire on a runner whose git predates check-attr --source.
	if !checkAttrSourceSupported(base) {
		t.Skip("git too old for check-attr --source")
	}

	files, err := diffFiles(base, head)
	if err != nil {
		t.Fatalf("diffFiles: %v", err)
	}
	attr := attrPolicy{source: base, useSource: true}
	attr.trusted = attrTrusted(attr.useSource, TouchesGitattributes(files), false)
	if !attr.trusted {
		t.Fatal("attribute path should be trusted (PR does not touch .gitattributes)")
	}
	classify(files, base, head, attr, Extras{}, false)

	byPath := make(map[string]FileChange, len(files))
	for _, f := range files {
		byPath[f.Path] = f
	}
	moved, ok := byPath["gen/x.json"]
	if !ok {
		t.Fatalf("git did not report gen/x.json as changed; got %+v", files)
	}
	if moved.OldPath != "handwritten/x.json" {
		t.Fatalf("gen/x.json OldPath = %q, want handwritten/x.json (rename detection did not fire)", moved.OldPath)
	}
	if moved.Generated {
		t.Error("a hand-written file renamed INTO a linguist-generated tree must not be classified generated — its edit would vanish from both the count and the reviewed diff")
	}
	kept, ok := byPath["gen/keep2.json"]
	if !ok {
		t.Fatalf("git did not report gen/keep2.json as changed; got %+v", files)
	}
	if !kept.Generated {
		t.Error("a rename whose source AND destination both carry linguist-generated should stay excluded")
	}

	res := Evaluate(files, Policy{Max: 1000})
	if res.Counted == 0 {
		t.Errorf("Counted = %d, want the injected edit counted", res.Counted)
	}

	// End-to-end on the second symptom: the injected edit must reach the patch
	// the review panel reads, while the genuinely generated rename stays out.
	out := filepath.Join(t.TempDir(), "pr-diff.patch")
	if err := writeReviewedDiff(base, head, nil, files, out); err != nil {
		t.Fatalf("writeReviewedDiff: %v", err)
	}
	patch, err := os.ReadFile(out)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(patch), "injected") {
		t.Error("the injected edit is missing from the --reviewed-diff-out patch — the review panel would never see it")
	}
	if strings.Contains(string(patch), "keep2.json") {
		t.Error("a rename entirely inside a generated tree should still be dropped from the reviewed diff")
	}
}

// TestAnnotateDiscountsPairsRenamesSoTheEditStillCounts guards the downstream
// half of the rename guard. classify() correctly leaves a hand-written file
// renamed out of its tree non-generated, but annotateDiscounts then re-derives a
// patch from git; naming only the DESTINATION in that pathspec hid the deletion
// side, git could not pair the rename, and it re-emitted the file as a
// whole-file ADDITION whose every comment/blank line became a discount. Clamped
// at Changed(), that drove the rename's Counted() to 0 — silently cancelling the
// guard for any caller running with --ignore-comments (cursor-review's
// ignore_comments input feeds exactly that).
func TestAnnotateDiscountsPairsRenamesSoTheEditStillCounts(t *testing.T) {
	dir := initTestRepo(t)
	// Comment-heavy on purpose: unpaired, the whole-file addition discounts far
	// more lines than the rename actually changed, so the clamp zeroes it out.
	var sb strings.Builder
	sb.WriteString("package hw\n")
	for i := 0; i < 200; i++ {
		fmt.Fprintf(&sb, "// comment line %d\n\nvar V%d = %d\n", i, i, i)
	}
	writeFile(t, dir, "handwritten/big.go", sb.String())
	base := commitAll(t, dir, "base")

	if err := os.MkdirAll(filepath.Join(dir, "moved"), 0o755); err != nil {
		t.Fatal(err)
	}
	gitRun(t, dir, "mv", "handwritten/big.go", "moved/big.go")
	writeFile(t, dir, "moved/big.go", sb.String()+"var Injected = 42\n")
	head := commitAll(t, dir, "head")
	t.Chdir(dir)

	files, err := diffFiles(base, head)
	if err != nil {
		t.Fatalf("diffFiles: %v", err)
	}
	classify(files, base, head, attrPolicy{}, Extras{}, false)

	var moved *FileChange
	for i := range files {
		if files[i].Path == "moved/big.go" {
			moved = &files[i]
		}
	}
	if moved == nil {
		t.Fatalf("git did not report moved/big.go as changed; got %+v", files)
	}
	if moved.OldPath != "handwritten/big.go" {
		t.Fatalf("moved/big.go OldPath = %q, want handwritten/big.go (rename detection did not fire)", moved.OldPath)
	}
	if moved.Generated {
		t.Fatal("a plain hand-written rename must not be classified generated")
	}

	annotateDiscounts(files, base, head)
	if moved.Discounted != 0 {
		t.Errorf("Discounted = %d, want 0 — the rename was re-read as a whole-file addition", moved.Discounted)
	}
	if got := Evaluate(files, Policy{Max: 1000}).Counted; got != 1 {
		t.Errorf("Counted = %d, want 1 (the injected line); discounting cancelled the rename guard", got)
	}
}

// TestWriteReviewedDiff proves the end-to-end --reviewed-diff-out path against
// real git: the emitted patch keeps the hand-written file's section, drops the
// generated file's section entirely, and honors a verbatim exclude pathspec —
// with no per-file argv involved, however many files are excluded.
func TestWriteReviewedDiff(t *testing.T) {
	dir := initTestRepo(t)
	writeFile(t, dir, "hand.go", "package main\n")
	writeFile(t, dir, "gen.go", "// Code generated by tool DO NOT EDIT.\npackage main\n")
	writeFile(t, dir, "notes.txt", "old\n")
	base := commitAll(t, dir, "base")

	writeFile(t, dir, "hand.go", "package main\n\nfunc F() {}\n")
	writeFile(t, dir, "gen.go", "// Code generated by tool DO NOT EDIT.\npackage main\n\nvar Regenerated = true\n")
	writeFile(t, dir, "notes.txt", "new\n")
	head := commitAll(t, dir, "head")
	t.Chdir(dir)

	files, err := diffFiles(base, head)
	if err != nil {
		t.Fatalf("diffFiles: %v", err)
	}
	classify(files, base, head, attrPolicy{}, Extras{}, false)

	out := filepath.Join(t.TempDir(), "pr-diff.patch")
	if err := writeReviewedDiff(base, head, []string{":(exclude)notes.txt"}, files, out); err != nil {
		t.Fatalf("writeReviewedDiff: %v", err)
	}
	patch, err := os.ReadFile(out)
	if err != nil {
		t.Fatal(err)
	}
	got := string(patch)
	if !strings.Contains(got, "diff --git a/hand.go b/hand.go") {
		t.Errorf("reviewed diff should keep hand.go:\n%s", got)
	}
	if strings.Contains(got, "gen.go") {
		t.Errorf("reviewed diff must not contain the generated file's section:\n%s", got)
	}
	if strings.Contains(got, "notes.txt") {
		t.Errorf("reviewed diff must honor the verbatim exclude pathspec:\n%s", got)
	}
}

// TestContentGeneratedMarkerFromBase pins the base-blob marker invariant: with
// markerFromBase set, a marker the PR ADDS is ignored (a PR cannot self-exempt
// a file from a consumer's review), while a file already generated at base
// stays classified even if the head strips the marker.
func TestContentGeneratedMarkerFromBase(t *testing.T) {
	dir := initTestRepo(t)
	writeFile(t, dir, "hand.go", "package main\nvar A = 1\n")
	writeFile(t, dir, "gen.go", "// Code generated by tool DO NOT EDIT.\npackage main\n")
	base := commitAll(t, dir, "base")

	// Head: the attack (marker prepended to the hand-written file), plus the
	// inverse (marker stripped from the generated file).
	writeFile(t, dir, "hand.go", "// Code generated by tool DO NOT EDIT.\npackage main\nvar A = 2\n")
	writeFile(t, dir, "gen.go", "package main\n")
	writeFile(t, dir, "new_gen.go", "// Code generated by tool DO NOT EDIT.\npackage main\n")
	head := commitAll(t, dir, "head")
	t.Chdir(dir)

	if contentGenerated("hand.go", base, head, true) {
		t.Error("a head-added marker must not classify a file generated when markerFromBase is set")
	}
	if !contentGenerated("hand.go", base, head, false) {
		t.Error("sanity: without markerFromBase the head content is honored (the size-cap behavior)")
	}
	if !contentGenerated("gen.go", base, head, true) {
		t.Error("a file generated at base should stay classified via the base blob")
	}
	if contentGenerated("new_gen.go", base, head, true) {
		t.Error("a file new in the PR has no base blob, so the marker must not match (conservative)")
	}
}

// TestClassifyBinaryExtras is the regression for binary generated files: a
// binary matching an extra generated glob (e.g. data/object_info.json.gz) must
// be marked Generated — previously classify short-circuited binaries before the
// path rules, so the binary reappeared in the reviewed diff as a binary-differs
// stanza despite the caller excluding it.
func TestClassifyBinaryExtras(t *testing.T) {
	t.Parallel()
	extras, err := ParseExtras("", "data/object_info.json.gz")
	if err != nil {
		t.Fatalf("ParseExtras: %v", err)
	}
	files := []FileChange{
		{Path: "data/object_info.json.gz", Binary: true},
		{Path: "assets/logo.png", Binary: true},
		{Path: "hand.go", Added: 3},
	}
	classify(files, "", "", attrPolicy{}, extras, false)
	for _, f := range files {
		switch f.Path {
		case "data/object_info.json.gz":
			if !f.Generated {
				t.Error("binary matching an extra generated glob must be classified generated")
			}
		default:
			if f.Generated {
				t.Errorf("%s must not be classified generated", f.Path)
			}
		}
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
	classify(files, "", "", attrPolicy{}, extras, false)

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
	res := Evaluate(files, Policy{Max: 1000})
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

// section returns the body of the named <details> block in a report, or "" if
// absent — so a test can assert which LIST a file appears in, not merely that
// its name occurs somewhere in the markdown.
func section(report, summary string) string {
	start := strings.Index(report, "<summary>"+summary+"</summary>")
	if start < 0 {
		return ""
	}
	rest := report[start:]
	if end := strings.Index(rest, "</details>"); end >= 0 {
		return rest[:end]
	}
	return rest
}

// TestRenderReportTestLines is the visibility guarantee for the exclusion: the
// test total must appear in the report BOTH ways round — as an explicit
// exclusion when opted in (so a large test-only PR cannot pass unremarked), and
// as a breakdown of the counted number when not (so the knob is discoverable).
func TestRenderReportTestLines(t *testing.T) {
	t.Parallel()
	files := []FileChange{
		{Path: "hand.go", Added: 300, Deleted: 36},
		{Path: "hand_test.go", Added: 1200, Deleted: 33, Test: true},
	}

	t.Run("excluded tests are reported and kept out of the file list", func(t *testing.T) {
		t.Parallel()
		got := renderReport(Evaluate(files, Policy{Max: 1000, ExcludeTests: true}), modeEnforce, "oversized-ok")
		for _, want := range []string{
			"✅ Passed",
			"Changed lines counted (non-generated, non-test): **336**",
			"Excluded (tests): 1233",
		} {
			if !strings.Contains(got, want) {
				t.Errorf("report missing %q:\n%s", want, got)
			}
		}
		// An excluded test file has no business in the COUNTED list (it does
		// belong in the excluded one, checked separately below).
		if strings.Contains(section(got, "Largest counted files"), "hand_test.go") {
			t.Errorf("excluded test file must not appear in the largest-counted list:\n%s", got)
		}
	})

	t.Run("counted tests are broken out without changing the verdict", func(t *testing.T) {
		t.Parallel()
		got := renderReport(Evaluate(files, Policy{Max: 1000}), modeEnforce, "oversized-ok")
		for _, want := range []string{
			"❌ Failed",
			"Changed lines counted (non-generated): **1569**",
			"1233 are tests (`exclude_tests` is off)",
			"1569 lines of hand-written code",
		} {
			if !strings.Contains(got, want) {
				t.Errorf("report missing %q:\n%s", want, got)
			}
		}
		if strings.Contains(got, "Excluded (tests)") {
			t.Errorf("tests are counted here, so nothing may claim they were excluded:\n%s", got)
		}
	})

	t.Run("a green PR saved by the exclusion says so and lists the excluded files", func(t *testing.T) {
		t.Parallel()
		got := renderReport(Evaluate(files, Policy{Max: 1000, ExcludeTests: true}), modeEnforce, "oversized-ok")
		for _, want := range []string{
			"✅ Passed",
			"Under the cap only because test lines are excluded",
			"changes 1569 non-generated lines (336 counted + 1233 test)",
			// The excluded number must be auditable, not just asserted.
			"Largest excluded test files",
			"hand_test.go",
		} {
			if !strings.Contains(got, want) {
				t.Errorf("report missing %q:\n%s", want, got)
			}
		}
	})

	t.Run("a PR that would pass anyway gets no such explanation", func(t *testing.T) {
		t.Parallel()
		small := []FileChange{
			{Path: "hand.go", Added: 10},
			{Path: "hand_test.go", Added: 20, Test: true},
		}
		got := renderReport(Evaluate(small, Policy{Max: 1000, ExcludeTests: true}), modeEnforce, "oversized-ok")
		if strings.Contains(got, "only because test lines are excluded") {
			t.Errorf("a comfortably-under PR must not claim the exclusion saved it:\n%s", got)
		}
	})

	t.Run("no test bullet when a PR has no test changes", func(t *testing.T) {
		t.Parallel()
		got := renderReport(Evaluate(files[:1], Policy{Max: 1000}), modeEnforce, "oversized-ok")
		if strings.Contains(got, "are tests") {
			t.Errorf("a PR with no test changes should not get a test bullet:\n%s", got)
		}
	})
}

// TestSanitizePath is the report-injection guard. `git diff --numstat -z` emits
// paths verbatim, so a PR author controls these bytes; untreated they let forged
// lines land inside a BOT-authored comment, which is the one place a reader
// trusts the numbers.
func TestSanitizePath(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name string
		path string
		want string
	}{
		{"ordinary path untouched", "pkg/foo/bar.go", "pkg/foo/bar.go"},
		{"unicode preserved", "café/go.sum", "café/go.sum"},
		{
			// Closing the code span and forging a passing verdict.
			name: "backtick cannot close the code span",
			path: "a`.go\n\n## ✅ Passed — PR size check\n`x",
			want: "a'.go??## ✅ Passed — PR size check?'x",
		},
		{
			// A forged marker would hijack the sticky-comment lookup.
			name: "marker injection is defanged",
			path: "x`\n<!-- ci-pr-size -->\n`y.go",
			want: "x'?<!-- ci-pr-size -->?'y.go",
		},
		{
			// Newlines reaching stdout can emit workflow commands.
			name: "workflow command injection is defanged",
			path: "a\n::error::spoofed\nb.go",
			want: "a?::error::spoofed?b.go",
		},
		{"carriage return replaced", "a\rb.go", "a?b.go"},
		{
			// Trojan-source class: a bidi override renders the path as a
			// filename other than the one on disk, in the very list a reviewer
			// uses to confirm the excluded files really are tests.
			name: "bidi override is replaced",
			path: "src/‮og.tset_x/a.go",
			want: "src/?og.tset_x/a.go",
		},
		{"bidi isolate is replaced", "a⁦b⁩c.go", "a?b?c.go"},
		{"zero-width joiner is replaced", "a‍b.go", "a?b.go"},
		// C1 controls are NOT caught by `r < 0x20`; U+0085 (NEL) renders as a
		// line break, and U+009B (CSI) opens ANSI sequences in the public log.
		{"C1 next-line is replaced", "ab.go", "a?b.go"},
		{"C1 CSI is replaced", "ab.go", "a?b.go"},
		// Zl/Zp separators are line breaks too.
		{"line separator is replaced", "a b.go", "a?b.go"},
		{"paragraph separator is replaced", "a b.go", "a?b.go"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()
			if got := sanitizePath(tt.path); got != tt.want {
				t.Errorf("sanitizePath(%q) = %q, want %q", tt.path, got, tt.want)
			}
		})
	}

	t.Run("over-long paths are bounded", func(t *testing.T) {
		t.Parallel()
		got := sanitizePath(strings.Repeat("a", 5000) + ".go")
		if len(got) > maxPathDisplay+len("…") {
			t.Errorf("len = %d, want <= %d", len(got), maxPathDisplay+len("…"))
		}
		// The ellipsis sits in the MIDDLE, not at the end — the tail is kept.
		if !strings.Contains(got, "…") {
			t.Errorf("truncated path should be marked with an ellipsis, got %q", got)
		}
		if !strings.HasSuffix(got, ".go") {
			t.Errorf("the tail should survive elision, got %q", got)
		}
	})

	// Truncating the TAIL would discard exactly the segment that explains why a
	// file was classified as a test, in the list whose purpose is checking that
	// classification. Eliding the middle keeps it.
	t.Run("truncation keeps the classifying tail", func(t *testing.T) {
		t.Parallel()
		got := sanitizePath(strings.Repeat("a", 400) + "/tests/prod.go")
		if !strings.HasSuffix(got, "/tests/prod.go") {
			t.Errorf("the tail that shows WHY this counted as a test was truncated away: %q", got)
		}
		if !strings.Contains(got, "…") {
			t.Errorf("an elided path should say so: %q", got)
		}
		if len(got) > maxPathDisplay+len("…") {
			t.Errorf("len = %d, over bound", len(got))
		}
	})

	// The ASCII case above cannot catch a cut landing mid-rune. Multi-byte runes
	// at every offset make the byte boundary fall inside a rune for some input
	// length, so a byte-slice truncation emits invalid UTF-8 here.
	t.Run("truncation never splits a rune", func(t *testing.T) {
		t.Parallel()
		for _, r := range []string{"é", "☃", "🙂"} {
			for n := 1; n <= 200; n++ {
				got := sanitizePath(strings.Repeat(r, n) + ".go")
				if !utf8.ValidString(got) {
					t.Fatalf("sanitizePath(%d×%q) produced invalid UTF-8: %q", n, r, got)
				}
				if len(got) > maxPathDisplay+len("…") {
					t.Fatalf("sanitizePath(%d×%q) len = %d, over bound", n, r, len(got))
				}
			}
		}
	})
}

// TestRenderReportSanitizesPaths proves the guard is wired into the report, not
// merely available.
//
// The invariant is structural, not textual: markdown only sees a heading at the
// START of a line, so what must be impossible is a hostile path creating a NEW
// line. The forged text surviving inside a code span on the path's own line is
// harmless and expected — asserting its mere absence would test the wrong thing.
func TestRenderReportSanitizesPaths(t *testing.T) {
	t.Parallel()
	hostile := "evil`.go\n\n## ✅ Passed — PR size check\n\n`x.go"
	got := renderReport(Evaluate([]FileChange{
		{Path: hostile, Added: 2000},
	}, Policy{Max: 1000}), modeEnforce, "oversized-ok")

	var headings []string
	for _, line := range strings.Split(got, "\n") {
		if strings.HasPrefix(line, "## ") {
			headings = append(headings, line)
		}
	}
	if len(headings) != 1 {
		t.Fatalf("report must carry exactly one status heading, got %d: %q\n%s", len(headings), headings, got)
	}
	if !strings.Contains(headings[0], "❌ Failed") {
		t.Errorf("the surviving heading should be the real failing verdict, got %q", headings[0])
	}
	if strings.Contains(got, hostile) {
		t.Errorf("raw hostile path reached the report unsanitized:\n%s", got)
	}
}

// TestClassifySetsTestRegardlessOfPolicy proves classification is policy-free:
// Test is set from the path alone, so Evaluate can report the total whether or
// not the caller opted to exclude it. Non-.go paths keep git out of the picture
// (contentGenerated never consults it) and attr.trusted is false, so classify
// needs no repo.
func TestClassifySetsTestRegardlessOfPolicy(t *testing.T) {
	t.Parallel()
	files := []FileChange{
		{Path: "web/src/Button.test.tsx", Added: 400},
		{Path: "web/src/__tests__/render.tsx", Added: 200},
		{Path: "web/src/Button.tsx", Added: 60},
		{Path: "web/src/manifest.ts", Added: 40},
	}
	classify(files, "", "", attrPolicy{}, Extras{}, false)

	wantTest := map[string]bool{
		"web/src/Button.test.tsx":      true,
		"web/src/__tests__/render.tsx": true,
		"web/src/Button.tsx":           false,
		"web/src/manifest.ts":          false,
	}
	for _, f := range files {
		if f.Test != wantTest[f.Path] {
			t.Errorf("%s: Test = %v, want %v", f.Path, f.Test, wantTest[f.Path])
		}
		if f.Generated {
			t.Errorf("%s: nothing here is generated, got Generated = true", f.Path)
		}
	}

	// Same classification, two policies: only the accounting moves.
	if res := Evaluate(files, Policy{Max: 1000}); res.Counted != 700 || res.Test != 600 {
		t.Errorf("default policy: Counted = %d (want 700), Test = %d (want 600)", res.Counted, res.Test)
	}
	if res := Evaluate(files, Policy{Max: 1000, ExcludeTests: true}); res.Counted != 100 || res.Test != 600 {
		t.Errorf("exclude policy: Counted = %d (want 100), Test = %d (want 600)", res.Counted, res.Test)
	}
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
	if !contentGenerated("gen.go", base, head, false) {
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

// TestRunGitFoldsStderrIntoError guards the refactor regression: runGit
// delegates to runGitStdin, which sets cmd.Stderr, so a returned *exec.ExitError
// no longer carries git's stderr. runGit must fold the captured stderr back into
// its error so callers (e.g. diffFiles) keep git's diagnostics.
func TestRunGitFoldsStderrIntoError(t *testing.T) {
	dir := initTestRepo(t)
	t.Chdir(dir)

	// A bogus revision makes git exit non-zero and write a diagnostic to stderr.
	_, err := runGit("show", "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef:nope")
	if err == nil {
		t.Fatal("runGit on a bogus revision should return an error")
	}
	if !strings.Contains(strings.ToLower(err.Error()), "fatal") {
		t.Errorf("runGit error should carry git's stderr diagnostic, got: %v", err)
	}
}

// TestAnnotateDiscountsGit exercises the whole count path against real git:
// diffFiles (numstat) -> classify -> annotateDiscounts -> Evaluate. The PR adds
// significant code alongside comment and blank lines; only the significant lines
// must count toward the cap.
func TestAnnotateDiscountsGit(t *testing.T) {
	dir := initTestRepo(t)
	writeFile(t, dir, "hand.go", "package main\n")
	base := commitAll(t, dir, "base")

	// Head adds 6 lines: 1 blank + 2 comments (insignificant) + 3 code (significant).
	writeFile(t, dir, "hand.go", "package main\n\n// a comment\n// another comment\nfunc F() int {\n\treturn 41\n}\n")
	head := commitAll(t, dir, "head")
	t.Chdir(dir)

	files, err := diffFiles(base, head)
	if err != nil {
		t.Fatalf("diffFiles: %v", err)
	}
	classify(files, base, head, attrPolicy{}, Extras{}, false)
	annotateDiscounts(files, base, head)

	var hand *FileChange
	for i := range files {
		if files[i].Path == "hand.go" {
			hand = &files[i]
		}
	}
	if hand == nil {
		t.Fatal("hand.go not present in diff")
	}
	if hand.Changed() != 6 {
		t.Fatalf("hand.go Changed() = %d, want 6 added lines", hand.Changed())
	}
	if hand.Discounted != 3 {
		t.Errorf("hand.go Discounted = %d, want 3 (1 blank + 2 comments)", hand.Discounted)
	}
	if hand.Counted() != 3 {
		t.Errorf("hand.go Counted() = %d, want 3 significant lines", hand.Counted())
	}
	if res := Evaluate(files, Policy{Max: 1000}); res.Counted != 3 || res.Discounted != 3 {
		t.Errorf("Evaluate: Counted=%d Discounted=%d, want 3 and 3", res.Counted, res.Discounted)
	}
}

// TestAnnotateDiscountsSkipsGenerated proves comment-discounting never touches a
// generated file: its lines stay in the generated total (excluded wholesale),
// not the counted or discounted totals.
func TestAnnotateDiscountsSkipsGenerated(t *testing.T) {
	dir := initTestRepo(t)
	writeFile(t, dir, "hand.go", "package main\n")
	base := commitAll(t, dir, "base")

	writeFile(t, dir, "gen.go", "// Code generated by tool DO NOT EDIT.\npackage gen\n\n// comment\nvar X = 1\n")
	writeFile(t, dir, "hand.go", "package main\n\n// note\nvar Y = 2\n")
	head := commitAll(t, dir, "head")
	t.Chdir(dir)

	files, err := diffFiles(base, head)
	if err != nil {
		t.Fatalf("diffFiles: %v", err)
	}
	classify(files, base, head, attrPolicy{}, Extras{}, false)
	annotateDiscounts(files, base, head)

	for i := range files {
		if files[i].Path == "gen.go" {
			if !files[i].Generated {
				t.Error("gen.go should be classified generated")
			}
			if files[i].Discounted != 0 {
				t.Errorf("generated gen.go must not be discounted, got %d", files[i].Discounted)
			}
		}
	}
}

// TestElideMiddleHonoursTinyBounds pins the case round 5's reply claimed was
// fixed when it was not: asked for a bound below the ellipsis's own length, the
// function must not return something longer than the bound.
func TestElideMiddleHonoursTinyBounds(t *testing.T) {
	t.Parallel()
	for _, max := range []int{0, 1, 2, 3} {
		if got := elideMiddle("some/long/path.go", max); len(got) > max {
			t.Errorf("elideMiddle(max=%d) = %q (%d bytes) — exceeds its own bound", max, got, len(got))
		}
	}
}
