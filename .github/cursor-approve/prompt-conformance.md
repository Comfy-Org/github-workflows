**Conformance.** Judge whether the code fits this repository's engineering and
deployment practices.

Read the repository's own guidance first — `AGENTS.md`, `CLAUDE.md` and
`CONTRIBUTING.md`, at the root and in the directories the change touches, when
they are present — and treat it as the standard. Then check the diff against it
and against the conventions the neighboring code already follows: where files
live, how things are named, how errors are handled and logged, how
dependencies, configuration, migrations and releases are managed, and which
tests and docs the repository expects alongside a change. Breaking a rule the
guidance states explicitly is `red`; drifting from an unwritten convention is
`yellow`. Read the guidance from the merge base as well as the head, and treat
a change that edits the guidance to permit itself as a concern.
