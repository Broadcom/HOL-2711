#!/usr/bin/env bash
# deploy-vks-wordpress.sh: the Module 12 end state without the ceremony.
#
# Builds, on a HOL-2711 pod, a database VM (terraform), a VKS cluster
# (raw Cluster CR against the supervisor, bypassing VCF Automation and
# its intermittently flaky API), seeds the credentials, deploys
# WordPress, and completes the installer so the site is real.
#
# Run ON the pod's Linux Main Console as holuser:
#     bash deploy-vks-wordpress.sh
#
# Prereqs it checks for: the current lab files under
# ~/Documents/files/hol-2711-24/ (Module 12 + GitLab folders), sshpass,
# kubectl, terraform, and ~/Desktop/PASSWORD.txt.
#
# Idempotent-ish: every phase checks state first and skips what exists.
# Wall clock on a quiet pod: about 25 minutes, most of it the cluster.
#
# Proven quirks this script encodes (learned 2026-08-18):
#   - class builtin-generic-v3.6.0 lives in vmware-system-vks-public,
#     so classNamespace is required; the tenant namespace only exposes
#     v3.1.0-v3.3.0.
#   - The version is the normalized form (v1.33.6+vmware.1-fips), not
#     the TKR name.
#   - A control plane VM can fail placement and silently auto-retry for
#     about 7 minutes; machines exist but nothing looks like progress.
#     WAIT. Deleting and retrying is how a working build gets killed.
#   - seed_db_credentials.sh deletes gc.kubeconfig on exit, so the
#     guest kubeconfig is re-extracted after seeding.

set -euo pipefail

NS=ns-scitech-gitops-lynys
CLUSTER=tf-workload
APPNS=hol-wordpress
BASE="$HOME/Documents/files/hol-2711-24"
SUP="$HOME/sup-admin.conf"
GC="$HOME/gc.kubeconfig"
VC=vc-wld01-a.site-a.vcf.lab
PW="$(cat "$HOME/Desktop/PASSWORD.txt")"

say() { printf '\n== %s\n' "$*"; }

say "prereqs"
for t in sshpass kubectl terraform curl; do command -v $t >/dev/null || { echo "missing tool: $t"; exit 1; }; done
[ -d "$BASE/Module 12" ] || { echo "lab files not staged at $BASE"; exit 1; }

say "supervisor kubeconfig"
if ! KUBECONFIG=$SUP kubectl get ns "$NS" >/dev/null 2>&1; then
  OUT=$(sshpass -p "$PW" ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        root@$VC /usr/lib/vmware-wcp/decryptK8Pwd.py)
  CP=$(echo "$OUT" | awk '/^IP:/{print $2}')
  CPPW=$(echo "$OUT" | awk '/^PWD:/{print $2}')
  sshpass -p "$CPPW" ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        root@"$CP" cat /etc/kubernetes/admin.conf > "$SUP"
  chmod 600 "$SUP"
fi
KUBECONFIG=$SUP kubectl get ns "$NS" >/dev/null

say "database VM (terraform)"
cd "$BASE/Module 12"
grep -q '<VCFA_ORG_ADMIN_PASSWORD>' terraform.tfvars && \
  sed -i "s|<VCFA_ORG_ADMIN_PASSWORD>|$PW|" terraform.tfvars
if ! KUBECONFIG=$SUP kubectl -n "$NS" get vm hol-db >/dev/null 2>&1; then
  terraform init -input=false >/dev/null
  terraform apply -input=false -auto-approve
fi
DB_HOST=$(terraform output -raw db_host 2>/dev/null || true)
echo "db_host: ${DB_HOST:-unknown (VM pre-existed; check terraform output db_host)}"
if [ -n "$DB_HOST" ]; then
  echo "waiting for the MySQL greeting (a couple of minutes after first boot)"
  for i in $(seq 1 40); do
    if timeout 5 bash -c "exec 3<>/dev/tcp/$DB_HOST/3306; head -c 60 <&3" 2>/dev/null | strings | grep -q .; then
      echo "database answering"; break
    fi
    sleep 15
  done
fi

