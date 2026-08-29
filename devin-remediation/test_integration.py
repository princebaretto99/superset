"""End-to-end test through the real GitHub/Devin client code.

The unit tests replace the clients with fakes, so they never exercise the actual
HTTP calls -- URLs, headers, pagination, JSON shapes. This spins up a stub server
that speaks both APIs and drives a full dispatch -> poll -> pull request cycle
through `remediate.GitHub` and `remediate.Devin` themselves.
"""

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import remediate

REPO = "octo/demo"


class Store:
    """The state the stub server pretends to hold."""

    def __init__(self):
        self.issues = {
            1: {
                "number": 1,
                "title": "Legend overlaps the axis",
                "body": "Broken on narrow screens.",
                "labels": [{"name": "bug"}],
                "state": "open",
                "html_url": f"https://github.com/{REPO}/issues/1",
                "created_at": "2026-08-28T09:00:00Z",
                "closed_at": None,
            }
        }
        self.comments = {1: []}
        self.labels_created = []
        self.sessions = {}
        self.prompts = []
        self.next_comment_id = 500


class Stub(BaseHTTPRequestHandler):
    store: Store = None

    def _send(self, code, payload=None):
        body = json.dumps(payload if payload is not None else {}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")

    def do_GET(self):
        store, path = self.store, self.path.split("?")[0]

        if path == "/api/repos/octo/demo":
            return self._send(200, {"full_name": REPO, "has_issues": True, "default_branch": "main"})
        if path.startswith("/api/repos/octo/demo/labels/"):
            return self._send(404, {"message": "Not Found"})
        if path == "/api/repos/octo/demo/issues/comments":
            # Repo-wide comment listing, as GitHub returns it for analytics.
            return self._send(200, [
                {**comment, "issue_url": f"https://api.github.com/repos/{REPO}/issues/{number}"}
                for number, comments in store.comments.items()
                for comment in comments
            ])
        if path == "/api/repos/octo/demo/issues":
            # The client paginates; one short page ends it.
            return self._send(200, list(store.issues.values()))
        if match := re.fullmatch(r"/api/repos/octo/demo/issues/(\d+)", path):
            return self._send(200, store.issues[int(match.group(1))])
        if match := re.fullmatch(r"/api/repos/octo/demo/issues/(\d+)/comments", path):
            return self._send(200, store.comments[int(match.group(1))])
        if match := re.fullmatch(r"/devin/sessions/(.+)", path):
            return self._send(200, store.sessions[match.group(1)])
        return self._send(404, {"message": f"unstubbed GET {path}"})

    def do_POST(self):
        store, path, body = self.store, self.path.split("?")[0], self._body()

        if path == "/api/repos/octo/demo/labels":
            store.labels_created.append(body["name"])
            return self._send(201, body)
        if match := re.fullmatch(r"/api/repos/octo/demo/issues/(\d+)/comments", path):
            store.next_comment_id += 1
            comment = {"id": store.next_comment_id, "body": body["body"]}
            store.comments[int(match.group(1))].append(comment)
            return self._send(201, comment)
        if path == "/devin/sessions":
            store.prompts.append(body)
            session_id = f"devin-{len(store.prompts)}"
            store.sessions[session_id] = {"session_id": session_id, "status_enum": "working"}
            return self._send(200, {"session_id": session_id,
                                    "url": f"https://app.devin.ai/sessions/{session_id}"})
        return self._send(404, {"message": f"unstubbed POST {path}"})

    def do_PATCH(self):
        store, body = self.store, self._body()
        if match := re.fullmatch(r"/api/repos/octo/demo/issues/comments/(\d+)", self.path):
            for comments in store.comments.values():
                for comment in comments:
                    if comment["id"] == int(match.group(1)):
                        comment["body"] = body["body"]
                        return self._send(200, comment)
        return self._send(404, {"message": f"unstubbed PATCH {self.path}"})

    def do_PUT(self):
        store, body = self.store, self._body()
        if match := re.fullmatch(r"/api/repos/octo/demo/issues/(\d+)/labels", self.path):
            store.issues[int(match.group(1))]["labels"] = [{"name": n} for n in body["labels"]]
            return self._send(200, body["labels"])
        return self._send(404, {"message": f"unstubbed PUT {self.path}"})

    def log_message(self, *args):
        pass


@pytest.fixture
def server(monkeypatch):
    store = Store()
    Stub.store = store
    httpd = HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    monkeypatch.setattr(remediate, "GITHUB_API", f"{base}/api")
    monkeypatch.setattr(remediate, "DEVIN_API", f"{base}/devin")
    yield store
    httpd.shutdown()


def test_full_cycle_over_real_http(server):
    gh = remediate.GitHub("ghs_token", REPO)
    devin = remediate.Devin("apk_key")

    # --- first pass: the issue has no session, so one is created ---------------
    first = remediate.run(gh, devin, branch="main")

    assert first["results"][0]["action"] == "dispatched"
    assert server.labels_created == [name for name, _ in remediate.LABELS.values()]
    assert len(server.prompts) == 1
    sent = server.prompts[0]
    assert sent["idempotent"] is True
    assert sent["max_acu_limit"] == remediate.MAX_ACU
    assert sent["structured_output_schema"]["required"] == [
        "root_cause", "fix_summary", "confidence", "needs_human"]
    assert sent["tags"] == ["devin-remediation", f"repo:{REPO}", "issue:1"]
    assert "Legend overlaps the axis" in sent["prompt"]
    # The bug label survives; ours is added alongside it.
    assert [l["name"] for l in server.issues[1]["labels"]] == ["bug", "devin:queued"]
    assert len(server.comments[1]) == 1

    # --- Devin finishes and opens a PR ----------------------------------------
    server.sessions["devin-1"] = {
        "session_id": "devin-1",
        "status_enum": "finished",
        "pull_request": {"url": f"https://github.com/{REPO}/pull/7"},
        "structured_output": {
            "root_cause": "the axis width is computed before the font loads",
            "fix_summary": "measure after the font settles",
            "files_changed": ["src/Legend.tsx"],
            "confidence": "high",
            "needs_human": False,
        },
    }

    second = remediate.run(gh, devin, branch="main")

    assert len(server.prompts) == 1, "no duplicate session"
    assert second["results"][0]["action"] == "completed"
    assert second["results"][0]["pr"].endswith("/pull/7")
    assert [l["name"] for l in server.issues[1]["labels"]] == ["bug", "devin:pr-open"]

    # Status comment edited in place, plus one closure comment at the end.
    assert len(server.comments[1]) == 2
    body = server.comments[1][0]["body"]
    assert "the axis width is computed before the font loads" in body
    assert "src/Legend.tsx" in body
    assert f"https://github.com/{REPO}/pull/7" in body
    assert "✅ Devin fixed this" in server.comments[1][1]["body"]
    state, comment_id = remediate.read_state(server.comments[1])
    assert state["status"] == "finished"
    assert comment_id == 501


def test_single_issue_mode_hits_the_issue_endpoint(server):
    gh = remediate.GitHub("ghs_token", REPO)
    devin = remediate.Devin("apk_key")

    report = remediate.run(gh, devin, only_issue=1, branch="main")

    assert [r["issue"] for r in report["results"]] == [1]
    assert report["errors"] == []


def test_api_errors_are_reported_not_swallowed(server):
    gh = remediate.GitHub("ghs_token", "octo/missing")   # nothing stubbed for this repo
    devin = remediate.Devin("apk_key")

    with pytest.raises(RuntimeError, match="404"):
        gh.repo_info()


def test_stats_dashboard_over_real_http(server):
    """`stats` must work through the real client, including the bulk comment fetch."""
    gh = remediate.GitHub("ghs_token", REPO)
    devin = remediate.Devin("apk_key")

    remediate.run(gh, devin, branch="main")
    server.sessions["devin-1"] = {
        "session_id": "devin-1",
        "status_enum": "finished",
        "pull_request": {"url": f"https://github.com/{REPO}/pull/7"},
        "structured_output": {"confidence": "high", "needs_human": False},
    }
    remediate.run(gh, devin, branch="main")

    stats = remediate.collect_stats(gh)

    assert stats["repo"] == REPO
    assert stats["totals"]["attempted"] == 1
    assert stats["totals"]["shipped"] == 1
    assert stats["rates"]["success_pct"] == 100.0
    assert stats["stages"]["shipped"] == 1
    # Pickup is measured from the issue's real created_at.
    assert stats["issues"][0]["pickup_hours"] is not None

    markdown = remediate.stats_markdown(stats)
    assert "Produced a PR" in markdown
    assert f"https://github.com/{REPO}/pull/7" in markdown
