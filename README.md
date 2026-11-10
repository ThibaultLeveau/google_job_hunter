# Google Job Hunter

Google Job Hunter is a private job-research application: review profile facts from documents,
run grouped Google searches, and inspect public job evidence and private profile matches.

**Start here:** [Quickstart: configuration, installation and local or container runs](docs/quickstart.md).

The API, worker, React interface and PostgreSQL/pgvector pipeline are implemented, with
authentication, profile review, searches, job evidence, private matches and account data controls.
CI covers backend, frontend, browser journeys, containers and recovery. Live mail and optional
Google/search/AI providers require configuration; see the quickstart for both startup paths.

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
uv run --locked uvicorn job_hunter.app:create_app --factory --host 127.0.0.1 --port 8000 --no-proxy-headers
```

`GET http://127.0.0.1:8000/api/health/live` returns `200` with `{"status":"ok"}`.
Liveness does not contact external services or report configuration values.
It requires no database, provider account or credentials.

Run the implemented backend checks from `backend/`:
Start [local PostgreSQL](#local-postgresql-and-migrations) first and set
`TEST_DATABASE_URL` to its URL for the same complete suite used by CI.

```bash
uv run --locked ruff format --check .
uv run --locked ruff check .
uv run --locked mypy
uv run --locked python scripts/check_migration_heads.py
uv run --locked pytest --require-database --cov --cov-report=term-missing \
  --cov-report=json:reports/coverage.json --cov-report=xml:reports/coverage.xml \
  --junitxml=reports/junit.xml
uv run --locked python scripts/check_coverage.py reports/coverage.json
```

Mypy runs in strict mode for source, quality scripts and tests. Tests inject local settings and
exercise actual server startup failure with a synthetic credential canary to verify redacted output.
The coverage gate independently requires 90% lines and 85% branches without rounding up.
It measures all backend application source, quality scripts and migrations, with no source or branch
exclusions. Test code is not part of the coverage denominator. A measured branch-free codebase
counts as 100% branch coverage; missing branch instrumentation or empty line coverage fails.

The `Backend quality` check runs these commands on every pull request and push to `main`.
It uses pinned actions and uv 0.13.7, read-only repository access, a ten-minute timeout and
cancels superseded runs. The uv cache includes the runner platform, Python and lockfile identity;
only pushes to `main` save it. Failures upload available JUnit and coverage reports for seven days.
Tests still run after lint failure when installation succeeds, so failure reports remain useful.
The job waits for the pinned pgvector PostgreSQL service health check, rejects zero or multiple
migration heads and upgrades its empty database before testing. Service credentials are disposable
CI fixtures; fork PRs need no secrets or write access. `--require-database` rejects a missing
`TEST_DATABASE_URL` instead of silently skipping integration tests.
Validate workflow syntax locally with `actionlint .github/workflows/backend.yml` from the root.

The frontend uses Node 26.13.0 and npm 11.20.0, pinned in `frontend/package.json`.
From `frontend/`, install the lockfile and start the local UI:

```bash
npm ci
uv sync --directory ../backend --locked
npm run dev
```

Open `http://127.0.0.1:5173`. Run the backend on port 8000 using the command above;
Vite forwards same-origin `/api` requests there. Production hosting must provide the same-origin
API boundary and serve `index.html` for frontend route requests, including direct page reloads;
`npm run preview` serves only the built UI at `http://127.0.0.1:4173`.
Keep provider secrets server-side and never place them in `VITE_*` variables.

Run the same frontend commands as the `Frontend quality` GitHub check:

```bash
npm run lint
npm run typecheck
npm run test:coverage
npm run build
```

`npm test` runs the component tests without coverage. The shared jsdom setup uses Testing Library,
user-event and MSW with baseline handlers for background account/document/job loads. Tests register
their scenario responses through `server.use(...)`; handlers and the DOM are reset after each test. Unhandled requests are blocked
and fail teardown even when application code catches the request error. No API keys are needed.

Frontend coverage includes every `src/**/*.ts` and `src/**/*.tsx` module, including unimported files,
with independent 90% line and 85% branch floors. The only application exclusion is `src/main.tsx`,
which solely mounts React into the document and must not contain business logic. Tests and shared
test setup are outside the maintained application denominator. Generated declarations under
`frontend/generated/` contain no business logic and are outside maintained source/lint coverage.
Vitest writes JUnit and coverage reports under ignored `frontend/reports/`, including on test failure.
Both workflows retain available failure artifacts for seven days, use read-only repository access,
verified action pins, bounded timeouts and cancel superseded runs. The npm cache contains package
downloads keyed by platform and lockfile; it does not cache `node_modules` or build output.
Validate both workflows with `actionlint .github/workflows/*.yml` from the repository root.

`npm run generate:api` exports the actual FastAPI routes with application settings removed from the
child environment, no database configured and offline uv execution. It writes deterministic ignored
`generated/openapi.json` and `generated/api.d.ts`; external schema references are rejected.
Dev, lint, typecheck, tests and build regenerate before using the contract. Frontend CI installs
locked backend dependencies first; it needs no database, provider credentials or live API server.
The generator is pinned to openapi-typescript 6.7.6, which supports the project's TypeScript 6 without
a conflicting compiler peer dependency. Its Undici dependency is pinned to patched 6.29.1; generation
uses an in-memory schema and forbids network fetching. Do not edit or commit generated declarations.

`frontend/src/shared/api.ts` exports the application's `api` instance, installed through React Query's
provider, and `createApiClient` for isolated tests. Use `useQuery(api.currentSession)` with the stable
`sessionKey`; a 401 becomes an anonymous session, while service failures remain errors. Use generated
`paths`/`components` types for request payloads and response types passed to `api.request<T>`.
JSON requests stay within same-origin `/api/`, include credentials, refuse redirects and bypass HTTP
caching. Writes use the current in-memory CSRF token. `ApiError` distinguishes authentication,
validation, retryable HTTP, other HTTP, network and invalid-response failures; validated error details
retain field errors and request IDs. AbortSignal cancellation stays an abort instead of a network error.
Read queries retry at most twice with backoff; Retry-After is respected up to 30 seconds, with longer
waits requiring an explicit retry. Mutation retries are disabled. Call `api.acceptSession` after a
successful identity change and `api.invalidateSession` after a change such as email verification.
Logout and `clearSession` cancel pending requests, clear private caches and publish a null session
to mounted consumers. Identity replacement updates those consumers immediately. Session refresh
also discards private data when the identity or CSRF token changes. Logout failures still clear local
data and propagate the error; they do not prove the server session was revoked. No cache is persisted.

The router exposes public `/login`, `/register`, `/verify-email`, `/forgot-password` and
`/reset-password` routes. `/profile`, `/searches`, `/jobs` and `/settings` wait for a successful
session check; anonymous visitors go to login with a validated `returnTo` path. Return paths must
stay within these workspace route families; external, malformed and auth-loop targets use `/profile`.
Session failures offer a retry, expiry explains the redirect, and sign-out reports success only when
the server confirms it. Missing routes and unexpected render errors offer recovery actions.
Email/password registration, login, email verification, Google sign-in and password recovery are
available. Actual workspace feature content remains in its owning issues.

Navigation stays visible at mobile widths. Route changes update the title and focus the main
container; the initial Tab reaches the skip link. The account disclosure uses ordinary Tab order,
Enter/Space activation and Escape to close and restore its trigger. Issue #21's recorded Chromium
keyboard pass covered skip-to-main, account/settings navigation, Escape, workspace links and sign-out.
The real API and isolated PostgreSQL journey verified safe redirects, cookie sessions, expiry and
sign-out cache clearing. All six 320/768/1440px × 100%/200% text layouts passed with long labels,
reduced motion, no horizontal overflow and no automated WCAG A/AA violations. Representative
[desktop](docs/validation/issue-21-desktop.jpg) and [mobile](docs/validation/issue-21-mobile.jpg)
captures were visually checked. Those captures remain local evidence; see [browser CI](#browser-checks).

Email forms use labelled fields, browser autocomplete, a keyboard-operated password visibility toggle
and native input validation. Server field errors remain beside their fields and receive focus; other
errors focus the form alert. Passwords clear after each request, while email survives retries and the
registration/login transition. Pending requests prevent repeated submissions; HTTP 429 disables the
affected action for Retry-After (60 seconds when absent or unusable), without automatically retrying.
Registration focuses its sign-in link on success, and verification results receive focus.
Credentials and verification tokens are never written to localStorage or sessionStorage.

Unverified sessions see verification guidance instead of profile/search/job tools; settings and logout
remain accessible. Verification links are kept only in page memory, and their fragment/query is removed
from the address immediately. Sign in within that page to preserve a pending link. Verification submits
once per activation, consumes the in-memory token and refreshes account state: generic 202 acceptance
never proves verification. An invalid, expired, replayed or foreign link cannot verify an account;
request a new email or reopen a link after a failed attempt. An already verified account shows its
current state without submitting a replayed link. Reloading before use requires reopening the email.

Issue #22's local Chromium journey used the real API, worker and isolated PostgreSQL with captured mail:
registration, login, resend/replaced links, inline login, verification and replay passed. The keyboard
pass covered fields, password visibility, submits, verification and resend. Both forms passed all six
320/768/1440px × 100%/200% text layouts with long labels, reduced motion, no horizontal overflow and
zero automated WCAG A/AA violations; the verified state also passed its scan. Visually checked
[desktop registration](docs/validation/issue-22-desktop.jpg) and
[mobile verification login](docs/validation/issue-22-mobile.jpg) captures record representative states.

Google sign-in appears only after `/api/auth/providers` confirms it is configured. Availability failures
have an explicit retry. The action navigates to the backend start endpoint and stores only a validated
workspace return path in sessionStorage, consumed on callback; unavailable storage falls back safely.
Callback outcomes are allowlisted, sensitive URL content is removed, and successful sign-in requires
an actual API session. Cancellation remains retryable; collisions explain signing into the existing
account before explicitly connecting Google in settings. Matching email addresses never link accounts.
The G artwork, light palette and Arial fallback follow Google's supplied
[HTML button example](https://developers.google.com/identity/branding-guidelines) (Apache 2.0 code sample);
no Google SDK or remote font loads in the page.

Forgot-password confirms the same generic result for every email. Reset links stay in page memory
while all credential fragments/query parameters are removed immediately. Links last one hour and
are single-use; malformed links offer replacement. Generic 202 acceptance never claims the password
changed: expired or replayed links receive the same response. Accepted resets refresh the session,
clear the pending token and focus a return-to-login link. Passwords clear after every attempt; failed
requests allow correction/retry without persisting the token. Reloading requires reopening the link.

Issue #23's Chromium run used the real API, worker and fresh isolated PostgreSQL with signed OIDC
and captured-mail fixtures: Google return/cancel/collision, recovery delivery, reset, revoked sessions,
old-password rejection, new-password login and harmless replay passed. The keyboard pass covered
skip links, Google activation, recovery fields, visibility, submit and focused return-to-login.
Login/forgot/reset passed all 18 layouts (320/768/1440px × 100%/200% text), including long labels and
reduced motion, without horizontal overflow or automated WCAG A/AA violations. Both callback error
states also passed scans. No external requests, page errors or persisted browser credentials remained.
Visually checked [desktop sign-in](docs/validation/issue-23-desktop.jpg) and
[mobile reset](docs/validation/issue-23-mobile.jpg) captures record representative states.

Shared interface primitives live in `frontend/src/shared/ui.tsx`; color, spacing, type, radius and
focus tokens live in `frontend/src/styles.css`. Use `Button` with `primary`, `secondary` or `danger`
tone and a visible action label. Buttons default to `type="button"`; opt into `type="submit"` for
forms. Set controlled `loading` immediately when submission starts and clear it when it settles.
Loading disables activation and displays `loadingLabel` (default `Working…`); the caller owns the
async lifecycle. Plain disabled buttons remain readable and leave the keyboard tab sequence.
Use `Field` for a labeled text input with optional description, required marker and associated error.
It preserves native input props/ref and any external `aria-describedby`; standalone `Input` needs
an associated label or accessible name from its caller. Labels, errors and status text remain plain
text. `Badge` takes visible text plus `neutral`, `accent` or `danger` tone. `StatePanel` takes a title,
description, optional action and `loading`, `empty`, `error`, `unsupported`, `unknown` or `stale` state.
Choose a useful next action for recoverable states. The shared styles wrap long text, use system
fonts and turn off spinner motion/transitions when reduced motion is requested.
Component tests cover keyboard/disabled/loading behavior, field associations and token contrast.
The issue #19 validation preview was checked at 320/768/1440px and 100%/200% text scaling, including
long text, with a keyboard pass and no automated WCAG A/AA violations in Chromium. Representative
[desktop](docs/validation/issue-19-desktop.jpg) and [mobile](docs/validation/issue-19-mobile.jpg)
captures show these primitives; the temporary preview is not an application page.

Settings are read for each app creation and validated before serving requests:

| Environment variable | Default | Behavior |
| --- | --- | --- |
| `JOB_HUNTER_DOCS_ENABLED` | `false` | Boolean; `true` enables `/docs` and `/openapi.json` |
| `JOB_HUNTER_TRUSTED_HOSTS` | `["localhost", "127.0.0.1"]` | JSON list of exact lowercase hostnames, without wildcards or ports. |
| `JOB_HUNTER_ALLOWED_ORIGINS` | `["http://localhost:5173", "http://127.0.0.1:5173"]` | JSON list of exact HTTP(S) origins; no paths, credentials or wildcards. |
| `JOB_HUNTER_REQUEST_MAX_BYTES` | `1048576` | Raw body limit, 1–16777216 bytes; actual received bytes are counted. |

Set variables in the process environment; the application does not load `.env` files automatically.
Invalid values abort startup with field names only, without submitted values or chained validation errors.
Tests may pass immutable `Settings` explicitly to `create_app` to avoid ambient configuration.
No provider credentials or business settings are introduced by this bootstrap.

API errors use the typed OpenAPI envelope `{code, message, field_errors, request_id}`.
Messages are fixed English text; field errors expose only declared names (at most 20), never values,
dynamic keys or exception details. Valid UUID `X-Request-ID` headers propagate; invalid/missing IDs
are replaced. Unexpected failures log only the request ID and return a generic 500 before headers start.
After a streaming response starts, an error cannot replace that response; its exception stays redacted.

Production must replace local origins/hosts with its exact same-origin HTTPS origin and hostname.
Unknown Origin/Host values are rejected, and credentialed CORS permits only configured origins with
explicit methods/headers. Requests without Origin still require a trusted Host. API responses use
no-store, nosniff, frame/referrer restrictions and a restrictive CSP; HTTPS responses add HSTS.
Bodies are buffered within the byte limit before handlers run, with a ten-second receive deadline;
declared or streamed excess returns 413. Upload parsing belongs to later issues.

Password login uses `POST /api/auth/login` with JSON email/password and an exact allowed `Origin`.
The response contains the current user and a session-bound `csrf_token`; the opaque session token
is sent only in a host-only `job_hunter_session` cookie, scoped to `/api`, with `HttpOnly`, `Secure`,
`SameSite=Lax` and a 12-hour absolute lifetime. Use HTTPS outside loopback development.
`GET /api/auth/me` returns the current user and CSRF token. Authenticated unsafe requests require
both an allowed `Origin` and `X-CSRF-Token`, including another login and `POST /api/auth/logout`.
Login rotates the token and revokes the previous session; logout revokes it and clears the cookie.
Do not put session tokens in browser storage. Missing, invalid, expired or revoked sessions return
`401 unauthenticated`; invalid credentials use that same safe result.
Last-seen updates never refresh authentication recency or expiry.

Protected routes depend on `job_hunter.api.sessions.authenticated_user`. Scope private queries by
its `user_id`, never a submitted owner; return 404 for absent or foreign records. When a loaded
record needs an ownership check, `SessionUser.require_owner` applies the same 404 convention.
Uploads and paid search/AI routes must use `verified_user`; unverified accounts receive 403 there.
Account state, verification/resend and logout use ordinary authentication, so verification remains reachable.

Authentication requests have an 8 KiB body cap (or the smaller configured limit), bounded email/password
fields and generic errors. `limit_auth` consumes shared PostgreSQL counters before password work;
all instances use the same persistent, private HMAC key. Counters contain no raw email or IP values.
Keep `auth_rate_key` private and preserve it with database backups. At most 100 expired buckets are
reclaimed per attempt in a separate transaction; a live bucket's count never exceeds its limit.

| Action (independent counters) | Window | Per IP | Per normalized email |
| --- | --- | --- | --- |
| Registration, recovery, verification resend | 1 hour | 20 | 3 |
| Login | 5 minutes | 30 | 5 |
| OAuth start | 5 minutes | 30 | — |
| Verification | 5 minutes | 30 | 10 |

IPv4-mapped addresses share their IPv4 counter; IPv6 addresses share a /64. Missing/invalid peers
share an unknown-peer counter. The API uses only the ASGI client address, never request forwarding
headers. Direct Uvicorn uses `--no-proxy-headers`; behind a proxy, enable proxy headers only with an
explicit `--forwarded-allow-ips` list, and require that proxy to replace incoming forwarding headers.
Never trust `*`. Limited requests return 429 with `Retry-After` seconds, also readable through CORS.
OAuth adapters must call `limit_auth` with their matching action when added.

### Email verification and mail delivery

With a configured database, API and worker startup also require these server-side variables:

| Variable | Contract |
| --- | --- |
| `JOB_HUNTER_MAIL_PUBLIC_URL` | Canonical HTTPS origin without a path; HTTP allowed only for localhost/127.0.0.1 |
| `JOB_HUNTER_MAIL_SENDER` | Sender email address |
| `JOB_HUNTER_MAIL_KEY` | Secret Fernet key, identical across API/worker instances |
| `JOB_HUNTER_MAIL_SMTP_HOST` | SMTP host supporting implicit TLS |
| `JOB_HUNTER_MAIL_SMTP_PORT` | Optional port, default 465 |
| `JOB_HUNTER_MAIL_SMTP_USERNAME`, `JOB_HUNTER_MAIL_SMTP_PASSWORD` | Optional credentials, supplied together |

Generate a key once with `uv run --locked python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
from `backend/`, then store it securely outside the repository. Preserve it for pending deliveries and
backups; rotate only after deliveries have drained or expired. Missing/invalid mail settings fail startup.

Registration atomically saves the account, a SHA-256 token hash, an encrypted delivery record and
referenced mail/expiry tasks. Duplicate registration sends no additional mail. Each link expires after
one hour and uses the configured origin and `/verify-email#token=…`; fragments avoid access-log exposure.
The verification screen signs in before submitting the token in the JSON body of
`POST /api/auth/verify`; `POST /api/auth/resend-verification` needs no body. Both require the session's
Origin/CSRF protection and shared attempt limits. They return generic `202 {"status":"accepted"}`;
read `/api/auth/me` for verified state. Invalid, expired, foreign and replayed tokens cannot verify an
account. Resend replaces earlier links; verified accounts receive no further mail.

The worker sends plain-text mail over certificate-verified SMTP TLS with five-second operation timeouts
and at most three attempts. A stable Message-ID helps identify duplicates; delivery remains at least once.
Successful publication deletes the encrypted secret. A separate durable expiry task deletes undelivered
secrets even after mail failure; expiry cleanup requires a running worker. Failed tasks retain safe error
codes for operational recovery. Mail content, credentials and tokens are never logged or returned by APIs.
Tests use in-memory mail, fake clocks and real PostgreSQL; no provider credentials or external calls are required.

### Password recovery

`POST /api/auth/forgot-password` accepts `{"email":"…"}` and returns generic 202 acceptance for
known, unknown and OAuth-only accounts. Only existing password identities receive mail. Requests share
recovery limits (20 per IP/hour, 3 per email/hour); reset attempts use the same IP budget before hashing.
The configured origin supplies `/reset-password#token=…`; no submitted redirect or owner is accepted.
`POST /api/auth/reset-password` accepts `{"token":"…","password":"…"}` and returns generic 202 acceptance.
Both endpoints require an allowed Origin; valid session cookies also require CSRF. Revoked/expired
cookies do not block recovery. Invalid password fields return redacted 422 errors; passwords retain
the registration policy. Recovery links expire after one hour, are single-use, and replace earlier links.

Reset atomically consumes the token, replaces the credential and revokes all of that user's sessions.
A changed credential invalidates previously issued recovery links; concurrent login rechecks the locked
credential before issuing a session. Reset never creates a password identity or signs the user in.
Mail and expiry tasks reuse encrypted delivery, bounded SMTP retries and cleanup; no raw token enters
task payloads, API responses or logs. The recovery screens preserve this generic acceptance contract.

### Google sign-in

Set server-only `JOB_HUNTER_GOOGLE_CLIENT_ID`, `JOB_HUNTER_GOOGLE_CLIENT_SECRET` and
`JOB_HUNTER_GOOGLE_REDIRECT_URI` together. Register the exact callback URL ending in
`/api/auth/google/callback` in Google; HTTPS is required outside localhost/127.0.0.1.
Without these settings, `GET /api/auth/providers` returns `{"google": false}`; partial
configuration fails startup. Database and the existing mail settings are also required.
Navigate to `GET /api/auth/google/start`. Authlib requests only `openid email profile`
with nonce and S256 PKCE. Browser/session-bound state is encrypted with `JOB_HUNTER_MAIL_KEY`,
expires after five minutes and is consumed before exchange. Failed exchanges require a new start.
Google responses are limited to 64 KiB with five-second I/O timeouts and a 15-second read budget;
discovery must match the fixed Google issuer/endpoints. No provider tokens are persisted.
The callback redirects to `JOB_HUNTER_MAIL_PUBLIC_URL/login#google=` followed by `signed_in`,
`cancelled`, `failed` or `linking_required`, clearing the flow cookie and callback query.
Uvicorn receives a scrubbed query; any reverse proxy must also omit callback query strings from logs.
New accounts use the verified email claim; existing issuer/subject identities retain their stored
email and verification state. Email conflicts never link accounts; the sign-in screen explains the
separate authenticated linking action. Account settings provides explicit Google linking with recent-authentication handling.
The migration downgrade logs Google sessions out. Tests expose `OidcTransport` in `test_oidc.py`
for local signed-token/JWKS/code fixtures; normal CI does not contact Google.

### Recent authentication and explicit linking

`JOB_HUNTER_RECENT_AUTH_SECONDS` defaults to 300 (allowed 60–600). Activity updates only
`last_seen_at`; password proof or validated Google `auth_time` establishes authentication recency.
Google sessions without that claim have an internal epoch timestamp, exposed as `null` by
`GET /api/auth/methods`, with `recent: false`. The migration conservatively clears legacy Google
recency; rollback cannot restore discarded proof. Session expiry remains unchanged.
`GET /api/auth/actions/{action}` returns `ready` or HTTP 403 with typed
`reauthentication_required` and `action`. Actions are `link_google`, `export_data`, `delete_account`
and `settings`; these endpoints report access status and do not perform those operations.
Future handlers use `api.reauth.require_recent`; sensitive writes must recheck the active session
and recency in their transaction, as linking does. `/methods` reports linked methods and current proof.

With the current session, allowed Origin and CSRF token, POST `{"action":"settings","password":"…"}` to
`/api/auth/reauthenticate/password`, or `{"action":"settings"}` to `/api/auth/google/reauthenticate`.
POST `{"action":"link_google"}` to `/api/auth/google/link` requires recent existing-account proof.
Google starts return `authorization_url` and a flow cookie; navigate to that URL. Register the
additional fixed `/api/auth/google/reauthenticate/callback` and `/api/auth/google/link/callback`
URLs at the configured callback origin. They redirect to the configured login URL with
`#google_{intent}={result}&action={action}`; results include `reauthenticated`, `linked`, `cancelled`,
`conflict`, `reauthentication_required` and `failed`. Only successful callbacks return their bound action.
Fresh Google proof requests `max_age=0` and essential `auth_time`, which must be enabled in
[Google client settings](https://developers.google.com/identity/openid-connect/openid-connect).

Missing, future or pre-flow authentication time fails closed; the already-linked subject must match.
Intent, user, session and action are saved in encrypted single-use flow state. Linking never changes
email/verification or merges accounts; collisions are conflicts and same-owner links are idempotent.
Password proof shares login limits (5/account and 30/IP per five minutes); Google starts share the
30/IP per-five-minute OAuth budget. Callback queries stay scrubbed from application access logs.

### Account settings

`/settings` shows the signed-in account's verification and linked methods. Linking Google is an
explicit action; the UI first obtains a protected authorization URL and offers Continue to Google.
Provider availability and failures are shown without exposing server details. Callback fragments
are removed, methods are checked with the API, and no sensitive action runs automatically on return.
When recent authentication is required, confirm with an existing password or linked Google identity,
then explicitly retry the original action. Passwords are cleared on submission and stay out of caches.

Download personal data creates `job-hunter-personal-data.json` using the export contract below.
It contains private data; store it accordingly. Export responses are not saved in application caches.
Manage or remove an individual upload opens the existing profile/document controls.
Deletion opens a keyboard-accessible modal with Cancel focused and requires typing `DELETE`.
Cancel/Escape restores focus; accepted deletion clears private queries and mutations immediately,
revokes the browser session and explains that asynchronous cleanup continues. Existing sign-out
behavior also clears private caches. A delayed result cannot download data or change a new session.
Retention copy distinguishes operator configuration, live cleanup, backups and external providers.

Issue #64 validation uses real Chromium/API/worker/PostgreSQL with a local signed Google fixture:
password proof, Google cancellation/link/conflict, wrong-subject and successful Google proof,
JSON download, individual upload removal, deletion proof/confirmation, and session revocation.
Keyboard focus/Cancel/Escape/Tab were exercised; automated WCAG scans cover 320/768/1440px at
100%/200% text sizing with reduced motion. Representative [desktop](docs/validation/issue-64-desktop.jpg)
and [mobile](docs/validation/issue-64-mobile.jpg) captures accompany the recorded results.

### Account tombstone fencing

[Issue #147](https://github.com/ThibaultLeveau/google_job_hunter/issues/147) makes
`users.deleted_at` authoritative for password/Google sign-in, session reads,
verification, recovery, reauthentication and explicit identity linking. Authentication,
upload and search-definition/run-creation writes lock and recheck the live user before
child records. Run cancellation keeps the worker's task/run/user lock order and checks
the same tombstone before writing. A waiting operation rechecks the account after
the deletion transaction commits.
Password hashing, Google exchanges and mail delivery remain outside these locks.
Account-bound OAuth starts require the owner's active, unexpired session; anonymous
sign-in starts retain their existing behavior. Upload publication and saved-search
mutations also recheck the owner even when HTTP authentication was already cached.
A rejected upload discards its published blob through the existing storage port.

Pending verification/recovery deliveries skip tombstoned accounts. Mail already
handed to a transport cannot be recalled; its link cannot modify the tombstoned
account. The account-deletion endpoint and cleanup policy are described below.
`tests/test_account_fencing.py` covers real PostgreSQL lock waits, late Google
callbacks, mail suppression, upload cleanup and unchanged access for another owner.

### Account deletion

POST `/api/v1/account/delete` with `{"intent":"delete_account"}`, the session cookie,
allowed Origin and CSRF header. The server derives the owner, rejects extra fields
and query parameters, and rechecks recent `delete_account` authentication inside the
write transaction. Success returns 202 `{"status":"deleting"}` and clears session/OAuth
cookies. All sessions are revoked immediately; the account tombstone invalidates
verification, password recovery and delayed identity proofs before cleanup starts.
The tombstone and durable `account_cleanup` task commit together. Subsequent requests
with the old session receive 401, including a repeated deletion request.

Cleanup removes at most 100 private tasks and 10 document blobs per step. Private
records remain available to the cleanup worker until their task references and blobs
are removed. Missing files are harmless; a crash after unlinking retries the same
step. Storage/database failures use the durable worker retry policy with ten attempts;
exhausted cleanup remains failed until the retention command retries it after the cause is fixed. Missing
storage configuration never permits discarding database references to existing blobs.
Uncommitted staging files use the separate storage-sweep command documented below,
with its one-hour minimum grace period and active-upload protection.

The purge removes identities/sessions, verification/recovery tokens and protected
mail, bound OAuth/linking state, uploads and parsed text, proposals/provenance, profile
facts, saved searches/runs/discovery associations, private matches and retry receipts.
It also removes owner/run budget buckets and anonymizes reservation references while
preserving global accounting. Shared public jobs, source snapshots and public
processing artifacts remain usable by other associated owners. Cleanup retains an
opaque completion task and accounting without owner/run links; existing unassociated
task and rate-limit records follow their separate retention policies.

This covers the live database and private volume. Encrypted backup copies remain
until the operator's separately configured backup-retention expiry; restoring a backup
must reconcile subsequent deletions before reopening access. Retention invocation is
documented below; backup/restore runbooks belong to #72. Mail and AI providers may retain data
already received under their own policies; this endpoint cannot recall messages or
erase provider-held copies. No backup or external-provider deletion is claimed.

### Retention maintenance

Run one preview batch with the API's database settings and an existing private document root:

```sh
uv run --directory backend --locked python -m job_hunter.infrastructure.retention
```

Add `--apply` to perform cleanup. JSON contains the phase, counts, scanned/protected/contended
totals and an opaque checkpoint. Pass a non-null value with `--checkpoint TOKEN` on the next
invocation, using the same configuration and mode. Null means the pass is finished; begin
the next pass without a checkpoint. Dry runs preserve records, files and task generations.
The checkpoint freezes age cutoffs and cannot cross preview/apply modes or policy changes.

Configure integer environment variables with the `JOB_HUNTER_RETENTION_` prefix:

| Suffix | Default | Retention basis |
| --- | ---: | --- |
| `ACTION_HOURS` | 24 | Hours after verification/recovery/OIDC expiry |
| `SESSION_HOURS` | 24 | Hours after session expiry or revocation |
| `RATE_HOURS` | 1 | Hours after rate-bucket expiry; keep the hashing secret |
| `MAIL_HOURS` | 0 | Hours after successful delivery; normal publication already erases payloads |
| `FILE_HOURS` | 24 | Hours since orphan file modification; minimum 1 |
| `TASK_HOURS` | 720 | Hours since an eligible terminal task's last update; minimum 1 |
| `RETRY_HOURS` | 1 | Minimum tombstone age and failed cleanup retry interval |
| `ARTIFACT_DAYS` | 30 | Snapshot age, with the protections described below |
| `BATCH_SIZE` | 20 | Candidates per invocation, range 1–100 |

Hour limits are 0–87600; file/task/retry hours require at least 1. Artifact days are 1–3650.
Counts identify primary records processed; token deletion also removes its at-most-one
encrypted delivery payload. Files use inode locks and recheck every committed reference,
including tombstones. Directory enumeration uses bounded memory; its time depends on the
directory size, while file openings/reference checks/deletions are batch-limited.
Running token/mail tasks, even with expired leases, remain protected for worker recovery.
Terminal task cleanup only removes recognized private kinds with absent subjects and no
crawl/retry receipts. Unknown/public tasks and keys with existing subjects remain durable.
Tombstones re-enqueue missing cleanup tasks or retry failed generations; live work is retained.
The existing worker performs private deletion; accepted facts and other owners' public jobs
keep their existing deletion/evidence contracts. This command makes no provider calls.

Invoke hourly through the existing scheduler, persisting the last successful checkpoint for
the next invocation; when null, start a fresh pass next hour. A database failure rolls back
that batch. After any failure or lost output, retry the same input checkpoint; file unlink
and task recovery are repeatable. New passes revisit busy rows and files created before an
earlier cursor. This adds no scheduler service or recurring user searches. The artifact
phase delegates to the command below with its separate 10000-row graph limit.

### Old public processing artifacts

Preview one bounded cleanup batch from the repository root:

```sh
uv run --directory backend --locked python -m job_hunter.infrastructure.artifact_retention --retention-days 30 --scan-limit 20 --row-limit 10000
```

Set `JOB_HUNTER_DATABASE_URL` as for the API. `JOB_HUNTER_ARTIFACT_RETENTION_DAYS`
sets the default age; the flag overrides it. Add `--apply` to delete eligible rows.
JSON output contains per-table counts, scanned/protected/oversized/contended counts
and a checkpoint, without source or provider payloads. Pass a non-null checkpoint
back with `--checkpoint TOKEN` and the same age and mode until it becomes null.
Dry-run checkpoints cannot authorize deletion: start a new apply pass. Checkpoints
freeze the cutoff; a later pass starts without a checkpoint. Days must be 1–3650,
scan limits 1–100 snapshots, and row limits 1–10000 total deleted rows per batch.

Eligibility uses the snapshot fetch time, not a guessed artifact creation time.
Current snapshots and all their derivatives remain protected, regardless of age or
owner count. Historical private matches protect their source content/requirements;
accepted profile facts, private document evidence and job associations are untouched.
Pending/running/failed tasks (including expired leases), recently updated terminal
tasks, mixed embedding receipts and unprovable eligibility protect the entire graph.
Task keys remain durable to prevent duplicate delivery and repeated paid work.
Crawl receipts and lineage references retain the original snapshot bytes; their
unreferenced derivatives can expire. Global embedding-space definitions remain.

Each graph is reclaimed atomically, with reference/lease rechecks and publication
locks. Busy graphs are skipped without waiting; rerun a new pass to revisit them.
Graphs larger than the row budget are retained and reported as oversized; increase
the budget within its bound and start another pass. Graphs exceeding 10000 rows
remain retained. A database error rolls back the whole batch: retry the same input
checkpoint. Repetition is safe; no external provider calls occur during cleanup.
This command is artifact-only; overall retention and tombstone recovery use the command above.
Real PostgreSQL tests in `backend/tests/test_artifact_retention.py` cover pipeline
replay, shared/private evidence, lease/publication races, bounded continuation and rollback.

### Personal data export

POST `/api/v1/account/export` with the session cookie, allowed Origin and CSRF header;
omit the body or send `{}`. No owner, filter or destination parameter is accepted.
Recent authentication is required for `export_data`, including a second session/recency
check inside the export's read-only PostgreSQL snapshot. An expired proof returns 403
`reauthentication_required`; complete the existing password or Google proof flow with
`action: "export_data"` and repeat the request. The Account settings screen provides this download and its authentication prompt.

The response is a UTF-8 JSON attachment named `google-job-hunter-export-v1.json`, with
`Cache-Control: no-store` and `X-Content-Type-Options: nosniff`. Its stable envelope is
`format: "google-job-hunter"`, `version: 1`, `exported_at` (UTC), `owner_id` and:

- `profile`: revision, accepted facts, user-edited fields, available source quotes,
  document versions/languages and unavailable-source markers.
- `documents`: metadata, including retained deletion timestamps, without storage keys.
- `saved_searches`, `runs`, `discoveries`: retained definitions/deletion timestamps,
  frozen run inputs, status/counters and the owner's discovered URLs/ranks.
- `matches`: every retained private result generation, provenance/version identifiers,
  scores, assessments and the professional facts captured for that evaluation.

Top-level collections have deterministic ordering; absent collections are empty and
unknown values remain null. The OpenAPI `PersonalExport` schema defines the complete format. Historical
matches are snapshots, not claims of current eligibility. Credentials, action tokens,
request keys, provider call/diagnostic payloads and other owners' records are excluded.
No raw crawled pages or pending extraction proposals are included.

Exports are bounded to 2,000 records (including source links, unavailable-source markers
and historical professional facts) and 8 MiB of UTF-8 JSON. Larger exports return 413
`export_too_large` before sending an attachment; they are never silently truncated.
No queued artifact or third-party delivery is created. Original files remain available
through owner-only `GET /api/v1/documents/{id}/download` while retained. Deleted documents
cannot be downloaded; accepted facts retain unavailable-source markers without their old
quotes. Exports include only retained data and do not restore purged sources or artifacts.

Issue #62 browser validation used real API/worker/PostgreSQL services with deterministic
providers: empty/populated exports, a downloaded JSON file, byte-identical UTF-8 original
download, expired proof, password reauthentication and second-owner isolation passed.
No browser errors or external provider requests occurred.

### Accepted profile facts

The verified-owner `/profile` page shows accepted skills, experience and projects, with explicit
unknown dates and empty sections. Manual values, source-supported facts and unavailable evidence are
distinguished; expandable evidence shows literal quotes, original language and authorized source downloads.
Skills can be added and removed with keyboard controls. Exact duplicates keep the existing wording.
Writes use the current profile revision and invalidate its query; conflicts preserve drafts and require
an explicit reload/review before retrying. Failed refreshes keep visible facts marked out of date and block
writes. Account changes clear cached facts and remount the form; late responses cannot restore old data.
Changed accepted facts require job evaluations to refresh; evaluation processing is implemented later.
Document upload and candidate review appear below the accepted facts.

Experience/project sections provide create/edit dialogs and explicit removal confirmation. Forms use
the same fact API, including calendar dates, nullable ongoing status, optional descriptions and safe
HTTP(S) project links. Blank optional values become null. Reversed dates and an ongoing activity with
an end date are rejected with associated field errors. Saved facts show current evidence alongside
manual-change guidance; edits retain only supported source links. Duplicate outcomes come from the
server and keep existing values. Conflicts preserve the draft, show current saved facts after explicit
reload, and require review before resubmission. A fact removed elsewhere cannot be saved again from
its old editor. Removal deletes the accepted fact and its links while keeping source documents.
Dialogs focus the first field, or Cancel for removal, support Escape and keyboard navigation, and
restore focus to the opener or the section's Add button after removal. Requests cannot be submitted twice.

Issue #31 browser validation used real API/worker/PostgreSQL services with fixture extraction. Keyboard
addition, duplicate handling, removal, evidence expansion, conflict recovery and focus restoration passed.
All 320/768/1440px layouts at 100%/200% text scaling, with reduced motion and long/literal hostile text,
had no horizontal overflow or automated WCAG A/AA violations. Visually checked
[desktop](docs/validation/issue-31-desktop.jpg) and [mobile](docs/validation/issue-31-mobile.jpg) captures
record the accepted-profile state; automated scans do not establish complete accessibility conformance.

Issue #32 repeats the real API/worker/PostgreSQL journey for experience/project creation, editing,
server duplicates, date errors, retained conflict drafts, supported evidence and confirmed removal.
Native dialog entry, Tab/Shift+Tab wrapping, Escape and focus restoration passed keyboard checks.
The same six viewport/text-scale combinations passed with no horizontal overflow or automated WCAG
A/AA violations. Visually checked [desktop editor](docs/validation/issue-32-desktop.jpg) and
[mobile confirmation](docs/validation/issue-32-mobile.jpg) captures record those dialog states.

Verified sessions can read `GET /api/v1/profile` and `/api/v1/profile/facts/{id}`. Ownership comes
only from the session; foreign and missing facts return the same 404. The profile ID is the owner's
UUID. An untouched profile reads as revision zero with no facts; the first write materializes it.
POST `/api/v1/profile/facts` or PUT `/api/v1/profile/facts/{id}` with
`{"expected_revision":0,"fact":{"kind":"skill","name":"Python"}}`; DELETE the item with an
`expected_revision` query parameter. Writes require Origin/CSRF and a current aggregate revision.
PUT replaces the full fact; omitted optional values become unknown. Responses carry `outcome`,
`revision` and `fact`: create is 201, existing/updated/deleted are 200;
stale revisions or duplicate-content conflicts return the standard 409 `conflict` envelope. Refetch
before resolving conflicts. Effective fact edits, deletes and new accepted evidence advance revision;
a stored match revision that differs from the current profile revision is stale.

Fact kinds are skill (`name`), experience (`company`, `title`) and project (`name`, optional `url`).
Experience/project dates use `YYYY-MM-DD` or null; `ongoing` is true/false/null, and description is
optional. Unknown is distinct from known values. Reversed dates, ongoing with an end date, blank text,
unsafe URLs and oversized fields are rejected. Display text is preserved. Exact keys hash sorted JSON
of kind plus skill name, experience company/title/dates/ongoing, or project name/URL/dates/ongoing.
Labels use Unicode NFKC, trimmed/collapsed whitespace and casefolding; URLs are only trimmed, retaining
case-sensitive paths. Descriptions are outside identity. Duplicate insertion preserves existing text;
differing descriptions conflict instead of overwriting manual edits. Similar names remain separate.

PostgreSQL uniqueness is per owner/kind/key. One transaction locks the owner aggregate, checks its
revision and writes both facts and revision; reads hold a shared aggregate lock for a coherent snapshot.
`source_associations` is separate read-only evidence metadata (document UUID, source version, original
language, field, proposal UUID, Unicode span and exact quote). Clients cannot submit evidence through fact writes.
`user_fields` distinguishes user-provided values/corrections; unsupported links are removed on manual edits.

POST `/api/v1/profile/review` with `expected_revision`, a UUID `idempotency_key`, and 1–100 unique `items`.
Each item supplies `proposal_id` and `action` (`accept`, `reject`, or `edit`); only `edit` requires a full `fact`.
Verified ownership and Origin/CSRF are required. Responses contain revision and per-item `created`, `linked`,
`rejected` or `conflict` outcomes with fact IDs. Foreign/deleted/stale-source proposals return 404 atomically.
Stale profile revisions or reuse of a key for a different payload return 409: refetch and use a new key.
Identical keys replay the original response, including its historical revision; they never reapply changes.
Decisions are final per proposal; identical items replay, while changing a decided item returns conflict.
Conflicts remain undecided and can be corrected using a new request. Mixed successful decisions commit together.
Exact matching preserves existing display/manual content; differing descriptions conflict. Explicit edits can
choose existing content or a distinct natural key. Similar project/employment names never cause fuzzy merges.
Evidence links retain only fields still supported by immutable source quotes; corrections cannot invent citations.
Revision advances once per batch that creates facts or adds evidence; rejection/replay alone never advances it.

Choose **Review candidates** on a document whose extraction is complete. Candidates are grouped by
document, with original-language quotes, source version/locations, uncertain or missing values and
suggested existing facts. Suggestions compare normalized identifying values; the server remains the
authority for exact linking/conflicts, including Unicode casefolding and differing descriptions.
Select up to 100 candidates across pages, optionally correct them with the shared skill/experience/project
editors, then explicitly accept or reject. Keeping a draft alone never changes accepted facts.
Created/linked/rejected results are shown per candidate; conflicts keep their selections and corrections.
Stale revisions require explicit reload/review before another submission. Unconfirmed results lock the
captured decisions and offer an identical retry with the same idempotency key, including after a lost response.
Choices/drafts survive recoverable errors and pagination; closing review discards local choices and receipts.
The proposals API includes previously decided candidates. Reopening does not infer historical decisions:
the server replays identical choices or reports conflicting ones. Account changes abort private requests;
document deletion also cancels an open review and removes its cached proposals.

Issue #34 browser validation used the real API/worker/PostgreSQL with fixture extraction: keyboard review,
shared draft editing, exact duplicate linking, acceptance/rejection, stale revision recovery and account
isolation passed. PostgreSQL retained four accepted facts and four supported evidence spans at revision three.
All six 320/768/1440px and 100/200% text-scale layouts passed overflow and axe checks, with reduced motion,
native dialog Escape and focus recovery. See [desktop review](docs/validation/issue-34-desktop.jpg) and
[mobile draft editor](docs/validation/issue-34-mobile.jpg). No paid providers or unexpected external requests ran.

### Private document uploads

The profile page provides a keyboard-accessible PDF/DOCX/TXT picker and drop zone, explicit upload
confirmation or existing-document feedback, and a paginated document list. Upload progress is
indeterminate. The server enforces the configured size limit; the picker describes the 10 MiB default
and rejects empty, unsupported or larger-than-50-MiB selections before sending bytes.
Active parsing, extraction and deletion status refreshes every five seconds while the page is visible.
Polling stops at terminal states or request errors; refresh/retry controls recover failed requests.
Unmounting or switching accounts aborts requests and stops timers; private caches clear on account changes.
Downloads use the authorized API. Deletion requires confirmation, preserves accepted facts, immediately
removes cached source quotes and follows cleanup until completion; failed cleanup can be resumed.
The UI explains scanned/encrypted/invalid files and unavailable extraction. It does not parse files in
the browser; failed parsing requires a corrected/different file or manual facts.

Issue #33 browser validation used the real API, worker and PostgreSQL with fixture extraction: keyboard
file selection/upload, TXT/DOCX/scanned PDF handling, duplicate feedback, invalid-content recovery,
byte-identical download, confirmed deletion with accepted-fact retention, and account isolation passed.
At 320/768/1440px and 100/200% text scale, there was no horizontal overflow or axe violation; reduced
motion and keyboard confirmation/cancellation/focus recovery passed. See the document-list
[desktop](docs/validation/issue-33-desktop.jpg) and deletion [mobile](docs/validation/issue-33-mobile.jpg) captures.

Verified sessions POST raw PDF, DOCX or UTF-8 TXT bytes to `/api/v1/documents` with the matching
`Content-Type`, percent-encoded UTF-8 `X-Document-Name`, Origin and CSRF headers. The binary body streams;
signature/container/encoding checks precede persistence.
201 returns the queued document; exact same-owner bytes return the existing document with 200.
GET the collection (100 items/page, `offset`), `/{id}` for status, or `/{id}/download` for an attachment.
Foreign/missing IDs return 404. Filenames are sanitized metadata; keys, hashes and paths stay private.
Set `JOB_HUNTER_DOCUMENT_ROOT` to an absolute local-volume directory outside the web root, mode 0700,
API/worker service account only; files use 0600. Unconfigured document storage returns 503 after authentication.
`JOB_HUNTER_DOCUMENT_MAX_BYTES` defaults to 10485760 (range 1–52428800), `QUOTA_BYTES` to 104857600
(range 1–1073741824), and `QUOTA_COUNT` to 100 (range 1–1000), all with the same document prefix.
Errors: 60-second timeout 408, oversize 413, invalid content 415, quota/pending deletion 409, storage failure 503.
Quotas count committed documents until cleanup completes; checks serialize per owner. Temporary use is bounded per request.
Document creation and `document_parse` enqueue commit together; the configured worker consumes these tasks.
Use retention maintenance above for checkpointed scheduling on a filesystem with working POSIX locks.
The legacy `uv run --directory backend --locked python -m job_hunter.infrastructure.documents` command
applies one storage-only batch of up to 100 candidates with a one-hour grace period. Both preserve
active uploads and committed references, including tombstones and uncertain upload commits.

Verified owners DELETE `/{id}` with Origin/CSRF headers. The transaction tombstones the document,
cancels and fences unfinished parsing/extraction, marks accepted evidence unavailable and enqueues cleanup.
DELETE returns 202 while `status` is `deleting`, then 200 when `deleted`; GET `/{id}` returns the same
state containing only `id`, `status` and nullable `error`. Repeated deletion is idempotent. Tombstoned
documents disappear from the collection; downloads and proposals immediately return 404.
Accepted facts remain, with removed document IDs in `unavailable_sources` and no old source quotes.
Affected profile revisions advance once; manual content and evidence from other documents remain intact.

The configured worker also consumes `document_cleanup`. It removes the blob outside database locks,
then commits at most 100 proposals and 10 parsed versions per step, including their private dependent data.
Completed steps and the next task commit together. Missing files succeed; directory changes are fsynced.
Storage/database failures retry safely, including restart after unlink but before database commit.
After five failed attempts, `error` is `cleanup_failed`; repeating DELETE resumes the failed step.
The owner-scoped `PostgresDocuments.delete` operation is reusable by later account deletion.
After cleanup, the document row is removed, including filename, hash, size and storage key.
This releases count/byte quota and permits identical re-upload with a new document ID and storage key.
Only an owner-scoped operation receipt (document/owner IDs, step and completion time), task metadata
and text-free accepted-fact source markers remain for status, retries and provenance. Account deletion
removes these receipts too. Migration `0033_document_removal` also removes previously completed
document tombstones. Downgrading cannot recover erased metadata and removes orphan receipts/markers.

### Bounded document parsing

The worker uses the same private volume and registers `document_parse` when storage is configured.
PDF uses pinned [pypdf](https://pypdf.readthedocs.io/en/stable/user/extract-text.html), DOCX uses pinned
[python-docx](https://python-docx.readthedocs.io/en/latest/api/document.html), and TXT uses strict UTF-8.
Each parse runs in a fresh Python subprocess with a cleared environment, private temporary directory,
10-second wall/CPU budget, 256 MiB address-space cap, 50 MiB input/expansion bounds, 100 PDF pages,
10,000 segments, 1,000,000 characters and a 16 MiB output cap. Timeouts/cancellation kill and reap the
child; temporary output is removed. This provides resource isolation, not an OS security sandbox.
The API reports an allowlisted `parse_error`: `ocr_required` marks scanned PDFs as unsupported in v1.
Provide encrypted files decrypted; replace empty/invalid files, or reduce limit/time failures in size/complexity.
TXT lines and DOCX body paragraphs (including table cells) use `paragraph:N`; PDF uses `page:N`.
Offsets are Unicode character ranges in canonical segment text joined by newlines, not source-byte offsets.
DOCX headers/footers and layout previews are not extracted. Source language metadata is preserved;
TXT language stays unknown. Artifacts retain source/text hashes, document/parser versions and engine versions.
The worker reuses existing artifacts and checks lease/version plus owner/document tombstones before
atomically saving text, completing the task and enqueuing configured extraction. Parsed text stays private.

### Bounded AI boundary

`application.ai` exposes typed chat/extraction and embedding ports, strict Pydantic `Output`
schemas and versioned `Prompt` values. `infrastructure.ai.create_ai` composes the configured
adapter with required accounting hooks and a cancellation predicate for synchronous worker jobs.
`available` means configured, not a live connectivity/quality probe. Feature prompts arrive in #28 onward.

API and worker startup validate these server-only `JOB_HUNTER_AI_` settings:

| Suffix | Default / contract |
| --- | --- |
| `PROVIDER` | `disabled`; supported alternatives `openai` and `fixture` |
| `ENVIRONMENT` | `production`; fixtures require explicit `development` or `test` |
| `API_KEY`, `CHAT_MODEL`, `EMBEDDING_MODEL` | Required for OpenAI; select versioned compatible model IDs at deployment |
| `TEMPERATURE` | Omitted by default; set 0–2 only for a model that supports it |
| `LIMITS` | JSON object: `input_tokens=16000` (1024–64000), `output_tokens=2000` (1–8000), `seconds=20` (0.01–60), `dimensions=1536` (1–3072) |

The pinned LangChain/OpenAI adapter uses the official HTTPS endpoint, strict JSON Schema and
no tools, agents, callbacks, tracing, response storage or ambient proxy/base-URL overrides.
See the [structured-output contract](https://developers.openai.com/api/docs/guides/structured-outputs).
UTF-8 byte bounds conservatively limit byte-BPE input tokens, including prompt/schema/framing.
Embeddings allow at most 16 nonempty inputs, 8000 bytes each, within the aggregate input bound;
one request returns finite vectors of the configured dimensions. Output tokens are capped by the request.
Every call has a request timeout and wall deadline; cancellation aborts and awaits transport cleanup.
SDK retries are zero. Chat can repair schema or opted-in feature-validation failures once with a fixed instruction, without replaying
invalid output; provider failures/refusals are terminal. Callers must not add automatic task retries
around these calls; later explicit retries require fresh budget reservations.

Source text travels in a JSON data envelope; output still requires feature-specific evidence checks.
Accounting reserves before each request and records its UUID, configured model, prompt/schema versions,
schema hash, bounds, known usage (otherwise null), elapsed time and safe error code. Hooks receive no
source/output text. Hook failures stop execution; #54 adds durable atomic budget enforcement.
Fixtures require an explicit response supplied to the factory; embedding fixtures are deterministic,
not semantic vectors. Normal tests intercept HTTP and use no credential or paid service.
The unused LangSmith pytest plugin is disabled; one pinned dependency's Python 3.14 import deprecation
is narrowly filtered. Other warnings remain errors, and no maintained code is excluded from coverage.

Verified-owner `GET /api/v1/documents/{id}/proposals?offset=0` returns status, artifact/source version,
language, count and 100 proposals/page. Accepted profile facts never change.
Each field retains uncertainty, explicit nulls and exact Unicode spans/quotes validated against its window
and preserved text. Extraction v2 resolves incorrect model offsets using exact quotes in the declared
source window: unique matches can be relocated, while repeated skill quotes retain all occurrences.
Grouped skill-name fields become separate candidates, each subject to full evidence validation.
Profile evidence checks use the same two-call ceiling as schema repair; invalid evidence is never published.
Ambiguous employment/project quotes and absent quotes fail. Already valid positions remain unchanged.
Unsupported date precision becomes unknown with uncertain, validated source evidence; no day is inferred.
Sentence-final periods followed by whitespace/end are punctuation; partial tokens such as `C`
in `C++` still fail. Unsupported values fail; source presence does not prove factual truth.
Only exact normalized skills merge evidence; similarly named employment/projects remain separate.
Windows use 500-character pieces/100-character overlap and about 4 KiB; excess over 64 windows,
128 calls including repair or 512 candidates fails. One task attempt prevents nested paid retries.
Artifacts survive redelivery; startup backfills 100 per batch. Disabled AI is unavailable. Fixture workers need
`--profile-fixture` JSON matching `Batch`; acceptance requires an explicit profile review request.

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
export TEST_DATABASE_URL="$JOB_HUNTER_DATABASE_URL"
uv run --locked python scripts/check_migration_heads.py
uv run --locked pytest --require-database -m integration
```

The test role must create/drop databases. Shared fixtures in `backend/tests/conftest.py` provide
`empty_database` for migration tests and `isolated_database` with all migrations applied for feature
tests. Each creates a randomly named database, then closes connections and drops only that database,
including when the test or migration fails. Mark database tests `integration`. Keep integration
execution sequential: the fixtures reject xdist workers when a database URL is present; parallel
support requires a separate verified isolation change. Tests cover fresh extension migrations and
downgrade/upgrade, readiness, failed-transaction rollback, committed inserts, session closure,
pool exhaustion and statement timeout. They never substitute SQLite or mock transaction semantics.
Without `TEST_DATABASE_URL`, these tests explicitly skip; ordinary tests still verify redacted
database-down readiness, settings and offline migration SQL. Use `pytest -m 'not integration'` for
an intentional unit-only run; full verification and CI use `--require-database`.

### Durable task store

The task store already uses the same migrated database. `PostgresTaskQueue(session)` implements
the narrow application queue port inside the caller's `database.transaction()` unit of work;
it never commits independently. Write subject records and enqueue their task in that same session.
Tasks store UUID subject/payload references, a positive subject revision, a bounded kind code,
status, UTC schedule/timestamps, attempt budget, generation, fencing token and safe error code.
The database uniquely constrains `(kind, subject_id, subject_version)`; duplicate enqueue preserves
the task and rejects a conflicting payload reference. No raw personal content belongs in task rows.

Claims use `FOR UPDATE SKIP LOCKED`, select only supplied registered kinds that are due and within
their attempt budget, and assign a new token/expiry. Commit the short claim transaction before any
external work. Complete/fail with the original task snapshot in a new transaction; expired or changed
tokens/generations raise `StaleLease`. Let that exception roll back publication and any next enqueue.
Feature use cases must authorize the referenced owner/subject and recheck its live version in the
publication transaction. Generic UUID references cannot enforce foreign keys to every future feature.

An authorized retry supplies the same logical key and observed generation. It reopens only failed
tasks, resets attempts, advances generation and replaces the token. Concurrent/replayed retries for
that generation share the existing result; succeeded tasks and successful artifacts are preserved.
These internal primitives expose no HTTP retry endpoint.
From `backend/`, run `uv run --locked pytest --require-database tests/test_tasks.py` with the test URL
configured above; controlled concurrent tests use separate sessions and barriers within one test.

Run the separate worker from `backend/` with the same `JOB_HUNTER_DATABASE_URL`:

```bash
uv run --locked python -m job_hunter.worker --concurrency 2
```

The worker registers verification/recovery mail and expiry handlers alongside the explicit
`job_hunter.worker.HANDLERS` registry; unknown kinds stay pending. Concurrency is 1–16 per process (default 2).
Claims use 60-second leases, renewed every 20 seconds while handlers run outside transactions.
Each claim consumes an attempt; expired leases are recovered in batches of at most 100 registered tasks.
`RetryableTaskError` schedules full-jitter exponential backoff (1-second base, 60-second cap);
permanent/unclassified handler errors fail immediately. Attempts never exceed the stored budget.
Database publication errors are retryable; logs and durable errors omit exception contents.
SIGTERM/SIGINT stop new claims and drain active handlers, maintaining their leases.
Handlers must bound every external call; Python threads cannot forcibly stop an unbounded call.
Lease loss discards a result and retains its concurrency slot until that call ends.

A handler's publication callback rechecks its live subject/version, writes the validated artifact
and enqueues the next stage in the supplied session, with no external I/O. The worker fences and
acknowledges the stage in that same transaction; any publication failure rolls all three back.
Committed stages are not rerun, so the next task resumes from their preserved artifacts.
Delivery is at least once: a crash after a provider response but before commit can repeat the call.
Handlers must reserve/reconcile user and global budgets before paid calls and use provider
idempotency where available, including any SDK retries. Search uses bounded per-run attempts;
atomic user/global search budgets are enforced by #54. External adapters require explicit configuration.
Tests inject clock/sleep and jitter, plus deterministic handlers, against real PostgreSQL.

### Search preview

Issue #35 adds `POST /api/v1/searches/preview`: a synchronous, authenticated, stateless preview.
Send the session cookie, exact allowed `Origin` and `X-CSRF-Token`, as with other authenticated POSTs.
This free preview permits unverified sessions; paid search execution remains verification-gated.
It neither saves definitions nor executes searches. For example:

```json
{
  "groups": [["Python", "Go"], ["data engineer"]],
  "exclusions": ["recruitment agency"],
  "sites": ["jobs.lever.co"],
  "language": "fr",
  "country": "fr",
  "location": "Paris, France",
  "recency_days": 30
}
```

The response contains `query`, `provider_parameters` and explanatory `hints`. The example produces
`("Python" OR "Go") AND ("data engineer") AND (site:jobs.lever.co) -"recruitment agency"`
and separate `{"lr":"lang_fr","gl":"fr","location":"Paris, France","as_qdr":"d30"}` parameters.
Every term is literal text; a multiword term becomes an exact-phrase hint. Supply phrases without
surrounding quotes. NFKC normalization and collapsed spaces are deterministic; exact normalized
duplicates within a group, exclusions or sites compile once in first-seen order. Case is preserved;
groups are not flattened or deduplicated across AND boundaries.

Limits are 1–6 nonempty groups, 1–8 terms per group, 32 keyword terms total before deduplication,
8 exclusions, 4 sites, 80 characters per term/exclusion, 120 per location and 2,048 UTF-8 bytes for
the compiled query. Lengths apply before and after normalization; recency is an integer from 1–365 days.
Words may contain Unicode letters/numbers/marks/spaces and `+ # . , ' / -` (including C++, C# and CI/CD).
Empty text, leading minus signs, controls, raw operators, double quotes, backslashes, URL schemes,
`//`, `www.`, URL parameter syntax and unknown fields are rejected. Embedded double quotes are
rejected because Google does not document reliable escaping; no backslash escape is invented.

Sites must exactly match `JOB_HUNTER_SEARCH_ALLOWED_DOMAINS`, a JSON array of at most 100 ASCII
domains; defaults are `linkedin.com`, `indeed.com`, `jobs.lever.co` and `boards.greenhouse.io`.
An empty allowlist disables site filters. Input domains normalize to lowercase; wildcards, URLs,
paths, ports, credentials and implicit subdomain matching are rejected. Google's resulting `site:`
hint can include subdomains and is not a crawler authorization rule.

Language/country codes use offline snapshots of SerpApi's [result languages](https://serpapi.com/google-lr-languages.json)
and [countries](https://serpapi.com/google-countries.json), checked on 6 November 2026; omitted values
remain unspecified. The [provider parameters](https://serpapi.com/search-api) and
[recency parameter](https://serpapi.com/advanced-google-query-parameters) are separate values for a
future adapter to encode, never a user-supplied URL. [Google query hints](https://support.google.com/websearch/answer/2466433?hl=en)
may be rewritten or ignored: Boolean/phrase/site hints do not guarantee exact results, language is
the result language, country/location describe search origin, and recency concerns document dates.
None guarantees a job's location, remote eligibility, work authorization or posting date.
Hard filtering on extracted job metadata is separate future behavior; the response states these limits.

`uv run --locked pytest --require-database tests/test_searches.py` from `backend/` covers deterministic
compilation, bounds, unsafe input and authenticated API round trips with local PostgreSQL sessions.
It needs no provider credential or external service.

Issue #36 adds authenticated saved-search CRUD at `/api/v1/searches` and `/{search_id}`.
Create with `{name, definition}`; updates add `expected_revision`, and DELETE takes that revision
as a query parameter. Names are 1–120 characters. Definitions use the preview schema and configured
site allowlist. Revisions start at 1; stale writes return 409 and foreign/missing IDs return 404.
Deletion hides the saved definition while retaining historical runs and their independent cancellation.

Verified users can `POST /api/v1/searches/{search_id}/runs` with `expected_revision` and a UUID
`idempotency_key`; 202 returns the run ID, key, revision, immutable name/definition/query/provider
parameter snapshot and counters. The run and `search_discovery` task commit atomically. Keys are
unique per owner: the same search/revision replays the existing run even after edits/deletion;
reuse for different inputs returns 409. A database trigger protects snapshot inputs from updates.
Compiled provider parameters are snapshotted; discovery execution arrives in #39.

List searches or `/api/v1/searches/runs` with `limit` (1–100, default 20) and `offset` (0–10,000).
Responses contain `items` and `next_offset`. Saved definitions are oldest first; run history is newest
first, with UUID ordering breaking equal creation timestamps. Read a run at
`/api/v1/searches/runs/{run_id}`; POST its `/cancel` subpath to cancel pending/running discovery.
Repeated cancellation preserves the result; completed/partial/failed runs remain terminal.
Cancellation fences only that personal task, preserving shared processing. Worker publication must
lock the task before the run and recheck its active state before creating personal associations.

Run states are pending/running/completed/partial/failed/cancelled; #39 registers the discovery worker.
Counters distinguish provider results found, newly registered URLs,
previously known URLs and skipped results. `processing_total` counts associated jobs to analyze;
`processing_ready` counts those ready for inspection. Discovery completion does not imply analysis
completion. Tests in `tests/test_saved_searches.py` cover real-PostgreSQL ownership, revisions,
immutable history, atomic rollback, concurrent start and both cancellation/publication orderings.

### Saved-search editor

Issue #40 replaces the `/searches` placeholder with a private, paginated saved-search list and
create/edit/delete controls. Named definitions show OR within groups and AND between groups, with
literal phrases and optional exclusions. The editor bounds groups, terms and exclusions to the API
contract, associates local validation errors with fields, and shows duplicate-term guidance.
The server normalizes and validates saved values; the browser does not compile Google queries.
Discovery parameters are preserved when editing groups; #41 adds their controls and query preview.

Edits and confirmed deletions send the saved revision. A conflict or uncertain network outcome
requires reloading and reviewing before another write; unsaved names, groups and exclusions remain
in the open editor. The latest saved definition is shown beside the retained draft after reload.
Deleted definitions cannot be resaved through a stale editor. Drafts are in memory only: cancellation,
navigation, page reload or an account change discards them. Mutations are never automatically retried.

Issue #40 validation used Chromium with the real API and a fresh PostgreSQL database: create/reload,
revision-conflict recovery, update, delete and another owner's denied access passed. The keyboard
pass covered form entry, Tab, associated errors, adding/removing groups/terms/exclusions, save,
reload, deletion confirmation/cancellation and restored focus. At 320/768/1440px and 100%/200% text
size, the editor had no page overflow or automated WCAG A/AA violations, with reduced motion enabled.
See the [desktop](docs/validation/issue-40-desktop.jpg) and [mobile](docs/validation/issue-40-mobile.jpg)
captures. Component tests cover limits, CRUD, failed requests, stale drafts and session changes.
No provider calls or search runs were made; [browser CI](#browser-checks) now runs a navigation smoke.

Issue #41 adds country, result-language, region/city, document-recency and domain hints. Country and
region describe search origin, not job eligibility; Google may rewrite or ignore every hint.
Authenticated `GET /api/v1/searches/hints` exposes the configured domain allowlist and the same offline
country/language catalogs used by validation. The UI offers those values, at most four domains,
1–365 recency days and explicit unspecified/provider-default choices. Unavailable saved selections
remain visible until corrected; clearing hints persists null locale/date values and an empty site list.

After 300ms without a definition change, the editor calls the existing stateless preview endpoint.
Incomplete/bounded-invalid inputs skip preview; the exact returned query is read-only plain text,
with separate provider parameters and approximation guidance. Changed definitions abort obsolete
requests and hide old results. Saving requires a successful preview of the current definition;
server-invalid definitions require editing, while network failures offer an explicit retry.
No Google query compiler or provider execution is added to the browser.

Issue #41's real API/PostgreSQL browser pass covered persisted hints, stale edits with retained values,
explicit clearing and owner isolation. Keyboard country/language selection, Tab, region/date entry,
checkbox Space, query focus, save/reload and clearing passed. All six width/text-scale combinations
above had zero overflow or automated WCAG A/AA violations with reduced motion enabled. See
[desktop hints](docs/validation/issue-41-desktop.jpg) and [mobile preview](docs/validation/issue-41-mobile.jpg).
Component tests use a fake clock for debounce and gates for cancelled/out-of-order responses.

Issue #42 adds manual run selection from the current saved-search page, cancellation and paginated
discovery history. Availability comes from the server; disabled/unconfigured search explains the
required setup. A start freezes its saved revision and a UUID request key. Double clicks are blocked;
an uncertain response retries the same request, while confirmed revision conflicts require reloading
the saved definition. Keys remain in memory: after a page reload, review history before starting again.

Active runs refresh every five seconds while visible, stop polling on terminal states or read errors,
and abort requests and remove run caches when leaving the page or changing accounts. HTTP 429 honors
`Retry-After` (60 seconds when absent) and requires explicit retry. Partial discovery, quota failures,
temporary failures and configured caps are distinct. Found/new/existing/skipped counts retain backend
semantics; existing URLs can be shared public sources, not previously seen personal results.
Separate processing-ready/associated/failed counts do not imply discovery results are fully analyzed.
Cancellation preserves committed discoveries and shared work. Refresh history manually to update
processing counts after discovery finishes. Job links await the usable list route in #55.

Issue #42's Chromium pass used the real API, worker and fresh PostgreSQL with a deterministic search
adapter: complete discovery, partial quota failure, cancellation during provider I/O, reload history
and cross-owner denial passed. Keyboard selection, Tab, starting and cancellation passed. All six
width/text-scale combinations above had no overflow or automated WCAG A/AA violations, with reduced
motion enabled and no external requests or page errors. The [desktop history](docs/validation/issue-42-desktop.jpg)
and [mobile outcomes](docs/validation/issue-42-mobile.jpg) were visually checked. Component tests cover
double clicks, uncertain retries, fake-clock visibility/terminal polling, HTTP 429 and session changes.

### Public HTTP fetch boundary

Issue #43 adds the synchronous `PublicFetcher.fetch(FetchRequest)` port and `HttpFetcher` adapter.
It performs one GET hop only. A redirect response contains a validated next request with its visited
URLs; callers must retain that history and apply robots policy and host pacing before fetching it.
The #118 prerequisite provides robots policy and shared host leases. Issue #44 registers the
`job_crawl` handler in the worker; no separate crawl API is added.

Only HTTP/80 and HTTPS/443 are permitted. Credentials, ambiguous address forms, non-public/mixed DNS
answers, mapped/transition IPv6 and unsafe redirects fail closed. Azure's special
[WireServer IP](https://learn.microsoft.com/en-us/azure/virtual-network/what-is-ip-address-168-63-129-16)
is explicitly blocked too. Async DNS queries both address
families without search suffixes; every answer is validated before any connection. Failed TCP
connections fall back through the same pinned address set within one shared connect deadline,
so an unavailable IPv6 route does not prevent IPv4 access. HTTP requests are never replayed on fallback.
HTTP Host, TLS SNI and certificate verification retain the original hostname. Each hop revalidates
DNS; there is no transport retry, automatic redirect, proxy or cookie jar.
HTTPCore handles HTTP/TLS; gzip decoding has separate wire/body limits and rejects truncated or
concatenated streams. Other content encodings are unsupported. Returned bytes remain untrusted.

`FetchSettings` reads `JOB_HUNTER_FETCH_` variables: `ALLOWED_PORTS` defaults to JSON `[80,443]`
(only subsets of those standard ports); `MAX_REDIRECTS=5` (0–10); `CONNECT_SECONDS=5` and
`READ_SECONDS=5` (0.01–30); `TOTAL_SECONDS=20` (0.01–60, including slot wait and DNS);
`MAX_WIRE_BYTES=2097152` (up to 8 MiB), `MAX_BODY_BYTES=4194304` (up to 16 MiB), and
`MAX_CONNECTIONS=4` (1–16). Share one adapter among worker threads to enforce its socket cap;
each admitted hop owns at most one HTTP connection and bounded DNS work. Response headers are
limited to 16 KiB. Errors expose only stable codes, transient/permanent classification, HTTP status
and Retry-After seconds capped at one hour. The #118 prerequisite also supports HTTP-date values.

Tests use the reusable injected `backend/tests/http_fixture.py` transport and resolver; URL and
address validation still run, with no production bypass setting. They cover rebinding, redirects,
compression bombs, deadlines, concurrent slots, proxy variables and redacted logs. A loopback TLS
server with an ephemeral test certificate verifies the original hostname and rejects a mismatch;
only its test transport maps a validated public IP to that server. No external network is required.
The boundary follows [OWASP's SSRF guidance](https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html)
and [HTTPCore's network-backend contract](https://www.encode.io/httpcore/network-backends/).

### Public source snapshot persistence

The authorized #44 prerequisites are [#118](https://github.com/ThibaultLeveau/google_job_hunter/issues/118)
(host policy) followed by [#119](https://github.com/ThibaultLeveau/google_job_hunter/issues/119)
(snapshot reconciliation). They preserve the existing PR caps; #44 composes the crawl handler.

Snapshots retain bounded public bytes (at most 16 MiB), requested/final URLs, HTTP status, MIME,
UTC fetch time, SHA-256 and immutable UUID/version/previous-version lineage. PostgreSQL verifies
hashes and prevents payload/revision edits. Task ID plus generation records idempotent delivery.
Unchanged compatible current content reuses its snapshot; changed or reverted content creates a
new version. Observations retain the actual fetch URLs/time even when reusing older bytes.
An older observation cannot replace the current source after a newer fetch has been recorded.

Redirect convergence preserves the existing final URL identity, unions private run memberships,
retains each original discovery in owner-bound run history, and moves aliases and snapshot
associations without rewriting content lineage. Old source references resolve to the survivor.
Discovery writers share a PostgreSQL transaction advisory lock; reconciliation takes it exclusively
before run/source changes. No external I/O belongs in these transactions. Callers publishing worker
results must first fence the task within the same transaction, as the worker store already does.
Job reads still require an authenticated owner's run association. Private discovery tracking values
stay in run provenance, not public snapshots. Future processing stages must retain snapshot IDs and
versions; this prerequisite does not add cleanup, AI, browser fetching or a live worker handler.

### Crawl worker outcomes and recovery

The normal worker now consumes `job_crawl` tasks created by discovery. Each execution attempts one
checked HTTP hop, then commits its redirect destination/history before durable deferral. Redirect
limits span all claims and retries. Host waits refund the current failure attempt, have a 24-hour
capacity allowance, and release task leases; HTTP failures use bounded retries and Retry-After.
Every destination passes robots policy, shared host pacing and the public HTTP boundary again.
Robots rules cache for 24 hours; missing/denied status decisions for one hour; unavailability for
60 seconds or Retry-After. Redirected robots files fail closed. Each host permits one leased request
and at least one second between completed calls, respecting supported crawl-delay/request-rate rules.

Outcomes distinguish fetched, blocked, removed, unsupported, non-job and failed pages. HTTP 401/403,
password forms and recognized CAPTCHA markers block processing; 404/410 indicate removal. Missing
or unsupported MIME is explicit. Empty bodies are non-job outcomes; an empty known app root with
an explicit JavaScript requirement is unsupported. These checks are conservative; they do not clean
HTML or decide whether ordinary prose describes a job. Marker checks inspect at most 256 KiB;
JavaScript notices are limited to 2,048 characters. Full preparation arrives in #45 and later.
No login, challenge, robots or browser-execution bypass is attempted. Source bytes remain untrusted.

Snapshot/alias publication and task success or permanent failure commit together under the task
fence. Retry exhaustion and crashed final leases update source failures and owned run counters.
Initial tasks reuse compatible completed snapshots; manual retries use a fresh generation. Fetch
observations use request-start timestamps, so late responses cannot replace newer success or denial.
Tests inject HTTP fixtures and exercise the authenticated API, real worker and PostgreSQL without
external traffic. Production workers use bounded public HTTP; provider secrets are unnecessary for
crawling itself. Read discovery counters separately from later processing readiness.

### Deterministic public text preparation

Issue #45 adds an internal preparation adapter and immutable `job_preparations` artifacts keyed by
snapshot ID and policy/library/Python version. It uses [Beautiful Soup](https://www.crummy.com/software/BeautifulSoup/bs4/doc/)
with the explicit HTML parser, discarding scripts, styles, navigation, controls, hidden content and
embedded active elements. Visible prose and list items retain document order and original language;
inline text stays together. Ordinary content is `uncertain`, not a confirmed vacancy. Only exact
error-page markers are `non_job`; empty, unsupported MIME/status/encoding and exceeded limits are explicit.

Each paragraph has a deterministic UUID, SHA-256 and Unicode character offsets into the authoritative
prepared text, separated by two newlines. Offsets are not raw HTML byte positions. The artifact retains
the immutable fetch snapshot ID/hash; original bounded bytes remain available for evidence. Escaped
markup and plain-text responses remain literal text: consumers must never render them as HTML.

Preparation accepts at most 1 MiB, 20,000 traversal visits, depth 128, 2,000 paragraphs and 100,000
output characters. Exceeding a limit produces no partial text. UTF-8 is the default; explicit UTF-8,
ISO-8859-1 and Windows-1252 headers are supported. Invalid/other encodings are unsupported, not guessed.
The internal `PostgresPreparation.run(snapshot_id)` parses outside database transactions and reuses the
unique committed artifact on redelivery or concurrent execution. No current-result pointer is advanced;
old revisions remain historical. Durable stage orchestration arrives in #51. No AI or new API is added.

### Verified AI plain-text cleaning

Issue #46 adds `PostgresCleaning.run(preparation_id)` using the existing bounded LangChain adapter.
The model returns ordered paragraph IDs and boilerplate IDs, with every source paragraph accounted
for exactly once. The application copies verified paragraphs; it accepts no generated job prose.
Selections must retain source order so headings and caveats cannot acquire different requirements.
Only exact standalone Home, Privacy policy, Cookie policy and Accept cookies labels can be removed
(case-insensitive). Other omitted content, unknown/duplicate references or invented fields invalidate
cleanup. This conservative rule preserves substantive requirements, salary, application instructions
and caveats even when the model misclassifies them. Source language remains unchanged.

Invalid cleanup, timeout, unavailable input/provider or exceeded input limits produce a `degraded`
artifact containing the complete prepared source with its original order. Both accepted and degraded
results retain source-to-cleaned Unicode mappings and hashes. Unknown token usage remains null.
Each execution permits at most two schema attempts under the existing input/output/time limits;
no semantic-validation retry is added. Accounting stores bounded call metadata, never source text.

Immutable artifacts reference their preparation revision and hash; cache identity includes the cleaner
prompt/schema and non-secret model/settings configuration. Reruns reuse committed artifacts, including
degraded fallback; configuration/source changes produce new identities. Concurrent uncached runs can
repeat calls before the unique publication commits (at-least-once delivery), but share one artifact.
Each completed execution's call records are stored separately from the winning artifact's usage list,
so concurrent publication retains both executions' known usage. A crash before publication can still
lose usage evidence; it never establishes zero cost. Model calls hold no database transaction.
Worker orchestration and global budgets are provided by #51/#54;
this issue adds no API, embeddings, metadata extraction or client HTML rendering.

### Deterministic job chunks

Issue #47 adds `ChunkJob` through the narrow `Chunks` application port and `PostgresChunks` adapter.
Default policy limits each chunk to 1,500 Unicode characters and a conservative 6,000-token upper
bound measured as UTF-8 bytes, consistent with the AI adapter; it overlaps 150 characters.
Policies allow 128–2,000 characters, 512–6,000 token-bound bytes and overlap no larger than a quarter
of the character cap or an eighth of the byte cap. The version hashes all policy settings and revision.

Chunks prefer paragraph ends while advancing at least 50 characters between starts. This keeps the
100,000-character source limit within 2,048 chunks. Oversized paragraphs split between Unicode code
points. Each chunk retains exact cleaned offsets plus original paragraph/source spans. Newlines are not
misrepresented as paragraph evidence. Empty content records an empty completed batch. Identity binds
the cleaning revision, policy, index and text hash; identical text at different source locations keeps
its distinct evidence positions. Repeated/concurrent creation does not duplicate these identities.

Batch and chunks publish atomically, with immutable records, uniqueness, foreign keys and database
size/hash constraints. Interrupted publication leaves no incomplete batch. Source/policy changes
create separate batches; retrieval requires both identities. This internal feature adds no embeddings,
vector tuning, API or worker orchestration; those arrive in their subsequent roadmap issues.

### Public job embeddings

Issue #48 adds `EmbedJobs`, the narrow `EmbeddingStore` port and `PostgresEmbeddings`.
`create_embeddings(database, AISettings, revision)` wires the configured provider. The initial
database contract is `vector(1536)`; other configured dimensions fail before provider I/O.
Finite, nonzero float32 vectors are normalized to avoid overflow in cosine distance accumulation.
Provider/model/revision identify separate spaces; change the revision when a provider changes a model.
Each call embeds at most 16 chunks within the AI input bound, with at most eight batches per execution
(configurable 1–128). Remaining work is explicit and resumable. Complete batches and known call usage
publish atomically; failures retain earlier batches, and concurrent writes reuse immutable vectors.
An interruption before publication can lose stage usage evidence; #54 retains durable budget reservations.

Retrieval requires owner, job, cleaning revision and chunk policy. It checks the owner's association
and current snapshot before embedding the needed private query, then checks them again in the exact
cosine-distance query. Results require a complete vector set in the selected model space; ties use
chunk index and UUID. Source/model/policy changes select separate artifacts, preserving historical
evidence. Query text/vectors stay transient, with accounting returned to the caller instead of stored
in public tables. There is no private resume index or ANN index. Worker orchestration remains #51.

Issue [#129](https://github.com/ThibaultLeveau/google_job_hunter/issues/129), an approved prerequisite
to #51, separates `EmbedJobs.execute(pending_chunks)` from publication. It performs at most one
bounded provider call and returns selected chunks, validated vectors, known accounting and a safe
error; it writes nothing. Run it before opening the publication transaction. `EmbedJobs.run` keeps
its existing bounded standalone behavior using this same execution path.

`PostgresChunks.publish(result, transaction=session)` and
`PostgresEmbeddings.publish(space, chunks, vectors, records, transaction=session)` can join an
active caller-owned transaction. They neither commit nor close it; rollback removes all their
writes. Omitting `transaction` preserves standalone publication. The caller must validate its lease
and subject before publishing and enqueue successors in that same transaction; that wiring remains
#51. Real PostgreSQL commit/rollback and failure-accounting tests are in `tests/test_stage_publication.py`.

### Versioned job processing

Issue #51 wires the worker's explicit crawl → text → clean → chunks/embeddings and text → metadata →
requirements handlers. A live lease fences artifact publication and successor enqueue in one transaction.
Task identities include upstream revisions. Startup scans 100 sources per page; each completion schedules
at most two successors and each embedding task makes one bounded call. Committed artifacts are reused.
Pipeline failures retain accounting without caching failed artifacts; explicit queue retries resume missing work.
Existing standalone failed artifacts keep their immutable revision semantics.

`PostgresPipeline.status(session, snapshot_id)` reports independent content/analysis/embeddings readiness,
safe stage states/errors, task timestamps and dependency revisions. Fetch status stays on the public source.
Publication rechecks the current snapshot and a reference through a live user/search and non-cancelled run.
Another live user preserves shared work; otherwise late results fail. Database errors use bounded retries.
Provider I/O holds no transaction. Pre-publication crashes can repeat calls; #54 retains uncertain spend.

With `TEST_DATABASE_URL` configured, run the real-queue fixture end to end:
`uv run --directory backend --locked pytest --require-database tests/test_pipeline.py -q`.
Fixture AI retains source text and returns ambiguous metadata, unknown requirements and deterministic vectors;
production uses configured providers. This issue adds no job API, private matching or frontend timeline.

### Source-verified job metadata

Issue #49 adds `ExtractMetadata` and `PostgresMetadata`, composed by `create_metadata`.
Bounded typed LangChain calls receive every preserved preparation paragraph, including its
identity and global Unicode offsets, in at most 16 sections sized to the configured input bound.
Each section allows one repair, for at most 32 accounted calls per stage. Input sizing includes
the schema, guard, JSON envelope and repair instruction; redundant paragraph hashes are omitted.
Paragraphs remain intact; an oversized paragraph or more than 16 sections fails before any call.
AI-cleaned display text is not a factual input; source text is never silently truncated.
Exact citations and complete-token values are checked against the immutable preparation. Fields
carry known, unknown, ambiguous or conflicting status; multiple locations/skills remain separate.
Incorrect offsets can resolve only to a unique exact quote in the named paragraph of that section.
Evidence and semantic validation run inside the bounded repair attempt; invented or ambiguous
evidence remains invalid. Differing section classifications produce an ambiguous page classification.
All section results must validate before publication; aggregate fact/salary limits still apply.
Work arrangement and geographic eligibility are distinct. Conservative arrangement normalization
retains unknown for unsupported or negated wording; original-language claims and evidence remain.
Salary alternatives preserve source text, explicit amounts, ISO currency, period and gross/net basis.
Missing components stay null; currency symbols and shorthand amounts are not guessed or converted.
Contradictory salary alternatives remain conflicting. Non-job pages publish no listing facts.

Artifacts and known call records publish atomically and are immutable, keyed by preparation plus
model/configuration/prompt/schema digest. Concurrent deliveries retain their known call usage while
reusing one artifact; changed sources/configuration create new artifacts. Failures are explicit and
cached for that revision, with all fields unknown. A crash before publication can lose usage evidence.
Calls run outside database transactions; #54/#51 provide durable budgets and stage orchestration.
This internal stage adds no job endpoint, requirements extraction or derived salary estimates.

### Full application requirements and bounded summaries

Issue #50 adds `ExtractRequirements` and `PostgresRequirements`, composed by `create_requirements`.
Extraction receives every preserved source paragraph in the same bounded sections alongside the
verified metadata classification and source hash, independently of AI cleanup. Each atomic item
keeps exact citations, a stable UUID, full original-language text,
required/preferred/unclear priority and its job-condition/material/instruction/deadline category.
Explicit English priority cues are validated; unsupported or ambiguous wording stays unclear.
Contradictory items remain separate. Missing requirements yield unknown; unavailable or failed
inputs have separate states and never imply eligibility. Calls and response sizes remain bounded.
Missing section context stays unclear. Any failed section rejects the entire result; partial
requirements are never published as complete. The v2 analysis prompts create new artifact revisions;
worker startup schedules missing analysis revisions while preserving existing source snapshots.
Existing exhausted crawls require the normal authorized retry after deploying the fetch fix.

The plain-text summary is at most 600 Unicode characters, prioritizes required items and includes
only complete items. `more_requirements` counts every item absent from that summary. Even if the
first item is too long to display, its full text remains in `items` for detail and matching. Consumers
must render these strings as text. Deadline text is always preserved; only one valid ISO timestamp
with an explicit offset is normalized to UTC. Missing/ambiguous dates or timezones remain unknown.

Immutable artifacts and known call records publish atomically, unique by metadata revision plus
model/configuration/prompt/schema version. Repeated/concurrent delivery reuses artifacts; changes
create new versions. Failed results are cached for that revision; pre-publication crashes can lose
usage evidence. #51/#54 provide stage orchestration and durable budgets; no application is submitted.

### Evidence-backed profile fit

Issue #52 adds `EvaluateMatch`, composed by `create_matcher`, for accepted `ProfileSnapshot`
values and validated, versioned `MatchJob` inputs. The caller supplies owned database snapshots;
this internal evaluator exposes no HTTP endpoint. Job association/current-content checks bracket
provider work. Every requirement and its full citations enter the bounded LangChain call, even
when vectors are incomplete. Top-five job-scoped passages supplement that evidence; only the
retrieval query is shortened (1,800 characters), never requirements or accepted professional facts.
Oversized inputs fail explicitly. Calls allow one query embedding and at most two schema attempts.

The model receives skill names, experience titles and professional descriptions with fact IDs.
Company/project names, URLs, calendar dates, owner identifiers and document provenance are omitted;
tenure must be explicitly described, not inferred from omitted dates. Free text remains untrusted.
The prompt excludes protected traits and treats contradictory experience and unsupported fit
conservatively. Evidence membership is validated; semantic judgments remain model interpretations.
Duplicate/missing requirement evaluations, foreign citations and unsupported profile IDs fail closed.
Matched/partial need profile support; missing/unknown permit empty support without invented facts.

Rubric `explicit-fit-v1` weights required job conditions 2 and preferred/unclear conditions 1.
Matched earns full weight, partial half, missing zero; unknowns leave the assessed denominator.
Materials/instructions/deadlines remain unknown with zero score weight. The result exposes each
component, unknown count/weight and weighted assessed coverage; all-unknown/no-condition results
have a null score. Scores round half-up to two decimals and never establish hiring eligibility.
Profile, source, requirements, chunk, embedding, model, prompt and rubric revisions accompany results.
Known usage records contain no raw inputs. Private persistence, scheduling and publication fencing
are delivered by #53; durable budget reservation is #54. No profile facts are changed.

### Private match artifact storage

Approved prerequisite [#133](https://github.com/ThibaultLeveau/google_job_hunter/issues/133)
adds `PostgresMatches`. Logical identities include owner, accepted-profile revision, requirements,
cleaned content/chunker and evaluator configuration (including model, prompt, rubric and embedding
space). Immutable attempt generations preserve results and the professional facts actually supplied.
Known calls are private, schema-checked records; no raw documents, prompts or private vectors are stored.
Public analysis is referenced and reused. Historical source IDs resolve through the public alias map.

Capture and publication check live ownership/discovery, current source/configuration and profile
revision under short database locks. Provider work runs after those transactions close. Publication
revalidates result identity, citations and deterministic scores, and shares the caller's transaction
with known usage records. Duplicate deliveries reuse one artifact while retaining distinct known calls.
`run` reuses its default generation, including failures; only an explicit internal generation evaluates
again. `get` exposes owned history, including stale attempts, while denying removed associations.
Issue #53 adds scheduling, status selection, task fencing and budgeted user reevaluation below;
the storage service itself provides no endpoint or retry loop. #54 enforces unified worker budgets.

### Private match scheduling

Issue #53 wires `MatchSchedule` into the worker. Public requirements plus chunks becoming ready,
discovery of analyzed jobs, and accepted-profile changes enqueue durable scans in the originating
transaction. Scans visit at most 25 owners/jobs per task using UUID cursors; unique event and
evaluation keys reuse committed work. Startup backfill visits 100 owners per page and accounts
for matching-policy changes. Public analysis stays shared; each evaluation captures owned inputs.

The internal status service distinguishes pending, current, stale history and failed evaluations.
`request(owner, job, key)` reserves one of three reevaluations per owner per rolling 24 hours and
enqueues in the same transaction; exhaustion returns `None`. Repeating an owned idempotency key
returns its original task without another charge, including after a profile change. #54 enforces
provider-wide token reservations. Publication rechecks live ownership, source/profile versions,
latest requested generation and the worker lease; rejected completions cannot replace current
results. Failed evaluations require an explicit budgeted request. Fixture matching reports unknown
fit. Job HTTP endpoints and the explanation interface remain subsequent roadmap issues.

### Owned job listing

Issue #55 adds authenticated `GET /api/v1/jobs`. Only live owned discovery associations
are eligible; duplicate membership across runs returns one canonical job. Cards contain preserved
source/fetch timestamps and status, current-version analysis status, title/company/location/region
facts with explicit certainty, remote mode, quoted salary values and private current/stale/pending/
failed match summaries. Historical scores have `stale=true`, including after a failed refresh;
missing scores are null, never zero. Stored requirement failures/unavailability remain explicit.
GET performs no model calls, matching or task enqueue, and returns no detail evidence or private facts.

`limit` is 1–50 (default 20). `sort` supports `newest`, `oldest`, `title` and `match`, with UUID
tie-breaks and unknown values last. Discovery sorting uses the first eligible owned discovery;
match sorting ranks only current known scores. Return `next_cursor` unchanged with the same owner,
sort, filters and limit; mismatched/malformed cursors return 422. Pages reflect live data, not a
frozen snapshot, so changes to metadata, associations or matches can change later ordering.

Filters are `run_id`, literal case-insensitive `keyword` over known title/employer/skills/location/
region facts, exact case-insensitive `region`, `remote` (`remote`, `hybrid`, `on_site`) and
`match_status` (`current`, `stale`, `pending`, `failed`, or `unknown` for a null score).
`include_unknown_region`, `include_unknown_remote` and `include_unknown_salary` default to false
when those filters apply. Search-origin and language hints never substitute for factual filters.
Salary filters require explicit `currency` and `period` together, with optional `salary_min` and
`salary_max`: the recorded minimum must meet the floor and the recorded maximum must meet the ceiling.
Missing bounds remain unknown; include-unknown can retain them, but known incompatible units are
always excluded. No currency, period, gross/net or missing-bound conversion is inferred.

Listing uses one SQL statement after authentication, plus one ownership check when filtering a run;
ownership and filters precede pagination. Existing association/revision indexes and migration
`0031_job_listing` index owned match history and canonical alias lookup. Foreign/deleted/cancelled
run IDs return 404; invalid filters return 422 and unavailable storage returns a safe 503.
Job query strings are removed from the transport scope used by application-server access logs;
configure upstream proxies to omit query strings as well.

### Jobs page

`/jobs` shows owner-scoped discoveries, 20 per page, newest first. Cards retain long
job titles and present company, location, work arrangement, salary units, source
fetch time and match availability. Unknown facts stay explicit; zero values remain
visible. Salaries are not converted or annualized. Earlier evaluations carry a
stale label and display assessed requirement weight separately from match score.

Backend cursors remain in the URL. Previous-page navigation uses cursor history;
a directly opened page can return to the first page. Same-owner cached pages and
scroll positions support browser return; account changes discard private results,
positions and pending requests. Loading skeletons, empty pages, expired cursors,
refresh failures and retries have explicit states. Empty lists can be refreshed.

Title links open `/jobs/{job_id}` with the filtered list URL and cursor history.
The detail page's back link restores that context and the saved list position.
The original posting link opens the public source in a new tab.
The filter area supports keyword, owned search run (including older runs), exact
region, work arrangement, match status and salary comparisons. Sort by newest,
oldest, title or match. Filters use validated URL parameters; changes reset the
cursor, while deep links and browser back restore selections. Text waits 300 ms
before requesting results; obsolete reads are cancelled. Clear all resets filters
and pagination. Filtering runs on the server across all owned discoveries.

Salary comparisons require both a three-letter currency and a period. Bounds retain
up to four decimal places, including zero, without conversion. Separate toggles
include unknown region, work arrangement or salary data when that filter is active;
known incompatible salary units remain excluded. Invalid links and incomplete salary
filters show associated errors. Empty filtered results offer a return to all discoveries.

At mobile widths, filters open in a modal drawer with contained keyboard focus,
Escape/close handling and focus restored to its trigger. Changes apply automatically;
Show results applies pending valid edits immediately. Closing discards text that has
not yet applied. Run choices and result caches remain scoped to the signed-in account.

Validation uses a real API, worker and isolated PostgreSQL database with 21 fixture
discoveries, factual and unknown metadata, and stale private matches. The keyboard
pass covers skip-to-main, Tab to refresh and row links, Enter activation, pagination,
back navigation and recovery from an expired cursor. Layouts are checked at
320/768/1440px and 100%/200% text size, with reduced motion and accessibility scans.
Representative [desktop](docs/validation/issue-57-desktop.jpg) and
[mobile](docs/validation/issue-57-mobile.jpg) captures show the list and a long title.

Filter validation repeats the real API/worker/PostgreSQL journey with 21 discoveries,
checking owned run selection, exact region/remote values, salary units and unknown
toggles, match status, sorting, pagination reset, back navigation and deep-link reload.
Invalid filters, empty results and all six viewport/text-size combinations pass
accessibility scans. Keyboard checks cover every drawer control without footer occlusion, focus wrapping,
Escape and trigger restoration. Reviewed [desktop filters](docs/validation/issue-58-desktop.jpg) and
[mobile drawer](docs/validation/issue-58-mobile.jpg) captures record the layout.

### Job details and retry actions

The dedicated detail route shows factual title/company, location and region, work
arrangement, salary units, employment type, seniority, skills, geographic eligibility
and requested languages. Missing facts remain unknown. Source language and fetch time
stay visible, alongside notices for incomplete, failed, stale, removed or unsupported
content. Terminal unavailable content is not presented as pending processing.

A concise application checklist comes from the preserved requirement summary. Its
full disclosure groups required, preferred and unclear items, distinguishes job
requirements, materials, instructions and deadlines, and provides source excerpts.
Explicit deadline timestamps are retained; ambiguous dates are not inferred. Separate
disclosures expose factual evidence, prepared text and original source paragraphs.
All external/source/model text renders literally, with safe original-posting links.

Deep links require an authenticated, verified session and an owned discovery. Invalid
or inaccessible IDs show a safe unavailable state; loading, failed refresh and retry
states retain a useful route back. Queries are cancelled and private caches cleared
on account changes. The profile-match section separates evaluated evidence from public
job facts; processing status keeps available content usable while other stages finish.

Match explanations show server-calculated scores, weighted evidence coverage and
matched/partial/missing/unknown assessments, with each score contribution when
meaningful. Unknown evidence is not a confirmed gap, and a score does not establish
eligibility. Links reveal the exact accepted professional facts and preserved source
excerpts used by that evaluation; newer facts are never substituted for old evidence.
The detail API's `match_context` reads those immutable snapshots and exposes only cited
professional profile facts. It identifies profile revision changes without inferring
why other evidence became stale. Inaccessible owners cannot read the context.

Outdated results remain explicitly labeled while a replacement is pending or failed.
Reevaluation uses the existing owner-scoped allowance and provider budgets, blocks
duplicate submission and reuses the request key after uncertain delivery. Accepted
requests mark the prior result outdated immediately; refresh checks completion and
remaining allowance. Account changes abort requests and discard private match caches.

The processing timeline shows posting download, readable/prepared text, evidence
sections/search, job facts and application requirements with independent readiness.
Safe messages distinguish unsupported or denied sources, quota, temporary failures
and partly ready evidence. Search and document pages reuse the processing state and
failure language. Provider payloads, task IDs, revisions and other owners' activity
are never displayed.

Only active attempts or pending eligible evaluations poll the compact status endpoint
every five seconds while visible. Terminal states, failed reads, unmount and account
changes stop polling; manual refresh recovers a failed read. Status changes refresh
the owned detail/list/run views. Retry controls require the API's retryable flag and
remaining allowance, send its stage/version/generation fence, block duplicate clicks
and reuse a request key after uncertain delivery. Shared retries read the current
attempt after admission, including when an earlier receipt is already complete.

Issue #61 browser validation used the real API, worker and isolated PostgreSQL with
deterministic providers. Active/hidden/terminal polling, partial and degraded content,
denied sources, retry recovery and two-owner isolation passed. Tab reached the retry
control and repeated Enter submitted one request. At 320/768/1440 px and 100%/200% text,
there was no horizontal overflow or axe violation. Captures:
[desktop](docs/validation/issue-61-desktop.jpg) and
[mobile](docs/validation/issue-61-mobile.jpg).

Issue #60 browser validation used the real API, worker and isolated PostgreSQL with
deterministic providers. Current/unknown scores, profile-change staleness, historical
evidence links, reevaluation, failed replacement, exhausted allowance and second-owner
denial passed. Keyboard activation revealed and focused the cited profile/source
evidence. At 320/768/1440px and 100%/200% text size, the covered layouts had no overflow
or axe violations. Reviewed [desktop matches](docs/validation/issue-60-desktop.jpg)
and [mobile matches](docs/validation/issue-60-mobile.jpg) show the outdated-result label.

Issue #59 browser validation used Chromium with the real API, worker and isolated
PostgreSQL database, with deterministic external adapters. Job facts, checklist and
evidence disclosures, removed/unknown content, stale match availability, filtered-list
return/scroll restoration and second-account denial passed. Keyboard checks covered
Enter/Space disclosure activation. At 320/768/1440px and 100%/200% text size, covered
states had no horizontal overflow or axe violations. Reviewed
[desktop details](docs/validation/issue-59-desktop.jpg) and
[mobile details](docs/validation/issue-59-mobile.jpg) show the factual reading layout.

`GET /api/v1/jobs/{job_id}` returns preserved source paragraphs, clean text,
metadata citations, requirements and the signed-in owner's match. Artifact IDs,
source hashes and paragraph IDs identify the evidence revision. Missing artifacts
remain null; readiness flags are independent. Removed sources have explicit
`removed` freshness and evidence only when available for that revision. Previous
matches remain visible with a stale flag when source or profile revisions change.

`GET /api/v1/jobs/{job_id}/status` returns the compact stage timeline, safe reason
codes, timestamps and remaining retry allowances. It excludes provider prompts,
task payloads and other owners' activity. Inaccessible jobs return 404.

Verified users may POST `/retry` under the job URL with `stage`, the status entry's
`revision` and `expected_generation`, and a UUID `idempotency_key`. Retries reopen
failed tasks atomically; simultaneous owners share the same reopened attempt.
Unsupported terminal stages cannot be retried. Admission allows five requests per
owner over a rolling 24 hours. POST `/reevaluate` with a UUID `idempotency_key`
uses the existing private matching limit of three requests per rolling 24 hours.
Both actions require the session's CSRF token, return 202 on admission, 409 for a
stale or ineligible request, and 429 when the allowance is exhausted. Repeating an
admitted key returns its original generation without spending another allowance.

### Provider budget ledger

Approved prerequisite [#136](https://github.com/ThibaultLeveau/google_job_hunter/issues/136)
adds `PostgresBudgets` and migration `0030_provider_budgets`. Limits use UTC calendar days
and integer search requests or combined AI/embedding tokens, with no currency conversion.
`JOB_HUNTER_BUDGET_SEARCH` and `JOB_HUNTER_BUDGET_TOKENS` accept JSON objects containing
`global_daily`, `owner_daily` and `run_daily` nonnegative integers (maximum 1,000,000,000,000).
Defaults are respectively 100/20/10 requests and 1,000,000/100,000/50,000 tokens; zero disables
new reservations in that scope. `JOB_HUNTER_BUDGET_RESERVATION_SECONDS` defaults to 300 (1–3600).

`reserve` commits a unique call ID and its upper bound across global, optional live owner and
owned search-run counters in a consistent lock order. A quota failure rolls back every scope;
a duplicate call ID cannot authorize another dispatch. Only dispatch after a successful return.
`settle` reconciles verified usage once against the original day; missing/invalid usage cannot
release capacity. `expire` processes bounded pages, marking abandoned reservations unknown
while retaining their full charge. Late verified usage may still reduce that original charge.
`availability` is an advisory minimum across applicable scopes with a UTC reset time; only the
atomic reservation decides admission. Internal records contain counters/IDs, not provider payloads.
Owner/run deletion anonymizes reservation references while preserving global accounting.

Issue #54 enforces these reservations in worker search, profile extraction, public analysis and
private matching handlers. Each search page reserves one request before dispatch; each AI call,
including a repair or embedding batch, reserves its conservative input plus maximum output tokens.
Known usage refunds unused capacity; timeouts, crashes and missing usage retain the full ceiling.
Workers expire abandoned reservations in bounded batches without releasing uncertain spend.
Provider calls hold no database transaction; retries obtain fresh reservations and stay within
existing stage/task attempt limits. A database/accounting failure prevents further dispatch.

Search charges the authenticated run's owner, run and global daily counters. Profile extraction
charges its document owner and the global counter. Private matching charges its owner, global
counter and oldest live owned run associated with the canonical job (creation time, then UUID).
Shared public-job analysis charges only the global counter, independent of any private initiator.
Reused public artifacts make no model calls and incur no additional token charge. This policy does
not store private initiators or profile/search text on public artifacts or in budget records.

Authenticated `GET /api/v1/searches/provider` includes advisory `quota.search_remaining`,
`quota.tokens_remaining` and UTC `quota.resets_at` for the session owner and global limits.
Zero means exhausted; a particular run or a call's upper bound can still deny admission earlier.
Quota denial uses the existing safe `quota` failure code; unavailable accounting returns a safe
503 response. Existing provider availability, usage, task timing/retry and redacted logging fields
remain in use. Disabled/unconfigured providers make no external calls or budget reservations.
Configured paid providers reject calls outside a worker charge scope. Fixtures obey budgets inside
workers; isolated adapter tests can still use fixtures without a scope. API and worker processes
must use the same budget settings. Raising a limit does not erase previously charged usage.

### Search provider

Issue #37 adds the synchronous `SearchProvider` port, SerpApi adapter and synthetic JSON fixtures.
Authenticated `GET /api/v1/searches/provider` reports disabled/unconfigured/configured/fixture status,
supported parameters and effective page/result bounds without making a provider request.
Set server-only `JOB_HUNTER_SEARCH_PROVIDER=serpapi` and `JOB_HUNTER_SEARCH_API_KEY` to configure it;
the default is disabled. Missing/blank credentials report unconfigured. Invalid settings stop startup
with a redacted error. `PROVIDER=fixture` requires `JOB_HUNTER_SEARCH_ENVIRONMENT=development` or `test`.

Other `JOB_HUNTER_SEARCH_` settings are `BASE_URL` (trusted HTTPS `/search.json`, default
`https://serpapi.com/search.json`), `TIMEOUT_SECONDS` (0.01–30, default 10), `MAX_PAGES` (1–10, default 5),
`MAX_RESULTS` (1–100, default 50) and `MAX_RESPONSE_BYTES` (1,024–2,097,152, default 1,048,576).
Only compiled `q`, `lr`, `gl`, `location`, `as_qdr` and adapter-owned Google/JSON/pagination parameters
are sent. [Google organic pagination](https://serpapi.com/blog/filtering-google-search-and-google-news-results/)
uses up to ten results per call and `start` offsets; the obsolete `num` parameter is omitted.
Zero-based page indexes and result slots are capped; next-page URLs are validated but never followed
or returned. The adapter makes one request per fetch, with no redirects, environment proxies or retries,
an overall deadline and a bounded, uncompressed JSON response. #39/#54 add orchestration and budgets.

Results retain title, optional snippet, source URL and absolute rank; snippets are unverified text.
Malformed entries and credential-bearing URLs are skipped. Missing organic results with successful
metadata produce an empty page; [auth/quota/rate-limit errors](https://serpapi.com/api-status-and-error-codes),
timeouts and malformed/provider failures remain distinct errors. Logs contain sanitized request IDs
and request/received/returned/skipped counters only; raw provider metadata and HTTP diagnostics for
these calls are suppressed. Fixtures contain no credentials and count zero external requests.
Run `uv run --locked pytest --require-database tests/test_search_provider.py` from `backend/` for
transport, pagination, privacy, disabled-state and authenticated-capability tests. No live paid call,
Google HTML scraping, crawling or search-worker registration is introduced by this issue.

### Public job registry

Issue #38 adds canonicalization version 1 and a PostgreSQL source registry. HTTP(S) URLs are limited
to 2,048 UTF-8 bytes before/after normalization. Scheme/host case, IDNA host names, IPv6 spelling and
default ports normalize; fragments and the explicit `utm_source`, `utm_medium`, `utm_campaign`,
`utm_term`, `utm_content`, `utm_id`, `gclid`, `dclid`, `fbclid`, `msclkid` tracking keys are removed.
An absent path becomes `/`. Meaningful path/query text, encoding, parameter order, repeated keys,
blank values and an explicitly empty query remain distinct. Credentials, invalid escapes, controls,
whitespace and malformed hosts/ports are rejected. No DNS lookup or connection safety claim is made.
[URI component rules](https://www.rfc-editor.org/rfc/rfc3986.html) guide parsing;
[IDNA 2008](https://pypi.org/project/idna/) with nontransitional UTS #46 avoids merging `ß` with `ss`.

`PostgresJobRegistry` uses the caller's transaction. Registration locks and authorizes the active
run, upserts the global canonical URL, and attaches private provenance atomically. Both primary and
alias URLs occupy `job_urls`' single unique namespace; a deferred composite foreign key requires
every source's primary URL to belong to that same source. Racing equivalent discoveries create one
source and separate owner/run associations; a rollback leaves no candidate source or association.
Each `(run_id, source_id)` retains its first observed original URL, rank and UTC timestamp. Replays
preserve that observation. Public records contain no search terms, owner IDs, snippets or matches.

Internal alias insertion requires caller-established exact identity; it never merges sources and
rejects a key already assigned elsewhere. Registration alone never infers aliases between distinct
canonical URLs. Registry reads require the owner's run association; UUID knowledge grants no access.
Deleting an owner cascades private associations while preserving public sources for reuse.
`tests/test_job_urls.py` and `tests/test_job_registry.py` cover canonicalization, privacy, forced
concurrent inserts, namespace constraints, rollback and migration downgrade/upgrade with PostgreSQL.
Discovery execution/enqueue (#39), SSRF-safe fetching (#43), fuzzy deduplication and jobs APIs are
outside this issue; there is no new HTTP endpoint or external call.

### Discovery execution

Issue #39 registers `search_discovery` with the existing worker command. Each task handles one
provider page from the immutable run snapshot. The run's `discovery` object exposes committed
`pages_completed`, frozen `max_pages`/`max_results`, conservative `provider_requests`, sanitized
`last_request_id`, `error_code` and `limited`. Requests are reserved before external I/O, so the
count includes uncertain outcomes after crashes; fixtures reserve zero external requests.
No database lock is held during provider I/O. Publication fences the lease, locks/rechecks the
active run and account tombstone, and commits sources, private associations, counters, new-source
`job_crawl` tasks and the next page task together. Cancellation fences pending/running page tasks
and blocks late publication; already committed public sources and completed pages remain reusable.

Page checkpoints survive restarts. A crash before commit can repeat that provider page, while
unique source/association/task keys prevent duplicate stored work. At most three automatic attempts
per page and ten pages are allowed; ordinary provider errors terminate the run without hidden retries.
Missing/disabled configuration reports `unconfigured`/`disabled`; other safe provider codes remain
distinct. Failure after a committed page reports `partial`; an exhausted worker task is projected as
`worker_failure` through run APIs. Starting a new run requires a new idempotency key.

Counters count returned slots, new/known URLs and skipped entries; repeated known URLs do not add
duplicate associations. `processing_failed` counts already-failed crawl tasks encountered when new
associations are made. Existing pending/completed/failed source work is preserved without automatic
retry. Discovery completion does not mean job analysis is complete; crawl execution arrives in #43.
Run `uv run --locked pytest --require-database tests/test_discovery.py tests/test_saved_searches.py`
from `backend/` for real-PostgreSQL API/worker checks, overlap/reuse, caps, crash/rollback recovery,
partial failures, cancellation and account deletion during a blocked fixture request. Tests also
exercise registered worker shutdown/draining; no live paid call or new interface is part of this issue.

| Available since | Working directory | Command contract |
| --- | --- | --- |
| [#20](https://github.com/ThibaultLeveau/google_job_hunter/issues/20) | `frontend/` | `npm run generate:api` exports deterministic OpenAPI and generates ignored declarations before checks/build/tests |

Normal CI uses deterministic provider fixtures and requires no Google, mail, SerpApi, LLM or embedding secret.
Real PostgreSQL is required when database behavior is tested; browser journeys later use the real API/worker.
Live-provider credentials, public URL, retention and budgets are operator settings detailed in
[the architecture](docs/architecture.md#operator-settings-and-fixture-contract).
The backend bootstrap makes no paid calls and performs no deployment.

## Deterministic test stack

Start local PostgreSQL/pgvector first and export `TEST_DATABASE_URL` as above. The database
role needs `CREATE DATABASE`; only loopback hosts are accepted. From the repository root:

```bash
uv run --directory backend --locked python tests/stack_harness.py start --mode test --state /tmp/job-hunter-test.json
```

The foreground command creates a fresh database, migrates it, seeds Alice and Bob, then starts
the real API and worker as separate processes. Readiness produces private JSON containing the
API URL, synthetic credentials and storage path. Use a new state path for each simultaneous run.
Ctrl-C tears down both processes, its database and private storage, including on startup failure.
For automation, use the same command with `stop --state /tmp/job-hunter-test.json`; `seed` is
idempotent and `reset --scenario success|duplicate|transient|owner-isolation` restarts a clean stack.
These actions require the same `--state` argument. Optional `--port` fixes the API port; otherwise
it is allocated dynamically. `--frontend-url` defaults to `http://127.0.0.1:5173`.

Search for the single keyword `Python` without filters, or use `(Python OR Go) AND remote`
with country `fr`, language `en` and domain `jobs.example.org`. Duplicate returns the same canonical job
on two pages; transient returns HTTP 503 on the first crawl before durable retry succeeds. Each scenario
seeds two verified owners for isolation assertions. Crawl/chat/vector fixtures produce a tiny
synthetic job and versioned artifacts; profile extraction accepts the source text from
`backend/tests/test_extraction.py`. Unconfigured provider requests and external sockets fail.
The real database, ownership checks, queue, leases and validators remain active.

`mail --state PATH` reads observed links from private files; `authorize --state PATH --url URL`
registers the real Google authorization URL and returns a synthetic callback for the same browser.
This exercises nonce, PKCE and signed tokens without an external account. Observation never uses
an HTTP bypass route. Files use mode 0600 in private temporary storage; teardown deletes them.
Composition requires explicit test mode and rejects inherited production provider settings.
Helpers live outside the packaged application; no production switch enables this harness.
Run its smoke and lifecycle tests with `uv run --directory backend --locked pytest
tests/test_stack_harness.py --require-database`.

## Browser checks

The [accessibility audit and keyboard record](docs/accessibility.md) covers the core screens,
responsive text, focus, contrast and review screenshots retained by Browser CI.

With local PostgreSQL/pgvector running and `TEST_DATABASE_URL` exported, install the locked
backend/frontend dependencies and run from `frontend/`:

```bash
uv sync --directory ../backend --locked
npm ci
npx playwright install --with-deps chromium
npm run test:e2e
npm run test:e2e:guards
```

The first check builds the production UI and runs Chromium journeys with the real API, worker,
queue and database. Playwright starts #66's harness and a local preview proxy,
waits for readiness, resets private test data before each test and tears down the stack afterward.
It never reuses a running server. `E2E_PORT` defaults to 4175; simultaneous runs need distinct
ports. Each run generates a private state path; `E2E_STATE` can specify a new path explicitly.
No provider credentials or alternate application routes are involved. Accessibility stress cases
explicitly transform real responses to exercise long titles and absent metadata; acceptance
journeys otherwise use the real API.

The five guard probes deliberately fail a direct request and external HTTPS/HTTP redirects, fail after readiness, and supply
production provider settings. They pass only when the expected failure is reported; the browser
probes also verify that the API, temporary files and isolated database disappear. All browser
HTTP requests must remain on the local frontend/API origins; unexpected requests, WebSockets and page errors
fail even if the UI catches them. A local allowlist proxy also blocks redirect chains, which
Playwright's route handler does not intercept after the first request. Service workers are blocked.
Future journeys reuse the private `control` helper and the existing scenario option. The Google
journey fetches the real OAuth-start response without following redirects, then passes its authorization
URL through the private signed-identity fixture and returns to the real backend callback.
No request reaches Google; state, nonce, PKCE, token validation and session creation remain real.

Issue #68 covers registration and email verification, password sign-in/protected navigation/sign-out,
password recovery, actual server-side session expiry, and synthetic Google sign-in. Profile journeys
upload the existing tiny multilingual resume fixture, wait for durable parsing/extraction, inspect
source evidence and unknown dates, accept candidates and edit an accepted fact. Identical re-upload
and normalized skill insertion verify deduplication through visible outcomes, API IDs and owner-scoped
database IDs/counts. Separate journeys cover stale-review reload with retained selections and malformed
TXT rejection. Each journey starts with reset data; Playwright retries remain disabled.

Private harness commands `resume`, `expire --owner UUID`, and `dedup --owner UUID` supply the existing
resume text, expire a seeded owner's sessions, and inspect deduplication artifacts respectively.
They use the private state-file command channel, accept only seeded owners for stateful operations,
and expose no HTTP routes or production configuration. The guard probes run only `smoke.pw.ts`;
full journey repetition uses two separate `npm run test:e2e` invocations to verify clean startup.

Issue #69 follows an accepted Python skill through AND/OR discovery, crawl, preparation, cleanup,
metadata, requirements, chunks, embeddings and a cited match. Database assertions check versioned
artifact identities, unique durable task keys and one current match per owner/job. Duplicate provider
pages, repeated runs and another owner's discovery reuse the public artifact chain. Browser checks
cover source/profile evidence links, filter reload/back navigation, and a profile edit that shows the
earlier match as stale before the real worker publishes a new revision. A separate crawl-503 scenario
verifies two attempts on the same durable task and one resulting artifact chain. The second owner
cannot read the first owner's profile fact, search, run or unassociated job; after discovery the shared
job has an independent unknown match for that owner's empty profile.

Private commands `jobs --owner UUID`, `pause-worker` and `resume-worker` inspect synthetic artifact
IDs/versions and stop/restart only the harness worker. Pausing makes the stale-result assertion
deterministic while the real API and durable queue remain active; tests always resume it afterward.
These commands share the private state-file channel and add no application endpoints. All journeys
use bounded state polling without fixed browser sleeps; the suite has a ten-minute global deadline.

`Browser quality` runs the checks in unprivileged GitHub Actions with a disposable pgvector
service, pinned actions and bounded startup/test/shutdown deadlines. Its Ubuntu runner already
supplies Chromium libraries, so CI downloads the pinned browser without system-package upgrades.
Failures retain traces,
screenshots and reports under `frontend/reports/browser/` for three days. Production builds include
only application assets; the harness and orchestration remain outside the browser bundle.
See [Playwright's test practices](https://playwright.dev/docs/best-practices) and
[web-server lifecycle](https://playwright.dev/docs/test-webserver) for the runner contracts.

## Synthetic AI regression evaluations

From `backend/`, run `uv run --locked python -m scripts.evaluate_ai` for the six versioned
cases in `evaluations/synthetic-v1.json`, or `uv run --locked pytest tests/test_ai_evaluations.py`
for the ordinary CI regressions and deliberate failures. No credentials or network are needed.
Fixtures and independent labels cover multilingual resumes, uncertain dates, duplicate facts,
prompt injection, missing metadata, complete application requirements and matching unknowns.
These checks measure deterministic application behavior; they do not measure live model quality.

Every applicable metric must equal 1.0; otherwise the command exits 1. Schema validity and evidence
support use the production schemas/validators. Mandatory-item recall compares unique labelled
clauses with retained/extracted clauses; deduplication checks expected counts and retained evidence.
Labels check facts, ambiguity, priorities and unknowns. The rubric checks percentages, coverage,
counts and each weighted component independently of generated rationale prose. Fabricated citations,
omitted mandatory requirements and an incorrect score deliberately fail the test evaluator.
Matching uses complete fixed source evidence and accepted facts with the production prompt,
validator and aggregation; retrieval and database publication are covered by the real-stack journeys.

Live evaluation is a separate manual opt-in. Configure the server-side `JOB_HUNTER_AI_` OpenAI
settings described above, verify the selected model's current input/output USD-per-million-token
rates, and supply them explicitly. For example, with both rate variables set by the operator:

```bash
uv run --locked python -m scripts.evaluate_ai --live --allow-paid \
  --max-requests 12 --max-cost-usd 0.25 \
  --input-usd-per-million "${MODEL_INPUT_USD_PER_MILLION:?Set the current input rate}" \
  --output-usd-per-million "${MODEL_OUTPUT_USD_PER_MILLION:?Set the current output rate}"
```

Live mode requires all caps, permits at most 32 requests and USD 5, and reserves worst-case bounded
input/output token cost before every attempt, including schema repair. Reservations are never refunded;
failure or unknown usage cannot free capacity. Cost estimates depend on the supplied rates. Tests use
a fake adapter to prove exhaustion prevents calls; normal CI never runs a paid provider.
Reports contain dataset version/hash, model, prompt/schema versions/hashes, metrics and call accounting.
They default to ignored `backend/reports/evaluations/` files with private permissions; `--output` requires
a new path. Live results stay outside commits unless deliberately reviewed. Prompt, schema, rubric or
model changes require rerunning these checks, reviewing failed labels, and a separately authorized,
budgeted live comparison when live behavior is being assessed. Never silently relabel a regression.

## Single-host container runtime

[Dockerfile](Dockerfile) pins Python, uv, Node and nginx bases by digest and installs the
existing lockfiles. The `backend` target contains runtime dependencies and migrations;
`web` contains the compiled frontend and [same-origin proxy](deploy/nginx.conf).
No build argument accepts credentials. The allowlisted build context excludes environment
files, private data, local dependencies and generated reports. API/worker run as UID/GID
10001; nginx runs as 101. Only API/worker mount the mode-0700 private document volume.

Use Docker Engine and Compose v2 with [compose.runtime.yaml](compose.runtime.yaml), separately
from the development database in `compose.yaml`. From the repository root:

```bash
cp .env.example .env.runtime
chmod 600 .env.runtime
# Edit all placeholders before continuing; never commit this file.
docker compose --env-file .env.runtime -f compose.runtime.yaml build api web
docker compose --env-file .env.runtime -f compose.runtime.yaml up -d --wait database
docker compose --env-file .env.runtime -f compose.runtime.yaml run --rm migrate
docker compose --env-file .env.runtime -f compose.runtime.yaml up -d --wait api worker web
docker compose --env-file .env.runtime -f compose.runtime.yaml stop web api worker database
```

Set the PostgreSQL password and the same URL-encoded password in its DSN, the canonical
HTTPS origin/trusted hostname, SMTP settings and a Fernet mail key. Keep `127.0.0.1` in
trusted hosts for internal health checks. See the existing provider sections for Google,
SerpApi, AI models and budgets. Disabled search/AI providers permit startup without paid
calls; SMTP delivery still requires valid operator settings. Production commands always
reject development/test modes and fixture providers. Settings enter backend containers
at runtime through the private env file; Docker administrators can inspect that environment.
Never publish `docker compose config` output containing resolved settings.

The proxy binds only `127.0.0.1:8080` (`RUNTIME_PORT` overrides the port). Terminate HTTPS
at a host reverse proxy, preserve the original Host, and forward to that loopback port.
HTTP alone is insufficient for browser sessions: cookies remain Secure. API and PostgreSQL
have no published ports. Forwarded client headers are not trusted. Proxy access logs and
API access logs are disabled to avoid private URL/token logging; process status and backend
structured operational logs remain available. Frontend deep links fall back to index.html.

Run migrations exactly once, serially, before starting upgraded API/worker processes; they
never migrate themselves. Their startup and container health probes require the exact
Alembic head. `/api/health/live` measures API process liveness; `/api/health/ready` checks
database connectivity. The worker probe checks its PID-1 process and database schema; it
does not establish task throughput or provider availability. Monitor queue age separately.
SIGTERM reaches the processes directly; bounded shutdown drains active work, with durable
lease recovery after forced termination. Compose bounds memory, CPU, PIDs and temporary
storage and makes application root filesystems read-only. Named volumes survive restart
and ordinary `down`; `down --volumes` destroys data and is only for disposable tests.

This supports one host with POSIX locks and persistent local storage. Replicas on another
host cannot share these document paths; multi-host scaling requires a separately designed
storage adapter. Use the [operations runbook](docs/operations.md) for consistent encrypted
backup/restore, provider setup, migrations, incident recovery and secret rotation.

`Container quality` builds all targets, checks production startup/health, same-origin routes,
private volume persistence, graceful shutdown, and image history/static files for a synthetic
credential canary. It then runs the existing real PostgreSQL/API/worker pipeline scenarios
in the explicitly separate `fixture-test` image, whose adapters deny external traffic.
The test creates a uniquely named Compose project and removes only its own volumes/images.
Run the same check locally (Docker required; no real credentials or paid calls):

```bash
RUN_CONTAINER_TESTS=1 uv run --directory backend --locked pytest tests/test_runtime.py -k container_runtime -q --tb=short
```

## Release quality gates

The [release checklist and regression budgets](docs/release.md) record coverage exclusions,
authorization/queue evidence, bounded listings and worker inputs, deliberate failure probes and
remaining product/provider limitations. `npm run test:guards` verifies the frontend coverage and
network guards; backend gate probes run in the ordinary pytest suite.

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
| 44 | [#44 Crawl public pages with robots policy, host pacing, and snapshots](https://github.com/ThibaultLeveau/google_job_hunter/issues/44) | Crawling | [#43](https://github.com/ThibaultLeveau/google_job_hunter/issues/43), [#39](https://github.com/ThibaultLeveau/google_job_hunter/issues/39), authorized prerequisites [#118](https://github.com/ThibaultLeveau/google_job_hunter/issues/118) then [#119](https://github.com/ThibaultLeveau/google_job_hunter/issues/119) |
| 45 | [#45 Convert fetched HTML into bounded plain text with provenance](https://github.com/ThibaultLeveau/google_job_hunter/issues/45) | Crawling | [#44](https://github.com/ThibaultLeveau/google_job_hunter/issues/44) |
| 46 | [#46 Use LangChain to select and organize faithful plain-text job content](https://github.com/ThibaultLeveau/google_job_hunter/issues/46) | AI job processing | [#45](https://github.com/ThibaultLeveau/google_job_hunter/issues/45), [#27](https://github.com/ThibaultLeveau/google_job_hunter/issues/27) |
| 47 | [#47 Chunk cleaned job text deterministically with source offsets](https://github.com/ThibaultLeveau/google_job_hunter/issues/47) | AI job processing | [#46](https://github.com/ThibaultLeveau/google_job_hunter/issues/46), [#8](https://github.com/ThibaultLeveau/google_job_hunter/issues/8) |
| 48 | [#48 Embed job chunks and retrieve evidence through pgvector](https://github.com/ThibaultLeveau/google_job_hunter/issues/48) | AI job processing | [#47](https://github.com/ThibaultLeveau/google_job_hunter/issues/47), [#27](https://github.com/ThibaultLeveau/google_job_hunter/issues/27) |
| 49 | [#49 Extract evidence-backed job metadata with explicit unknown values](https://github.com/ThibaultLeveau/google_job_hunter/issues/49) | AI job processing | [#45](https://github.com/ThibaultLeveau/google_job_hunter/issues/45), [#46](https://github.com/ThibaultLeveau/google_job_hunter/issues/46), [#27](https://github.com/ThibaultLeveau/google_job_hunter/issues/27) |
| 50 | [#50 Extract a concise application-requirements list with source evidence](https://github.com/ThibaultLeveau/google_job_hunter/issues/50) | AI job processing | [#49](https://github.com/ThibaultLeveau/google_job_hunter/issues/49) |
| 51 | [#51 Connect versioned crawl, analysis, and embedding stages](https://github.com/ThibaultLeveau/google_job_hunter/issues/51) | AI job processing | [#11](https://github.com/ThibaultLeveau/google_job_hunter/issues/11), [#48](https://github.com/ThibaultLeveau/google_job_hunter/issues/48), [#50](https://github.com/ThibaultLeveau/google_job_hunter/issues/50), approved prerequisite [#129](https://github.com/ThibaultLeveau/google_job_hunter/issues/129) |
| 52 | [#52 Evaluate profile fit against explicit job requirements](https://github.com/ThibaultLeveau/google_job_hunter/issues/52) | Personalized matching | [#29](https://github.com/ThibaultLeveau/google_job_hunter/issues/29), [#48](https://github.com/ThibaultLeveau/google_job_hunter/issues/48), [#50](https://github.com/ThibaultLeveau/google_job_hunter/issues/50), [#27](https://github.com/ThibaultLeveau/google_job_hunter/issues/27) |
| 53 | [#53 Persist private matches and recompute stale evaluations safely](https://github.com/ThibaultLeveau/google_job_hunter/issues/53) | Personalized matching | [#52](https://github.com/ThibaultLeveau/google_job_hunter/issues/52), [#51](https://github.com/ThibaultLeveau/google_job_hunter/issues/51), [#39](https://github.com/ThibaultLeveau/google_job_hunter/issues/39), approved prerequisite [#133](https://github.com/ThibaultLeveau/google_job_hunter/issues/133) |
| 54 | [#54 Enforce atomic per-user and global provider budgets](https://github.com/ThibaultLeveau/google_job_hunter/issues/54) | Operations and privacy | [#27](https://github.com/ThibaultLeveau/google_job_hunter/issues/27), [#39](https://github.com/ThibaultLeveau/google_job_hunter/issues/39), [#51](https://github.com/ThibaultLeveau/google_job_hunter/issues/51), [#53](https://github.com/ThibaultLeveau/google_job_hunter/issues/53), approved prerequisite [#136](https://github.com/ThibaultLeveau/google_job_hunter/issues/136) |
| 55 | [#55 List discovered jobs with stable pagination and typed filters](https://github.com/ThibaultLeveau/google_job_hunter/issues/55) | Jobs API | [#53](https://github.com/ThibaultLeveau/google_job_hunter/issues/53) |
| 56 | [#56 Expose job evidence, safe processing status, and authorized retries](https://github.com/ThibaultLeveau/google_job_hunter/issues/56) | Jobs API | [#55](https://github.com/ThibaultLeveau/google_job_hunter/issues/55), [#51](https://github.com/ThibaultLeveau/google_job_hunter/issues/51) |
| 57 | [#57 Build the responsive job listing with factual summaries](https://github.com/ThibaultLeveau/google_job_hunter/issues/57) | Job interface | [#21](https://github.com/ThibaultLeveau/google_job_hunter/issues/21), [#55](https://github.com/ThibaultLeveau/google_job_hunter/issues/55) |
| 58 | [#58 Add URL-synchronized job filters and sorting](https://github.com/ThibaultLeveau/google_job_hunter/issues/58) | Job interface | [#57](https://github.com/ThibaultLeveau/google_job_hunter/issues/57) |
| 59 | [#59 Display job detail, application requirements, and source evidence](https://github.com/ThibaultLeveau/google_job_hunter/issues/59) | Job interface | [#57](https://github.com/ThibaultLeveau/google_job_hunter/issues/57), [#56](https://github.com/ThibaultLeveau/google_job_hunter/issues/56) |
| 60 | [#60 Explain match scores, gaps, unknowns, and stale results](https://github.com/ThibaultLeveau/google_job_hunter/issues/60) | Job interface | [#59](https://github.com/ThibaultLeveau/google_job_hunter/issues/59), [#53](https://github.com/ThibaultLeveau/google_job_hunter/issues/53), [#54](https://github.com/ThibaultLeveau/google_job_hunter/issues/54) |
| 61 | [#61 Show processing stages and safe retry actions](https://github.com/ThibaultLeveau/google_job_hunter/issues/61) | Job interface | [#59](https://github.com/ThibaultLeveau/google_job_hunter/issues/59), [#56](https://github.com/ThibaultLeveau/google_job_hunter/issues/56), [#42](https://github.com/ThibaultLeveau/google_job_hunter/issues/42) |
| 62 | [#62 Export the authenticated user's profile and research data](https://github.com/ThibaultLeveau/google_job_hunter/issues/62) | Operations and privacy | [#56](https://github.com/ThibaultLeveau/google_job_hunter/issues/56), [#29](https://github.com/ThibaultLeveau/google_job_hunter/issues/29), [#18](https://github.com/ThibaultLeveau/google_job_hunter/issues/18) |
| 62a | [#147 Fence authentication and private writes for tombstoned accounts](https://github.com/ThibaultLeveau/google_job_hunter/issues/147) | Approved prerequisite for #63 | [#62](https://github.com/ThibaultLeveau/google_job_hunter/issues/62), [#18](https://github.com/ThibaultLeveau/google_job_hunter/issues/18), [#16](https://github.com/ThibaultLeveau/google_job_hunter/issues/16), [#25](https://github.com/ThibaultLeveau/google_job_hunter/issues/25), [#30](https://github.com/ThibaultLeveau/google_job_hunter/issues/30) |
| 63 | [#63 Delete accounts and private data with race-safe cleanup](https://github.com/ThibaultLeveau/google_job_hunter/issues/63) | Operations and privacy | [#62](https://github.com/ThibaultLeveau/google_job_hunter/issues/62), [#25](https://github.com/ThibaultLeveau/google_job_hunter/issues/25), [#53](https://github.com/ThibaultLeveau/google_job_hunter/issues/53), [#11](https://github.com/ThibaultLeveau/google_job_hunter/issues/11), [#18](https://github.com/ThibaultLeveau/google_job_hunter/issues/18), [#16](https://github.com/ThibaultLeveau/google_job_hunter/issues/16), [#30](https://github.com/ThibaultLeveau/google_job_hunter/issues/30), [#147](https://github.com/ThibaultLeveau/google_job_hunter/issues/147) |
| 64 | [#64 Build account settings for identity linking, export, and deletion](https://github.com/ThibaultLeveau/google_job_hunter/issues/64) | Operations and privacy | [#23](https://github.com/ThibaultLeveau/google_job_hunter/issues/23), [#62](https://github.com/ThibaultLeveau/google_job_hunter/issues/62), [#63](https://github.com/ThibaultLeveau/google_job_hunter/issues/63), [#17](https://github.com/ThibaultLeveau/google_job_hunter/issues/17), [#18](https://github.com/ThibaultLeveau/google_job_hunter/issues/18), [#30](https://github.com/ThibaultLeveau/google_job_hunter/issues/30) |
| 64a | [#151 Reclaim unreferenced old processing artifacts](https://github.com/ThibaultLeveau/google_job_hunter/issues/151) | Approved prerequisite for #65 | [#63](https://github.com/ThibaultLeveau/google_job_hunter/issues/63), [#54](https://github.com/ThibaultLeveau/google_job_hunter/issues/54), [#64](https://github.com/ThibaultLeveau/google_job_hunter/issues/64) |
| 65 | [#65 Expire temporary data, stale artifacts, and abandoned tasks safely](https://github.com/ThibaultLeveau/google_job_hunter/issues/65) | Operations and privacy | [#63](https://github.com/ThibaultLeveau/google_job_hunter/issues/63), [#54](https://github.com/ThibaultLeveau/google_job_hunter/issues/54), [#151](https://github.com/ThibaultLeveau/google_job_hunter/issues/151) |
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
