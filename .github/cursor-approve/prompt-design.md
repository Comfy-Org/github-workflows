**Design.** Judge whether the change matches its issue, its plan, and its
intended design.

Read the issue and any plan or design notes in `{{context_file}}`, then check
the diff against them: does the change take the approach that was asked for,
put the logic where the plan says it belongs, and keep the interfaces it was
supposed to keep? A different approach than the one described, an unplanned new
dependency or public interface, or a change that quietly widens its own scope is
a concern. A design that contradicts an explicit decision in the context is
`red`. When there is no issue or plan to compare against, say so and judge the
design against the patterns the surrounding code already follows.
