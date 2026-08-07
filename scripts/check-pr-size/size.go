// Command check-pr-size caps a pull request's size, measured in lines of code
// changed, so diffs stay reviewable for humans and AI agents alike.
//
// It counts added + deleted lines across the PR diff, EXCLUDING generated files
// (codegen can emit huge amounts of code that would trip the cap unfairly), and
// fails if the remaining count exceeds a configurable ceiling. Opting in to
// Policy.ExcludeTests additionally keeps test-file lines out of the count, so a
// mostly-test PR is judged on the production code a reviewer must actually
// reason about. A PR label provides an explicit bypass for legitimate large
// changes.
//
// This file holds the pure, side-effect-free logic (diff parsing, generated-file
// and test-file classification, cap evaluation) so it can be unit tested without
// a git repo; main.go wires it to git and the CI environment.
package main

import (
	"bytes"
	"fmt"
	"io"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"unicode"
)

// generatedMarker matches Go's canonical generated-file header. A file counts as
// generated only when this appears BEFORE its package clause — the same rule the
// go toolchain uses, so a contributor cannot opt hand-written code out of the
// count by pasting the marker mid-file.
var generatedMarker = regexp.MustCompile(`^// Code generated .* DO NOT EDIT\.$`)

// lockfileNames are dependency lockfiles: machine-maintained, frequently huge,
// and not hand-reviewed line by line. They rarely carry a generated marker, so
// they are matched by base name. Per-repo additions come in via Extras.
var lockfileNames = map[string]bool{
	"go.sum":            true,
	"go.work.sum":       true,
	"package-lock.json": true,
	"pnpm-lock.yaml":    true,
	"yarn.lock":         true,
	"Cargo.lock":        true,
	"poetry.lock":       true,
	"uv.lock":           true,
}

// Test directories are matched in THREE cases, not one, because the segment
// names are not equally trustworthy. Matching every name at every depth is what
// caused the bug this split exists to fix: a real consumer keeps its production
// ArgoCD manifests — cluster RBAC, ingress, cert issuers — under
// `infrastructure/argocd/apps/testing/`, where `testing` names the target
// ENVIRONMENT, not test code. An any-depth rule silently excluded cluster RBAC
// changes from the cap, which is close to the worst thing this feature could do.
//
// Matching is on whole, slash-delimited SEGMENTS, never substrings, so
// `contest/`, `attestation/` and `latest.go` are untouched, and it is
// case-insensitive over ASCII so .NET/Unity trees (`Tests/`, `TestData/`) work.
// Only DIRECTORY segments are considered, never the file name itself.

// unambiguousTestSegments name test code and nothing else, so they are matched
// at ANY depth. Nobody names a production directory `__snapshots__`, and Go's
// `testdata` is defined by the toolchain to be nested.
var unambiguousTestSegments = map[string]bool{
	"__mocks__":     true, // Jest/Vitest manual mocks
	"__snapshots__": true, // Jest/Vitest snapshots
	"__tests__":     true, // Jest/Vitest
	"testdata":      true, // Go's fixture convention, nested by design
}

// ambiguousTestSegments usually mean tests but also legitimately name an
// environment or product area, so they are matched ONLY at the repo root or
// directly under a source root (see srcRoots). Root-level `testing/` is a test
// tree; `infrastructure/argocd/apps/testing/` is a deployment target.
var ambiguousTestSegments = map[string]bool{
	"e2e":     true,
	"test":    true,
	"testing": true,
	"tests":   true,
}

// srcRoots are directories under which an ambiguous segment is still a test
// directory — Maven/Gradle put tests at `src/test/java` and integration tests at
// `src/it`, both nested by convention. Without this case those repos would count
// their entire test tree, so the feature would quietly UNDER-deliver for them,
// which is the mirror of the over-delivery it caused for the ArgoCD tree.
var srcRoots = map[string]bool{"src": true}

// srcRootOnlyTestSegments are additionally allowed directly under a srcRoot.
// `it` is Maven failsafe's integration-test directory; it is far too generic to
// honour at the repo root.
var srcRootOnlyTestSegments = map[string]bool{"it": true}

