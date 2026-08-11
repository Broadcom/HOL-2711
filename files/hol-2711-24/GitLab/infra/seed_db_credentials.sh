#!/usr/bin/env bash
#
# Copy the database credential from the supervisor namespace into the guest
# cluster, so the application can read it without git ever holding it.
#
# This is the third leg of the credential story:
#
#   demo/hol-data   generates the password and writes it into the supervisor
#                   namespace as the Secret hol-db-credentials
#   THIS SCRIPT     copies that Secret into the guest cluster, every time a
#                   cluster is built
#   demo/hol-apps   references it by name with secretKeyRef, and contains no
#                   secret material at all
#
# It runs on every pipeline, not just the first, because the guest cluster is
# disposable: a rebuilt cluster is empty and needs the credential seeded again
# before Argo's WordPress pods can start.
#
# Usage: seed_db_credentials.sh <cluster-name> <supervisor-namespace> [target-namespace]
set -euo pipefail

CLUSTER="${1:?cluster name required}"
SUPERVISOR_NS="${2:?supervisor namespace required}"
TARGET_NS="${3:-hol-wordpress}"
# hol-wp-salts carries WordPress's eight cookie secrets. They have to be
# identical on every replica or logins bounce back to the login form at random,
# so they are generated once in the persistent tier and copied like the
# database password.
SECRETS="${SEED_SECRETS:-hol-db-credentials hol-wp-salts}"

# kubectl with no --kubeconfig honors $KUBECONFIG, which the job's
# .kube_login step points at a per-job minted supervisor credential. The
# runner itself carries no Kubernetes identity.
echo "reading ${CLUSTER}-kubeconfig from ${SUPERVISOR_NS}"
kubectl -n "$SUPERVISOR_NS" get secret "${CLUSTER}-kubeconfig" \
  -o jsonpath='{.data.value}' | base64 -d > gc.kubeconfig
chmod 0600 gc.kubeconfig

# Create the namespace here rather than letting Argo's CreateNamespace=true do
# it. Argo creates it unlabelled, which means it briefly enforces "restricted"
# and rejects the WordPress pods; they then sit in a failed ReplicaSet until
# something re-triggers them. Creating it up front with the label closes that
# window. Argo still owns the Namespace object and reconciles it afterwards.
kubectl --kubeconfig gc.kubeconfig create namespace "$TARGET_NS" \
  --dry-run=client -o yaml \
  | kubectl --kubeconfig gc.kubeconfig apply -f -
kubectl --kubeconfig gc.kubeconfig label namespace "$TARGET_NS" \
  pod-security.kubernetes.io/enforce=baseline --overwrite

# Copy data verbatim. Strip everything cluster-specific: a Secret carries a
# uid, resourceVersion and creationTimestamp that are meaningless in another
# cluster, and applying them fails.
for SECRET in $SECRETS; do
  echo "copying secret $SECRET -> $TARGET_NS in $CLUSTER"
  kubectl -n "$SUPERVISOR_NS" get secret "$SECRET" -o json \
    | jq --arg ns "$TARGET_NS" --arg name "$SECRET" \
        '{apiVersion, kind, type, data, metadata: {name: $name, namespace: $ns}}' \
    | kubectl --kubeconfig gc.kubeconfig apply -f -
done

# Never leave an admin kubeconfig in the build directory for the next job to
# pick up out of the cache.
rm -f gc.kubeconfig

# Argo CD caches the state of every cluster it manages. A destroyed and rebuilt
# cluster keeps the OLD cache, so the Application reports Synced/Healthy while
# the new cluster is completely empty - a green that means nothing. A hard
# refresh discards the cache and makes Argo re-read the live cluster.
#
# Without this the teardown demo needs a manual `argocd app get --hard-refresh`
# to come back, which rather undercuts the point.
for APP in ${ARGO_APPS:-hol-wordpress}; do
  if kubectl -n "$SUPERVISOR_NS" get application "$APP" >/dev/null 2>&1; then
    echo "forcing hard refresh of Argo application $APP"
    kubectl -n "$SUPERVISOR_NS" annotate application "$APP" \
      argocd.argoproj.io/refresh=hard --overwrite
  fi
done

echo "seeded into $TARGET_NS:$SECRETS"
