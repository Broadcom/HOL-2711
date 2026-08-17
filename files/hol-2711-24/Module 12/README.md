# Module 1: deploy a VM with Terraform

Run from the console, by hand. This is the "what if on-prem worked like the
public cloud" moment: declare a machine, `apply`, get an address.

```bash
cd demo/hol-data

cat > terraform.tfvars <<'EOF'
vcfa_url      = "https://<vcfa>"
vcfa_org      = "<org>"
vcfa_user     = "admin"
vcfa_password = "<password>"
project       = "<project>"
namespace     = "<supervisor-namespace>"
EOF

terraform init
terraform apply
```

About 4 minutes. Then:

```
db_host        = "172.18.0.x"
db_name        = "wordpress"
db_user        = "wordpress"
seeded_secrets = ["hol-db-credentials", "hol-wp-salts"]
```

Cloud-init needs another minute or two after the VM powers on to finish
installing MySQL. Check it:

```bash
timeout 5 bash -c '</dev/tcp/<db_host>/3306' && echo reachable
```

## What it built

A VM Service virtual machine running MySQL, published on a load balancer, plus
two Secrets in the supervisor namespace that later modules consume.

Two providers, one run. `vmware/vcfa` has no VirtualMachine resource, so it
mints a namespace-scoped credential and hands it to the Kubernetes provider:

```hcl
data "vcfa_kubeconfig" "ns" { ... }

provider "kubernetes" {
  host  = data.vcfa_kubeconfig.ns.host
  token = data.vcfa_kubeconfig.ns.token
}
```

No kubeconfig file is shipped anywhere; the token is minted per run.

## The question this leaves you with

State is a local file, on one machine, belonging to one person. Nobody else can
run this safely and there is no record of who applied what.

That is Module 2.

## Do not destroy this

This VM holds the data. Everything else in the lab is disposable; this is not.
It has its own Terraform state precisely so the cluster's destroy job cannot
reach it.

```bash
terraform destroy    # only when finished with the lab entirely
```