// testFileSuffixes are base-name suffixes that mark test code. Each carries its
// own leading separator, so a production file that merely ENDS in the word
// (`latest.go`, `contest.py`) is not a match.
var testFileSuffixes = []string{
	"_test.go", // Go
	"_test.py", // Python, suffix style
}

// testFileNames are exact base names that are test scaffolding wherever they sit.
var testFileNames = map[string]bool{
	"conftest.py": true, // pytest fixture module — no production role
}

// jsTestExts are the JavaScript/TypeScript source extensions on which the
// `.test.` / `.spec.` infix convention applies (`Button.test.tsx`,
// `api.spec.ts`). The infix is matched on the stem with its leading dot, so a
// hand-written module literally named `spec.ts` is not a match.
var jsTestExts = map[string]bool{
	".cjs": true, ".cts": true, ".js": true, ".jsx": true,
	".mjs": true, ".mts": true, ".ts": true, ".tsx": true,
}

// FileChange is one file's contribution to the diff.
type FileChange struct {
	Path string
	// OldPath is a rename's SOURCE path, empty otherwise. It exists so
	// classification can be conservative: `git diff --numstat` books a rename's
	// deletions against the destination, so `git mv src/big.go tests/big.go`
	// would otherwise charge removed PRODUCTION lines to an excluded test path
	// and let the change slip under the cap. See IsTestPath's use in classify.
	OldPath   string
	Added     int
	Deleted   int
	Binary    bool
	Generated bool
	Test      bool
}

// Changed returns the line count this file contributes to PR size (added +
// deleted). Binary files contribute nothing (they are not lines of code).
func (f FileChange) Changed() int {
	if f.Binary {
		return 0
	}
	return f.Added + f.Deleted
}

// Policy is the configuration Evaluate applies to a classified diff. It is a
// struct rather than positional arguments because the knobs are booleans, which
// are silently swappable at a call site.
type Policy struct {
	Max          int  // the ceiling on counted lines
	Bypassed     bool // a bypass label was present
	ExcludeTests bool // subtract test-file lines from the counted total
}

// Result is the outcome of evaluating a diff against the cap.
type Result struct {
	// Counted is the number the cap is compared against: changed lines from
	// non-binary files that are neither generated nor — when TestsExcluded is
	// set — test files.
	Counted int
	// Generated is changed lines excluded because the file is generated. A
	// generated file that is ALSO a test file is tallied here and not in Test.
	Generated int
	// Test is changed lines in non-generated test files. It is subtracted from
	// Counted only when TestsExcluded is set; otherwise it is a subset of
	// Counted, reported for information.
	Test     int
	Max      int  // the configured ceiling
	Bypassed bool // a bypass label was present
	OK       bool // Bypassed OR Counted <= Max
	// TestsExcluded records whether Policy.ExcludeTests was set, so the report
	// can say whether Test lines were subtracted from Counted or are part of it.
	TestsExcluded bool
	// Files sorted by descending Changed(), for reporting.
	Files []FileChange
	// Note is an optional human-facing explanation appended to the report (e.g.
	// why linguist-generated exclusions were skipped). Set by the caller.
	Note string
}

// ExclusionDecisive reports whether the test exclusion is the ONLY reason this
// PR is under the cap — it would be over if test lines counted.
//
// This is the case the reporting exists for. A PR that passes on its own merits
// needs no explanation; one that passes only because 1,200 test lines were
// subtracted is exactly the "large test-only PR sails through unremarked" risk,
// so the workflow uses this to force the sticky comment even when the check is
// green (the comment otherwise posts only on overage, which would hide the
// number in precisely the case it matters most).
//
// Bypassed results are excluded: the label already explains why the PR passed,
// so attributing it to the exclusion would be misleading.
//
// Both halves of "under the cap ONLY because" are checked. Without the
// `Counted <= Max` term a run that is over cap even after the exclusion
// satisfies `Counted+Test > Max` trivially and would report itself decisive on a
// RED run — harmless in today's callers (the report gates on OK, and the comment
// job tests over-cap first) but wrong in the machine-readable output, which must
// stand on its own.
func (r Result) ExclusionDecisive() bool {
	return r.TestsExcluded && !r.Bypassed && r.Counted <= r.Max && r.Counted+r.Test > r.Max
}

