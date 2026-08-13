#!/usr/bin/env python3
"""Register a VKS guest cluster with an Argo CD instance.

This is the handoff step in the GitOps storyboard that is NOT automatic and
has no built-in equivalent: VCF Automation builds a guest cluster, and Argo CD
has no idea it exists. Someone has to hand Argo the credentials.

The credentials already exist. CAPI publishes an admin kubeconfig as a secret
named <cluster>-kubeconfig in the same supervisor namespace, which is exactly
the secret the hol-vks-wordpress blueprint already mounts to drive a guest
cluster from a Job. This reads that secret and writes the corresponding Argo CD
cluster secret, after which the guest cluster is a valid Application
destination.

Designed to run as a pipeline step after `terraform apply`, and to be safe to
re-run: registering an already-registered cluster just updates the secret.

Usage:
  argocd_register_cluster.py --cluster <name> --namespace <supervisor-ns>
                             [--argocd-namespace <ns>] [--context <kubectl ctx>]
                             [--name <display name>] [--dry-run]
"""
import argparse
import base64
import json
import subprocess
import sys

import yaml


def kubectl(args, context=None, stdin=None):
    cmd = ["kubectl"]
    if context:
        cmd.append(f"--context={context}")
    cmd += args
    p = subprocess.run(cmd, input=stdin, capture_output=True, text=True)
    if p.returncode != 0:
        sys.exit(f"kubectl {' '.join(args)} failed:\n{p.stderr.strip()}")
    return p.stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cluster", required=True,
                    help="guest cluster name, as shown by `kubectl get cluster`")
    ap.add_argument("--namespace", required=True,
                    help="supervisor namespace holding the cluster")
    ap.add_argument("--argocd-namespace",
                    help="namespace running Argo CD (default: --namespace)")
    ap.add_argument("--context", help="kubectl context to use")
    ap.add_argument("--name", help="display name in Argo (default: cluster name)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the secret instead of applying it")
    a = ap.parse_args()

    argo_ns = a.argocd_namespace or a.namespace
    display = a.name or a.cluster

    # CAPI names this secret deterministically. If it is missing, the cluster
    # is not finished provisioning yet.
    raw = kubectl(["-n", a.namespace, "get", "secret",
                   f"{a.cluster}-kubeconfig", "-o", "jsonpath={.data.value}"],
                  context=a.context)
    if not raw.strip():
        sys.exit(f"secret {a.cluster}-kubeconfig is empty or missing in {a.namespace}")

    kc = yaml.safe_load(base64.b64decode(raw))
    cluster = kc["clusters"][0]["cluster"]
    user = kc["users"][0]["user"]

    server = cluster["server"]
    ca = cluster.get("certificate-authority-data")
    cert = user.get("client-certificate-data")
    key = user.get("client-key-data")

    if not (ca and cert and key):
        sys.exit("kubeconfig lacks client certificate credentials; "
                 "expected a CAPI admin kubeconfig")

    # Argo stores per-cluster credentials as a labelled Secret in its own
    # namespace. The label is what makes it a cluster rather than a repo.
    config = {
        "tlsClientConfig": {
            "insecure": False,
            "caData": ca,
            "certData": cert,
            "keyData": key,
        }
    }
    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": f"cluster-{a.cluster}",
            "namespace": argo_ns,
            "labels": {"argocd.argoproj.io/secret-type": "cluster"},
        },
        "type": "Opaque",
        "stringData": {
            "name": display,
            "server": server,
            "config": json.dumps(config),
        },
    }

    doc = yaml.safe_dump(secret, sort_keys=False)
    if a.dry_run:
        # Never print the credential material in a pipeline log.
        redacted = json.loads(json.dumps(secret))
        redacted["stringData"]["config"] = "<redacted>"
        print(yaml.safe_dump(redacted, sort_keys=False))
        return

    kubectl(["apply", "-f", "-"], context=a.context, stdin=doc)
    print(f"registered {display} -> {server} in {argo_ns}")


if __name__ == "__main__":
    main()
