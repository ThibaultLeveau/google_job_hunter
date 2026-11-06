# Google Job Hunter

Google Job Hunter is a planned private job-research application: review profile facts from documents,
run grouped Google searches, and inspect public job evidence and private profile matches.

**Current state:** the backend bootstrap provides a typed FastAPI app factory and a credential-free
liveness endpoint. Architecture and contribution contracts are established.
The React frontend provides a responsive session-aware shell, guarded routes and local API proxy.
Backend and frontend GitHub Actions checks enforce lint, strict types and coverage.
PostgreSQL/pgvector, explicit transaction sessions and Alembic schema setup are available.
Backend CI runs migrations and isolated real-database tests. The durable task store is available;
worker execution, heartbeat/recovery and automatic backoff remain planned for #11.

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
user-event and MSW with no default handlers. Tests register only the requests they need through
`server.use(...)`; handlers and the DOM are reset after each test. Unhandled requests are blocked
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
captures were visually checked. Browser checks are local evidence; browser CI arrives in #67.

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
separate authenticated linking action. The identity-linking settings screen remains planned for #64.
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
Errors: 60-second timeout 408, oversize 413, invalid content 415, quota/tombstone 409, storage failure 503.
Quotas count committed documents and tombstones; checks serialize per owner. Temporary use is bounded per request.
Document creation and `document_parse` enqueue commit together; the configured worker consumes these tasks.
Schedule `uv run --directory backend --locked python -m job_hunter.infrastructure.documents` hourly
with the same database/storage settings on the same filesystem with working POSIX locks. It scans without
loading all files. It removes unreferenced blobs older than one hour, preserving active uploads and committed
references (including tombstones). Uncertain commits retain blobs until references can be checked; retry failed sweeps.

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
Minimal tombstone/task metadata and text-free evidence markers remain. Existing tombstones still count
toward quotas and prevent identical reuploads; account deletion and quota reclamation are outside #30.

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
SDK retries are zero. Chat can repair schema failures once with a fixed instruction, without replaying
invalid output; provider failures/refusals are terminal. Callers must not add automatic task retries
around these calls; later explicit retries require fresh budget reservations.

Source text travels in a JSON data envelope; output still requires feature-specific evidence checks.
Accounting reserves before each request and records its UUID, configured model, prompt/schema versions,
schema hash, bounds, known usage (otherwise null), elapsed time and safe error code. Hooks receive no
source/output text. Hook failures stop execution; atomic budgets/persistence remain #54.
Fixtures require an explicit response supplied to the factory; embedding fixtures are deterministic,
not semantic vectors. Normal tests intercept HTTP and use no credential or paid service.
The unused LangSmith pytest plugin is disabled; one pinned dependency's Python 3.14 import deprecation
is narrowly filtered. Other warnings remain errors, and no maintained code is excluded from coverage.

Verified-owner `GET /api/v1/documents/{id}/proposals?offset=0` returns status, artifact/source version,
language, count and 100 proposals/page. Accepted profile facts never change.
Each field retains uncertainty, explicit nulls and exact Unicode spans/quotes validated against its window
and preserved text. Partial tokens/unsupported values fail; source presence does not prove factual truth.
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
atomic user/global search budgets arrive in #54. External adapters require explicit configuration.
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
No provider calls or search runs were made; browser automation in CI remains planned for #67.

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
families without search suffixes; every answer is validated and the connection uses one validated IP.
HTTP Host, TLS SNI and certificate verification retain the original hostname. Each hop revalidates
DNS; there is no address fallback, transport retry, automatic redirect, proxy or cookie jar.
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
Worker orchestration and global budgets remain #51/#54;
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
an overall deadline and a bounded, uncompressed JSON response. Durable orchestration/budgets are later work.

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
