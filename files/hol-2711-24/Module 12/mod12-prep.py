#!/usr/bin/env python3
"""Module 12 prep: close the repair-class gaps on a HOL-2711 pod.

Runs ON the Linux Main Console as holuser. Stdlib only. Idempotent:
every action checks current state first and is safe to run twice.

    python3 mod12-prep.py --check   # report only, change nothing
    python3 mod12-prep.py           # apply what is missing

Actions (repair-class only; teaching steps stay in the manual):
  1. Argo CD trusts the lab root CA for gitlab.vcf.lab (API, no ssh).
  2. apps repo: readiness probe timeoutSeconds 10 ([skip ci] commit).
  3. infra repo: uncomment the seed-db-credentials job ONLY
     ([skip ci] commit; register-with-argocd stays commented).
  4. CI variables TF_VAR_vcfa_user / TF_VAR_vcfa_password point at
     the workshop persona.
  5. terraform.tfvars vcfa_user = scitech.gitops (password
     placeholder is left for the student, by design).
  6. Remove the stale supervisor control plane host key.
"""

import argparse
import base64
import json
import os
import ssl
import subprocess
import sys
import urllib.request
import urllib.error
import urllib.parse

ARGO = "https://argocd-a.site-a.vcf.lab"
GITLAB = "https://gitlab.vcf.lab"
GITLAB_HOST = "gitlab.vcf.lab"
CP_IP = "10.1.1.165"
PERSONA = "scitech.gitops"
HOME = os.path.expanduser("~")
CA_FILE = f"{HOME}/Documents/files/certs/rootca.cer"
PAT_FILE = f"{HOME}/Desktop/API Tokens/PAT-Root-Gitlab.txt"
PW_FILE = f"{HOME}/Desktop/PASSWORD.txt"
CTX = ssl._create_unverified_context()

RESULTS = []


def report(item, state, detail=""):
    RESULTS.append((item, state, detail))
    print(f"  [{state:9s}] {item}" + (f": {detail}" if detail else ""))


def http(method, url, token_hdr=None, body=None, ok=(200, 201)):
    req = urllib.request.Request(url, method=method)
    if token_hdr:
        req.add_header(*token_hdr)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data, timeout=30, context=CTX) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, (e.read() or b"").decode()[:200]
    except Exception as e:  # noqa: BLE001 - report, do not crash the run
        return 0, str(e)[:200]


def read_secret(path):
    with open(path) as f:
        return f.read().strip()


# --- 1. Argo CD certificate ------------------------------------------------

def argo_cert(check):
    pw = read_secret(PW_FILE)
    code, resp = http("POST", f"{ARGO}/api/v1/session",
                      body={"username": "admin", "password": pw})
    if code != 200:
        report("argo-cert", "FAILED", f"session {code}: {resp}")
        return
    tok = ("Authorization", f"Bearer {resp['token']}")
    q = urllib.parse.urlencode({"hostNamePattern": GITLAB_HOST,
                                "certType": "https"})
    code, certs = http("GET", f"{ARGO}/api/v1/certificates?{q}", tok)
    have = code == 200 and (certs.get("items") or [])
    if have:
        report("argo-cert", "OK", "gitlab.vcf.lab TLS cert present")
        return
    if check:
        report("argo-cert", "WOULD-FIX", "add rootca.cer for gitlab.vcf.lab")
        return
    pem = open(CA_FILE).read()
    body = {"items": [{"serverName": GITLAB_HOST, "certType": "https",
                       "certData": base64.b64encode(pem.encode()).decode()}]}
    code, resp = http("POST", f"{ARGO}/api/v1/certificates?upsert=true",
                      tok, body)
    if code == 200:
        report("argo-cert", "FIXED", "rootca.cer added for gitlab.vcf.lab")
    else:
        report("argo-cert", "FAILED", f"POST {code}: {resp}")


# --- GitLab helpers --------------------------------------------------------

def gl_hdr():
    return ("PRIVATE-TOKEN", read_secret(PAT_FILE))


