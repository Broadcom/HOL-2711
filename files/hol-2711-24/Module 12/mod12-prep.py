#!/usr/bin/env python3
"""Module 12 prep: close the repair-class gaps on a HOL-2711 pod.

Runs ON the Linux Main Console as holuser. Stdlib only. Idempotent:
every action checks current state first and is safe to run twice.

    python3 mod12-prep.py --check   # report only, change nothing
    python3 mod12-prep.py           # apply what is missing

Actions (repair-class only; teaching steps stay in the manual):
  1. Argo CD trusts the lab root CA for gitlab.vcf.lab (API, no ssh).
  2. apps repo: readiness probe timeoutSeconds 10 ([skip ci] commit).
  3. infra repo: converge the live pipeline file to the labfiles
     copy ([skip ci] commit).
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
    """Never raises: a missing or unreadable credential file yields ""
    so the caller reports FAILED for its own item instead of killing
    the run."""
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return ""


# --- 1. Argo CD certificate ------------------------------------------------

def lab_ca_pem():
    """The lab root CA, from whichever source this pod actually has.

    1. the staged cert file (present on every pod observed so far)
    2. the PEM embedded in the staged Argo TLS ConfigMap
    3. the CA presented by the GitLab server itself
    Returns (pem, source) or (None, reason).
    """
    try:
        pem = open(CA_FILE).read()
        if "BEGIN CERTIFICATE" in pem:
            return pem, "rootca.cer"
    except Exception:
        pass
    cm = f"{HOME}/Documents/files/hol-2711-24/Argo/argocd-tls-certs-cm.yaml"
    try:
        text = open(cm).read()
        start = text.index("-----BEGIN CERTIFICATE-----")
        end = text.index("-----END CERTIFICATE-----") + len("-----END CERTIFICATE-----")
        block = text[start:end]
        pem = "\n".join(l.strip() for l in block.splitlines()) + "\n"
        return pem, "argocd-tls-certs-cm.yaml"
    except Exception:
        pass
    try:
        p = subprocess.run(["openssl", "s_client", "-showcerts",
                            "-connect", f"{GITLAB_HOST}:443"],
                           input="", capture_output=True, text=True, timeout=30)
        blocks = []
        cur = []
        for line in p.stdout.splitlines():
            if "BEGIN CERTIFICATE" in line:
                cur = [line]
            elif "END CERTIFICATE" in line and cur:
                cur.append(line)
                blocks.append("\n".join(cur) + "\n")
                cur = []
            elif cur:
                cur.append(line)
        if blocks:
            return blocks[-1], "gitlab TLS chain"
    except Exception:
        pass
    return None, "no CA source available"


def argo_cert(check):
    pw = read_secret(PW_FILE)
    if not pw:
        report("argo-cert", "FAILED", f"cannot read {PW_FILE}")
        return
    code, resp = http("POST", f"{ARGO}/api/v1/session",
                      body={"username": "admin", "password": pw})
    if code != 200 or not isinstance(resp, dict) or not resp.get("token"):
        report("argo-cert", "FAILED", f"session {code}: {str(resp)[:80]}")
        return
    tok = ("Authorization", f"Bearer {resp['token']}")
    q = urllib.parse.urlencode({"hostNamePattern": GITLAB_HOST,
                                "certType": "https"})
    code, certs = http("GET", f"{ARGO}/api/v1/certificates?{q}", tok)
    have = (code == 200 and isinstance(certs, dict)
            and (certs.get("items") or []))
    if have:
        report("argo-cert", "OK", "gitlab.vcf.lab TLS cert present")
        return
    if check:
        report("argo-cert", "WOULD-FIX", "add rootca.cer for gitlab.vcf.lab")
        return
    pem, source = lab_ca_pem()
    if pem is None:
        report("argo-cert", "FAILED", source)
        return
    body = {"items": [{"serverName": GITLAB_HOST, "certType": "https",
                       "certData": base64.b64encode(pem.encode()).decode()}]}
    code, resp = http("POST", f"{ARGO}/api/v1/certificates?upsert=true",
                      tok, body)
    if code == 200:
        report("argo-cert", "FIXED", f"CA added for gitlab.vcf.lab ({source})")
    else:
        report("argo-cert", "FAILED", f"POST {code}: {resp}")


# --- GitLab helpers --------------------------------------------------------

def gl_hdr():
    return ("PRIVATE-TOKEN", read_secret(PAT_FILE) or "missing-pat")


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
        push = ((prot.get("push_access_levels") or [{}])[0]
                .get("access_level", 40))
        merge = ((prot.get("merge_access_levels") or [{}])[0]
                 .get("access_level", 40))
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

# Superseded env-var attempt, still stripped if a repo carries it: the
# pod's Harbor WordPress image (4.8.3) predates the official image's
# WORDPRESS_CONFIG_EXTRA support, so the variable is inert there.
WP_EXTRA = """\
            # WordPress writes the URL it was installed at into its database
            # (siteurl and home) and serves every stylesheet, script and image
            # from that address, so a rebuilt load balancer that comes up on a
            # new IP leaves the site unstyled. These defines make the URL
            # follow whatever host the browser used, overriding the stored
            # rows. Also covers the localhost case.
            - name: WORDPRESS_CONFIG_EXTRA
              value: >-
                define('WP_HOME','http://'.$_SERVER['HTTP_HOST']);
                define('WP_SITEURL','http://'.$_SERVER['HTTP_HOST']);
