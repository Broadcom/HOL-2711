terraform {
  # REQUIRED for the GitLab HTTP state backend. Without this empty block the
  # -backend-config flags in .gitlab-ci.yml are silently ignored, Terraform
  # falls back to the LOCAL backend, and state is written into the job working
  # directory and discarded when the job ends. The symptom is a second run
  # planning a create for something that already exists.
  backend "http" {}

  required_providers {
    vcfa = { source = "vmware/vcfa", version = "1.2.0" }
  }
}

variable "vcfa_url"      { type = string }
variable "vcfa_org"      { type = string }
variable "vcfa_user"     { type = string }
variable "vcfa_password" {
  type      = string
  sensitive = true
}
variable "project"       { type = string }
variable "namespace"     { type = string }
variable "cluster_name"  { type = string }
variable "worker_count"  { type = number }
variable "os_image_version" {
  type    = string
  default = "24.04"
}

provider "vcfa" {
  url                  = var.vcfa_url
  org                  = var.vcfa_org
  user                 = var.vcfa_user
  password             = var.vcfa_password
  auth_type            = "integrated"
  allow_unverified_ssl = true
}

resource "vcfa_vks_cluster" "workload" {
  name    = var.cluster_name
  # The TKR NAME (v1.33.6---vmware.1-fips-vkr.2) is accepted on create but the
  # server stores the normalised Kubernetes version, so declaring the TKR name
  # leaves a permanent in-place diff. Declare what it stores.
  version = "v1.33.6+vmware.1-fips"

  # This provider is built on the plugin framework, so every nested section is
  # an ATTRIBUTE assigned with "=", not a block. Writing `context { ... }`
  # fails with "Blocks of type context are not expected here".
  context = {
    project   = var.project
    namespace = var.namespace
  }

  # Declare what the platform ACTUALLY uses, not what you asked for. The
  # supervisor rewrites both fields after creation:
  #   name      builtin-generic-v3.3.0   -> builtin-generic-v3.6.0
  #   namespace <your supervisor ns>     -> vmware-system-vks-public
  # It auto-upgrades to the newest compatible ClusterClass, and the real
  # ClusterClasses live in vmware-system-vks-public (a tenant namespace only
  # shows a subset). Both fields force replacement, so getting this wrong means
  # every pipeline run plans "1 to add, 1 to destroy" and rebuilds the cluster.
  cluster_class = {
    name      = "builtin-generic-v3.6.0"
    namespace = "vmware-system-vks-public"
  }

  cluster_network = {
    pods     = { cidr_blocks = ["192.168.156.0/20"] }
    services = { cidr_blocks = ["10.96.0.0/12"] }
  }

  # os_image.version is REQUIRED here even though it looks optional, and it is
  # the single most confusing field in this resource.
  #
  # "ubuntu" alone resolves only while exactly one Ubuntu image exists for the
  # chosen Kubernetes version. This lab carries two - 22.04 and 24.04, both for
  # v1.33.6+vmware.1-fips - and the admission webhook refuses the ambiguity:
  #
  #   admission webhook "tkr-resolver-cluster-webhook.tanzu.vmware.com" denied
  #   the request: Multiple OSImages resolved for Control Plane
  #
  # The trap is that this only bites on CREATE. The server does not persist the
  # field, so after a successful create the plan looks clean without it, and
  # removing it appears harmless right up until the next teardown and rebuild.
  #
  # Because it is not persisted, leaving it in produces a permanent in-place
  # diff, and applying that diff crashes the provider with "does not correlate
  # with any element in actual. This is a bug in the provider." So it is
  # declared for create and ignored thereafter.
  control_plane = {
    replicas = 1
    os_image = {
      name    = "ubuntu"
      version = var.os_image_version
    }
  }

  machine_deployments = [
    {
      name     = "${var.cluster_name}-np"
      class    = "node-pool"
      replicas = var.worker_count
      os_image = {
        name    = "ubuntu"
        version = var.os_image_version
      }
    }
  ]

  lifecycle {
    # machine_deployments is a SET, not a list, so there is no
    # machine_deployments[0] to reach into - Terraform rejects the index with
    # "Elements of a set are identified only by their value". The narrowest
    # workable path is therefore the whole attribute for the node pools and a
    # precise one for the control plane.
    #
    # The cost is real and worth knowing: worker_count changes are ignored
    # after create. Scaling the node pool is a `terraform apply -replace` or a
    # `vcf cluster scale`, not a git commit.
    ignore_changes = [
      control_plane.os_image.version,
      machine_deployments,
    ]
  }

  # Without this, apply returns as soon as the Cluster OBJECT is created, not
  # when the cluster is usable. The next pipeline stage then looks for the
  # CAPI-published <cluster>-kubeconfig secret and gets "not found", because
  # the control plane has not come up yet.
  wait_for = {
    available = true
  }

  timeouts = {
    create = "45m"
    delete = "30m"
  }

  # Values are JSON strings: the underlying CCI Cluster topology variables are
  # arbitrary JSON, so the provider takes them encoded.
  # SCALARS GO IN PLAIN, objects go in JSON-encoded. The API returns scalar
  # values unquoted, so jsonencode("best-effort-small") produces a permanent
  # in-place diff: TF holds "\"best-effort-small\"" and the server reports
  # best-effort-small, forever.
  variables = [
    { name = "vmClass",      value = "best-effort-small" },
    { name = "storageClass", value = "vsan-default-storage-policy" },
    { name = "vsphereOptions", value = jsonencode({
        persistentVolumes = { defaultStorageClass = "vsan-default-storage-policy" }
    }) },
  ]
}

output "cluster_name"           { value = vcfa_vks_cluster.workload.name }
output "control_plane_endpoint" { value = vcfa_vks_cluster.workload.control_plane_endpoint }
