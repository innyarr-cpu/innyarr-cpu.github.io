#!/usr/bin/env python3
"""One-time Google sign-in for Claude Sheets.

Run it in Terminal with the path to the OAuth client JSON from CoWork playground/tools/:

    python3 login.py "/path/to/client_secret_XXXX.json"

It opens Google's sign-in page, catches the reply on a local port, checks Google granted
exactly the spreadsheets permission and nothing more, and saves the sign-in to
~/.config/claude-sheets/token.json (owner-only, outside OneDrive). Never prints a secret.

Based on Hermes's drive-oauth-login.py, minus the container: on the Mac a local listener
can catch the redirect, so there's nothing to copy and paste.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

SCOPE = "https://www.googleapis.com/auth/spreadsheets"
AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
TOKEN_FILE = os.environ.get(
    "CLAUDE_SHEETS_TOKEN", os.path.expanduser("~/.config/claude-sheets/token.json")
)
WAIT_SECONDS = 300


def read_client(path: str) -> tuple[str, str]:
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if "installed" not in data:
        kind = next(iter(data), "unknown")
        raise SystemExit(f"This client is type '{kind}'. It must be a Desktop app client (setup steps, part 7).")
    client = data["installed"]
    return client["client_id"], client["client_secret"]


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit('Usage: python3 login.py "/path/to/client_secret_XXXX.json"')
    client_id, client_secret = read_client(sys.argv[1])

    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(32)
    reply: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (http.server's naming)
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "code" not in query and "error" not in query:
                self.send_response(404)
                self.end_headers()
                return
            reply.update({k: v[0] for k, v in query.items()})
            good = "code" in query and query.get("state", [""])[0] == state
            text = ("Signed in. You can close this tab and go back to Terminal."
                    if good else "Sign-in didn't complete. Go back to Terminal for the reason.")
            body = f"<html><body style='font-family:sans-serif;padding:3rem'><h2>{text}</h2></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode())
            threading.Thread(target=self.server.shutdown, daemon=True).start()

        def log_message(self, *args):  # keep Terminal quiet
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    redirect_uri = f"http://127.0.0.1:{server.server_address[1]}/"
    url = AUTHORIZE_URL + "?" + urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": SCOPE,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",  # ask for a refresh token
        "prompt": "consent",       # get one even on a repeat sign-in
    })

    print("\nOpening Google sign-in in your browser.")
    print("Sign in as innyarr@gmail.com. Never the Sando Workspace account.")
    print("If no browser opens, paste this into one:\n\n" + url + "\n")
    webbrowser.open(url)

    timer = threading.Timer(WAIT_SECONDS, server.shutdown)
    timer.start()
    server.serve_forever()
    timer.cancel()
    server.server_close()

    if not reply:
        raise SystemExit(f"No reply from Google within {WAIT_SECONDS // 60} minutes. Run it again.")
    if "error" in reply:
        raise SystemExit(f"Google said: {reply['error']}. Nothing saved.")
    if reply.get("state") != state:
        raise SystemExit("The reply didn't match this sign-in (state mismatch). Nothing saved.")

    body = urllib.parse.urlencode({
        "code": reply["code"],
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
        "code_verifier": verifier,
    }).encode()
    request = urllib.request.Request(
        TOKEN_URL, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            token = json.load(response)
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"Token exchange failed: HTTP {exc.code} {exc.read().decode('utf-8', 'replace')[:300]}")

    granted = token.get("scope", "").split()
    if granted != [SCOPE]:
        raise SystemExit(
            f"Google granted {granted}, not exactly {SCOPE}. Nothing saved. Tell Claude before going further."
        )
    if not token.get("refresh_token"):
        raise SystemExit("Google sent no refresh token, so the sign-in wouldn't last. Nothing saved. Run it again.")

    os.makedirs(os.path.dirname(TOKEN_FILE), mode=0o700, exist_ok=True)
    os.chmod(os.path.dirname(TOKEN_FILE), 0o700)
    saved = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": token["refresh_token"],
        "scope": SCOPE,
        "client_file": os.path.basename(sys.argv[1]),
    }
    fd = os.open(TOKEN_FILE + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(saved, handle, indent=2)
    os.replace(TOKEN_FILE + ".tmp", TOKEN_FILE)

    print("Signed in.")
    print(f"Permission : {SCOPE} (only)")
    print(f"Saved to   : {TOKEN_FILE} (only your user can read it)")
    print("Tell Claude it's done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
