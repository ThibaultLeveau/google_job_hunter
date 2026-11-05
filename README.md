# Google Job Hunter

Google Job Hunter is a planned private job-research application: review profile facts from documents,
run grouped Google searches, and inspect public job evidence and private profile matches.

**Current state:** the backend bootstrap provides a typed FastAPI app factory and a credential-free
liveness endpoint. Architecture and contribution contracts are established.
The React frontend provides an accessible workspace preview and local API proxy.
Backend and frontend GitHub Actions checks enforce lint, strict types and coverage.
PostgreSQL/pgvector, explicit transaction sessions and Alembic schema setup are available.
Worker setup and database integration in CI remain planned.

- Read [the architecture](docs/architecture.md) for product scope, ownership, recovery and operator settings.
- Read [the contribution rules](AGENTS.md) before implementation.
- Follow [the dependency roadmap](#implementation-roadmap), starting from merged prerequisites.

## Product and stack

The intended flow is upload → bounded parsing → evidence-backed proposals → human review → versioned profile.
Saved AND-of-OR searches run manually through SerpApi; public pages are crawled, analyzed and matched
against that user's accepted profile. Only discovered jobs associated with that user appear in their API.

Use FastAPI, synchronous SQLAlchemy 2, Alembic and PostgreSQL/pgvector with a separate Python worker
and durable PostgreSQL tasks. Store private documents on a local volume behind a narrow port.
Use Vite, React, strict TypeScript, React Router, TanStack Query and a small CSS-token visual system.

V1 includes email/password and Google OIDC sign-in, PDF/DOCX/TXT, reviewed profile extraction,
bounded public HTTP crawling, typed LangChain stages, embeddings and an accessible job UI.
Google account integration is sign-in only. OCR, mailbox reading, automatic applications, Google HTML
scraping, browser crawling, recurring searches and billing are excluded.
Scanned PDFs, JavaScript-only pages and absent fields receive explicit unsupported/unknown states.

## Development status and command contracts

Use Python 3.14 and [uv](https://docs.astral.sh/uv/getting-started/installation/).
The Python minor is constrained in `backend/pyproject.toml`; `backend/uv.lock` locks dependencies.
From `backend/`, install and start the API locally:

```bash
uv sync --locked
uv run --locked uvicorn job_hunter.app:create_app --factory --host 127.0.0.1 --port 8000
```

`GET http://127.0.0.1:8000/api/health/live` returns `200` with `{"status":"ok"}`.
Liveness does not contact external services or report configuration values.
It requires no database, provider account or credentials.

Run the implemented backend checks from `backend/`:

```bash
uv run --locked ruff format --check .
uv run --locked ruff check .
uv run --locked mypy
uv run --locked pytest --cov --cov-report=term-missing \
  --cov-report=json:reports/coverage.json --cov-report=xml:reports/coverage.xml \
  --junitxml=reports/junit.xml
uv run --locked python scripts/check_coverage.py reports/coverage.json
```

Mypy runs in strict mode for source, quality scripts and tests. Tests inject local settings and
exercise actual server startup failure with a synthetic credential canary to verify redacted output.
The coverage gate independently requires 90% lines and 85% branches without rounding up.
It measures all backend application source and quality scripts, with no source or branch
exclusions. Test code is not part of the coverage denominator. A measured branch-free codebase
counts as 100% branch coverage; missing branch instrumentation or empty line coverage fails.

The `Backend quality` check runs these commands on every pull request and push to `main`.
It uses pinned actions and uv 0.13.7, read-only repository access, a ten-minute timeout and
cancels superseded runs. The uv cache includes the runner platform, Python and lockfile identity;
only pushes to `main` save it. Failures upload available JUnit and coverage reports for seven days.
Tests still run after lint failure when installation succeeds, so failure reports remain useful.
Validate workflow syntax locally with `actionlint .github/workflows/backend.yml` from the root.

The frontend uses Node 26.13.0 and npm 11.20.0, pinned in `frontend/package.json`.
From `frontend/`, install the lockfile and start the local UI:

```bash
npm ci
npm run dev
```

Open `http://127.0.0.1:5173`. Run the backend on port 8000 using the command above;
Vite forwards same-origin `/api` requests there. Production hosting must provide the same-origin
API boundary; `npm run preview` serves only the built UI at `http://127.0.0.1:4173`.
Keep provider secrets server-side and never place them in `VITE_*` variables.

Run the same frontend commands as the `Frontend quality` GitHub check:

```bash
npm run lint
npm run typecheck
npm run test:coverage
npm run build
```

`npm test` runs the component tests without coverage. The shared jsdom setup uses Testing Library,
user-event and MSW with no default handlers. Tests register only the requests they need through
`server.use(...)`; handlers and the DOM are reset after each test. Unhandled requests are blocked
and fail teardown even when application code catches the request error. No API keys are needed.

Frontend coverage includes every `src/**/*.ts` and `src/**/*.tsx` module, including unimported files,
with independent 90% line and 85% branch floors. The only application exclusion is `src/main.tsx`,
which solely mounts React into the document and must not contain business logic. Tests and shared
test setup are outside the maintained application denominator; generated API types do not exist yet.
Vitest writes JUnit and coverage reports under ignored `frontend/reports/`, including on test failure.
Both workflows retain available failure artifacts for seven days, use read-only repository access,
verified action pins, bounded timeouts and cancel superseded runs. The npm cache contains package
downloads keyed by platform and lockfile; it does not cache `node_modules` or build output.
Validate both workflows with `actionlint .github/workflows/*.yml` from the repository root.

Settings are read for each app creation and validated before serving requests:

| Environment variable | Default | Behavior |
| --- | --- | --- |
| `JOB_HUNTER_DOCS_ENABLED` | `false` | Boolean; `true` enables `/docs` and `/openapi.json` |

Set variables in the process environment; the application does not load `.env` files automatically.
Invalid values abort startup with field names only, without submitted values or chained validation errors.
Tests may pass immutable `Settings` explicitly to `create_app` to avoid ambient configuration.
No provider credentials or business settings are introduced by this bootstrap.

### Local PostgreSQL and migrations

From the root, start the development-only database (Docker Compose required):

```bash
docker compose -p job-hunter-development up -d --wait database
```

Compose pins pgvector 0.8.7/PostgreSQL 18 by immutable image digest, publishes only
`127.0.0.1:5432` and stores data in the project's `postgres_data` named volume.
The committed password is a local fixture, never a production credential. If port 5432 is occupied,
set `JOB_HUNTER_DB_PORT` when starting Compose and use that port in the URL below.
Stop with `docker compose -p job-hunter-development down`; this preserves the named volume.

From `backend/`, configure the local URL in the process environment and apply the schema:

```bash
export JOB_HUNTER_DATABASE_URL='postgresql+psycopg://job_hunter:local-development-only@127.0.0.1:5432/job_hunter'
uv run --locked alembic upgrade head
uv run --locked alembic heads
uv run --locked alembic current
```

Alembic reads `[tool.alembic]` in `pyproject.toml`; migrations are explicitly authored in
`backend/migrations/versions/`. There is no application `create_all` or automatic startup migration.
The initial migration enables `vector` only. Run migrations using a role permitted to create the
extension; runtime permissions are an operator concern when deployment is introduced.
`uv run --locked alembic upgrade head --sql` emits reviewable offline PostgreSQL SQL without a URL.
Downgrading to `base` drops the extension without CASCADE and is only appropriate on disposable data.

| Database environment variable | Default | Behavior |
| --- | --- | --- |
| `JOB_HUNTER_DATABASE_URL` | Unset | Secret PostgreSQL URL using `postgresql+psycopg`; one host and database required. Optional `sslmode`/`sslrootcert` query parameters only. |
| `JOB_HUNTER_DATABASE_POOL_SIZE` | `5` | Integer 1–20; no overflow connections. |

Database construction is lazy; liveness stays available without a configured or reachable database.
`GET /api/health/ready` returns `200 {"status":"ok"}` when `SELECT 1` succeeds, otherwise
`503 {"status":"unavailable"}`. It reveals no connection settings or exception text. It checks
connectivity, not schema compatibility. Both health handlers are synchronous; blocking SQL runs
off the API event loop. App shutdown disposes the pool in a thread.

Pool acquisition, connection attempts, statements and locks have two-second limits. TCP user and
keepalive timeouts also bound stalled connections; DNS resolution and sequential connection phases
mean these are per-operation bounds, not a two-second HTTP deadline.
Use `with database.transaction() as session:` for one explicit unit of work. Success commits;
exceptions roll back; either outcome permanently closes the session. Adapters must not commit their
caller's transaction, share sessions or hold a transaction during external provider calls.

Run real database tests locally after starting Compose, from `backend/`:

```bash
TEST_DATABASE_URL="$JOB_HUNTER_DATABASE_URL" uv run --locked pytest -m integration
```

The test role must create/drop databases. Each integration test creates a randomly named database,
then closes connections and drops only that database. Tests cover fresh extension migrations and
downgrade/upgrade, readiness, failed-transaction rollback, committed inserts, session closure,
pool exhaustion and statement timeout. They never substitute SQLite or mock transaction semantics.
Without `TEST_DATABASE_URL`, these tests explicitly skip; ordinary tests still verify redacted
database-down readiness, settings and offline migration SQL. CI database service wiring arrives in #8.

The following are **planned command contracts, not working setup instructions**.
They were checked against the linked foundation issues; those PRs must implement and verify them.
Supported Python/Node minors and dependency versions are deliberately resolved and pinned in their bootstraps.

| Available after | Working directory | Planned commands or contract |
| --- | --- | --- |
| [#8](https://github.com/ThibaultLeveau/google_job_hunter/issues/8) | `backend/` | Supplies integration invocation, fresh migrations and single-head verification in CI |
| [#11](https://github.com/ThibaultLeveau/google_job_hunter/issues/11) | `backend/` | Supplies the separate worker command and registered handlers |
| [#20](https://github.com/ThibaultLeveau/google_job_hunter/issues/20) | Backend / frontend | Supplies deterministic OpenAPI export and ignored type generation before frontend checks/build/tests |

Normal CI uses deterministic provider fixtures and requires no Google, mail, SerpApi, LLM or embedding secret.
Real PostgreSQL is required when database behavior is tested; browser journeys later use the real API/worker.
Live-provider credentials, public URL, retention and budgets are operator settings detailed in
[the architecture](docs/architecture.md#operator-settings-and-fixture-contract).
The backend bootstrap makes no paid calls and performs no deployment.

## Contribution workflow

Start from fetched current `main` after the selected issue's explicit prerequisites merge.
Use its prescribed branch name, one issue per branch and one focused PR with `Closes #N`.
Use [the PR template](.github/pull_request_template.md) to describe concrete behavior, scope,
validation commands/results, limitations and measured size. Implement only that issue.
Write focused English commits; multiple coherent commits are welcome. Clean up fixup/WIP commits
before review without rewriting shared `main` or unrelated work. Do not change repository settings.
Follow [AGENTS.md](AGENTS.md#git-and-pr-delivery): record the complete-diff review, fix findings,
repeat affected checks and squash-merge the reviewed head only after applicable checks pass.

From the repository root on an issue branch, inspect and measure committed changes:

```bash
git fetch origin main
git diff --check origin/main
uv run --no-project --python 3.14 python backend/scripts/check_pr_size.py origin/main HEAD
```

The whitespace check includes tracked uncommitted edits. The size script measures the merge-base
diff of committed history; add and commit all intended files before the final measurement.
The stable `PR size` check runs the same script on `pull_request` with read-only access, no secrets,
full history and no shell interpolation of PR titles or filenames. It reports each failed cap and
exits nonzero above 10 changed files or 1,000 authored added + deleted lines. Split excess into a
focused issue before implementation; do not silently raise caps. Smaller issue targets still apply.

Renames count once, deletions count, and tests/config/docs/generated application source count fully.
Only exact dependency locks `backend/uv.lock` and `frontend/package-lock.json` have their lines
reported separately; both still count as files. Renaming authored source into a lock path does not
exempt it. Raw and adjusted additions/deletions/totals appear in the output and must go in the PR.
The script requests Git text counts; if attributes still produce uncountable binary records, it
fails with a measurement error instead of exempting those files. This is a line/file cap, not a byte cap.
Filenames are NUL-delimited during parsing and JSON-escaped for logs; external diff/textconv helpers
are disabled. The check installs no project dependencies and uses no write token or privileged trigger.

## Bootstrap file budgets

Each bootstrap is its own PR after its prerequisites merge. Both bootstrap inventories are implemented.
Reserve space for tests/configuration within the ten-file limit; add feature layers only when used.

| Backend issue #2 file | Purpose |
| --- | --- |
| `backend/pyproject.toml` | Supported Python minor, package metadata and quality configuration |
| `backend/uv.lock` | Locked dependencies; generated lines reported separately |
| `backend/src/job_hunter/__init__.py` | Minimal package |
| `backend/src/job_hunter/app.py` | Composed app factory and health endpoint |
| `backend/src/job_hunter/settings.py` | Validated, redacted settings |
| `backend/tests/test_health.py` | Health, deterministic startup and invalid-settings behavior |
| `.gitignore` | Shared exclusions, if absent |
| `README.md` | Verified local commands and configuration contract |
| `AGENTS.md` | Review-and-merge delivery policy |

This uses nine of ten file slots, including the lockfile and documentation.
Database, worker and feature modules belong to their later issues.

| Frontend issue #4 file | Purpose |
| --- | --- |
| `frontend/package.json` | Pinned Node engine, dependencies and dev/typecheck/lint/build scripts |
| `frontend/package-lock.json` | Reproducible `npm ci`; counts toward the file limit |
| `frontend/index.html` | Application mount |
| `frontend/tsconfig.json` | Strict TypeScript |
| `frontend/vite.config.ts` | Vite and local same-origin API proxy |
| `frontend/src/main.tsx` | Entry point |
| `frontend/src/App.tsx` | Small accessible initial page |
| `frontend/src/styles.css` | Minimal base styling |
| `frontend/eslint.config.js` | Lint configuration |
| `.gitignore` | Reuse or update shared ignores only if needed |

That is nine files, or ten including an ignore-file change; do not add demo assets or unused configs.
Vitest/CI arrives in #5, UI primitives in #19, generated API contracts in #20 and routing in #21.

## Implementation roadmap

This is the complete 74-issue plan from [issue #1](https://github.com/ThibaultLeveau/google_job_hunter/issues/1).
Every link and prerequisite declaration was checked against its issue on 5 November 2026.
The order is one valid dependency order. Only explicit prerequisites determine readiness;
independent ready issues may proceed separately, each with its own branch and PR.
Listing an issue here does not authorize implementing it as part of issue #1.

| Order | Issue | Phase | Prerequisites |
|---|---|---|---|
| 1 | [#1 Define architecture, product contracts, and contribution rules](https://github.com/ThibaultLeveau/google_job_hunter/issues/1) | Foundation | None |
| 2 | [#2 Bootstrap the typed FastAPI application and health test](https://github.com/ThibaultLeveau/google_job_hunter/issues/2) | Foundation | [#1](https://github.com/ThibaultLeveau/google_job_hunter/issues/1) |
| 3 | [#3 Run backend quality checks in GitHub Actions](https://github.com/ThibaultLeveau/google_job_hunter/issues/3) | Foundation | [#2](https://github.com/ThibaultLeveau/google_job_hunter/issues/2) |
| 4 | [#4 Bootstrap Vite, React, and strict TypeScript within ten files](https://github.com/ThibaultLeveau/google_job_hunter/issues/4) | Foundation | [#1](https://github.com/ThibaultLeveau/google_job_hunter/issues/1) |
| 5 | [#5 Add frontend component tests and GitHub Actions checks](https://github.com/ThibaultLeveau/google_job_hunter/issues/5) | Foundation | [#4](https://github.com/ThibaultLeveau/google_job_hunter/issues/4) |
| 6 | [#6 Enforce small PRs and a clear contribution workflow](https://github.com/ThibaultLeveau/google_job_hunter/issues/6) | Foundation | [#3](https://github.com/ThibaultLeveau/google_job_hunter/issues/3), [#5](https://github.com/ThibaultLeveau/google_job_hunter/issues/5) |
| 7 | [#7 Add PostgreSQL sessions, Alembic, and local database service](https://github.com/ThibaultLeveau/google_job_hunter/issues/7) | Foundation | [#2](https://github.com/ThibaultLeveau/google_job_hunter/issues/2) |
| 8 | [#8 Run PostgreSQL integration and migration tests in CI](https://github.com/ThibaultLeveau/google_job_hunter/issues/8) | Foundation | [#3](https://github.com/ThibaultLeveau/google_job_hunter/issues/3), [#7](https://github.com/ThibaultLeveau/google_job_hunter/issues/7) |
| 9 | [#9 Standardize API errors, request context, and browser security boundaries](https://github.com/ThibaultLeveau/google_job_hunter/issues/9) | Foundation | [#2](https://github.com/ThibaultLeveau/google_job_hunter/issues/2), [#3](https://github.com/ThibaultLeveau/google_job_hunter/issues/3) |
| 10 | [#10 Persist idempotent PostgreSQL tasks and atomic enqueue](https://github.com/ThibaultLeveau/google_job_hunter/issues/10) | Foundation | [#7](https://github.com/ThibaultLeveau/google_job_hunter/issues/7), [#8](https://github.com/ThibaultLeveau/google_job_hunter/issues/8) |
| 11 | [#11 Execute tasks with leases, retry budgets, and crash recovery](https://github.com/ThibaultLeveau/google_job_hunter/issues/11) | Foundation | [#10](https://github.com/ThibaultLeveau/google_job_hunter/issues/10) |
| 12 | [#12 Persist users and register password accounts safely](https://github.com/ThibaultLeveau/google_job_hunter/issues/12) | Identity | [#8](https://github.com/ThibaultLeveau/google_job_hunter/issues/8), [#9](https://github.com/ThibaultLeveau/google_job_hunter/issues/9) |
| 13 | [#13 Add revocable cookie sessions, login, logout, and CSRF protection](https://github.com/ThibaultLeveau/google_job_hunter/issues/13) | Identity | [#12](https://github.com/ThibaultLeveau/google_job_hunter/issues/12) |
| 14 | [#14 Bound authentication attempts and verification requirements](https://github.com/ThibaultLeveau/google_job_hunter/issues/14) | Identity | [#13](https://github.com/ThibaultLeveau/google_job_hunter/issues/13) |
| 15 | [#15 Add transactional mail adapter and single-use email verification](https://github.com/ThibaultLeveau/google_job_hunter/issues/15) | Identity | [#14](https://github.com/ThibaultLeveau/google_job_hunter/issues/14), [#11](https://github.com/ThibaultLeveau/google_job_hunter/issues/11) |
| 16 | [#16 Implement password recovery and revoke compromised sessions](https://github.com/ThibaultLeveau/google_job_hunter/issues/16) | Identity | [#15](https://github.com/ThibaultLeveau/google_job_hunter/issues/15), [#11](https://github.com/ThibaultLeveau/google_job_hunter/issues/11) |
| 17 | [#17 Add secure Google OIDC sign-in](https://github.com/ThibaultLeveau/google_job_hunter/issues/17) | Identity | [#13](https://github.com/ThibaultLeveau/google_job_hunter/issues/13), [#14](https://github.com/ThibaultLeveau/google_job_hunter/issues/14) |
| 18 | [#18 Add recent reauthentication and explicit Google identity linking](https://github.com/ThibaultLeveau/google_job_hunter/issues/18) | Identity | [#17](https://github.com/ThibaultLeveau/google_job_hunter/issues/17), [#13](https://github.com/ThibaultLeveau/google_job_hunter/issues/13), [#14](https://github.com/ThibaultLeveau/google_job_hunter/issues/14) |
| 19 | [#19 Define the visual system and accessible UI primitives](https://github.com/ThibaultLeveau/google_job_hunter/issues/19) | Interface foundation | [#5](https://github.com/ThibaultLeveau/google_job_hunter/issues/5) |
| 20 | [#20 Generate API types and add a tested query client](https://github.com/ThibaultLeveau/google_job_hunter/issues/20) | Interface foundation | [#5](https://github.com/ThibaultLeveau/google_job_hunter/issues/5), [#9](https://github.com/ThibaultLeveau/google_job_hunter/issues/9), [#13](https://github.com/ThibaultLeveau/google_job_hunter/issues/13) |
| 21 | [#21 Build responsive navigation, route guards, and page states](https://github.com/ThibaultLeveau/google_job_hunter/issues/21) | Interface foundation | [#19](https://github.com/ThibaultLeveau/google_job_hunter/issues/19), [#20](https://github.com/ThibaultLeveau/google_job_hunter/issues/20) |
| 22 | [#22 Build registration, login, and email-verification screens](https://github.com/ThibaultLeveau/google_job_hunter/issues/22) | Identity | [#21](https://github.com/ThibaultLeveau/google_job_hunter/issues/21), [#15](https://github.com/ThibaultLeveau/google_job_hunter/issues/15) |
| 23 | [#23 Add Google sign-in and password-recovery user flows](https://github.com/ThibaultLeveau/google_job_hunter/issues/23) | Identity | [#22](https://github.com/ThibaultLeveau/google_job_hunter/issues/22), [#16](https://github.com/ThibaultLeveau/google_job_hunter/issues/16), [#17](https://github.com/ThibaultLeveau/google_job_hunter/issues/17) |
| 24 | [#24 Persist editable profile facts with exact deduplication and revisions](https://github.com/ThibaultLeveau/google_job_hunter/issues/24) | Profiles | [#13](https://github.com/ThibaultLeveau/google_job_hunter/issues/13), [#8](https://github.com/ThibaultLeveau/google_job_hunter/issues/8) |
| 25 | [#25 Upload private documents with validation, deduplication, and processing state](https://github.com/ThibaultLeveau/google_job_hunter/issues/25) | Profiles | [#24](https://github.com/ThibaultLeveau/google_job_hunter/issues/24), [#10](https://github.com/ThibaultLeveau/google_job_hunter/issues/10), [#14](https://github.com/ThibaultLeveau/google_job_hunter/issues/14) |
| 26 | [#26 Parse documents in bounded workers and preserve source locations](https://github.com/ThibaultLeveau/google_job_hunter/issues/26) | Profiles | [#25](https://github.com/ThibaultLeveau/google_job_hunter/issues/25), [#11](https://github.com/ThibaultLeveau/google_job_hunter/issues/11) |
| 27 | [#27 Add bounded typed LangChain execution and deterministic provider fakes](https://github.com/ThibaultLeveau/google_job_hunter/issues/27) | AI foundation | [#11](https://github.com/ThibaultLeveau/google_job_hunter/issues/11), [#9](https://github.com/ThibaultLeveau/google_job_hunter/issues/9) |
| 28 | [#28 Extract document facts into evidence-backed review proposals](https://github.com/ThibaultLeveau/google_job_hunter/issues/28) | Profiles | [#26](https://github.com/ThibaultLeveau/google_job_hunter/issues/26), [#27](https://github.com/ThibaultLeveau/google_job_hunter/issues/27), [#24](https://github.com/ThibaultLeveau/google_job_hunter/issues/24) |
| 29 | [#29 Apply profile review decisions transactionally without duplicate facts](https://github.com/ThibaultLeveau/google_job_hunter/issues/29) | Profiles | [#28](https://github.com/ThibaultLeveau/google_job_hunter/issues/28) |
| 30 | [#30 Delete private documents without reviving data or losing accepted facts](https://github.com/ThibaultLeveau/google_job_hunter/issues/30) | Profiles | [#25](https://github.com/ThibaultLeveau/google_job_hunter/issues/25), [#26](https://github.com/ThibaultLeveau/google_job_hunter/issues/26), [#28](https://github.com/ThibaultLeveau/google_job_hunter/issues/28), [#11](https://github.com/ThibaultLeveau/google_job_hunter/issues/11) |
| 31 | [#31 Display the accepted profile and manage skills](https://github.com/ThibaultLeveau/google_job_hunter/issues/31) | Profiles | [#21](https://github.com/ThibaultLeveau/google_job_hunter/issues/21), [#24](https://github.com/ThibaultLeveau/google_job_hunter/issues/24) |
| 32 | [#32 Edit experience and project facts with revision-aware forms](https://github.com/ThibaultLeveau/google_job_hunter/issues/32) | Profiles | [#31](https://github.com/ThibaultLeveau/google_job_hunter/issues/31) |
| 33 | [#33 Build document upload, document list, and processing states](https://github.com/ThibaultLeveau/google_job_hunter/issues/33) | Profiles | [#31](https://github.com/ThibaultLeveau/google_job_hunter/issues/31), [#25](https://github.com/ThibaultLeveau/google_job_hunter/issues/25), [#26](https://github.com/ThibaultLeveau/google_job_hunter/issues/26), [#28](https://github.com/ThibaultLeveau/google_job_hunter/issues/28), [#30](https://github.com/ThibaultLeveau/google_job_hunter/issues/30) |
| 34 | [#34 Review extracted candidates with evidence and duplicate outcomes](https://github.com/ThibaultLeveau/google_job_hunter/issues/34) | Profiles | [#33](https://github.com/ThibaultLeveau/google_job_hunter/issues/33), [#29](https://github.com/ThibaultLeveau/google_job_hunter/issues/29), [#32](https://github.com/ThibaultLeveau/google_job_hunter/issues/32) |
| 35 | [#35 Compile bounded AND/OR search definitions into Google queries](https://github.com/ThibaultLeveau/google_job_hunter/issues/35) | Search discovery | [#9](https://github.com/ThibaultLeveau/google_job_hunter/issues/9), [#13](https://github.com/ThibaultLeveau/google_job_hunter/issues/13) |
| 36 | [#36 Persist owned searches and immutable search-run snapshots](https://github.com/ThibaultLeveau/google_job_hunter/issues/36) | Search discovery | [#35](https://github.com/ThibaultLeveau/google_job_hunter/issues/35), [#10](https://github.com/ThibaultLeveau/google_job_hunter/issues/10), [#14](https://github.com/ThibaultLeveau/google_job_hunter/issues/14) |
| 37 | [#37 Integrate Google organic results through a replaceable SerpApi adapter](https://github.com/ThibaultLeveau/google_job_hunter/issues/37) | Search discovery | [#35](https://github.com/ThibaultLeveau/google_job_hunter/issues/35), [#27](https://github.com/ThibaultLeveau/google_job_hunter/issues/27) |
| 38 | [#38 Deduplicate public job URLs and preserve private discovery associations](https://github.com/ThibaultLeveau/google_job_hunter/issues/38) | Search discovery | [#8](https://github.com/ThibaultLeveau/google_job_hunter/issues/8), [#36](https://github.com/ThibaultLeveau/google_job_hunter/issues/36) |
| 39 | [#39 Execute bounded search runs and enqueue only new job sources](https://github.com/ThibaultLeveau/google_job_hunter/issues/39) | Search discovery | [#37](https://github.com/ThibaultLeveau/google_job_hunter/issues/37), [#38](https://github.com/ThibaultLeveau/google_job_hunter/issues/38), [#11](https://github.com/ThibaultLeveau/google_job_hunter/issues/11) |
| 40 | [#40 Build saved-search editing with explicit AND/OR groups](https://github.com/ThibaultLeveau/google_job_hunter/issues/40) | Search discovery | [#21](https://github.com/ThibaultLeveau/google_job_hunter/issues/21), [#36](https://github.com/ThibaultLeveau/google_job_hunter/issues/36) |
| 41 | [#41 Add discovery filters and server-generated Google query previews](https://github.com/ThibaultLeveau/google_job_hunter/issues/41) | Search discovery | [#40](https://github.com/ThibaultLeveau/google_job_hunter/issues/40), [#35](https://github.com/ThibaultLeveau/google_job_hunter/issues/35) |
| 42 | [#42 Run searches and display discovery progress and outcomes](https://github.com/ThibaultLeveau/google_job_hunter/issues/42) | Search discovery | [#41](https://github.com/ThibaultLeveau/google_job_hunter/issues/41), [#39](https://github.com/ThibaultLeveau/google_job_hunter/issues/39) |
| 43 | [#43 Build an SSRF-resistant bounded HTTP fetch adapter](https://github.com/ThibaultLeveau/google_job_hunter/issues/43) | Crawling | [#11](https://github.com/ThibaultLeveau/google_job_hunter/issues/11) |
| 44 | [#44 Crawl public pages with robots policy, host pacing, and snapshots](https://github.com/ThibaultLeveau/google_job_hunter/issues/44) | Crawling | [#43](https://github.com/ThibaultLeveau/google_job_hunter/issues/43), [#39](https://github.com/ThibaultLeveau/google_job_hunter/issues/39) |
| 45 | [#45 Convert fetched HTML into bounded plain text with provenance](https://github.com/ThibaultLeveau/google_job_hunter/issues/45) | Crawling | [#44](https://github.com/ThibaultLeveau/google_job_hunter/issues/44) |
| 46 | [#46 Use LangChain to select and organize faithful plain-text job content](https://github.com/ThibaultLeveau/google_job_hunter/issues/46) | AI job processing | [#45](https://github.com/ThibaultLeveau/google_job_hunter/issues/45), [#27](https://github.com/ThibaultLeveau/google_job_hunter/issues/27) |
| 47 | [#47 Chunk cleaned job text deterministically with source offsets](https://github.com/ThibaultLeveau/google_job_hunter/issues/47) | AI job processing | [#46](https://github.com/ThibaultLeveau/google_job_hunter/issues/46), [#8](https://github.com/ThibaultLeveau/google_job_hunter/issues/8) |
| 48 | [#48 Embed job chunks and retrieve evidence through pgvector](https://github.com/ThibaultLeveau/google_job_hunter/issues/48) | AI job processing | [#47](https://github.com/ThibaultLeveau/google_job_hunter/issues/47), [#27](https://github.com/ThibaultLeveau/google_job_hunter/issues/27) |
| 49 | [#49 Extract evidence-backed job metadata with explicit unknown values](https://github.com/ThibaultLeveau/google_job_hunter/issues/49) | AI job processing | [#45](https://github.com/ThibaultLeveau/google_job_hunter/issues/45), [#46](https://github.com/ThibaultLeveau/google_job_hunter/issues/46), [#27](https://github.com/ThibaultLeveau/google_job_hunter/issues/27) |
| 50 | [#50 Extract a concise application-requirements list with source evidence](https://github.com/ThibaultLeveau/google_job_hunter/issues/50) | AI job processing | [#49](https://github.com/ThibaultLeveau/google_job_hunter/issues/49) |
| 51 | [#51 Connect versioned crawl, analysis, and embedding stages](https://github.com/ThibaultLeveau/google_job_hunter/issues/51) | AI job processing | [#11](https://github.com/ThibaultLeveau/google_job_hunter/issues/11), [#48](https://github.com/ThibaultLeveau/google_job_hunter/issues/48), [#50](https://github.com/ThibaultLeveau/google_job_hunter/issues/50) |
| 52 | [#52 Evaluate profile fit against explicit job requirements](https://github.com/ThibaultLeveau/google_job_hunter/issues/52) | Personalized matching | [#29](https://github.com/ThibaultLeveau/google_job_hunter/issues/29), [#48](https://github.com/ThibaultLeveau/google_job_hunter/issues/48), [#50](https://github.com/ThibaultLeveau/google_job_hunter/issues/50), [#27](https://github.com/ThibaultLeveau/google_job_hunter/issues/27) |
| 53 | [#53 Persist private matches and recompute stale evaluations safely](https://github.com/ThibaultLeveau/google_job_hunter/issues/53) | Personalized matching | [#52](https://github.com/ThibaultLeveau/google_job_hunter/issues/52), [#51](https://github.com/ThibaultLeveau/google_job_hunter/issues/51), [#39](https://github.com/ThibaultLeveau/google_job_hunter/issues/39) |
| 54 | [#54 Enforce atomic per-user and global provider budgets](https://github.com/ThibaultLeveau/google_job_hunter/issues/54) | Operations and privacy | [#27](https://github.com/ThibaultLeveau/google_job_hunter/issues/27), [#39](https://github.com/ThibaultLeveau/google_job_hunter/issues/39), [#51](https://github.com/ThibaultLeveau/google_job_hunter/issues/51), [#53](https://github.com/ThibaultLeveau/google_job_hunter/issues/53) |
| 55 | [#55 List discovered jobs with stable pagination and typed filters](https://github.com/ThibaultLeveau/google_job_hunter/issues/55) | Jobs API | [#53](https://github.com/ThibaultLeveau/google_job_hunter/issues/53) |
| 56 | [#56 Expose job evidence, safe processing status, and authorized retries](https://github.com/ThibaultLeveau/google_job_hunter/issues/56) | Jobs API | [#55](https://github.com/ThibaultLeveau/google_job_hunter/issues/55), [#51](https://github.com/ThibaultLeveau/google_job_hunter/issues/51) |
| 57 | [#57 Build the responsive job listing with factual summaries](https://github.com/ThibaultLeveau/google_job_hunter/issues/57) | Job interface | [#21](https://github.com/ThibaultLeveau/google_job_hunter/issues/21), [#55](https://github.com/ThibaultLeveau/google_job_hunter/issues/55) |
| 58 | [#58 Add URL-synchronized job filters and sorting](https://github.com/ThibaultLeveau/google_job_hunter/issues/58) | Job interface | [#57](https://github.com/ThibaultLeveau/google_job_hunter/issues/57) |
| 59 | [#59 Display job detail, application requirements, and source evidence](https://github.com/ThibaultLeveau/google_job_hunter/issues/59) | Job interface | [#57](https://github.com/ThibaultLeveau/google_job_hunter/issues/57), [#56](https://github.com/ThibaultLeveau/google_job_hunter/issues/56) |
| 60 | [#60 Explain match scores, gaps, unknowns, and stale results](https://github.com/ThibaultLeveau/google_job_hunter/issues/60) | Job interface | [#59](https://github.com/ThibaultLeveau/google_job_hunter/issues/59), [#53](https://github.com/ThibaultLeveau/google_job_hunter/issues/53), [#54](https://github.com/ThibaultLeveau/google_job_hunter/issues/54) |
| 61 | [#61 Show processing stages and safe retry actions](https://github.com/ThibaultLeveau/google_job_hunter/issues/61) | Job interface | [#59](https://github.com/ThibaultLeveau/google_job_hunter/issues/59), [#56](https://github.com/ThibaultLeveau/google_job_hunter/issues/56), [#42](https://github.com/ThibaultLeveau/google_job_hunter/issues/42) |
| 62 | [#62 Export the authenticated user's profile and research data](https://github.com/ThibaultLeveau/google_job_hunter/issues/62) | Operations and privacy | [#56](https://github.com/ThibaultLeveau/google_job_hunter/issues/56), [#29](https://github.com/ThibaultLeveau/google_job_hunter/issues/29), [#18](https://github.com/ThibaultLeveau/google_job_hunter/issues/18) |
| 63 | [#63 Delete accounts and private data with race-safe cleanup](https://github.com/ThibaultLeveau/google_job_hunter/issues/63) | Operations and privacy | [#62](https://github.com/ThibaultLeveau/google_job_hunter/issues/62), [#25](https://github.com/ThibaultLeveau/google_job_hunter/issues/25), [#53](https://github.com/ThibaultLeveau/google_job_hunter/issues/53), [#11](https://github.com/ThibaultLeveau/google_job_hunter/issues/11), [#18](https://github.com/ThibaultLeveau/google_job_hunter/issues/18), [#16](https://github.com/ThibaultLeveau/google_job_hunter/issues/16), [#30](https://github.com/ThibaultLeveau/google_job_hunter/issues/30) |
| 64 | [#64 Build account settings for identity linking, export, and deletion](https://github.com/ThibaultLeveau/google_job_hunter/issues/64) | Operations and privacy | [#23](https://github.com/ThibaultLeveau/google_job_hunter/issues/23), [#62](https://github.com/ThibaultLeveau/google_job_hunter/issues/62), [#63](https://github.com/ThibaultLeveau/google_job_hunter/issues/63), [#17](https://github.com/ThibaultLeveau/google_job_hunter/issues/17), [#18](https://github.com/ThibaultLeveau/google_job_hunter/issues/18), [#30](https://github.com/ThibaultLeveau/google_job_hunter/issues/30) |
| 65 | [#65 Expire temporary data, stale artifacts, and abandoned tasks safely](https://github.com/ThibaultLeveau/google_job_hunter/issues/65) | Operations and privacy | [#63](https://github.com/ThibaultLeveau/google_job_hunter/issues/63), [#54](https://github.com/ThibaultLeveau/google_job_hunter/issues/54) |
| 66 | [#66 Compose a deterministic real-stack test harness and external fixtures](https://github.com/ThibaultLeveau/google_job_hunter/issues/66) | End-to-end quality | [#15](https://github.com/ThibaultLeveau/google_job_hunter/issues/15), [#17](https://github.com/ThibaultLeveau/google_job_hunter/issues/17), [#27](https://github.com/ThibaultLeveau/google_job_hunter/issues/27), [#37](https://github.com/ThibaultLeveau/google_job_hunter/issues/37), [#43](https://github.com/ThibaultLeveau/google_job_hunter/issues/43), [#51](https://github.com/ThibaultLeveau/google_job_hunter/issues/51) |
| 67 | [#67 Run Playwright against the real local stack in GitHub Actions](https://github.com/ThibaultLeveau/google_job_hunter/issues/67) | End-to-end quality | [#5](https://github.com/ThibaultLeveau/google_job_hunter/issues/5), [#8](https://github.com/ThibaultLeveau/google_job_hunter/issues/8), [#21](https://github.com/ThibaultLeveau/google_job_hunter/issues/21), [#66](https://github.com/ThibaultLeveau/google_job_hunter/issues/66) |
| 68 | [#68 Verify authentication and profile review through full browser journeys](https://github.com/ThibaultLeveau/google_job_hunter/issues/68) | End-to-end quality | [#67](https://github.com/ThibaultLeveau/google_job_hunter/issues/67), [#34](https://github.com/ThibaultLeveau/google_job_hunter/issues/34), [#23](https://github.com/ThibaultLeveau/google_job_hunter/issues/23) |
| 69 | [#69 Verify search, crawling, requirements, and matching end to end](https://github.com/ThibaultLeveau/google_job_hunter/issues/69) | End-to-end quality | [#68](https://github.com/ThibaultLeveau/google_job_hunter/issues/68), [#61](https://github.com/ThibaultLeveau/google_job_hunter/issues/61), [#60](https://github.com/ThibaultLeveau/google_job_hunter/issues/60), [#54](https://github.com/ThibaultLeveau/google_job_hunter/issues/54), [#58](https://github.com/ThibaultLeveau/google_job_hunter/issues/58) |
| 70 | [#70 Add evidence-fidelity and matching regression evaluations](https://github.com/ThibaultLeveau/google_job_hunter/issues/70) | End-to-end quality | [#28](https://github.com/ThibaultLeveau/google_job_hunter/issues/28), [#46](https://github.com/ThibaultLeveau/google_job_hunter/issues/46), [#50](https://github.com/ThibaultLeveau/google_job_hunter/issues/50), [#52](https://github.com/ThibaultLeveau/google_job_hunter/issues/52) |
| 71 | [#71 Package the API, worker, and frontend for reproducible operation](https://github.com/ThibaultLeveau/google_job_hunter/issues/71) | Release readiness | [#51](https://github.com/ThibaultLeveau/google_job_hunter/issues/51), [#61](https://github.com/ThibaultLeveau/google_job_hunter/issues/61), [#54](https://github.com/ThibaultLeveau/google_job_hunter/issues/54), [#65](https://github.com/ThibaultLeveau/google_job_hunter/issues/65) |
| 72 | [#72 Document and verify backup, restore, migrations, and incident recovery](https://github.com/ThibaultLeveau/google_job_hunter/issues/72) | Release readiness | [#71](https://github.com/ThibaultLeveau/google_job_hunter/issues/71) |
| 73 | [#73 Verify accessibility and responsive quality across core journeys](https://github.com/ThibaultLeveau/google_job_hunter/issues/73) | Release readiness | [#69](https://github.com/ThibaultLeveau/google_job_hunter/issues/69), [#64](https://github.com/ThibaultLeveau/google_job_hunter/issues/64) |
| 74 | [#74 Enforce final coverage, isolation, and performance regression gates](https://github.com/ThibaultLeveau/google_job_hunter/issues/74) | Release readiness | [#69](https://github.com/ThibaultLeveau/google_job_hunter/issues/69), [#70](https://github.com/ThibaultLeveau/google_job_hunter/issues/70), [#73](https://github.com/ThibaultLeveau/google_job_hunter/issues/73), [#72](https://github.com/ThibaultLeveau/google_job_hunter/issues/72) |

Each issue carries its implementation scope, acceptance criteria and exclusions.
Use one issue = one branch = one PR with `Closes #N`; do not merge as part of implementation.
The limit is ten changed files and about 1,000 authored added + deleted lines, including tests/docs/config.
Generated lockfile lines are reported separately but their files still count.
Issue #6 will automate the size rule; it is a documented rule until then.

## Validation and release expectations

For this documentation foundation, validate local links/anchors, all roadmap issue links and prerequisites,
the acyclic dependency order, command contracts, file budgets and whitespace.
Review ownership, atomic enqueue, crash recovery, source provenance and revision invalidation explicitly.
Do not add an application scaffold, workflow or artificial documentation test to claim a green build.
Leave the PR draft when required GitHub checks are unavailable and state that limitation.

Feature PRs add meaningful tests with their behavior. The final platform acceptance journey includes
sign-in/recovery, document review, grouped search, crawl progress, evidenced job requirements and private
matching, then export/deletion. Database races, owner boundaries, provider failure and stale results
are part of acceptance. This journey and release checks are future roadmap work, not delivered by issue #1.
