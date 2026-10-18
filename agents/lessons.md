# Lessons (append-only)

Format: `- YYYY-MM-DD <role>: <what went wrong> → <what changed>`

- 2026-10-17 orchestrator: original blueprint used GH Actions cron + committed DB and μ−1.5σ on 14 points → replaced with host cron, external bands only (ARCHITECTURE.md §0).
- 2026-10-17 orchestrator: single-route watchlists could not express "any Bay Area airport, Thu/Fri, 3–4 nights" → grid → SearchTarget expansion, capped at 60, round-robin under budget. Budget math is now the binding constraint; free SerpAPI tier covers ~1 target.
- 2026-10-17 tester: `disallowedTools: Bash(git push*)` removed Bash entirely in this harness → tester could not run pytest/ruff. Removed all argument-scoped patterns from every role; the restriction is prose now, and the orchestrator runs the checks the role could not.
