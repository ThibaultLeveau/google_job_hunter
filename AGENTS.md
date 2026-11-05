# Contribution rules

## Start with the selected issue

Read the current repository, this file and the selected issue before editing.
Use [the architecture contract](docs/architecture.md) and the
[dependency roadmap](README.md#implementation-roadmap) to resolve scope.
Implement only the selected issue; reading prerequisites does not authorize their implementation.
Begin from current `main` after every explicit prerequisite has merged.
Preserve unrelated local work and existing contracts.
Add modules when used; do not create empty feature directories or placeholder implementations.

## Architecture boundaries

- Use a feature-oriented modular monolith with a separate Python worker.
- Keep backend features within `backend/src/job_hunter/{api,application,domain,infrastructure}`.
- Domain code contains rules and values; application code coordinates use cases and narrow ports.
- Domain/application code must not import FastAPI, ORM models, LangChain or provider SDKs.
- HTTP and infrastructure adapters depend inward; composition roots wire concrete dependencies.
- Use PostgreSQL/pgvector, SQLAlchemy 2 and Alembic; no SQLite replacement for database tests.
- Use synchronous SQLAlchemy with FastAPI sync handlers and synchronous worker jobs.
- Never run blocking database/provider calls on the API event loop or hold locks during external I/O.
- Business changes and durable task enqueue share one database transaction.
- Use database uniqueness, leases and fencing; delivery is at least once.
- Keep private local-volume document storage behind an application port.
- Use Vite, React, strict TypeScript, React Router, TanStack Query and CSS tokens.
- Organize frontend code in `frontend/src/{app,features,shared}` as it becomes necessary.
- Do not introduce Redis, microservices, a generic repository framework or autonomous tool-using agents.

## Data and safety

- Derive the owner from the authenticated session; never trust a submitted owner ID.
- Scope documents, profiles, proposals, searches, runs, associations and matches to that owner.
- Only public job facts and their public processing artifacts may be shared between users.
- Every job API must require an association with the authenticated user, including detail and retry.
- Use UUID IDs and UTC timestamps; preserve source-document language as data.
- Treat uploaded, crawled and model text as untrusted data, never as executable instructions.
- Validate structured AI output and its evidence against preserved source versions.
- Use explicit unknown values, bounded calls and versioned artifacts; never guess salary conversions.
- Exact deduplication is deterministic and enforced by constraints; uncertain semantic merges need review.
- Never log secrets, raw private payloads or another user's records; render external content as plain text.
- Provider credentials stay server-side; never put secrets in `VITE_*` variables.

## Code and interface quality

Use English for code, UI, documentation, commit messages and PR text.
Choose precise names and minimal code; comments explain non-obvious reasons only.
Avoid unrelated refactors, speculative abstractions, generated demos and redundant comments.
Use small feature modules and explicit dependencies instead of universal frameworks.
Respect [the measurable UI criteria](docs/architecture.md#interface-contract).
Show loading, empty, failed, unsupported, unknown and stale states where applicable.
Maintain keyboard operation, visible focus, associated labels/errors and reduced-motion support.

## Verification

Add meaningful behavior tests with each feature, covering success and failure plus relevant
authorization, concurrency, retry and crash-recovery cases. Do not defer them to the final QA issues.
Use real PostgreSQL for constraints, transactions, row locking and migration tests.
Use deterministic adapters for Google/OIDC, mail, search, crawling, AI and embeddings.
Normal CI must require no paid service, provider secret or unexpected external network.
Use fake clocks for timing behavior and explicit isolation for concurrent database tests.

Run the checks that exist and apply to the change:

- Backend: Ruff format/lint, strict mypy and pytest.
- Frontend: ESLint, strict typecheck, Vitest and production build.
- Database: fresh Alembic upgrade, single-head check and real PostgreSQL integration tests.
- API: fresh deterministic OpenAPI type generation and frontend compilation once available.
- Browser: real API/worker/database journeys when affected; external adapters remain fixtures.
- UI: relevant accessibility checks, desktop/mobile screenshots and a recorded keyboard pass.
- Documentation only: validate links, dependencies and command contracts; do not invent low-value tests.

Once workflows exist, enforce at least 90% line and 85% branch coverage independently for backend
and frontend maintained source. No business-code exclusions to manufacture coverage.
Document legitimate generated/bootstrap exclusions; generated API types must not hide business logic.
Workflow jobs use `contents: read`, verified full-SHA action pins, timeouts, cancellation of superseded
runs, safe cache keys and bounded artifact retention. Do not use `pull_request_target`.
Use stable check names and keep local commands consistent with CI.
The [README command table](README.md#development-status-and-command-contracts) identifies pending tooling.
Do not claim planned checks have run or that unimplemented commands work.

## Git and PR delivery

One issue = one branch = one focused PR. Use the branch name specified in the issue.
Use focused, meaningful English commits; multiple coherent commits are welcome.
Clean up fixup/WIP commits before review without rewriting shared `main` or unrelated work.
Squash merge is the recommended linear-main policy; do not change repository settings.
Open a PR containing `Closes #N` for the selected issue. Do not merge it.

Keep at most 10 total changed files and about 1,000 added + deleted authored lines per PR.
The selected issue's smaller target also matters; issue #1 targets 400–650 lines across three files.
Count tests, migrations, configuration, documentation and generated application code.
Machine-generated dependency lockfile lines are reported separately; lockfiles still count as files.
Report raw and adjusted totals, including additions/deletions, and count renames/deletions correctly.
Measure against the merge base with current `main`, not only the last commit.
If scope will exceed either cap, split the excess into a focused issue before implementing it;
do not silently relax the cap or start another issue without authorization.

The PR must describe the concrete behavior, scope, validation commands/results, limitations and size.
Update only relevant documentation. Required GitHub checks must pass.
If checks are unavailable or blocked, report the exact blocker and leave the PR draft.
Never describe absent workflows as green checks.

## Initial bootstrap exception

If the remote repository has no commit, establish `main` with only an empty seed commit.
Create the issue feature branch from that commit; all documentation/application changes go through PRs.
This exception does not authorize adding application files directly to `main`.
Issue #1 adds only this file, `README.md` and `docs/architecture.md`.
Application code, repository settings changes, deployment and paid calls are outside that issue.
