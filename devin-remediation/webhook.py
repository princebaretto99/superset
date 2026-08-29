#!/usr/bin/env python3
"""Optional: receive GitHub issue webhooks and remediate immediately.

The scheduled workflow is enough on its own -- this is for when you want the
pipeline to react the instant an issue is opened, or want to demo a real webhook.

    export WEBHOOK_SECRET=... TARGET_REPO=owner/repo GH_TOKEN=... DEVIN_API_KEY=...
    python webhook.py                       # listens on :8080
    # then point a GitHub webhook (or a tunnel) at http://<host>:8080/webhook
    # with content type application/json and the "Issues" event ticked.
"""

import hashlib
import hmac
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import remediate

PORT = int(os.environ.get("PORT", "8080"))
SECRET = os.environ.get("WEBHOOK_SECRET", "")
ACT_ON = {"opened", "reopened", "labeled"}


def signature_ok(body: bytes, header: str) -> bool:
    """GitHub signs every delivery; an unsigned request is not from GitHub."""
    if not SECRET:
        return False
    expected = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header or "")


def remediate_issue(number: int) -> None:
    """Run one issue through the same loop the workflow uses."""
    try:
        gh, devin = remediate.clients(dry_run=False)
        branch = gh.repo_info().get("default_branch", "main")
        report = remediate.run(gh, devin, only_issue=number, branch=branch)
        print(remediate.to_markdown(report))
    except Exception as error:                      # a bad delivery must not kill the server
        print(f"issue #{number} failed: {error}", file=sys.stderr)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))

        if not signature_ok(body, self.headers.get("X-Hub-Signature-256", "")):
            return self.reply(401, "bad signature")
        if self.headers.get("X-GitHub-Event") != "issues":
            return self.reply(204, "ignored")

        payload = json.loads(body or b"{}")
        if payload.get("action") not in ACT_ON:
            return self.reply(204, "ignored")

        number = payload["issue"]["number"]
        print(f"issue #{number} {payload['action']} -> remediating")
        # Answer GitHub straight away; Devin sessions take minutes to start.
        threading.Thread(target=remediate_issue, args=(number,), daemon=True).start()
        self.reply(202, f"remediating #{number}")

    def reply(self, code, message):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(message.encode())

    def log_message(self, *args):
        pass                                        # keep our own logging only


if __name__ == "__main__":
    if not SECRET:
        sys.exit("Set WEBHOOK_SECRET to the same secret configured on the GitHub webhook.")
    print(f"listening on http://0.0.0.0:{PORT}/webhook for {os.environ.get('TARGET_REPO')}")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
