# Privacy and secret handling

Keep deployment data outside Git. `.env`, Google OAuth client credentials and
tokens, databases and backups, calendar exports, raw email examples, task backups,
and logs contain secrets or personal data and are ignored by this repository.
Never use `git add -f` to add these files. Example configurations and test fixtures
must use invented data.

This application intentionally sends booking details to the configured Google
Calendar and stores booking/email information in a local database. Diagnostic and
dry-run commands may print guest, reservation, or calendar details. Keep those
outputs private; review and redact logs, screenshots, and failure alerts before
posting them in issues or elsewhere.

Run Gitleaks before publishing or pushing:

```bash
gitleaks git . --log-opts="--all" --redact=100
```

The repository configuration extends its standard rules with Airbnb calendar-feed
and heartbeat URL detection. This catches secrets, not every form of personal
data; review fixtures and documentation as well. To check only the next commit:

```bash
gitleaks git . --staged --redact=100
```

The optional `.pre-commit-config.yaml` installs the same scanner as a commit hook
with `pre-commit install` after installing the `pre-commit` tool.

Ignoring or deleting a file does not remove previous versions from Git history.
If a secret was committed, revoke or rotate it and remove it from every branch
and tag before publishing. Cleaning the latest commit alone is insufficient.
See [GitHub's sensitive-data removal instructions](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository).
