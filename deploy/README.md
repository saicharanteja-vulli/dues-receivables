# Deployment package — private rollout

The image has been built and smoke-tested locally with no network access. No hosting, DNS or SMTP account has been provisioned. Public self-signup is intentionally disabled until verified-email registration and abuse protection are integrated; provision known users through the operator terminal.

1. Obtain a Linux server and domain. Point the domain to the server. Allow inbound 80/443, restrict SSH, and keep port 8120 private.
2. Install Docker with Compose. Copy this sanitized source to the server. Do not upload `.env`, local data, keys or backups to a public repository.
3. Copy `deploy/.env.example` to `deploy/.env`, set your domain and HTTPS origin, and restrict its permissions to the operator. Keep `DUES_MAIL_MODE=local` until SMTP has been connected and tested.
4. Generate a Fernet backup key using `cryptography.fernet.Fernet.generate_key()`. Save it privately as `DUES_BACKUP_KEY` and retain a separate offline copy. A key loss makes encrypted backups unrecoverable.
5. From the project folder, build: `docker compose -f deploy/compose.yaml build`.
6. Create an account through the operator terminal: `docker compose -f deploy/compose.yaml run --rm dues python provision.py`. Password entry is hidden. Public registration stays disabled.
7. Start: `docker compose -f deploy/compose.yaml up -d`. Caddy requests HTTPS certificates for the configured domain. Verify login, tenant isolation, file upload and a sample reconciliation before adding real records.
8. Check service health and logs with Docker Compose. Configure an external uptime check against `/api/health` when hosting is available. The data and encrypted backups use separate persistent volumes; periodically copy encrypted backups off the server.

The container runs without root, with a read-only root filesystem, bounded temporary storage, no extra Linux capabilities, CPU/memory bounds, and rotated logs. HTTP host/origin checks remain active; public mode requires HTTPS cookies and closed signup. One worker process is supported for the SQLite/scheduled-job design.

## Email

Set `DUES_MAIL_MODE=smtp` plus the SMTP host, port, sender, username and password in the private environment file. SMTP requires STARTTLS and certificate validation. Sender/domain verification and deliverability must be configured with your provider. Sending remains review-gated in Dues. A failure after submission is marked unknown, with no automatic retry, to avoid duplicate email. Never claim delivery merely because a server accepted a message.

## Backups and restore

Daily encrypted snapshots run while the app is running, if `DUES_BACKUP_KEY` is configured. Local use now has a private `.env` key and backups folder. Keep the key separate from exported backups. Monitor available disk space; snapshots are not automatically deleted.

To restore, stop Dues. Supply the original key privately in the environment, then run `python backup_secure.py restore /path/snapshot.dues-backup /path/new-data`. Restoration refuses an existing destination, authenticates the ciphertext, and checks the database manifest. Switch `DUES_DATA` or migrate the restored directory into a fresh data volume. Verify the app before discarding prior data. Full snapshots contain account/session records; consider revoking sessions after disaster recovery.

## Scope

Bank reconciliation uses reviewed CSV imports, not a bank feed. Email verification and public self-signup are not implemented. This package is for a controlled private rollout, not an audited, unlimited public SaaS launch. Hosting, domain, SMTP and external monitoring services can have costs; none were activated during development.