// ParseNumstat parses the output of `git diff --numstat -z`. Records are
// NUL-delimited and, crucially, paths are emitted verbatim (no C-style quoting
// of spaces/UTF-8, unlike the newline form), so a lockfile or generated file
// with an unusual name is classified correctly. Each record is
// "<added>\t<deleted>\t<path>", with "-\t-" counts marking a binary file. A
// rename/copy is emitted as "<added>\t<deleted>\t" (empty path field) followed
// by two extra NUL-terminated tokens — the old path then the new path; the new
// path is kept so classification reads the file at its post-diff location.
func ParseNumstat(r io.Reader) ([]FileChange, error) {
	data, err := io.ReadAll(r)
	if err != nil {
		return nil, err
	}
	tokens := strings.Split(string(data), "\x00")
	var changes []FileChange
	for i := 0; i < len(tokens); i++ {
		rec := tokens[i]
		if rec == "" {
			continue // trailing NUL or stray separator
		}
		var oldPath string
		parts := strings.SplitN(rec, "\t", 3)
		if len(parts) != 3 {
			return nil, fmt.Errorf("malformed numstat record: %q", rec)
		}
		path := parts[2]
		if path == "" {
			// Rename/copy: the following two tokens are old path, then new path.
			// A missing/empty new-path token means a truncated stream.
			if i+2 >= len(tokens) || tokens[i+2] == "" {
				return nil, fmt.Errorf("truncated rename record: %q", rec)
			}
			oldPath, path = tokens[i+1], tokens[i+2]
			i += 2
		}
		fc := FileChange{Path: path, OldPath: oldPath}
		if parts[0] == "-" || parts[1] == "-" {
			fc.Binary = true
			changes = append(changes, fc)
			continue
		}
		added, err := strconv.Atoi(parts[0])
		if err != nil {
			return nil, fmt.Errorf("bad added count in %q: %w", rec, err)
		}
		deleted, err := strconv.Atoi(parts[1])
		if err != nil {
			return nil, fmt.Errorf("bad deleted count in %q: %w", rec, err)
		}
		fc.Added = added
		fc.Deleted = deleted
		changes = append(changes, fc)
	}
	return changes, nil
}

// IsGeneratedContent reports whether Go source carries the canonical generated
// marker before its package clause. The content is scanned from memory with no
// line-length limit, so a very long line before the package clause cannot cause
// a false negative. A nil/empty read returns false. Callers restrict this to
// .go files (see contentGenerated in main.go): the package-clause gate is what
// stops a contributor opting a hand-written file out of the count by pasting the
// marker mid-file, and non-Go files have no package clause to anchor that.
func IsGeneratedContent(content []byte) bool {
	for len(content) > 0 {
		var line []byte
		if i := bytes.IndexByte(content, '\n'); i >= 0 {
			line, content = content[:i], content[i+1:]
		} else {
			line, content = content, nil
		}
		s := strings.TrimRight(string(line), "\r")
		if strings.HasPrefix(s, "package ") {
			return false
		}
		if generatedMarker.MatchString(s) {
			return true
		}
	}
	return false
}

// IsLockfile reports whether the path is one of the built-in dependency
// lockfiles. Per-repo additions are handled by Extras.Generated.
func IsLockfile(path string) bool {
	return lockfileNames[baseName(path)]
}

// baseName returns the final path segment of a slash-separated path.
func baseName(path string) string {
	if slash := strings.LastIndex(path, "/"); slash >= 0 {
		return path[slash+1:]
	}
	return path
}

// IsTestPath reports whether a repo-relative path is test code, by naming
// convention: it sits under a test directory segment, or its base name matches a
// per-language test-file convention.
//
// Unlike the generated-file rules this is a CONVENTION check, not a proof — the
// content is never consulted, so nothing stops a contributor from parking
// production code in `tests/`. That is exactly why excluding test lines from the
// cap is opt-in per repo (`exclude_tests`) rather than the default, and why the
// excluded total is always reported rather than silently dropped.
func IsTestPath(path string) bool {
	return hasTestSegment(path) || isTestFileName(baseName(path))
}

