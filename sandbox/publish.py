#!/usr/bin/env python3
"""publish.py — push results, the ledger, the MIND's JSON nodes and the MIND page to the public repo.

Uses the GitHub Contents API (no git needed). The token comes from the environment (GITHUB_TOKEN) or from
/srv/vault/.env on the server. It is never written anywhere by this script. Only derived files are pushed:
never raw bars, never .env. Databento files are the one raw dataset the license allows public — still not pushed
here (size); they stay in the vault.
"""
import os, sys, json, base64, pathlib, urllib.request, urllib.error

REPO = os.environ.get("QL_REPO", "juliantorrespr-afk/quant-lab-data")
API = f"https://api.github.com/repos/{REPO}/contents/"


def token():
    t = os.environ.get("GITHUB_TOKEN", "")
    if not t:
        env = pathlib.Path("/srv/vault/.env")
        if env.exists():
            for line in env.read_text().splitlines():
                if line.startswith("GITHUB_TOKEN="):
                    t = line.split("=", 1)[1].strip().strip('"').strip("'")
    return t


def _req(method, url, tok, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json",
                                        "User-Agent": "quantlab-runner", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def push_file(local, repo_path, message, tok):
    content = pathlib.Path(local).read_bytes()
    if len(content) > 900_000:
        return f"skip {repo_path}: {len(content)/1e6:.1f} MB is too large for the contents API (keep results small)"
    status, cur = _req("GET", API + repo_path, tok)
    sha = cur.get("sha") if status == 200 else None
    if sha and base64.b64decode(cur.get("content", "").encode()) == content if cur.get("content") else False:
        return f"unchanged {repo_path}"
    body = {"message": message, "content": base64.b64encode(content).decode()}
    if sha: body["sha"] = sha
    status, res = _req("PUT", API + repo_path, tok, body)
    if status in (200, 201):
        return f"pushed {repo_path} ({len(content)} bytes)"
    return f"FAILED {repo_path}: HTTP {status} {res.get('message','')}"


def push_all(out_dir, sandbox_dir, message=None):
    tok = token()
    if not tok:
        print("publish: no GITHUB_TOKEN in the environment or /srv/vault/.env — nothing pushed (results stay on the server)")
        return False
    out_dir, sandbox_dir = pathlib.Path(out_dir), pathlib.Path(sandbox_dir)
    message = message or "sandbox: results + ledger + MIND nodes (runner.py --push)"
    files = []
    for p in ["docs/sandbox.json", "docs/vault.json", "docs/mind.html", "docs/session.json"]:
        if (out_dir / p).exists(): files.append((out_dir / p, p))
    for p in ["ledger.json", "families.json", "runner.py", "rules.py", "judge.py", "vault.py", "publish.py", "selftest.py"]:
        if (sandbox_dir / p).exists(): files.append((sandbox_dir / p, f"sandbox/{p}"))
    for p in sorted((sandbox_dir / "tests").glob("*.json")):
        files.append((p, f"sandbox/tests/{p.name}"))
    for p in sorted((out_dir / "vps").glob("*.ps1")):
        files.append((p, f"vps/{p.name}"))
    for p in sorted((out_dir / "results").glob("*/verdict.json")):
        files.append((p, f"results/{p.parent.name}/verdict.json"))
    for p in sorted((out_dir / "results").glob("*/curve.csv")):
        files.append((p, f"results/{p.parent.name}/curve.csv"))
    for p in sorted((out_dir / "results").glob("*/trades.csv")):
        files.append((p, f"results/{p.parent.name}/trades.csv"))
    ok = True
    for local, repo_path in files:
        r = push_file(local, repo_path, message, tok)
        print("publish:", r)
        ok &= not r.startswith("FAILED")
    return ok


if __name__ == "__main__":
    here = pathlib.Path(__file__).resolve().parent
    push_all(here.parent, here)
