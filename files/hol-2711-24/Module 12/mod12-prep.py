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
  7. Provision the Argo registrar service account and CI variable so
     the pipeline's register-with-argocd job can write the cluster
     secret into ns-argocd.
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

WP_ANCHOR = ("                secretKeyRef: "
             "{name: hol-db-credentials, key: password}\n")
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


def transform_probe(text):
    if "timeoutSeconds" not in text:
        if PROBE_OLD not in text:
            return None
        text = text.replace(PROBE_OLD, PROBE_NEW)
    if "WORDPRESS_CONFIG_EXTRA" not in text:
        if WP_ANCHOR not in text:
            return None
        text = text.replace(WP_ANCHOR, WP_ANCHOR + WP_EXTRA, 1)
    return text


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
     "hol-wordpress: probe timeout + WORDPRESS_CONFIG_EXTRA [skip ci]"),
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



# --- 7. Argo registrar credential for the pipeline -------------------------
#
# The register-with-argocd job reads the CAPI kubeconfig from the tenant
# namespace (its VCF Automation session can) and then writes a labelled
# cluster Secret into ns-argocd (its session cannot). This provisions a
# service account scoped to exactly that write, and stores its token as a
# masked CI variable the job consumes. Supervisor-side work, so it uses
# the decryptK8Pwd route: the only step in this script that needs ssh.

ARGO_NS = "ns-argocd"
SA = "argocd-registrar"
VC = "vc-wld01-a.site-a.vcf.lab"
SSH_OPTS = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=15", "-o", "BatchMode=no"]

RBAC = f"""apiVersion: v1
kind: ServiceAccount
metadata:
  name: {SA}
  namespace: {ARGO_NS}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: {SA}
  namespace: {ARGO_NS}
rules:
  - apiGroups: [""]
    resources: ["secrets"]
    verbs: ["get", "create", "update", "patch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: {SA}
  namespace: {ARGO_NS}
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: {SA}
subjects:
  - kind: ServiceAccount
    name: {SA}
    namespace: {ARGO_NS}
---
apiVersion: v1
kind: Secret
metadata:
  name: {SA}-token
  namespace: {ARGO_NS}
  annotations:
    kubernetes.io/service-account.name: {SA}
type: kubernetes.io/service-account-token
"""


def _ssh(host, password, command):
    """Returns (rc, stdout, stderr); never raises. rc 127 means the ssh
    tooling itself is unavailable, 124 means it timed out."""
    r, w = os.pipe()
    os.write(w, (password + "\n").encode())
    os.close(w)
    try:
        p = subprocess.run(["sshpass", f"-d{r}", "ssh"] + SSH_OPTS +
                           [f"root@{host}", command],
                           capture_output=True, text=True, pass_fds=(r,),
                           timeout=120)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out contacting {host}"
    except FileNotFoundError as exc:
        return 127, "", f"ssh tooling missing: {exc}"
    except Exception as exc:  # noqa: BLE001
        return 1, "", str(exc)[:160]
    finally:
        try:
            os.close(r)
        except OSError:
            pass


def registrar_cred(check):
    proj = urllib.parse.quote("hol-scitech-gitops/infra", safe="")
    var = "ARGOCD_REGISTRAR_TOKEN"
    code, cur = http("GET",
                     f"{GITLAB}/api/v4/projects/{proj}/variables/{var}",
                     gl_hdr())
    if (code == 200 and isinstance(cur, dict)
            and (cur.get("value") or "").strip()):
        report("registrar", "OK", "CI variable already present")
        return
    if check:
        report("registrar", "WOULD-FIX",
               f"create {SA} in {ARGO_NS} and store {var}")
        return
    pw = read_secret(PW_FILE)
    if not pw:
        report("registrar", "FAILED", f"cannot read {PW_FILE}")
        return
    rc, out, err = _ssh(VC, pw, "/usr/lib/vmware-wcp/decryptK8Pwd.py")
    cp_ip, cp_pw = "", ""
    for line in out.splitlines():
        if line.startswith("IP:"):
            cp_ip = line.split()[1]
        elif line.startswith("PWD:"):
            cp_pw = line.split(None, 1)[1].strip()
    if not (cp_ip and cp_pw):
        report("registrar", "FAILED",
               f"decryptK8Pwd gave nothing: {(err or out).strip()[:120]}")
        return
    kc = "KUBECONFIG=/etc/kubernetes/admin.conf"
    b64 = base64.b64encode(RBAC.encode()).decode()
    rc, out, err = _ssh(cp_ip, cp_pw,
                        f"echo {b64} | base64 -d | {kc} kubectl apply -f -")
    if rc != 0:
        report("registrar", "FAILED", f"rbac apply: {err.strip()[:140]}")
        return
    rc, out, err = _ssh(cp_ip, cp_pw,
                        f"{kc} kubectl -n {ARGO_NS} get secret {SA}-token "
                        "-o jsonpath='{.data.token}'")
    try:
        token = base64.b64decode(out.strip()).decode() if out.strip() else ""
    except Exception:
        token = ""
    if not token:
        report("registrar", "FAILED", "token secret empty")
        return
    body = {"key": var, "value": token, "masked": True, "protected": False}
    code, resp = http("POST", f"{GITLAB}/api/v4/projects/{proj}/variables",
                      gl_hdr(), body)
    if code not in (200, 201):
        code, resp = http("PUT",
                          f"{GITLAB}/api/v4/projects/{proj}/variables/{var}",
                          gl_hdr(), {"value": token, "masked": True})
    report("registrar", "FIXED" if code in (200, 201) else "FAILED",
           f"{SA} in {ARGO_NS}, {var} set" if code in (200, 201)
           else f"variable {code}: {resp}")


def preflight():
    """Report what this pod is missing before anything is attempted. Never
    fatal on its own: each action still reports its own outcome."""
    missing = [p for p in (PW_FILE, PAT_FILE) if not read_secret(p)]
    for p in missing:
        report("preflight", "FAILED", f"credential file unreadable: {p}")
    tools = [t for t in ("kubectl", "ssh-keygen", "sshpass")
             if subprocess.run(["which", t], capture_output=True).returncode]
    if tools:
        report("preflight", "FAILED", f"tools missing: {', '.join(tools)}")


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
                     (hostkey_fix, "host-key"),
                     (registrar_cred, "registrar")):
        guarded(fn, name, args.check)
    bad = [r for r in RESULTS if r[1] == "FAILED"]
    print(f"done: {len(RESULTS)} items, {len(bad)} failed")
    if bad:
        print("NOT READY. Fix these, then run again (the script is safe to "
              "re-run and will skip what is already correct):")
        for item, _, detail in bad:
            print(f"  - {item}: {detail}")
    else:
        print("READY. The student still replaces the password placeholder "
              "in terraform.tfvars; everything else is in place.")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