// hasTestSegment reports whether path sits under a test directory, applying the
// three cases documented on the segment tables above. The final segment (the
// file name) is never considered, so a file merely sharing a name with a test
// directory is still counted.
func hasTestSegment(path string) bool {
	slash := strings.LastIndex(path, "/")
	if slash < 0 {
		return false // no directory part at all
	}
	segs := strings.Split(path[:slash], "/")
	for i := range segs {
		segs[i] = asciiLower(segs[i])
	}
	// Case 1 — unambiguous names, any depth.
	for _, seg := range segs {
		if unambiguousTestSegments[seg] {
			return true
		}
	}
	// Case 2 — ambiguous names, repo root only.
	if ambiguousTestSegments[segs[0]] {
		return true
	}
	// Case 3 — ambiguous names (plus `it`) directly under a source root.
	if len(segs) > 1 && srcRoots[segs[0]] &&
		(ambiguousTestSegments[segs[1]] || srcRootOnlyTestSegments[segs[1]]) {
		return true
	}
	return false
}

// asciiLower lowercases A–Z only. strings.ToLower applies Unicode simple case
// folding, under which U+212A KELVIN SIGN lowercases to `k` — so a directory
// named `__MOC⟨U+212A⟩S__` would fold to `__mocks__` and drop everything beneath
// it out of the count. The casing tolerance here exists for .NET/Unity trees
// (`Tests/`, `TestData/`), which is an ASCII concern, so ASCII is all it should
// do.
func asciiLower(s string) string {
	b := []byte(s)
	for i := range b {
		if b[i] >= 'A' && b[i] <= 'Z' {
			b[i] += 'a' - 'A'
		}
	}
	return string(b)
}

// isTestFileName reports whether a file's base name follows a test-file naming
// convention: an exact scaffolding name, a language suffix, Python's `test_`
// prefix, or the JS/TS `.test.`/`.spec.` infix before a source extension.
func isTestFileName(base string) bool {
	if testFileNames[base] {
		return true
	}
	for _, suffix := range testFileSuffixes {
		if strings.HasSuffix(base, suffix) {
			return true
		}
	}
	if strings.HasPrefix(base, "test_") && strings.HasSuffix(base, ".py") {
		return true
	}
	if dot := strings.LastIndex(base, "."); dot > 0 && jsTestExts[base[dot:]] {
		// A dot-separated stem component that is exactly `test` or `spec` marks
		// a test file, but the two are NOT symmetric:
		//
		//   `test` matches in any component after the first, which catches type
		//     tests (`foo.test.d.ts`) — the `*.test.*` convention the docs
		//     advertise.
		//   `spec` matches ONLY as the final stem component. In this org `spec`
		//     names OpenAPI artifacts, so `api.spec.types.ts` and
		//     `openapi.spec.client.ts` are PRODUCTION files — the same reasoning
		//     that keeps `spec`/`specs` out of testPathSegments above. Matching
		//     them would be an under-count, which is the unsafe direction:
		//     failing to exclude a test file only over-counts, but excluding a
		//     production file shrinks the number the cap protects.
		//
		// Requiring a non-first component is what keeps a hand-written module
		// literally named `spec.ts` counted.
		parts := strings.Split(base[:dot], ".")
		rest := parts[1:]
		for i, p := range rest {
			if p == "test" || (p == "spec" && i == len(rest)-1) {
				return true
			}
		}
	}
	return false
}

// Extras carries per-repo additions to the exclusion rules, parsed from the
// reusable workflow's extra_lockfiles / extra_generated_globs inputs.
type Extras struct {
	lockfiles map[string]bool
	globs     []extraGlob
}

type extraGlob struct {
	re *regexp.Regexp
	// baseOnly marks a pattern with no '/': it matches the file's base name at
	// any depth (like a .gitignore basename pattern) instead of the full path.
	baseOnly bool
}

// splitList splits a workflow-input list on whitespace and commas, so folded
// YAML scalars and comma lists both work.
func splitList(s string) []string {
	return strings.FieldsFunc(s, func(r rune) bool {
		return r == ',' || unicode.IsSpace(r)
	})
}

