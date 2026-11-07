# Single-host operations and recovery

Use the [container runtime](../README.md#single-host-container-runtime) and its pinned images.
These procedures target one host with PostgreSQL/pgvector and the private document volume.
They do not configure a host, enable GitHub protections or authorize paid provider calls.

## Configuration and capability checks

Keep `.env.runtime` mode 0600, outside commits and backups shared with developers. Configure
the canonical HTTPS origin, exact trusted hostname, database URL and private storage before
starting API/worker. Preserve Host at the TLS proxy; never log callback query strings.
`/api/health/live` and `/api/health/ready` do not establish provider availability.

| Capability | Operator configuration and check |
| --- | --- |
| Google sign-in | Register a Web application OAuth client and the exact HTTPS `/api/auth/google/callback` URI. Set all three `JOB_HUNTER_GOOGLE_` values. `GET /api/auth/providers` reports configuration; an authorized browser sign-in verifies consent/redirect setup. Only `openid email profile` are requested. |
| Mail | Set `JOB_HUNTER_MAIL_` sender, public URL, Fernet key and implicit-TLS SMTP transport (default port 465). Username/password are a pair. Verify delivery with an operator-controlled account; readiness never sends mail. |
| Search | Set `JOB_HUNTER_SEARCH_PROVIDER=serpapi` and its server-side API key. The authenticated `/api/v1/searches/provider` reports capability and advisory remaining quota. A live search consumes provider budget and needs deliberate operator authorization. |
| AI/embeddings | Set `JOB_HUNTER_AI_PROVIDER=openai`, its API key and both model IDs. Select a compatible embedding dimension/model revision. Configuration success does not prove provider entitlement. Use offline evaluations before a separately budgeted live check. |
| Limits | API and worker must share `JOB_HUNTER_BUDGET_SEARCH` and `JOB_HUNTER_BUDGET_TOKENS` JSON limits. Defaults are 100/20/10 search requests and 1,000,000/100,000/50,000 tokens per global/owner/run UTC day. Zero denies new reservations; uncertain calls retain their upper-bound charge. |

See [Google's redirect registration rules](https://developers.google.com/identity/protocols/oauth2/web-server),
the [mail contract](../README.md#email-verification-and-mail-delivery),
[search contract](../README.md#search-provider), [AI contract](../README.md#bounded-ai-boundary)
and [budget ledger](../README.md#provider-budget-ledger). Never put credentials in `VITE_*`.
Production rejects fixture adapters; the separately explicit synthetic harness is for tests only.

The crawler identifies itself as `JobHunter/0.1`; this is currently fixed in code, not an
environment setting. Preserve that identity and robots enforcement. A blocked/unsupported
source is an honest outcome, not a reason to spoof a browser or bypass robots policy.
Host pacing, Retry-After, size/time limits and cached robots decisions remain enforced.

Run [retention](../README.md#retention-maintenance) as bounded preview batches first, then
apply with the same configuration and persisted checkpoint. Start a new pass after a null
checkpoint; retry the same checkpoint after interruption. Use the documented age defaults
and reference protections. Schedule only maintenance, not recurring user searches. Suspend
maintenance and all other writers during backup/restore windows.

## Consistent encrypted backup

Require Docker/Compose, GNU tar, GnuPG and sufficient space on an encrypted local filesystem.
Import the backup recipient's public key and verify its full fingerprint out of band. Keep
the private decryption key separately; never put it or the runtime env file into the bundle.
Retain the matching mail key and runtime configuration in the operator's protected secret store.
An encrypted bundle protects content; it does not prove who created it. Restore only trusted,
access-controlled recovery sets. Do not accept uploaded or third-party database dumps.

Run from the exact deployed checkout, with API/worker initially running. The following uses
the default project name; substitute the actual name deliberately. Use a new output filename.

```bash
set -euo pipefail
umask 077
dc() { RUNTIME_ENV_FILE="$PWD/.env.runtime" docker compose -p job-hunter-runtime --env-file .env.runtime -f compose.runtime.yaml "$@"; }
recovery_work=$(mktemp -d /encrypted/backups/job-hunter-work.XXXXXX)
recovery_archive=/encrypted/backups/job-hunter-YYYYMMDDTHHMMSSZ.gpg
recovery_recipient=REPLACE_WITH_VERIFIED_FULL_FINGERPRINT
test ! -e "$recovery_archive"
dc stop web api
dc stop worker
dc exec -T database pg_dump -U job_hunter -d job_hunter --format=custom --no-owner --no-acl > "$recovery_work/database.dump"
dc run --rm --no-deps -T --entrypoint tar api -C /private/documents -cf - . > "$recovery_work/documents.tar"
{ git rev-parse HEAD; dc images; dc exec -T database psql -U job_hunter -d job_hunter -Atqc 'SELECT version_num FROM alembic_version'; } > "$recovery_work/metadata.txt"
tar -cf "$recovery_work/set.tar" -C "$recovery_work" database.dump documents.tar metadata.txt
gpg --batch --trust-model always --recipient "$recovery_recipient" --output "$recovery_archive" --encrypt "$recovery_work/set.tar"
dc up -d --wait api worker web
sha256sum "$recovery_archive"
rm -rf -- "$recovery_work"
```

The API stops accepting writes before the worker drains. Do not proceed with the dump if
either stop fails or another writer is active. Both copies finish before restart; preserve
the database and document archive as one indivisible recovery set. PostgreSQL's
[custom dump](https://www.postgresql.org/docs/18/backup-dump.html) preserves database contents,
including pgvector, provenance, versions, tombstones, budgets and durable tasks.
The private volume copy retains blob keys, modes and timestamps. It is never exposed through nginx.

If any command fails, treat the output as incomplete, keep plaintext scratch private, and
resolve the failure before retrying. After abandoning the failed backup, restart the original
unchanged services with `dc up -d --wait api worker web`; remove only that failed scratch/set.
Store the successful encrypted file and its checksum in protected off-host storage with a
defined retention/access policy. Enforce expiry there too; deleting live data does not delete
older backups. Secure deletion cannot be assumed on SSDs, so scratch must already be encrypted.

## Restore into a fresh, offline target

Use an isolated host/project, empty database and empty document volume. Never overlay a live
database or combine files from different backups. Provision compatible pinned PostgreSQL/pgvector
and the source application revision recorded in metadata. Decrypt with the separately retained key;
check [GnuPG's successful exit](https://www.gnupg.org/documentation/manuals/gnupg/GPG-Input-and-Output.html)
before extracting anything. A failed or truncated decrypt is unusable.

```bash
set -euo pipefail
umask 077
restore_work=$(mktemp -d /encrypted/backups/job-hunter-restore.XXXXXX)
gpg --batch --output "$restore_work/set.tar" --decrypt /encrypted/backups/VERIFIED_SET.gpg
tar --no-same-owner -xf "$restore_work/set.tar" -C "$restore_work" database.dump documents.tar metadata.txt
cat "$restore_work/metadata.txt"
```

Verify the recorded revision/images before continuing. Prepare `.env.restore` (0600), with
the matching mail key and a DSN for the new database service; keep public ingress closed.
The example uses a distinct project and loopback port. All commands must address that target.

```bash
dr() { RUNTIME_ENV_FILE="$PWD/.env.restore" RUNTIME_PORT=8081 docker compose -p job-hunter-restore --env-file .env.restore -f compose.runtime.yaml "$@"; }
dr build api web
dr up -d --wait database
test "$(dr exec -T database psql -U job_hunter -d job_hunter -Atqc "SELECT count(*) FROM pg_tables WHERE schemaname='public'")" = 0
dr run --rm --no-deps -T --entrypoint sh api -c 'test -z "$(find /private/documents -mindepth 1 -maxdepth 1 -print -quit)"'
dr exec -T database pg_restore -U job_hunter -d job_hunter --no-owner --no-acl --exit-on-error --single-transaction < "$restore_work/database.dump"
dr run --rm --no-deps -T --entrypoint tar api -C /private/documents --no-same-owner -xpf - < "$restore_work/documents.tar"
dr run --rm migrate
dr exec -T database psql -U job_hunter -d job_hunter -v ON_ERROR_STOP=1 -c 'BEGIN; DELETE FROM sessions; DELETE FROM oidc_flows; DELETE FROM email_verifications; DELETE FROM password_recoveries; COMMIT;'
dr up -d --wait api
```

Extraction runs as the backend UID, preserving private modes without importing an unrelated
host owner. Any restore failure leaves the target offline: discard that disposable target and
retry into a fresh one. Do not rerun a partial restore over populated data.
Check schema, row counts, document availability, profile evidence references and job artifact
versions before enabling workers. The SQL step invalidates restored sessions and authentication
links; users must sign in again or request new links. Reconcile post-backup account/document
deletions and password/identity changes before reopening access. This repository has no
automatic post-backup deletion journal reconciliation; require an operator recovery record.

Reconcile provider usage outside the backup window before allowing new spend; restored counters
alone cannot account for later paid calls. Then start `dr up -d --wait worker web`, verify queue
progress, and restore TLS ingress deliberately. Retain the original recovery set until verification
finishes. Remove decrypted scratch after verification. No real production restore is claimed here.

## Changes, draining and incidents

- Before a schema change, make a consistent backup and rehearse the target revision on a restored
  copy. Stop writes/workers, run one `migrate`, then start API/worker. They reject a mismatched head.
  Prefer a forward corrective migration. Git rollback is safe only while code remains compatible
  with the schema; destructive downgrades can erase data. A recovery-set rollback loses subsequent
  writes and needs the deletion/session/spend reconciliation above.
- SIGTERM stops claims and drains bounded calls. If the grace period expires, expired leases are
  recovered with fencing; delivery is at least once. Do not manually mark tasks succeeded or erase
  their identity keys. Inspect aggregate `kind/status/error_code` counts and queue age, never payloads,
  private text or credentials. Failed work needs its existing authorized retry operation after the
  cause is fixed; preserve successful public artifacts and private owner boundaries.
- During provider outages, keep bounded retries and quotas; do not loop live calls to test recovery.
  Stop workers before disabling/reconfiguring a provider when queued work must remain untouched.
  Unknown usage remains charged. Distinguish blocked/unsupported pages, invalid output, quota and
  infrastructure errors. Restart workers after capability and budget checks; use owner-authorized
  job retries/reevaluation, not direct SQL changes to task generations.
- Prompt/schema/parser/chunk/model changes need versioned artifacts and offline evaluations. Preserve
  old evidence references. An embedding model/dimension change creates a compatible new space;
  never mix vectors or overwrite provenance in place. Review queued old-version work before restart.
- Rotate provider/SMTP credentials during a drained window, update API/worker consistently, then
  revoke the old credentials after verification. Preserve the old mail Fernet key until encrypted
  deliveries and OIDC flows drain/expire; no key-ring migration is implemented. Keep keys needed by
  retained backups separately. Change database credentials in PostgreSQL and the runtime DSN together;
  editing `POSTGRES_PASSWORD` alone does not change an existing database volume's password.

## Release checks and rehearsal record

Required repository workflow jobs are `Backend quality`, `Frontend quality`, `Browser quality`,
`Container quality` and `PR size`. Recommend protected `main`, required successful checks, squash
merges, no force-push/deletion or protection bypass, and conversation resolution. Add independent
approval only if repository policy requires it; AGENTS.md permits documented agent self-review.
These are configuration instructions, not a claim that branch protections are enabled. This issue
does not mutate repository settings. Inspect the reviewed head and checks before merging.

Run the disposable, secret-free rehearsal locally (Docker and GnuPG required):

```bash
RUN_RECOVERY_TESTS=1 uv run --directory backend --locked pytest tests/test_recovery_rehearsal.py -q --tb=short
```

The rehearsal uses synthetic resume/profile evidence, public job artifacts and an unclaimed search
task with real API/worker/PostgreSQL. It quiesces the source, runs custom dump and document tar,
encrypts/decrypts with an ephemeral test key, restores into an empty database/private directory,
and checks evidence/artifact identity, owner isolation and recovery of the pending task. It also
rejects a truncated encrypted set and removes its own containers, databases and temporary keys.
Container CI runs this explicitly; ordinary backend CI skips the Docker/GPG-only test.
Record the executed results in the issue PR. This is a synthetic logical-restore rehearsal, not a
production disaster-recovery, storage-loss, key-escrow or provider-reconciliation certification.