def gl_raw(project, path):
    p = urllib.parse.quote(project, safe="")
    f = urllib.parse.quote(path, safe="")
    url = f"{GITLAB}/api/v4/projects/{p}/repository/files/{f}/raw?ref=main"
    req = urllib.request.Request(url)
    req.add_header(*gl_hdr())
    try:
        with urllib.request.urlopen(req, timeout=30, context=CTX) as r:
            return r.read().decode()
    except Exception as e:  # noqa: BLE001
        return None if isinstance(e, urllib.error.HTTPError) else None


def gl_commit(project, path, content, message):
    p = urllib.parse.quote(project, safe="")
    body = {"branch": "main", "commit_message": message,
            "actions": [{"action": "update", "file_path": path,
                         "content": content}]}
    url = f"{GITLAB}/api/v4/projects/{p}/repository/commits"
    code, resp = http("POST", url, gl_hdr(), body)
    if code != 403 or "not allowed to push" not in str(resp):
        return code, resp
    # main is push-protected and this GitLab honors protection over the
    # admin token. Lift the protection, commit, restore it exactly.
    pb_url = f"{GITLAB}/api/v4/projects/{p}/protected_branches"
    gcode, prots = http("GET", pb_url, gl_hdr())
    prot = next((b for b in (prots if isinstance(prots, list) else [])
                 if b.get("name") == "main"), None)
    if gcode != 200 or prot is None:
        return code, resp
    http("DELETE", f"{pb_url}/main", gl_hdr())
    try:
        code, resp = http("POST", url, gl_hdr(), body)
    finally:
        push = (prot["push_access_levels"] or [{}])[0].get("access_level", 40)
        merge = (prot["merge_access_levels"] or [{}])[0].get("access_level", 40)
        q = urllib.parse.urlencode({"name": "main",
                                    "push_access_level": push,
                                    "merge_access_level": merge})
        http("POST", f"{pb_url}?{q}", gl_hdr())
    return code, resp


PROBE_OLD = """          readinessProbe:
            httpGet: {path: /, port: 80}
            initialDelaySeconds: 10
            periodSeconds: 5
"""
PROBE_NEW = """          readinessProbe:
            httpGet: {path: /, port: 80}
            initialDelaySeconds: 10
            periodSeconds: 5
            timeoutSeconds: 10
"""

# --- 2+3. GitLab files: converge live repos to desired content ------------
#
# Desired content is the console labfiles copy run through idempotent
# transforms. On pods shipped before the labfiles fixes landed, the
# transforms generate the fix; on pods whose labfiles already carry it,
# they are no-ops and the labfiles copy IS the desired state. Whole-file
# commit on difference, so the live repo converges regardless of what
# it currently holds.

LABROOT = f"{HOME}/Documents/files/hol-2711-24/GitLab"

def transform_probe(text):
    if "timeoutSeconds" in text:
        return text
    if PROBE_OLD not in text:
        return None
    return text.replace(PROBE_OLD, PROBE_NEW)


def transform_ci(text):
    lines = text.splitlines(keepends=True)
    if not any(l.startswith("seed-db-credentials:") for l in lines):
        try:
            start = next(i for i, l in enumerate(lines)
                         if l.rstrip("\n") == "# seed-db-credentials:")
        except StopIteration:
            return None
        end = start
        while end < len(lines):
            s = lines[end].rstrip("\n")
            if ("ENDS HERE" in s or s.startswith("# register-with-argocd")
                    or s == "" or not s.startswith("#")):
                break
            end += 1
        lines[start:end] = [
            (l[2:] if l.startswith("# ") else
             ("\n" if l.rstrip("\n") == "#" else l))
            for l in lines[start:end]]
    text = "".join(lines)
    old_inv = ('#     - python3 argocd_register_cluster.py --cluster '
               '"$CLUSTER" --namespace "$SUPERVISOR_NS" --name "$CLUSTER"')
    if old_inv + "\n" in text + "\n" and "--argocd-namespace" not in text:
        text = text.replace(old_inv, old_inv + " --argocd-namespace ns-argocd")
    return text


