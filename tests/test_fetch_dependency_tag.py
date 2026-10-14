"""
Driving tests for `scripts/gatekeeper/fetch-dependency-tag.py` (#98): a
stdin-JSON -> stdout `key: value` helper that fetches a dependency at its
effective version, read-only, into a caller-named directory.

Contract (pinned here for `implement`):

  stdin:  {"source": {"kind": "git", "url": "<url or path>"}
                   | {"kind": "registry", "registry": "<base url>", "name": "<pkg>"},
           "version": "X.Y.Z", "dest": "<dir>"}
  stdout: "result: fetched|tag-missing|unreachable", then "ref: <tag/version>"
          and "path: <dest>" (fetched) or "detail: <line>" (otherwise);
          invalid input -> "error: <what>" instead of "result:".
  exit:   0 fetched, 2 tag-missing, 3 unreachable, 1 invalid input.

Everything runs against real git repositories (tmp_path) and a real local
http.server; no mocks. Before the script exists every call fails with
interpreter-level "can't open file", so stdout carries no `result:` line --
that missing line is the RED reason.
"""

import http.server
import io
import json
import os
import pathlib
import subprocess
import sys
import tarfile
import threading

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "gatekeeper" / "fetch-dependency-tag.py"

GIT_ID = ["-c", "user.name=t", "-c", "user.email=t@t",
          "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false"]


def _git(cwd, *args):
    subprocess.run(["git", *GIT_ID, *args], cwd=cwd, check=True,
                   capture_output=True, text=True)


def _run(payload, cwd=None):
    return subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps(payload),
                          capture_output=True, text=True, cwd=cwd)


def _fields(result):
    out = {}
    for line in result.stdout.splitlines():
        if ": " in line:
            k, v = line.split(": ", 1)
            out[k] = v
    return out


def _tree_text(root):
    chunks = []
    for p in pathlib.Path(root).rglob("*"):
        if p.is_file():
            chunks.append(p.read_text(errors="ignore"))
    return "\n".join(chunks)


def _files(root):
    root = pathlib.Path(root)
    return [p for p in root.rglob("*") if p.is_file()] if root.exists() else []