say "VKS cluster (raw Cluster CR)"
if ! KUBECONFIG=$SUP kubectl -n "$NS" get cluster $CLUSTER >/dev/null 2>&1; then
  KUBECONFIG=$SUP kubectl apply -f - <<EOF
apiVersion: cluster.x-k8s.io/v1beta1
kind: Cluster
metadata:
  name: $CLUSTER
  namespace: $NS
spec:
  clusterNetwork:
    pods:
      cidrBlocks: ["192.168.156.0/20"]
    services:
      cidrBlocks: ["10.96.0.0/12"]
    serviceDomain: cluster.local
  topology:
    class: builtin-generic-v3.6.0
    classNamespace: vmware-system-vks-public
    version: v1.33.6+vmware.1-fips
    controlPlane:
      replicas: 1
    workers:
      machineDeployments:
        - class: node-pool
          name: node-pool-1
          replicas: 1
    variables:
      - name: vmClass
        value: best-effort-medium
      - name: storageClass
        value: vsan-default-storage-policy
      - name: vsphereOptions
        value:
          persistentVolumes:
            defaultStorageClass: vsan-default-storage-policy
EOF
fi
echo "waiting for the cluster (10-20 min; a silent 7 minute placement retry is normal)"
KUBECONFIG=$SUP kubectl -n "$NS" wait cluster/$CLUSTER \
  --for=condition=Available --timeout=25m

say "guest kubeconfig + credential seeding"
KUBECONFIG=$SUP kubectl -n "$NS" get secret ${CLUSTER}-kubeconfig \
  -o jsonpath='{.data.value}' | base64 -d > "$GC"; chmod 600 "$GC"
kubectl --kubeconfig "$GC" get ns "$APPNS" >/dev/null 2>&1 || \
  kubectl --kubeconfig "$GC" create ns "$APPNS"
if ! kubectl --kubeconfig "$GC" -n "$APPNS" get secret hol-db-credentials >/dev/null 2>&1; then
  KUBECONFIG=$SUP bash "$BASE/GitLab/infra/seed_db_credentials.sh" "$CLUSTER" "$NS" "$APPNS"
  # the seed script removes gc.kubeconfig on exit; put it back
  KUBECONFIG=$SUP kubectl -n "$NS" get secret ${CLUSTER}-kubeconfig \
    -o jsonpath='{.data.value}' | base64 -d > "$GC"; chmod 600 "$GC"
fi

say "WordPress"
kubectl --kubeconfig "$GC" apply -f "$BASE/GitLab/apps/apps/hol-wordpress/manifest.yaml"
kubectl --kubeconfig "$GC" -n "$APPNS" wait pod --all --for=condition=Ready --timeout=5m
IP=$(kubectl --kubeconfig "$GC" -n "$APPNS" get svc wordpress-lb \
  -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
echo "site address: http://$IP/"
for i in $(seq 1 20); do
  code=$(curl -s -m 8 -o /dev/null -w '%{http_code}' -L "http://$IP/" || true)
  [ "$code" = "200" ] && break; sleep 10
done

say "complete the installer"
if curl -s -m 10 -L "http://$IP/" | grep -qi install; then
  curl -sk -m 30 -L "http://$IP/wp-admin/install.php?step=2" \
    --data-urlencode "weblog_title=SciTech Ops Blog" \
    --data-urlencode "user_name=admin" \
    --data-urlencode "admin_password=$PW" \
    --data-urlencode "admin_password2=$PW" \
    --data-urlencode "pw_weak=1" \
    --data-urlencode "admin_email=admin@scitech.lab" \
    --data-urlencode "blog_public=1" >/dev/null
fi
curl -s -m 10 -L "http://$IP/" | grep -qi install \
  && echo "NOTE: site still shows the installer; finish it in a browser" \
  || echo "site is live"

say "done"
echo "WordPress: http://$IP/  (admin / the standard lab password)"
echo "Database:  ${DB_HOST:-see terraform output} (survives cluster teardown)"
echo "Teardown:  kubectl -n $NS delete cluster $CLUSTER   removes the cluster+site;"
echo "           terraform destroy in $BASE/'Module 12'   removes the database."
