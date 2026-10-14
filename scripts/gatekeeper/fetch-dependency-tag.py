#!/usr/bin/env python3
"""
Fetch a dependency at its effective version, read-only (#98).

The clarifier checks a ticket's premise ("the dependency already offers X")
against real source at the version in scope. A local clone or package cache
(e.g. Unity's Library/PackageCache) can hold a different version, so this
helper never consults one: it reads nothing but the remote and writes nothing
but `dest`.

Usage: no arguments. Reads one JSON object from stdin:

  {
    "source": {"kind": "git", "url": "<url or path>"}
            | {"kind": "registry", "registry": "<base url>", "name": "<pkg>"},
    "version": "X.Y.Z",
    "dest": "<dir>"
  }

`version` matches X.Y.Z with an optional -/+ suffix; a leading `v` is
stripped. `dest` must not exist or be an empty directory (a non-empty one is
refused so an earlier fetch can never be re-served). Git: tag `vX.Y.Z`, else
`X.Y.Z`, is fetched at depth 1 and `.git` is removed. Registry: the
packument's `versions[version].dist.tarball` is extracted into `dest` with
the leading `package/` component stripped; unsafe members are rejected.

stdout (key: value lines):
  result: fetched | tag-missing | unreachable
  ref: <tag or version used>          (fetched)
  path: <dest>                        (fetched)
  detail: <one line>                  (tag-missing / unreachable)
  error: <what>                       (invalid input, instead of result:)

exit: 0 fetched, 2 tag-missing, 3 unreachable, 1 invalid input or unsafe
archive (nothing left in dest).
"""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request

VERSION_RE = re.compile(r"^v?(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.+-]+)?)$")
TIMEOUT = 120


class Invalid(Exception):
    pass


class Unreachable(Exception):
    pass


def _emit(code, **fields):
    for k, v in fields.items():
        print(f"{k}: {v}")
    sys.exit(code)


def _onerror(func, path, _exc):
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _empty(dest):
    for name in os.listdir(dest):
        p = os.path.join(dest, name)
        if os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p, onerror=_onerror)
        else:
            try:
                os.remove(p)
            except PermissionError:
                os.chmod(p, stat.S_IWRITE)
                os.remove(p)


def _git(*args):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    try:
        return subprocess.run(["git", *args], capture_output=True, text=True,
                              timeout=TIMEOUT, env=env)
    except (subprocess.TimeoutExpired, OSError) as e:
        raise Unreachable(f"git failed: {e}")


def _nonempty_str(v):
    return isinstance(v, str) and bool(v) and not v.startswith("-")


def _validate(req):
    if not isinstance(req, dict):
        raise Invalid("input must be a JSON object")
    src = req.get("source")
    if not isinstance(src, dict):
        raise Invalid("source missing")
    kind = src.get("kind")
    if kind == "git":
        if not _nonempty_str(src.get("url")):
            raise Invalid("source.url must be non-empty and not start with '-'")
    elif kind == "registry":
        if not _nonempty_str(src.get("registry")) or not _nonempty_str(src.get("name")):
            raise Invalid("source.registry and source.name must be non-empty "
                          "and not start with '-'")
    else:
        raise Invalid("source.kind must be git or registry")
    ver = req.get("version")
    m = VERSION_RE.match(ver) if isinstance(ver, str) else None
    if not m:
        raise Invalid("version must look like X.Y.Z")
    dest = req.get("dest")
    if not isinstance(dest, str) or not dest:
        raise Invalid("dest missing")
    if os.path.exists(dest) and (not os.path.isdir(dest) or os.listdir(dest)):
        raise Invalid("dest exists and is not an empty directory")
    return src, m.group(1), dest


