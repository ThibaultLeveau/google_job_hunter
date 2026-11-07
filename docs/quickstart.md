# Quickstart

Choose one path. Commands use Bash on Linux or WSL2, starting at the repository root unless
a step says otherwise. Keep API and worker configuration identical.

| Path | Runs on your host | Runs in Docker | Browser address |
| --- | --- | --- | --- |
| [Local development](#local-development) | Python API/worker and Vite frontend | PostgreSQL/pgvector | `http://127.0.0.1:5173` |
| [Containers](#containers) | An HTTPS reverse proxy | Database, API, worker and built frontend | Your configured HTTPS origin |

Both paths need mail configuration for account verification/recovery. Google sign-in, search
and AI are optional at startup. With search/AI disabled, you can register, verify your email
and edit a profile, but live discovery and AI extraction/matching are unavailable.
For a synthetic run without provider accounts, use the separate [test stack](../README.md#deterministic-test-stack).

## Get the source and tools

Install Git and [Docker with Compose](https://docs.docker.com/compose/install/), and start Docker.
Use the `docker compose` plugin with support for `--wait`.

```bash
git clone https://github.com/ThibaultLeveau/google_job_hunter.git
cd google_job_hunter
docker compose version
```

For local development, also install [uv](https://docs.astral.sh/uv/getting-started/installation/)
(CI uses 0.13.7) and [Node **26.13.0**](https://nodejs.org/en/download) with npm **11.20.0**, as pinned in
[package.json](../frontend/package.json). uv can install the required Python **3.14**.
Container builds install their pinned Python, Node and application dependencies inside images;
host Python/Node/uv are not required for that path.

## Local development

### 1. Install dependencies and start PostgreSQL

```bash
uv python install 3.14
uv sync --directory backend --locked
npm --prefix frontend ci
docker compose -p job-hunter-development up -d --wait database
```

The development database binds to `127.0.0.1:5432`. Its password is a committed local fixture.
If that port is occupied, prefix the Compose command with `JOB_HUNTER_DB_PORT=5433` and use
5433 in the database URL below. Keep the same port override on later Compose commands.

### 2. Configure the API and worker

On a fresh checkout, create your private configuration and document directory:

```bash
cp .env.example .env.local
chmod 600 .env.local
install -d -m 700 "$HOME/.local/share/google-job-hunter/documents"
uv run --directory backend --locked python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Save the generated key in `JOB_HUNTER_MAIL_KEY`; generate it once and preserve it across restarts.
Edit `.env.local` to contain the following values, replacing every `replace-...` value and the
document path. Remove the copied `POSTGRES_PASSWORD` line; the development Compose file supplies it.
Use an absolute document path outside the checkout, writable by the account running both processes.

```dotenv
JOB_HUNTER_DATABASE_URL=postgresql+psycopg://job_hunter:local-development-only@127.0.0.1:5432/job_hunter
JOB_HUNTER_TRUSTED_HOSTS='["localhost","127.0.0.1"]'
JOB_HUNTER_ALLOWED_ORIGINS='["http://127.0.0.1:5173"]'
JOB_HUNTER_DOCUMENT_ROOT=/absolute/path/to/your/.local/share/google-job-hunter/documents
JOB_HUNTER_MAIL_PUBLIC_URL=http://127.0.0.1:5173
JOB_HUNTER_MAIL_SENDER=replace-with-your-sender@example.org
JOB_HUNTER_MAIL_KEY=replace-with-the-generated-Fernet-key
JOB_HUNTER_MAIL_SMTP_HOST=replace-with-your-smtp-host
JOB_HUNTER_MAIL_SMTP_PORT=465
JOB_HUNTER_MAIL_SMTP_USERNAME=replace-with-your-smtp-username
JOB_HUNTER_MAIL_SMTP_PASSWORD='replace-with-your-smtp-password'
JOB_HUNTER_AI_PROVIDER=disabled
JOB_HUNTER_SEARCH_PROVIDER=disabled
```

SMTP must support certificate-verified **implicit TLS** (usually port 465); STARTTLS-only or
plaintext mail catchers do not work. Supply username/password together, or omit both if your
SMTP server needs no authentication. Mail settings are required whenever a database is configured.
Verification mail is sent by the worker, so it must be running when you register.

Environment files are ignored by Git. The application does not automatically load them:
the commands below pass `.env.local` explicitly through uv. Already exported environment
variables take precedence; clear old `JOB_HUNTER_*` settings if switching configurations.
Never put provider credentials in frontend files or `VITE_*` variables.

### 3. Apply migrations

From the repository root:

```bash
cd backend
uv run --env-file ../.env.local --locked alembic upgrade head
uv run --env-file ../.env.local --locked alembic current
cd ..
```

The application does not migrate automatically. Apply migrations before starting the API and worker.

### 4. Run three terminals

In each terminal, start at the repository root. Keep all three processes running.

Terminal 1 — API:

```bash
cd backend
uv run --env-file ../.env.local --locked uvicorn job_hunter.app:create_app --factory \
  --host 127.0.0.1 --port 8000 --no-proxy-headers --no-access-log
```

Terminal 2 — worker:

```bash
cd backend
uv run --env-file ../.env.local --locked python -m job_hunter.worker --concurrency 2
```

Terminal 3 — frontend:

```bash
cd frontend
npm run dev
```

Open **http://127.0.0.1:5173**. Vite forwards `/api` to port 8000; use the UI origin consistently
for sign-in and mail links. The browser suite verifies this loopback setup in Chromium.
Sessions use Secure cookies; remote/LAN access needs HTTPS, not a substituted LAN HTTP address.

Check the API through the same proxy used by the browser:

```bash
curl --fail http://127.0.0.1:5173/api/health/live
curl --fail http://127.0.0.1:5173/api/health/ready
```

Both return `{"status":"ok"}` when running with a reachable database. Readiness checks database
connectivity, not mail/provider delivery or schema compatibility in this local mode.
To stop, press Ctrl-C in the frontend and API terminals, then in the worker terminal and let active
work drain. From the root, stop PostgreSQL with `docker compose -p job-hunter-development down`.
The database volume and your document directory are preserved.

## Containers

### 1. Configure the runtime

This path uses [compose.runtime.yaml](../compose.runtime.yaml), independently of the development
database. It requires an HTTPS origin and a TLS reverse proxy on the same host. Arrange DNS and a
browser-trusted certificate for that hostname; for a laptop, use a locally trusted HTTPS setup.
The Compose stack does not provision certificates or the outer proxy. For a new installation:

```bash
cp .env.example .env.runtime
chmod 600 .env.runtime
docker compose --env-file .env.runtime -f compose.runtime.yaml build api web
docker compose --env-file .env.runtime -f compose.runtime.yaml run --rm --no-deps \
  --entrypoint python api -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Building images and generating the key do not start the application or contact providers.
Edit `.env.runtime` before the next step:

| Setting | Value to supply |
| --- | --- |
| `POSTGRES_PASSWORD` | A long random URL-safe password, not the example placeholder |
| `JOB_HUNTER_DATABASE_URL` | Same password in `postgresql+psycopg://job_hunter:PASSWORD@database:5432/job_hunter`; URL-encode it if it contains reserved characters |
| `JOB_HUNTER_TRUSTED_HOSTS` | JSON array of exact hostnames, e.g. `'["jobs.example.org","127.0.0.1"]'`; retain `127.0.0.1` for health probes |
| `JOB_HUNTER_ALLOWED_ORIGINS` | Exact HTTPS browser origin, e.g. `'["https://jobs.example.org"]'`, without a trailing slash |
| `JOB_HUNTER_MAIL_PUBLIC_URL` | Same HTTPS origin |
| `JOB_HUNTER_MAIL_KEY` | The generated Fernet key, preserved across restarts and backups |
| `JOB_HUNTER_MAIL_SENDER`, `JOB_HUNTER_MAIL_SMTP_*` | Your sender and implicit-TLS SMTP settings, as described above |

Keep search/AI disabled initially. Compose sets the document path to `/private/documents` and
mounts the same private named volume into API and worker. It also enforces production provider
environments; fixture/test modes are rejected. No host document directory is needed.

Configure the outer TLS proxy to forward the whole site to `http://127.0.0.1:8080`, preserve the
original `Host`, allow uploads up to 50 MiB, and omit query strings from access logs (or disable
them). The built-in nginx proxy handles frontend routes and `/api`. Only its loopback port is
published; API and PostgreSQL stay internal. Use the HTTPS origin in your browser: runtime startup
rejects HTTP origins and Secure session cookies require HTTPS outside the local development path.
To change the loopback port, set `RUNTIME_PORT` in `.env.runtime` and update the outer proxy target.

### 2. Migrate, start and verify

Run these sequentially from the repository root:

```bash
docker compose --env-file .env.runtime -f compose.runtime.yaml config --quiet
docker compose --env-file .env.runtime -f compose.runtime.yaml up -d --wait database
docker compose --env-file .env.runtime -f compose.runtime.yaml run --rm migrate
docker compose --env-file .env.runtime -f compose.runtime.yaml up -d --wait api worker web
docker compose --env-file .env.runtime -f compose.runtime.yaml ps
curl --fail http://127.0.0.1:8080/api/health/ready
```

Adjust the curl port if you changed `RUNTIME_PORT`; then open your HTTPS origin and check its
`/api/health/ready` too. API/worker startup and health checks require the exact current schema.
Health checks do not prove SMTP delivery, provider availability or worker throughput.

Inspect status or stop without deleting data:

```bash
docker compose --env-file .env.runtime -f compose.runtime.yaml logs --tail=100 api worker
docker compose --env-file .env.runtime -f compose.runtime.yaml stop web api worker database
```

Rerun the `up` commands to resume. Named volumes survive `stop` and ordinary `down`;
`down --volumes` deletes the database and private documents. Use the [operations runbook](operations.md)
for upgrades, encrypted backups and restore. Editing `POSTGRES_PASSWORD` does not change the password
inside an existing database volume. Avoid printing resolved `docker compose config` with secrets;
`config --quiet` above validates without dumping them.

## Enable optional providers and try the application

Edit the same server environment file, then restart both API and worker. Locally, stop and rerun
their commands. In containers, use `docker compose --env-file .env.runtime -f compose.runtime.yaml
up -d --wait api worker` to apply changed environment values; `restart` alone does not reload them.

| Capability | Settings |
| --- | --- |
| Google sign-in | Set `JOB_HUNTER_GOOGLE_CLIENT_ID`, `JOB_HUNTER_GOOGLE_CLIENT_SECRET`, and `JOB_HUNTER_GOOGLE_REDIRECT_URI` together. Register the exact browser origin plus `/api/auth/google/callback` in Google, e.g. `http://127.0.0.1:5173/api/auth/google/callback` locally. |
| Live job discovery | Set `JOB_HUNTER_SEARCH_PROVIDER=serpapi` and `JOB_HUNTER_SEARCH_API_KEY`. |
| AI extraction and matching | Set `JOB_HUNTER_AI_PROVIDER=openai`, `JOB_HUNTER_AI_API_KEY`, `JOB_HUNTER_AI_CHAT_MODEL`, and `JOB_HUNTER_AI_EMBEDDING_MODEL`. Choose compatible model IDs and embedding dimensions using the [AI settings contract](../README.md#bounded-ai-boundary). |

See [Google setup](../README.md#google-sign-in), [search settings](../README.md#search-provider),
and [provider budgets](../README.md#provider-budget-ledger). Live provider requests may incur charges;
review the global/user/run limits before enabling them.

1. Register with an email you can receive and a password of 15–128 characters; follow the verification email.
2. Add profile facts manually, or upload a supported PDF/DOCX/TXT and review the proposed facts when AI is enabled.
3. With search and AI configured, save a grouped search, run it, and inspect discovered jobs, evidence and matches.
4. Use Settings for account linking, export or deletion.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Missing/invalid mail configuration | Replace every mail placeholder; use a valid Fernet key and supply both SMTP credentials or neither. |
| Registration succeeds but no verification email | Worker is running with the same database/key; SMTP supports implicit TLS and the sender is authorized. Check spam and use resend verification after correcting settings. |
| Sign-in does not persist, or writes return 403 | Use the configured UI origin exactly; check allowed origins, trusted host, HTTPS/Secure cookies and preserved Host at the proxy. |
| Database unavailable or runtime schema error | Wait for database health, check host/port/password, then run migrations before starting API/worker. Container DSNs use `database`; local DSNs use `127.0.0.1`. |
| Uploads return 503 or stay queued | API/worker share a writable absolute private directory (or the container volume) and the worker is running. |
| Search/extraction unavailable | Disabled providers are intentional defaults; configure the capability and restart both processes. Health endpoints do not test provider access. |
| Frontend generation/build fails | Use pinned Node/npm, run `uv sync --directory backend --locked` first, and ensure uv is on PATH. API types are generated from the backend. |
| Port already in use | Change the documented database/runtime port override or free the local API/Vite ports; Vite's proxy expects API port 8000. |

For verification commands and operational limits, see [development checks](../README.md#development-status-and-command-contracts),
the [release checklist](release.md), and the [operations runbook](operations.md).
