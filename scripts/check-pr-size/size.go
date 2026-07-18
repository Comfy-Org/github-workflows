// Command check-pr-size caps a pull request's size, measured in lines of code
// changed, so diffs stay reviewable for humans and AI agents alike.
//
// It counts added + deleted lines across the PR diff, EXCLUDING generated files
// (codegen can emit huge amounts of code that would trip the cap unfairly), and
// fails if the remaining count exceeds a configurable ceiling. A PR label
// provides an explicit bypass for legitimate large changes.
//
// This file holds the pure, side-effect-free logic (diff parsing, generated-file
// classification, cap evaluation) so it can be unit tested without a git repo;
// main.go wires it to git and the CI environment.
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

// FileChange is one file's contribution to the diff.
type FileChange struct {
	Path      string
	Added     int
	Deleted   int
	Binary    bool
	Generated bool
}

// Changed returns the line count this file contributes to PR size (added +
// deleted). Binary files contribute nothing (they are not lines of code).
func (f FileChange) Changed() int {
	if f.Binary {
		return 0
	}
	return f.Added + f.Deleted
}

// Result is the outcome of evaluating a diff against the cap.
type Result struct {
	Counted   int  // changed lines from non-generated, non-binary files
	Generated int  // changed lines excluded because the file is generated
	Max       int  // the configured ceiling
	Bypassed  bool // a bypass label was present
	OK        bool // Bypassed OR Counted <= Max
	// Files sorted by descending Changed(), for reporting.
	Files []FileChange
	// Note is an optional human-facing explanation appended to the report (e.g.
	// why linguist-generated exclusions were skipped). Set by the caller.
	Note string
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
			path = tokens[i+2]
			i += 2
		}
		fc := FileChange{Path: path}
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
// max. A file's Generated field must already be set by the caller. When bypassed
// is true the result is always OK, but the counts are still reported.
func Evaluate(files []FileChange, max int, bypassed bool) Result {
	// Copy before sorting so we honor the file header's "side-effect-free"
	// contract and never reorder the caller's slice in place.
	sorted := make([]FileChange, len(files))
	copy(sorted, files)
	res := Result{Max: max, Bypassed: bypassed, Files: sorted}
	for _, f := range sorted {
		if f.Generated {
			res.Generated += f.Changed()
			continue
		}
		res.Counted += f.Changed()
	}
	res.OK = bypassed || res.Counted <= max
	sort.SliceStable(res.Files, func(i, j int) bool {
		return res.Files[i].Changed() > res.Files[j].Changed()
	})
	return res
}