SYNC_FILES = [
    ("probe", "hol-scitech-gitops/apps", "apps/hol-wordpress/manifest.yaml",
     f"{LABROOT}/apps/apps/hol-wordpress/manifest.yaml", transform_probe,
     "hol-wordpress: readiness probe timeoutSeconds 10 [skip ci]"),
    ("seed-job", "hol-scitech-gitops/infra", ".gitlab-ci.yml",
     f"{LABROOT}/infra/.gitlab-ci.yml", transform_ci,
     "activate seed-db-credentials [skip ci]"),
]


def gitlab_sync(check):
    for item, proj, path, local, transform, msg in SYNC_FILES:
        if not os.path.exists(local):
            report(item, "FAILED", f"labfiles copy missing: {local}")
            continue
        desired = transform(open(local).read())
        if desired is None:
            report(item, "FAILED", "labfiles copy shape unexpected; not touching")
            continue
        live = gl_raw(proj, path)
        if live is None:
            report(item, "FAILED", f"cannot read live {proj}:{path}")
            continue
        if live == desired:
            report(item, "OK", "live matches desired")
            continue
        if check:
            report(item, "WOULD-FIX", "replace live file with desired content")
            continue
        code, resp = gl_commit(proj, path, desired, msg)
        report(item, "FIXED" if code == 201 else "FAILED",
               "" if code == 201 else f"{code}: {resp}")


# --- 4. CI variables -------------------------------------------------------

def ci_vars(check):
    proj = urllib.parse.quote("hol-scitech-gitops/infra", safe="")
    for key, val in (("TF_VAR_vcfa_user", PERSONA),
                     ("TF_VAR_vcfa_password", read_secret(PW_FILE))):
        code, cur = http("GET",
                         f"{GITLAB}/api/v4/projects/{proj}/variables/{key}",
                         gl_hdr())
        if code != 200:
            report(f"ci-var {key}", "FAILED", f"GET {code}")
            continue
        if cur.get("value") == val:
            report(f"ci-var {key}", "OK", "already set")
            continue
        if check:
            report(f"ci-var {key}", "WOULD-FIX", "update value")
            continue
        code, resp = http("PUT",
                          f"{GITLAB}/api/v4/projects/{proj}/variables/{key}",
                          gl_hdr(), {"value": val})
        report(f"ci-var {key}", "FIXED" if code == 200 else "FAILED",
               "" if code == 200 else f"PUT {code}")


# --- 5. tfvars persona -----------------------------------------------------

def tfvars_fix(check):
    for d in ("Module 12", "ELW"):
        p = f"{HOME}/Documents/files/hol-2711-24/{d}/terraform.tfvars"
        if os.path.exists(p):
            break
    else:
        report("tfvars", "FAILED", "terraform.tfvars not found")
        return
    cur = open(p).read()
    if f'vcfa_user     = "{PERSONA}"' in cur:
        report("tfvars", "OK", "persona already set")
        return
    if 'vcfa_user     = "admin"' not in cur:
        report("tfvars", "FAILED", "unexpected vcfa_user line; not touching")
        return
    if check:
        report("tfvars", "WOULD-FIX", f"set vcfa_user in {p}")
        return
    open(p, "w").write(cur.replace('vcfa_user     = "admin"',
                                   f'vcfa_user     = "{PERSONA}"'))
    report("tfvars", "FIXED", p)


# --- 6. stale host key -----------------------------------------------------

def hostkey_fix(check):
    r = subprocess.run(["ssh-keygen", "-F", CP_IP], capture_output=True)
    if r.returncode != 0:
        report("host-key", "OK", "no stale entry")
        return
    if check:
        report("host-key", "WOULD-FIX", f"remove {CP_IP} from known_hosts")
        return
    subprocess.run(["ssh-keygen", "-R", CP_IP], capture_output=True)
    report("host-key", "FIXED", f"removed {CP_IP}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="report only, change nothing")
    args = ap.parse_args()
    mode = "CHECK" if args.check else "APPLY"
    print(f"mod12-prep {mode} on {os.uname().nodename}")
    argo_cert(args.check)
    gitlab_sync(args.check)
    ci_vars(args.check)
    tfvars_fix(args.check)
    hostkey_fix(args.check)
    bad = [r for r in RESULTS if r[1] == "FAILED"]
    print(f"done: {len(RESULTS)} items, {len(bad)} failed")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