// ParseExtras parses the extra_lockfiles and extra_generated_globs inputs.
// Lockfile entries must be base names — matching mirrors the built-in list,
// which excludes a lockfile at any directory depth, so a path would silently
// never match.
func ParseExtras(lockfiles, globs string) (Extras, error) {
	var e Extras
	for _, name := range splitList(lockfiles) {
		if strings.Contains(name, "/") {
			return Extras{}, fmt.Errorf("extra lockfile %q must be a base name, not a path", name)
		}
		if e.lockfiles == nil {
			e.lockfiles = map[string]bool{}
		}
		e.lockfiles[name] = true
	}
	for _, pattern := range splitList(globs) {
		e.globs = append(e.globs, extraGlob{
			re:       globRegexp(pattern),
			baseOnly: !strings.Contains(pattern, "/"),
		})
	}
	return e, nil
}

// Generated reports whether path matches the per-repo extra exclusion rules:
// its base name is an extra lockfile, or it matches an extra generated glob.
func (e Extras) Generated(path string) bool {
	base := baseName(path)
	if e.lockfiles[base] {
		return true
	}
	for _, g := range e.globs {
		target := path
		if g.baseOnly {
			target = base
		}
		if g.re.MatchString(target) {
			return true
		}
	}
	return false
}

// globRegexp compiles a glob pattern to a regexp: `**` matches any characters
// including `/`, `*` matches within a path segment, `?` matches one non-`/`
// character; everything else is literal. Every non-wildcard byte is
// QuoteMeta-escaped, so the built expression is always valid and compilation
// cannot fail.
func globRegexp(pattern string) *regexp.Regexp {
	var b strings.Builder
	b.WriteString(`^`)
	for i := 0; i < len(pattern); i++ {
		switch pattern[i] {
		case '*':
			if i+1 < len(pattern) && pattern[i+1] == '*' {
				b.WriteString(`.*`)
				i++
			} else {
				b.WriteString(`[^/]*`)
			}
		case '?':
			b.WriteString(`[^/]`)
		default:
			b.WriteString(regexp.QuoteMeta(pattern[i : i+1]))
		}
	}
	b.WriteString(`$`)
	return regexp.MustCompile(b.String())
}

// TouchesGitattributes reports whether any changed file is a .gitattributes file
// (at the repo root or in any subdirectory). A PR that edits .gitattributes can
// introduce linguist-generated rules, so this drives the attribute path's
// defense-in-depth fallback in main.go (see attrPolicy).
func TouchesGitattributes(files []FileChange) bool {
	for _, f := range files {
		if baseName(f.Path) == ".gitattributes" {
			return true
		}
	}
	return false
}

// Evaluate sums the changed lines of non-generated files and compares against
// the cap. A file's Generated and Test fields must already be set by the caller.
// When Policy.Bypassed is true the result is always OK, but the counts are still
// reported.
//
// Generated and Test never overlap: a file that is both counts once, as
// generated. Whether Test overlaps Counted depends on policy. With ExcludeTests
// set the three are a partition — Counted + Generated + Test is the diff's total
// non-binary changed lines. WITHOUT it, Test is a SUBSET of Counted, reported
// for information only, so that same sum double-counts the test lines.
func Evaluate(files []FileChange, p Policy) Result {
	// Copy before sorting so we honor the file header's "side-effect-free"
	// contract and never reorder the caller's slice in place.
	sorted := make([]FileChange, len(files))
	copy(sorted, files)
	res := Result{Max: p.Max, Bypassed: p.Bypassed, TestsExcluded: p.ExcludeTests, Files: sorted}
	for _, f := range sorted {
		switch {
		case f.Generated:
			res.Generated += f.Changed()
		case f.Test:
			// Always tallied so the report can show the number either way; only
			// kept OUT of Counted when the caller opted in.
			res.Test += f.Changed()
			if !p.ExcludeTests {
				res.Counted += f.Changed()
			}
		default:
			res.Counted += f.Changed()
		}
	}
	res.OK = p.Bypassed || res.Counted <= p.Max
	sort.SliceStable(res.Files, func(i, j int) bool {
		return res.Files[i].Changed() > res.Files[j].Changed()
	})
	return res
}
