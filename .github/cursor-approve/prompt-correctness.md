**Correctness.** Judge whether the code is correct under a pragmatic risk
model.

Weigh what can go wrong by how likely it is and how much it costs. A bug on a
normal path, data loss, a security hole, a broken migration, or an error that is
silently swallowed is `red`. An edge case that is plausible but unproven, or a
risky path with no test covering it, is `yellow`. Do not hold the change to a
bar of perfection: a theoretical failure that needs an implausible input, on a
path where a failure is cheap and easy to undo, does not block. Check that the
tests in the diff actually exercise the behavior they claim to cover.