def fetch_git(url, version, dest):
    r = _git("ls-remote", "--tags", url)
    if r.returncode != 0:
        tail = r.stderr.strip().splitlines()[-1:] or ["git ls-remote failed"]
        raise Unreachable(tail[0])
    refs = {line.split("\t", 1)[1] for line in r.stdout.splitlines() if "\t" in line}
    tag = next((t for t in (f"v{version}", version) if f"refs/tags/{t}" in refs), None)
    if tag is None:
        return None, f"neither tag v{version} nor {version} exists"
    os.makedirs(dest, exist_ok=True)
    steps = (
        ("init", "-q", dest),
        ("-C", dest, "fetch", "-q", "--depth", "1", "--no-tags", url, f"refs/tags/{tag}"),
        ("-C", dest, "-c", "advice.detachedHead=false", "checkout", "-q", "--detach",
         "FETCH_HEAD"),
    )
    for args in steps:
        r = _git(*args)
        if r.returncode != 0:
            tail = r.stderr.strip().splitlines()[-1:] or ["git step failed"]
            raise Unreachable(tail[0])
    shutil.rmtree(os.path.join(dest, ".git"), onerror=_onerror)
    return tag, None


def _http_get(url):
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
            return resp.read()
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise Unreachable(f"GET {url} failed: {e}")


def _safe_member(m):
    name = m.name.replace("\\", "/")
    if name.startswith("/") or ".." in name.split("/") or re.match(r"^[A-Za-z]:", name):
        return False
    return m.isfile() or m.isdir()


def fetch_registry(registry, name, version, dest):
    url = registry.rstrip("/") + "/" + urllib.parse.quote(name, safe="@")
    try:
        doc = json.loads(_http_get(url))
    except ValueError:
        raise Unreachable("registry returned an unreadable packument")
    try:
        tarball = doc["versions"][version]["dist"]["tarball"]
    except (KeyError, TypeError):
        return None, f"version {version} not in packument"
    data = _http_get(tarball)
    os.makedirs(dest, exist_ok=True)
    root = os.path.realpath(dest)
    try:
        with tempfile.TemporaryFile() as tmp:
            tmp.write(data)
            tmp.seek(0)
            with tarfile.open(fileobj=tmp, mode="r:*") as tf:
                members = tf.getmembers()
                for m in members:
                    if not _safe_member(m):
                        raise Invalid(f"unsafe archive member {m.name}")
                for m in members:
                    rel = m.name.replace("\\", "/").split("/", 1)
                    if len(rel) < 2 or not rel[1]:
                        continue
                    target = os.path.realpath(os.path.join(dest, rel[1]))
                    if target != root and not target.startswith(root + os.sep):
                        raise Invalid(f"unsafe archive member {m.name}")
                    if m.isdir():
                        os.makedirs(target, exist_ok=True)
                    else:
                        os.makedirs(os.path.dirname(target), exist_ok=True)
                        with tf.extractfile(m) as fsrc, open(target, "wb") as out:
                            shutil.copyfileobj(fsrc, out)
    except tarfile.TarError:
        raise Unreachable("tarball is not a readable archive")
    return version, None


def _cleanup(dest, existed):
    if not os.path.isdir(dest):
        return
    _empty(dest)
    if not existed:
        try:
            os.rmdir(dest)
        except OSError:
            pass


def main():
    try:
        req = json.loads(sys.stdin.read())
    except ValueError:
        _emit(1, error="stdin is not valid JSON")
    try:
        src, version, dest = _validate(req)
    except Invalid as e:
        _emit(1, error=e)
    existed = os.path.exists(dest)
    try:
        if src["kind"] == "git":
            ref, detail = fetch_git(src["url"], version, dest)
        else:
            ref, detail = fetch_registry(src["registry"], src["name"], version, dest)
    except Unreachable as e:
        _cleanup(dest, existed)
        _emit(3, result="unreachable", detail=" ".join(str(e).split()))
    except Invalid as e:
        _cleanup(dest, existed)
        _emit(1, error=e)
    if ref is None:
        _cleanup(dest, existed)
        _emit(2, result="tag-missing", detail=detail)
    _emit(0, result="fetched", ref=ref, path=dest)


if __name__ == "__main__":
    main()
