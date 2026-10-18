# Lessons (append-only)

Format: `- YYYY-MM-DD <role>: <what went wrong> → <what changed>`

- 2026-10-17 orchestrator: original blueprint used GH Actions cron + committed DB and μ−1.5σ on 14 points → replaced with host cron, external bands only (ARCHITECTURE.md §0).
- 2026-10-17 orchestrator: single-route watchlists could not express "any Bay Area airport, Thu/Fri, 3–4 nights" → grid → SearchTarget expansion, capped at 60, round-robin under budget. Budget math is now the binding constraint; free SerpAPI tier covers ~1 target.
- 2026-10-17 tester: `disallowedTools: Bash(git push*)` removed Bash entirely in this harness → tester could not run pytest/ruff. Removed all argument-scoped patterns from every role; the restriction is prose now, and the orchestrator runs the checks the role could not.
- 2026-10-17 reviewer: seeder tests pinned a hand-picked END, so the shipped `fares seed` produced zero BE traps and zero fence steps while 128 tests passed → test the command's output on the real config, not only the pure generator.
- 2026-10-17 orchestrator: Phase 2 took tester→builder→janitor→reviewer→builder(fixes)→orchestrator verify. Reviewer found 3 blockers the 128-test suite missed; worth its cost. Token mix ≈ sonnet 62%, opus 24%, haiku 11%, orchestrator remainder — opus share is above the 10% target because the review round-trip was needed; acceptable for the schema/DB phase, should drop for 3.
