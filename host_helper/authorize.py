"""Headless OAuth for all three Google APIs -- run this ON the VPS.

The normal get_*_service() helpers call run_local_server(port=0), which picks a
random port and opens a browser. Neither works on a headless box, so this script
uses a FIXED port and prints the URL instead of launching anything.

Usage:

  1. From your LAPTOP, SSH in with the auth port forwarded back to you:

         ssh -L 8080:localhost:8080 YOUR_USER@YOUR_VPS_HOST

  2. In that same SSH session, on the VPS:

         cd ~/host-helper && ./.venv/bin/python -m host_helper authorize

  3. Copy each printed URL into your laptop's browser, sign in as the account
     whose calendar/mail you actually want, and approve.

Google redirects to http://localhost:8080/?code=... -- your laptop's port 8080
is forwarded to the VPS's port 8080, so the code lands back in this script.

Pass token filenames to redo only some of them:

     ./.venv/bin/python -m host_helper authorize token_gmail.json token_tasks.json

Override the port with AUTH_PORT if 8080 is taken (forward the same one):

     ssh -L 47821:localhost:47821 YOUR_USER@YOUR_VPS_HOST
     AUTH_PORT=47821 ./.venv/bin/python -m host_helper authorize

NOTE ON THE SUBPROCESS DANCE: google_auth_oauthlib's run_local_server does not
fully release its listening socket before returning, so a SECOND flow in the
same process dies with EADDRINUSE even though nothing else holds the port
(`ss -tlnp` shows it free). Each flow therefore runs in its own subprocess,
which always gets a clean bind.
"""

import os
import subprocess
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

CREDENTIALS_FILE = "credentials.json"
AUTH_PORT = int(os.environ.get("AUTH_PORT", "8080"))

# (token filename, scopes) -- must match the constants in the modules that read
# them: calendar_sync.py, gmail_fetcher.py, tasks_sync.py.
TARGETS = [
    ("token.json", ["https://www.googleapis.com/auth/calendar.events"]),
    ("token_gmail.json", ["https://www.googleapis.com/auth/gmail.readonly"]),
    ("token_tasks.json", ["https://www.googleapis.com/auth/tasks"]),
]

SINGLE_FLAG = "--single"


def authorize(token_file, scopes):
    """Run one OAuth flow. Only ever called with a single target per process."""
    print("")
    print("=== " + token_file + " ===")
    print("scope: " + scopes[0])
    flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, scopes)
    # open_browser=False: nothing to open on a headless box, so print the URL.
    creds = flow.run_local_server(port=AUTH_PORT, open_browser=False)
    with open(token_file, "w") as handle:
        handle.write(creds.to_json())
    os.chmod(token_file, 0o600)
    print("wrote " + token_file)


def main(argv=None):
    if not os.path.exists(CREDENTIALS_FILE):
        sys.exit(CREDENTIALS_FILE + " not found -- run this from the project directory.")

    args = sys.argv[1:] if argv is None else argv

    # Child mode: one token, done in-process.
    if args and args[0] == SINGLE_FLAG:
        wanted = args[1]
        for token_file, scopes in TARGETS:
            if token_file == wanted:
                authorize(token_file, scopes)
                return
        sys.exit("unknown token file: " + wanted)

    # Parent mode: fan out, one subprocess per token (see NOTE above).
    only = args or None
    if only:
        known = {t for t, _ in TARGETS}
        unknown = [t for t in only if t not in known]
        if unknown:
            sys.exit("unknown token file(s): " + ", ".join(unknown))

    for token_file, _scopes in TARGETS:
        if only and token_file not in only:
            continue
        result = subprocess.run([sys.executable, "-m", "host_helper.authorize", SINGLE_FLAG, token_file])
        if result.returncode != 0:
            sys.exit("authorisation failed for " + token_file)

    print("")
    print("All done. Verify with:  ./.venv/bin/python -m host_helper sync")


if __name__ == "__main__":
    main()
