terraform {

  required_providers {
    vcfa       = { source = "vmware/vcfa", version = "1.2.0" }
    kubernetes = { source = "hashicorp/kubernetes", version = "2.38.0" }
    random     = { source = "hashicorp/random", version = "3.7.2" }
  }
}

variable "vcfa_url"      { type = string }
variable "vcfa_org"      { type = string }
variable "vcfa_user"     { type = string }
variable "vcfa_password" {
  type      = string
  sensitive = true
}
variable "project"   { type = string }
variable "namespace" { type = string }

variable "db_vm_name" {
  type    = string
  default = "hol-db"
}
variable "vm_class" {
  type    = string
  default = "best-effort-small"
}
variable "storage_class" {
  type    = string
  default = "vsan-default-storage-policy"
}
variable "vm_image" {
  type    = string
  default = "ubuntu-24-04-gold-ovf"
}
variable "db_name" {
  type    = string
  default = "wordpress"
}
variable "db_user" {
  type    = string
  default = "wordpress"
}

provider "vcfa" {
  url                  = var.vcfa_url
  org                  = var.vcfa_org
  user                 = var.vcfa_user
  password             = var.vcfa_password
  auth_type            = "integrated"
  allow_unverified_ssl = true
}

data "vcfa_kubeconfig" "ns" {
  project_name              = var.project
  supervisor_namespace_name = var.namespace
}

provider "kubernetes" {
  host                   = data.vcfa_kubeconfig.ns.host
  token                  = data.vcfa_kubeconfig.ns.token
  insecure               = data.vcfa_kubeconfig.ns.insecure_skip_tls_verify
}

resource "random_password" "db" {
  length  = 20
  special = false
}

resource "random_password" "holuser" {
  length  = 16
  special = false
}

locals {
  wp_salt_keys = [
    "AUTH_KEY", "SECURE_AUTH_KEY", "LOGGED_IN_KEY", "NONCE_KEY",
    "AUTH_SALT", "SECURE_AUTH_SALT", "LOGGED_IN_SALT", "NONCE_SALT",
  ]
}

resource "random_password" "wp_salt" {
  for_each = toset(local.wp_salt_keys)
  length   = 64
  special  = false
}

resource "kubernetes_secret" "wp_salts" {
  metadata {
    name      = "hol-wp-salts"
    namespace = var.namespace
  }
  data = {
    for k in local.wp_salt_keys : k => random_password.wp_salt[k].result
  }
}

locals {
  cloud_config = <<-CLOUDCFG
    #cloud-config
    hostname: ${var.db_vm_name}
    package_update: true
    packages:
      - mysql-server
    ssh_pwauth: true
    users:
      - name: holuser
        lock_passwd: false
        plain_text_passwd: ${random_password.holuser.result}
        sudo: ALL=(ALL) NOPASSWD:ALL
        shell: /bin/bash
    write_files:
      # The filename has to sort AFTER mysqld.cnf, not just look like a
      # high-priority drop-in. MySQL reads !includedir files in ALPHABETICAL
      # order and last-wins, and '9' sorts before 'm', so the conventional
      # "99-" prefix loses to the packaged mysqld.cnf and the server stays
      # bound to 127.0.0.1. The load balancer still answers on 3306 (Avi
      # accepts at the VIP), so this fails as a connection refused from the
      # application, not as an obviously dead port.
      - path: /etc/mysql/mysql.conf.d/zz-hol.cnf
        content: |
          [mysqld]
          bind-address = 0.0.0.0
          mysqlx-bind-address = 127.0.0.1
          # WordPress 4.8.3 era clients cannot negotiate MySQL 8 defaults:
          # utf8mb4_0900 collation and caching_sha2_password both fail the
          # handshake. Pin the compatible charset and auth plugin.
          character-set-server = utf8mb4
          collation-server = utf8mb4_unicode_ci
          default_authentication_plugin = mysql_native_password
    runcmd:
      - systemctl enable --now mysql
      - systemctl restart mysql
      # MySQL 8 on Ubuntu authenticates local root through auth_socket, so
      # these run as root with no password and no password on a command line.
      - mysql -e "CREATE DATABASE IF NOT EXISTS ${var.db_name} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
      - mysql -e "CREATE USER IF NOT EXISTS '${var.db_user}'@'%' IDENTIFIED WITH mysql_native_password BY '${random_password.db.result}';"
      - mysql -e "GRANT ALL PRIVILEGES ON ${var.db_name}.* TO '${var.db_user}'@'%'; FLUSH PRIVILEGES;"
      - touch /var/lib/cloud/hol-mysql-ready
  CLOUDCFG
}

resource "kubernetes_secret" "bootstrap" {
  metadata {
    name      = "${var.db_vm_name}-bootstrap"
    namespace = var.namespace
  }
  data = {
    "user-data" = local.cloud_config
  }
}

resource "kubernetes_secret" "db_credentials" {
  metadata {
    name      = "hol-db-credentials"
    namespace = var.namespace
  }
  data = {
    host     = local.db_host
    database = var.db_name
    username = var.db_user
    password = random_password.db.result
  }
}

resource "kubernetes_manifest" "db_vm" {
  manifest = {
    apiVersion = "vmoperator.vmware.com/v1alpha3"
    kind       = "VirtualMachine"
    metadata = {
      name      = var.db_vm_name
      namespace = var.namespace
      labels = {
        "vm-selector" = var.db_vm_name
      }
    }
    spec = {
      className    = var.vm_class
      imageName    = var.vm_image
      storageClass = var.storage_class
      powerState   = "PoweredOn"
      bootstrap = {
        cloudInit = {
          rawCloudConfig = {
            name = kubernetes_secret.bootstrap.metadata[0].name
            key  = "user-data"
          }
        }
      }
    }
  }

  timeouts {
    create = "20m"
  }
}

resource "kubernetes_manifest" "db_service" {
  manifest = {
    apiVersion = "vmoperator.vmware.com/v1alpha3"
    kind       = "VirtualMachineService"
    metadata = {
      name      = "${var.db_vm_name}-lb"
      namespace = var.namespace
    }
    spec = {
      type = "LoadBalancer"
      selector = {
        "vm-selector" = var.db_vm_name
      }
      ports = [
        {
          name       = "mysql"
          protocol   = "TCP"
          port       = 3306
          targetPort = 3306
        },
        {
          name       = "ssh"
          protocol   = "TCP"
          port       = 22
          targetPort = 22
        },
      ]
    }
  }

  wait {
    fields = {
      "status.loadBalancer.ingress[0].ip" = "^(\\d+\\.){3}\\d+$"
    }
  }

  timeouts {
    create = "10m"
  }

  depends_on = [kubernetes_manifest.db_vm]
}

data "kubernetes_resource" "db_service" {
  api_version = "vmoperator.vmware.com/v1alpha3"
  kind        = "VirtualMachineService"
  metadata {
    name      = "${var.db_vm_name}-lb"
    namespace = var.namespace
  }
  depends_on = [kubernetes_manifest.db_service]
}

locals {
  db_host = data.kubernetes_resource.db_service.object.status.loadBalancer.ingress[0].ip
}

output "db_host" {
  value = local.db_host
}

output "db_name" { value = var.db_name }
output "db_user" { value = var.db_user }

output "seeded_secrets" {
  value = [
    kubernetes_secret.db_credentials.metadata[0].name,
    kubernetes_secret.wp_salts.metadata[0].name,
  ]
  description = "Secrets in the supervisor namespace the app pipeline copies into the guest cluster"
}
