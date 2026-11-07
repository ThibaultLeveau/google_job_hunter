# Release verification

Issue #74 adds structural regression gates to the completed
[architecture contract](architecture.md), [browser journeys](../README.md#browser-checks),
[AI evaluations](../README.md#synthetic-ai-regression-evaluations) and [operator runbook](operations.md).
This is a repeatable release checklist, not authorization to deploy or make paid calls.

## Required evidence

Use the same locked commands as CI from a clean checkout and a disposable PostgreSQL/pgvector
instance. The [README command contracts](../README.md#development-status-and-command-contracts)
cover dependency setup. Normal tests use synthetic records and deterministic external adapters.
Keep these existing check names stable; an operator may require them in branch protection without
this change modifying repository settings:

| Check | Required result |
| --- | --- |
| Backend quality | Ruff format/lint, strict mypy, one Alembic head, fresh database upgrade, all unit/integration tests, independent line/branch coverage floors |
| Frontend quality | ESLint, strict TypeScript, fresh OpenAPI types, component coverage, deliberate quality-gate probes, production build |
| Browser quality | Real API/worker/PostgreSQL acceptance journeys, accessibility assertions/screenshots, external-request/redirect and failed-harness cleanup probes |
| Container quality | Locked production images, runtime/health/private-volume checks, fixture pipeline, encrypted database-plus-files recovery rehearsal |
| PR size | Merge-base file and authored-line limits, with dependency locks reported separately |

Run backend checks exactly as [documented](../README.md#development-status-and-command-contracts); the full pytest run includes
`tests/test_release_gates.py` and `tests/test_coverage_gate.py`. From `frontend`, run `npm run lint`,
`npm run typecheck`, `npm run test:coverage`, `npm run test:guards` and `npm run build`.
Then run `npm run test:e2e` and `npm run test:e2e:guards` with the documented database configuration.
The two Docker opt-in commands are documented in the [runtime instructions](../README.md#single-host-container-runtime)
and [recovery runbook](operations.md). Their skips in the ordinary backend job are intentional;
both must pass in Container quality before merging or releasing.

## Coverage and isolation audit

Backend coverage measures all `src/job_hunter`, quality scripts and migrations with branch
instrumentation and no line/branch exclusions. The checker compares integer counts, rejecting
89.99% lines or 84.99% branches, empty line coverage and missing branch instrumentation.
Frontend coverage includes unimported maintained `src/**/*.ts`/`tsx` modules. Only the React mount
in `src/main.tsx`, test files and test setup are excluded; generated OpenAPI declarations outside
`src` have no business logic. No additional exclusions or ignore pragmas were introduced.

`npm run test:guards` creates disposable synthetic projects using the actual Vitest configuration
and shared MSW setup. It requires a fully covered control to pass, an unimported source module to
fail the 90% line floor, a missed ternary outcome to fail the 85% branch floor independently, and
a caught unhandled loopback request to fail teardown. Temporary sources/reports are removed.
Backend subprocess probes likewise catch DNS, connect/connect_ex and UDP send/sendmsg failures;
pytest must still fail with the recorded network violation. A second probe-only hook prevents
real external traffic even if the primary guard regresses.

The backend test audit hook permits loopback fixtures, local Unix sockets and the explicitly
configured PostgreSQL endpoint. Unexpected Python socket/DNS attempts fail immediately and remain
failures even when caught by application code. Late recorded violations fail session completion.
This is a regression guard, not an OS network sandbox: native libpq and child processes do not
inherit Python audit hooks. Database access is explicit; real-stack API/worker children install
their own guard, and browser redirects pass through the existing allowlist proxy. Package/image
installation before tests still needs registry access. No test enables live provider credentials.
See the [CPython audit events](https://docs.python.org/3.14/library/audit_events.html).

| Invariant | Existing observable regression evidence |
| --- | --- |
| Session ownership, CSRF, recent proof and identity binding | `test_accounts.py`, `test_oidc.py`, `test_reauth.py`, `test_api_boundary.py`; browser sign-in/recovery/link/export journeys |
| Private documents/profile/searches/jobs/matches | `test_deletion.py`, `test_review.py`, `test_profiles.py`, `test_saved_searches.py`, `test_job_listing.py`, `test_job_details.py`, `test_matches.py`, `test_exports.py`; two-owner browser journeys |
| Atomic enqueue, leases, fencing and crash recovery | `test_tasks.py`, `test_worker.py`, `test_stage_publication.py`, `test_pipeline.py`; stale-token rollback and duplicate-worker publication |
| Deletion wins over concurrent private work | `test_account_fencing.py`, `test_account_deletion.py`, `test_deletion.py`; blocked writes recheck committed tombstones and late results cannot republish |
| Provider caps, privacy and uncertain output | `test_provider_budgets.py`, `test_ai.py`, `test_ai_evaluations.py`, `test_matching.py`; reservations, malformed output, evidence and unknowns |

These tests were delivered with their features. The final gates supplement them with scale and
negative-check evidence; coverage percentages alone do not establish authorization correctness.

## Structural budgets

The new tests count SQL statements and returned rows, never elapsed query time. Job pages contain
at most 50 items and fetch at most 51 rows in one statement, or two with an owned-run check.
Saved-search pages use one statement with one lookahead row; document pages use one statement and
at most 100 rows. Search-run history uses at most two statements and at most one task result per
page item. Its batched lookup preserves current-page worker failures, ignores obsolete failures,
and leaves terminal runs unchanged. Tests page through two owners' data and shared public jobs,
compare exact IDs/statuses, and reject cross-owner cursors.

A 1,000-task backlog with two competing workers and two slots each must claim only four tasks
while the external work is held, leave 996 pending, and publish four unique successes after release.
Each task processes an allowed 100,000-character four-byte text. Its output stays within 2,048
chunks/spans and 800,000 aggregate UTF-8 text bytes. A single embedding call with the 1,024-token
fixture cap selects only two 512-byte chunks from that large backlog. These are object/cardinality
and payload budgets, not allocator/RSS, database-plan, throughput or production latency guarantees.

## Operator release checklist and remaining limits

Before a release, record the exact reviewed commit, all five successful check URLs, actual backend
and frontend line/branch coverage, browser screenshots and the outcome of the recovery rehearsal.
Repeat the [keyboard/accessibility inspection](accessibility.md) for changed screens. Confirm
provider credentials, exact Google callback, mail delivery, HTTPS/cookies, budgets, model/dimension
versions and private storage permissions using the operator runbook. Verify encrypted backups and
deletions/password changes since the chosen backup before restoring access. A later release needs
its own evidence; the issue PR records this implementation's results.

OCR is unsupported; scanned PDFs report that state. JavaScript-only, blocked or unsupported job
pages cannot be made readable by the HTTP crawler. Live Google/mail/SerpApi/AI/embedding setup and
provider costs remain operator responsibilities; fixture success does not prove live-provider
availability or model quality. AI proposals require review, evidence can be incomplete, unknown
fields stay unknown, and match scores are uncertain rather than hiring predictions. Salary units
are preserved without inferred conversions. Accessibility evidence covers Chromium and the
recorded states, not a full assistive-technology study. No automatic release, deployment, paid
evaluation, real account migration or repository protection change is performed by these gates.