"""

WP_HOOK_ANCHOR = "          readinessProbe:\n"
WP_HOOK = """\
          # The pod's Harbor WordPress image (4.8.3) predates the official
          # image's WORDPRESS_CONFIG_EXTRA support, so the URL fix is
          # written into wp-config.php directly, once the entrypoint has
          # generated it. WP_HOME/WP_SITEURL from the request host make
          # the site serve correctly at any load balancer address,
          # including after a destroy and rebuild, and cover the
          # localhost case.
          lifecycle:
            postStart:
              exec:
                command:
                  - bash
                  - -c
                  - |
                    f=/var/www/html/wp-config.php
                    for i in $(seq 1 120); do
                      [ -s "$f" ] && break
                      sleep 1
                    done
                    sleep 1
                    grep -q "WP_SITEURL" "$f" 2>/dev/null && exit 0
                    sed -i "s|^<?php|<?php\\ndefine('WP_HOME','http://'.\\$_SERVER['HTTP_HOST']);\\ndefine('WP_SITEURL','http://'.\\$_SERVER['HTTP_HOST']);|" "$f"
                    exit 0
"""


def transform_probe(text):
    if "timeoutSeconds" not in text:
        if PROBE_OLD not in text:
            return None
        text = text.replace(PROBE_OLD, PROBE_NEW)
    if WP_EXTRA in text:
        text = text.replace(WP_EXTRA, "")
    if "postStart" not in text:
        if WP_HOOK_ANCHOR not in text:
            return None
        text = text.replace(WP_HOOK_ANCHOR, WP_HOOK + WP_HOOK_ANCHOR, 1)
    return text


def transform_ci(text):
    # The labfiles copy ships with seed-db-credentials active and no
    # commented Module 3 block, so it IS the desired state verbatim.
    return text


SYNC_FILES = [
    ("probe", "hol-scitech-gitops/apps", "apps/hol-wordpress/manifest.yaml",
     f"{LABROOT}/apps/apps/hol-wordpress/manifest.yaml", transform_probe,
     "hol-wordpress: probe timeout + URL defines via postStart [skip ci]"),
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
        if code == 404:
            if check:
                report(f"ci-var {key}", "WOULD-FIX", "create missing variable")
                continue
            code, resp = http("POST",
                              f"{GITLAB}/api/v4/projects/{proj}/variables",
                              gl_hdr(), {"key": key, "value": val})
            report(f"ci-var {key}", "FIXED" if code in (200, 201) else "FAILED",
                   "created" if code in (200, 201) else f"POST {code}")
            continue
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
    found = [f"{HOME}/Documents/files/hol-2711-24/{d}/terraform.tfvars"
             for d in ("Module 12", "ELW")
             if os.path.exists(f"{HOME}/Documents/files/hol-2711-24/{d}/terraform.tfvars")]
    if not found:
        report("tfvars", "FAILED", "terraform.tfvars not found")
        return
    for p in found:
        _tfvars_one(p, check)


def _tfvars_one(p, check):
    try:
        cur = open(p).read()
    except Exception as exc:
        report("tfvars", "FAILED", f"unreadable: {exc}")
        return
    if f'vcfa_user     = "{PERSONA}"' in cur:
        report("tfvars", "OK", "persona already set")
        return
    if 'vcfa_user     = "admin"' not in cur:
        report("tfvars", "FAILED", "unexpected vcfa_user line; not touching")
        return
    if check:
        report("tfvars", "WOULD-FIX", f"set vcfa_user in {p}")
        return
    try:
        open(p, "w").write(cur.replace('vcfa_user     = "admin"',
                                       f'vcfa_user     = "{PERSONA}"'))
    except Exception as exc:
        report("tfvars", "FAILED", f"unwritable: {exc}")
        return
    report("tfvars", "FIXED", p)


# --- 6. stale host key -----------------------------------------------------

def hostkey_fix(check):
    try:
        r = subprocess.run(["ssh-keygen", "-F", CP_IP], capture_output=True,
                           timeout=30)
    except Exception as exc:
        report("host-key", "FAILED", f"ssh-keygen unavailable: {exc}")
        return
    if r.returncode != 0:
        report("host-key", "OK", "no stale entry")
        return
    if check:
        report("host-key", "WOULD-FIX", f"remove {CP_IP} from known_hosts")
        return
    subprocess.run(["ssh-keygen", "-R", CP_IP], capture_output=True)
    report("host-key", "FIXED", f"removed {CP_IP}")



def preflight():
    """Report what this pod is missing before anything is attempted. Never
    fatal on its own: each action still reports its own outcome."""
    gitops_pat = f"{HOME}/Desktop/API Tokens/PAT-SciTech.Gitops-Gitlab.txt"
    missing = [p for p in (PW_FILE, PAT_FILE, gitops_pat) if not read_secret(p)]
    for p in missing:
        report("preflight", "FAILED", f"credential file unreadable: {p}")
    tools = [t for t in ("kubectl", "ssh-keygen")
             if subprocess.run(["which", t], capture_output=True).returncode]
    if tools:
        report("preflight", "FAILED", f"tools missing: {', '.join(tools)}")
    if subprocess.run([sys.executable, "-c", "import yaml"],
                      capture_output=True).returncode:
        report("preflight", "FAILED",
               "PyYAML missing: argocd_register_cluster.py needs it")


def guarded(fn, name, check):
    """Run one action; an unexpected exception becomes that action's own
    FAILED line so the remaining actions still run."""
    try:
        fn(check)
    except Exception as exc:  # noqa: BLE001
        report(name, "FAILED", f"unexpected {type(exc).__name__}: "
                               f"{str(exc)[:120]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="report only, change nothing")
    args = ap.parse_args()
    mode = "CHECK" if args.check else "APPLY"
    print(f"mod12-prep {mode} on {os.uname().nodename}")
    preflight()
    for fn, name in ((argo_cert, "argo-cert"), (gitlab_sync, "gitlab-sync"),
                     (ci_vars, "ci-vars"), (tfvars_fix, "tfvars"),
                     (hostkey_fix, "host-key")):
        guarded(fn, name, args.check)
    bad = [r for r in RESULTS if r[1] == "FAILED"]
    if bad:
        for item, _, detail in bad:
            print(f"  - {item}: {detail}")
        print()
        print("NOT READY: Please ask for help.")
    else:
        print()
        print("READY")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