@pytest.fixture
def upstream(tmp_path):
    repo = tmp_path / "upstream"
    (repo / "src").mkdir(parents=True)
    _git(repo, "init", "-q")
    (repo / "src" / "Use.cs").write_text("class UseObject {}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "A")
    _git(repo, "tag", "-a", "v0.0.2", "-m", "v0.0.2")
    _git(repo, "tag", "1.2.3")
    (repo / "src" / "Target.cs").write_text("interface IExternalUseTarget {}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "B")
    _git(repo, "tag", "v0.0.3")
    return repo


def _git_payload(url, version, dest):
    return {"source": {"kind": "git", "url": str(url)}, "version": version,
            "dest": str(dest)}


# --- R1 ---------------------------------------------------------------

def test_git_fetch_at_tag_contains_symbol_from_tag(upstream, tmp_path):
    dest = tmp_path / "out"
    r = _run(_git_payload(upstream, "0.0.2", dest))
    f = _fields(r)
    assert f.get("result") == "fetched", r.stdout + r.stderr
    assert f.get("ref") == "v0.0.2"
    assert r.returncode == 0
    assert "UseObject" in (dest / "src" / "Use.cs").read_text()
    assert not (dest / ".git").exists()


def test_git_bare_tag_fallback_and_v_prefixed_input(upstream, tmp_path):
    r = _run(_git_payload(upstream, "1.2.3", tmp_path / "a"))
    assert _fields(r).get("result") == "fetched", r.stdout + r.stderr
    assert _fields(r).get("ref") == "1.2.3"
    r = _run(_git_payload(upstream, "v0.0.2", tmp_path / "b"))
    assert _fields(r).get("result") == "fetched", r.stdout + r.stderr
    assert _fields(r).get("ref") == "v0.0.2"


# --- R2 ---------------------------------------------------------------

def test_git_fetch_at_tag_excludes_symbol_added_after_tag(upstream, tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    r_old = _run(_git_payload(upstream, "0.0.2", old))
    r_new = _run(_git_payload(upstream, "0.0.3", new))
    assert _fields(r_old).get("result") == "fetched", r_old.stdout + r_old.stderr
    assert _fields(r_new).get("result") == "fetched", r_new.stdout + r_new.stderr
    assert "IExternalUseTarget" not in _tree_text(old)
    assert "IExternalUseTarget" in _tree_text(new)


# --- R3 ---------------------------------------------------------------

def _decoy_cache(tmp_path):
    cwd = tmp_path / "cwd"
    cache = cwd / "Library" / "PackageCache" / "com.example.dep@0.0.2" / "src"
    cache.mkdir(parents=True)
    (cache / "Use.cs").write_text("class UseObject {}\n")
    return cwd


def test_git_missing_tag_reports_tag_missing_without_cache(upstream, tmp_path):
    cwd = _decoy_cache(tmp_path)
    prior = tmp_path / "prior"
    assert _fields(_run(_git_payload(upstream, "0.0.2", prior))).get("result") == "fetched"
    dest = tmp_path / "out"
    r = _run(_git_payload(upstream, "9.9.9", dest), cwd=cwd)
    assert _fields(r).get("result") == "tag-missing", r.stdout + r.stderr
    assert r.returncode == 2
    assert _files(dest) == []


def test_git_unreachable_source_reports_unreachable_without_cache(tmp_path):
    cwd = _decoy_cache(tmp_path)
    dest = tmp_path / "out"
    r = _run(_git_payload(tmp_path / "gone", "0.0.2", dest), cwd=cwd)
    assert _fields(r).get("result") == "unreachable", r.stdout + r.stderr
    assert r.returncode == 3
    assert _files(dest) == []


def test_non_empty_dest_is_refused_and_untouched(upstream, tmp_path):
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "keep.txt").write_text("old")
    r = _run(_git_payload(upstream, "0.0.2", dest))
    assert "error" in _fields(r), r.stdout + r.stderr
    assert r.returncode == 1
    assert [p.name for p in dest.iterdir()] == ["keep.txt"]
    assert (dest / "keep.txt").read_text() == "old"


# --- R4 ---------------------------------------------------------------

def _tgz(members):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture
def registry():
    state = {"tarball": _tgz({"package/src/Use.cs": b"class UseObject {}\n"})}

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            base = f"http://127.0.0.1:{self.server.server_port}"
            if self.path == "/com.example.dep":
                body = json.dumps({"versions": {"0.0.2": {"dist": {
                    "tarball": f"{base}/tarballs/dep-0.0.2.tgz"}}}}).encode()
            elif self.path == "/tarballs/dep-0.0.2.tgz":
                body = state["tarball"]
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    state["url"] = f"http://127.0.0.1:{srv.server_port}"
    yield state
    srv.shutdown()
    srv.server_close()


def _reg_payload(url, version, dest):
    return {"source": {"kind": "registry", "registry": url, "name": "com.example.dep"},
            "version": version, "dest": str(dest)}


def test_registry_fetch_extracts_tarball_at_version(registry, tmp_path):
    dest = tmp_path / "out"
    r = _run(_reg_payload(registry["url"], "0.0.2", dest))
    assert _fields(r).get("result") == "fetched", r.stdout + r.stderr
    assert r.returncode == 0
    assert "UseObject" in (dest / "src" / "Use.cs").read_text()


def test_registry_version_absent_is_tag_missing(registry, tmp_path):
    dest = tmp_path / "out"
    r = _run(_reg_payload(registry["url"], "0.0.9", dest))
    assert _fields(r).get("result") == "tag-missing", r.stdout + r.stderr
    assert r.returncode == 2
    assert _files(dest) == []


def test_registry_closed_port_is_unreachable(tmp_path):
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    dest = tmp_path / "out"
    r = _run(_reg_payload(f"http://127.0.0.1:{port}", "0.0.2", dest))
    assert _fields(r).get("result") == "unreachable", r.stdout + r.stderr
    assert r.returncode == 3


def test_registry_unsafe_archive_member_is_rejected(registry, tmp_path):
    registry["tarball"] = _tgz({"package/../evil.txt": b"x",
                                "package/src/Use.cs": b"class UseObject {}\n"})
    dest = tmp_path / "work" / "out"
    r = _run(_reg_payload(registry["url"], "0.0.2", dest))
    assert "unsafe archive member" in _fields(r).get("error", ""), r.stdout + r.stderr
    assert r.returncode == 1
    assert not (tmp_path / "work" / "evil.txt").exists()
    assert not (tmp_path / "evil.txt").exists()
    assert _files(dest) == []


# --- R5 ---------------------------------------------------------------

@pytest.mark.parametrize("version,url", [
    ("not-a-version", "UPSTREAM"),
    ("1.2", "UPSTREAM"),
    ("0.0.2", "--upload-pack=evil"),
])
def test_invalid_input_is_rejected(upstream, tmp_path, version, url):
    dest = tmp_path / "out"
    r = _run(_git_payload(upstream if url == "UPSTREAM" else url, version, dest))
    assert "error" in _fields(r), r.stdout + r.stderr
    assert "result" not in _fields(r)
    assert r.returncode == 1
    assert not dest.exists() or _files(dest) == []
