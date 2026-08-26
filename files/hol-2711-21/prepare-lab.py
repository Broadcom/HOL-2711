#!/usr/bin/env python3

import argparse
import base64
import json
import os
import re
import ssl
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional
from xml.sax.saxutils import escape, quoteattr
import xml.etree.ElementTree as ET

import requests
import urllib3

try:
    from pyVim.connect import SmartConnect, Disconnect
    from pyVmomi import vim
except ImportError:
    SmartConnect = None
    Disconnect = None
    vim = None


# ============================================================
# Logging
# ============================================================

def info(message: str) -> None:
    print(f"[INFO]   {message}")


def create(message: str) -> None:
    print(f"[CREATE] {message}")


def update(message: str) -> None:
    print(f"[UPDATE] {message}")


def skip(message: str) -> None:
    print(f"[SKIP]   {message}")


def warn(message: str) -> None:
    print(f"[WARN]   {message}")


# ============================================================
# Generic helpers
# ============================================================


def get_value(obj: Any, *names: str, default=None):
    if not isinstance(obj, dict):
        return default
    for name in names:
        if name in obj and obj[name] is not None:
            return obj[name]
    return default


def resolve_path(path_value: str, config_dir: Path) -> Path:
    p = Path(path_value).expanduser()
    if not p.is_absolute():
        p = (config_dir / p).resolve()
    return p


def read_text_file(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(path)
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError(f"File is empty: {path}")
    return value


def object_id(obj: Any) -> Optional[str]:
    if not isinstance(obj, dict):
        return None
    for name in ("id", "urn", "policy", "category_id", "tag", "supervisorId"):
        value = obj.get(name)
        if isinstance(value, str) and value:
            return value
    for name in ("orgRef", "regionRef", "region", "contentLibrary", "contentLibraryRef"):
        ref = obj.get(name)
        if isinstance(ref, dict):
            value = object_id(ref)
            if value:
                return value
    return None


def object_ref(obj: Any, *names: str) -> Optional[Dict[str, Any]]:
    if not isinstance(obj, dict):
        return None
    for name in names:
        value = obj.get(name)
        if isinstance(value, dict):
            return value
    return None


def string_set_equal(left: List[str], right: List[str]) -> bool:
    return sorted(set(str(x) for x in left if x is not None)) == \
           sorted(set(str(x) for x in right if x is not None))


# ============================================================
# REST client
# ============================================================

class RestClient:
    def __init__(self, server: str, verify: bool = True, headers: Optional[Dict[str, str]] = None):
        self.server = server
        self.verify = verify
        self.session = requests.Session()
        self.session.verify = verify
        if headers:
            self.session.headers.update(headers)

    def url(self, path: str) -> str:
        return f"https://{self.server}{path}"

    def request(
        self,
        method: str,
        path_or_url: str,
        max_attempts: int = 5,
        retry_statuses: tuple = (500, 502, 503, 504),
        **kwargs,
    ):
        url = (
            path_or_url
            if path_or_url.startswith("http")
            else self.url(path_or_url)
        )

        timeout = kwargs.pop("timeout", 120)
        retry_seconds = 2
        last_error = None

        for attempt in range(1, max_attempts + 1):
            try:
                response = self.session.request(
                    method,
                    url,
                    timeout=timeout,
                    **kwargs,
                )

            except requests.exceptions.RequestException as exc:
                last_error = (
                    f"{method} {url} failed with connection error: {exc}"
                )

                if attempt >= max_attempts:
                    raise RuntimeError(last_error) from None

                warn(
                    f"{method} {url} connection failure "
                    f"(attempt {attempt}/{max_attempts}); "
                    f"retrying in {retry_seconds}s"
                )

                time.sleep(retry_seconds)
                retry_seconds = min(retry_seconds * 2, 30)
                continue

            if not response.ok:
                try:
                    detail = response.json()
                    formatted = json.dumps(detail, indent=2)
                except Exception:
                    formatted = response.text

                last_error = (
                    f"{method} {url} failed: "
                    f"HTTP {response.status_code}\n{formatted}"
                )

                permanent_500_markers = (
                    "Cannot connect to LDAP because the hostname is empty",
                    "Cannot specify nameInSource for a new group",
                )

                permanent_error = (
                    response.status_code == 500
                    and any(
                        marker in formatted
                        for marker in permanent_500_markers
                    )
                )

                if (
                    response.status_code in retry_statuses
                    and not permanent_error
                    and attempt < max_attempts
                ):
                    warn(
                        f"{method} {url} returned HTTP "
                        f"{response.status_code} "
                        f"(attempt {attempt}/{max_attempts}); "
                        f"retrying in {retry_seconds}s"
                    )

                    time.sleep(retry_seconds)
                    retry_seconds = min(retry_seconds * 2, 30)
                    continue

                raise RuntimeError(last_error)

            if attempt > 1:
                info(
                    f"{method} {url} succeeded on attempt {attempt}"
                )

            if response.status_code == 204 or not response.content:
                return None

            ctype = response.headers.get("content-type", "")

            if "json" in ctype:
                return response.json()

            return response.text

        raise RuntimeError(last_error or f"{method} {url} failed")

    def get(self, path: str, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs):
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs):
        return self.request("PUT", path, **kwargs)

    def delete(self, path: str, **kwargs):
        return self.request("DELETE", path, **kwargs)

    def get_optional(self, path: str, **kwargs):
        kwargs.setdefault("max_attempts", 1)
        try:
            return self.get(path, **kwargs)
        except RuntimeError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise

    def paged(self, path: str, page_size: int = 128) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []
        page = 1

        while True:
            sep = "&" if "?" in path else "?"
            payload = self.get(f"{path}{sep}page={page}&pageSize={page_size}")

            if payload is None:
                break

            if isinstance(payload, list):
                result.extend(x for x in payload if isinstance(x, dict))
                break

            if not isinstance(payload, dict):
                break

            values = payload.get("values") or payload.get("items") or []
            if not isinstance(values, list):
                values = []

            result.extend(x for x in values if isinstance(x, dict))

            total = payload.get("resultTotal")
            if total is not None and len(result) >= int(total):
                break

            if len(values) < page_size:
                break

            page += 1

        return result

class CciClient:
    API_PREFIX = "/cci/kubernetes/apis/infrastructure.cci.vmware.com/v1alpha2"
    NAMESPACE_CLASS_RESOURCE = "supervisornamespaceclasses"
    NAMESPACE_CLASS_CONFIG_RESOURCE = "supervisornamespaceclassconfigs"
    SUPERVISOR_NAMESPACE_RESOURCE = "supervisornamespaces"

    def __init__(self, rest: RestClient):
        self.rest = rest

    @staticmethod
    def _encode(value: Any) -> str:
        return urllib.parse.quote(str(value), safe="")

    def cluster_path(self, resource: str) -> str:
        return f"{self.API_PREFIX}/{resource}"

    def namespaced_path(self, project: str, resource: str) -> str:
        return f"{self.API_PREFIX}/namespaces/{self._encode(project)}/{resource}"

    def get_named(self, resource: str, name: str) -> Optional[Dict[str, Any]]:
        value = self.rest.get_optional(
            f"{self.cluster_path(resource)}/{self._encode(name)}"
        )
        return value if isinstance(value, dict) else None

    def get_namespace_class(self, name: str) -> Optional[Dict[str, Any]]:
        return self.get_named(self.NAMESPACE_CLASS_RESOURCE, name)

    def create_namespace_class(self, payload: Dict[str, Any]):
        return self.rest.post(self.cluster_path(self.NAMESPACE_CLASS_RESOURCE), json=payload)

    def get_namespace_class_config(self, name: str) -> Optional[Dict[str, Any]]:
        return self.get_named(self.NAMESPACE_CLASS_CONFIG_RESOURCE, name)

    def create_namespace_class_config(self, payload: Dict[str, Any]):
        return self.rest.post(
            self.cluster_path(self.NAMESPACE_CLASS_CONFIG_RESOURCE), json=payload
        )

    def list_supervisor_namespaces(self, project: str) -> List[Dict[str, Any]]:
        payload = self.rest.get(
            self.namespaced_path(project, self.SUPERVISOR_NAMESPACE_RESOURCE)
        )
        items = payload.get("items", []) if isinstance(payload, dict) else []
        return [item for item in items if isinstance(item, dict)]

    def find_supervisor_namespace(
        self,
        project: str,
        configured_name: str,
    ) -> Optional[Dict[str, Any]]:
        # Reconciliation uses the JSON/YAML `name` as the actual Kubernetes
        # metadata.name. Do not compare it with generateName and do not use
        # prefix matching here.
        wanted = configured_name.strip().casefold()

        for item in self.list_supervisor_namespaces(project):
            meta = item.get("metadata") or {}
            actual_name = str(meta.get("name", "") or "").strip().casefold()

            if actual_name == wanted:
                return item

        return None

    def create_supervisor_namespace(self, project: str, payload: Dict[str, Any]):
        return self.rest.post(
            self.namespaced_path(project, self.SUPERVISOR_NAMESPACE_RESOURCE),
            json=payload,
        )

    def wait_supervisor_namespace(
        self,
        project: str,
        configured_name: str,
        timeout: int = 600,
        poll: int = 5,
    ) -> Dict[str, Any]:
        last_phase = {"value": None}

        def state(resource):
            status = resource.get("status") if isinstance(resource, dict) else {}
            return status if isinstance(status, dict) else {}

        def phase(resource):
            status = state(resource)
            return str(
                status.get("phase") or status.get("status") or status.get("state") or ""
            ).strip()

        def ready(resource):
            if not resource:
                return False
            if phase(resource).casefold() in {"ready", "created", "running", "active", "succeeded"}:
                return True
            for condition in state(resource).get("conditions") or []:
                if not isinstance(condition, dict):
                    continue
                if (
                    str(condition.get("type", "")).casefold() in {"ready", "created"}
                    and str(condition.get("status", "")).casefold() == "true"
                ):
                    return True
            return False

        def failed(resource):
            p = phase(resource).casefold()
            if p in {"failed", "error", "errored"}:
                return True
            return any(
                isinstance(c, dict)
                and str(c.get("reason", "")).casefold() in {"failed", "error"}
                for c in (state(resource).get("conditions") or [])
            )

        def on_wait(resource, remaining):
            p = phase(resource)
            if p and p != last_phase["value"]:
                info(
                    f"Supervisor namespace '{configured_name}' status: {p} "
                    f"({remaining}s remaining)"
                )
                last_phase["value"] = p

        try:
            return wait_for(
                f"Supervisor namespace '{configured_name}'",
                lambda: self.find_supervisor_namespace(project, configured_name),
                ready,
                failed=failed,
                timeout=timeout,
                poll=poll,
                on_wait=on_wait,
            )
        except RuntimeError:
            existing = self.find_supervisor_namespace(project, configured_name)
            if existing:
                warn(
                    f"Supervisor namespace '{configured_name}' exists but did not "
                    f"reach READY within {timeout}s"
                )
                return existing
            raise


def paged_values(
    client: RestClient,
    path: str,
    page_size: int = 128,
) -> List[Dict[str, Any]]:
    """Compatibility wrapper around RestClient.paged()."""
    return client.paged(path, page_size=page_size)


def casefold_key(value: Any) -> str:
    return str(value or "").strip().casefold()


def urn_uuid(value: Any) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    return value.rsplit(":", 1)[-1] if value.startswith("urn:") else value


def wait_for(
    description: str,
    getter,
    ready,
    failed=None,
    timeout: int = 600,
    poll: int = 5,
    on_wait=None,
):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = getter()
        if ready(last):
            return last
        if failed and failed(last):
            raise RuntimeError(f"{description} entered a failure state: {last}")
        if on_wait:
            on_wait(last, max(0, int(deadline - time.time())))
        time.sleep(poll)
    raise RuntimeError(
        f"Timed out after {timeout}s waiting for {description}. Last value: {last}"
    )


def ensure_resource(
    *,
    name: str,
    lookup,
    create_func,
    resource_label: str,
):
    """Generic idempotent create-if-missing helper."""
    existing = lookup()
    if existing is not None:
        skip(f"{resource_label} '{name}' already exists")
        return existing

    create(f"{resource_label} '{name}'")
    created = create_func()
    info(f"{resource_label} '{name}' created")
    return created


# ============================================================
# vCenter authentication / pyVmomi inventory
# ============================================================

def connect_vcenter(server: str, username: str, password: str, ignore_cert: bool):
    if SmartConnect is None:
        raise RuntimeError("pyvmomi is required: python3 -m pip install pyvmomi")
    context = None
    if ignore_cert:
        context = ssl._create_unverified_context()
    return SmartConnect(host=server, user=username, pwd=password, sslContext=context)


def find_vcenter_object(si, obj_type: str, name: str):
    mapping = {
        "virtualmachine": vim.VirtualMachine,
        "vm": vim.VirtualMachine,
        "vmhost": vim.HostSystem,
        "cluster": vim.ClusterComputeResource,
        "datacenter": vim.Datacenter,
        "datastore": vim.Datastore,
        "resourcepool": vim.ResourcePool,
        "folder": vim.Folder,
        "vapp": vim.VirtualApp,
    }
    key = obj_type.lower()
    if key not in mapping:
        raise RuntimeError(f"Unsupported vSphere object type '{obj_type}'")
    view = si.content.viewManager.CreateContainerView(
        si.content.rootFolder, [mapping[key]], True
    )
    try:
        for obj in view.view:
            if getattr(obj, "name", None) == name:
                return obj
    finally:
        view.Destroy()
    raise RuntimeError(f"{obj_type} '{name}' not found")


# ============================================================
# vCenter REST
# ============================================================

def vcenter_rest_session(server: str, username: str, password: str, verify: bool) -> RestClient:
    client = RestClient(server, verify=verify)
    auth = base64.b64encode(f"{username}:{password}".encode()).decode()
    result = client.post("/api/session", headers={"Authorization": f"Basic {auth}"})
    session_id = result if isinstance(result, str) else str(result)
    client.session.headers.update({"vmware-api-session-id": session_id})
    return client


def vc_list_categories(vc: RestClient):
    data = vc.get("/api/vcenter/tagging/categories")
    if isinstance(data, dict):
        return data.get("items", data.get("value", []))
    return data or []


def vc_list_tags(vc: RestClient):
    data = vc.get("/api/vcenter/tagging/tags")
    if isinstance(data, dict):
        return data.get("items", data.get("value", []))
    return data or []


def vc_get_category(vc: RestClient, name: str):
    for item in vc_list_categories(vc):
        info_obj = item.get("info", item)
        if info_obj.get("name") == name:
            return item
    return None


def vc_get_tag(vc: RestClient, category_name: str, tag_name: str):
    category = vc_get_category(vc, category_name)
    if not category:
        return None
    category_id = category.get("category_id") or category.get("category") or category.get("id")
    for item in vc_list_tags(vc):
        info_obj = item.get("info", item)
        if info_obj.get("name") == tag_name and info_obj.get("category") == category_id:
            return item
    return None


def vc_create_category_if_missing(vc: RestClient, cfg: Dict[str, Any]):
    existing = vc_get_category(vc, cfg["name"])
    if existing:
        skip(f"Category '{cfg['name']}' already exists")
        return existing
    body = {
        "name": cfg["name"],
        "description": cfg.get("description", ""),
        "cardinality": cfg.get("cardinality", "SINGLE").upper(),
        "associable_types": cfg.get("entity_types", []),
    }
    create(f"Category '{cfg['name']}'")
    cid = vc.post("/api/cis/tagging/category", json=body)
    return {"name": cfg["name"], "category_id": cid}


def vc_create_tag_if_missing(vc: RestClient, category_name: str, cfg: Dict[str, Any]):
    existing = vc_get_tag(vc, category_name, cfg["name"])
    if existing:
        skip(f"Tag '{category_name}/{cfg['name']}' already exists")
        return existing
    category = vc_get_category(vc, category_name)
    category_id = category.get("category_id") or category.get("category") or category.get("id")
    body = {
        "name": cfg["name"],
        "description": cfg.get("description", ""),
        "category_id": category_id,
    }
    create(f"Tag '{category_name}/{cfg['name']}'")
    tid = vc.post("/api/cis/tagging/tag", json=body)
    return {"name": cfg["name"], "tag": tid, "category": category_id}


def vc_compute_policy(vc: RestClient, name: str):
    policies = vc.get("/api/vcenter/compute/policies") or []
    for p in policies:
        if p.get("name") == name:
            return p
    return None


def vc_compute_capability(vc: RestClient, policy_type: str):
    caps = vc.get("/api/vcenter/compute/policies/capabilities") or []
    ptype = policy_type.lower()
    for c in caps:
        name = str(c.get("name", "")).lower()
        if ptype == "vm-host-affinity" and "host" in name and "affinity" in name and "anti" not in name:
            return c
        if ptype == "vm-host-anti-affinity" and "host" in name and "anti" in name:
            return c
    raise RuntimeError(f"No vCenter compute-policy capability found for '{policy_type}'")


def vc_create_compute_policy_if_missing(
    vc: RestClient,
    cfg: Dict[str, Any],
):
    name = str(cfg.get("name", "") or "").strip()

    if not name:
        raise RuntimeError(
            "Each vcenter.policies entry requires 'name'"
        )

    requirement = str(
        cfg.get("requirement", "preferred") or "preferred"
    ).strip().casefold()

    requirement_to_strictness = {
        "preferred": (
            "PREFERRED_DURING_PLACEMENT_"
            "PREFERRED_DURING_EXECUTION"
        ),
        "required": (
            "REQUIRED_DURING_PLACEMENT_"
            "PREFERRED_DURING_EXECUTION"
        ),
    }

    if requirement not in requirement_to_strictness:
        raise RuntimeError(
            f"vCenter compute policy '{name}' has unsupported "
            f"requirement '{requirement}'. Expected: preferred or required"
        )

    desired_strictness = requirement_to_strictness[requirement]

    existing = vc_compute_policy(vc, name)

    if existing:
        policy_id = str(
            existing.get("policy")
            or existing.get("id")
            or ""
        ).strip()

        if policy_id:
            encoded_policy_id = urllib.parse.quote(
                policy_id,
                safe="",
            )

            try:
                details = vc.get(
                    f"/api/vcenter/compute/policies/{encoded_policy_id}",
                    max_attempts=1,
                )
            except RuntimeError:
                details = None

            if isinstance(details, dict):
                current_strictness = str(
                    details.get("strictness", "") or ""
                ).strip().upper()

                if current_strictness:
                    if current_strictness == desired_strictness:
                        skip(
                            f"Compute policy '{name}' already exists "
                            f"with requirement '{requirement}'"
                        )
                        return existing

                    raise RuntimeError(
                        f"Compute policy '{name}' already exists with "
                        f"strictness '{current_strictness}', but JSON "
                        f"requires '{desired_strictness}'. vSphere 9.1 does "
                        "not support changing strictness with PATCH; "
                        "delete/recreate the compute policy to change its "
                        "Policy Requirement."
                    )

        skip(
            f"Compute policy '{name}' already exists; unable to confirm "
            f"Policy Requirement from returned policy details"
        )
        return existing

    vm_tag = vc_get_tag(
        vc,
        cfg["vm_tag"]["category"],
        cfg["vm_tag"]["tag"],
    )
    host_tag = vc_get_tag(
        vc,
        cfg["host_tag"]["category"],
        cfg["host_tag"]["tag"],
    )

    if not vm_tag or not host_tag:
        raise RuntimeError(
            f"Tags for compute policy '{name}' not found"
        )

    cap = vc_compute_capability(
        vc,
        cfg["type"],
    )

    vm_tag_id = vm_tag.get("tag") or vm_tag.get("id")
    host_tag_id = host_tag.get("tag") or host_tag.get("id")

    body = {
        "capability": cap.get("capability"),
        "name": name,
        "description": str(cfg.get("description", "") or ""),
        "vm_tag": vm_tag_id,
        "host_tag": host_tag_id,
        "strictness": desired_strictness,
    }

    info(
        f"Compute policy create payload for '{name}':\n"
        f"{json.dumps(body, indent=2)}"
    )

    create(
        f"Compute policy '{name}' "
        f"(requirement={requirement})"
    )

    pid = vc.post(
        "/api/vcenter/compute/policies",
        json=body,
    )

    return {
        "name": name,
        "policy": pid,
    }


def cis_value(payload):
    if isinstance(payload, dict) and "value" in payload:
        return payload["value"]
    return payload


def vc_vapi_object_type(config_type: str) -> str:
    mapping = {
        "virtualmachine": "VirtualMachine",
        "vm": "VirtualMachine",
        "vmhost": "HostSystem",
        "hostsystem": "HostSystem",
        "cluster": "ClusterComputeResource",
        "clustercomputeresource": "ClusterComputeResource",
        "datacenter": "Datacenter",
        "datastore": "Datastore",
        "resourcepool": "ResourcePool",
        "folder": "Folder",
        "vapp": "VirtualApp",
    }
    key = config_type.lower()
    if key not in mapping:
        raise RuntimeError(f"Unsupported vSphere tagging object type '{config_type}'")
    return mapping[key]


def vc_dynamic_id(
    config_type: str,
    moid: str,
    vcenter_instance_uuid: str = "",
) -> Dict[str, str]:
    """
    Build the DynamicID for normal vCenter inventory objects.

    For VMHost and normal VirtualMachine objects the CIS tagging API expects
    the raw managed-object ID (for example host-19 or vm-123). Do not append
    the vCenter instance UUID. Supervisor/VM Service VMs are skipped before
    reaching this code.
    """
    return {
        "type": vc_vapi_object_type(config_type),
        "id": str(moid or "").strip(),
    }


def vc_list_attached_tags(
    vc: RestClient,
    config_type: str,
    moid: str,
    vcenter_instance_uuid: str,
) -> List[str]:
    dynamic_id = vc_dynamic_id(config_type, moid, vcenter_instance_uuid)
    payload = vc.post(
        "/api/cis/tagging/tag-association?action=list-attached-tags",
        json={"object_id": dynamic_id},
    )
    value = cis_value(payload)
    return value if isinstance(value, list) else []


def vc_attach_tag(
    vc: RestClient,
    tag_id: str,
    config_type: str,
    moid: str,
    vcenter_instance_uuid: str,
) -> None:
    encoded_tag_id = urllib.parse.quote(tag_id, safe="")
    dynamic_id = vc_dynamic_id(config_type, moid, vcenter_instance_uuid)
    vc.post(
        f"/api/cis/tagging/tag-association/{encoded_tag_id}?action=attach",
        json={"object_id": dynamic_id},
    )


# ============================================================
# VCFA authentication
# ============================================================

def parse_vcfa_api_token_file(api_token_file: Path) -> str:
    raw = read_text_file(api_token_file)
    if raw.startswith("{"):
        token_obj = json.loads(raw)
        for key in ("refresh_token", "api_token", "token"):
            value = token_obj.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        raise RuntimeError(
            f"VCFA token file '{api_token_file}' is JSON but contains none of "
            "'refresh_token', 'api_token', or 'token'."
        )
    return raw.strip()


def vcfa_access_token(
    server: str,
    refresh_token: str,
    verify: bool,
    organization: Optional[str] = None,
    max_attempts: int = 6,
    initial_retry_seconds: int = 2,
) -> str:
    """
    Redeem a VCFA API/refresh token for a short-lived access token.

    Provider/System token:
        POST /oauth/provider/token

    Organization/Tenant token:
        POST /oauth/tenant/{organization}/token

    Transient gateway/upstream failures (HTTP 502/503/504) and requests
    connection errors are retried with exponential backoff. OAuth errors
    such as invalid_grant are not retried.
    """
    client = RestClient(server, verify=verify)

    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }

    if organization:
        encoded_org = urllib.parse.quote(
            str(organization),
            safe="",
        )
        endpoint = f"/oauth/tenant/{encoded_org}/token"
        context_name = f"organization '{organization}'"
    else:
        endpoint = "/oauth/provider/token"
        context_name = "provider"

    retry_seconds = max(1, int(initial_retry_seconds))

    for attempt in range(1, max_attempts + 1):
        try:
            data = client.post(
                endpoint,
                data=payload,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
            )

            if isinstance(data, dict) and data.get("access_token"):
                if attempt > 1:
                    info(
                        f"VCFA {context_name} OAuth token exchange "
                        f"succeeded on attempt {attempt}"
                    )
                return str(data["access_token"])

            raise RuntimeError(
                f"VCFA access token response from '{endpoint}' "
                "did not include access_token."
            )

        except requests.exceptions.RequestException as exc:
            transient = True
            error_text = str(exc)

        except RuntimeError as exc:
            error_text = str(exc)

            transient = any(
                marker in error_text
                for marker in (
                    "HTTP 502",
                    "HTTP 503",
                    "HTTP 504",
                )
            )

        if not transient:
            raise RuntimeError(error_text) from None

        if attempt >= max_attempts:
            raise RuntimeError(
                f"VCFA {context_name} OAuth endpoint remained unavailable "
                f"after {max_attempts} attempts.\n"
                f"Endpoint: https://{server}{endpoint}\n"
                f"Last error:\n{error_text}"
            ) from None

        warn(
            f"VCFA {context_name} OAuth endpoint temporarily unavailable "
            f"(attempt {attempt}/{max_attempts}); "
            f"retrying in {retry_seconds}s"
        )

        time.sleep(retry_seconds)
        retry_seconds = min(retry_seconds * 2, 30)

    raise RuntimeError(
        f"Unable to obtain VCFA {context_name} access token"
    )


def vcfa_client(
    server: str,
    api_token_file: Path,
    verify: bool,
    organization: Optional[str] = None,
) -> RestClient:
    refresh_token = parse_vcfa_api_token_file(api_token_file)

    token = vcfa_access_token(
        server,
        refresh_token,
        verify,
        organization=organization,
    )

    return RestClient(
        server,
        verify=verify,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json;version=10.0.0.0-alpha",
        },
    )


# ============================================================
# VCFA regions
# ============================================================

def vcfa_regions(client: RestClient):
    return client.paged("/cloudapi/v1/regions")


def vcfa_region(client: RestClient, name: str):
    for r in vcfa_regions(client):
        if r.get("name") == name:
            return r
    return None


def resolve_named_ref(objects: List[Dict[str, Any]], requested: Any, entity_type: str):
    if isinstance(requested, str):
        req_name, req_id = requested, ""
    else:
        req_name = str(get_value(requested, "name", default=""))
        req_id = str(get_value(requested, "id", "urn", "supervisorId", default=""))
    for obj in objects:
        obj_name = str(get_value(obj, "name", default=""))
        obj_id = str(get_value(obj, "id", "urn", "supervisorId", default=""))
        if (req_id and obj_id == req_id) or (req_name and obj_name == req_name):
            return {"name": obj_name, "id": obj_id}
    raise RuntimeError(f"{entity_type} '{req_name or req_id}' was not found")


def wait_region_ready(client: RestClient, name: str, timeout: int, poll: int):
    deadline = time.time() + timeout
    while time.time() < deadline:
        region = vcfa_region(client, name)
        if region:
            status = str(region.get("status", "")).upper()
            if status in ("", "READY"):
                return region
            if status in ("ERROR", "FAILED"):
                raise RuntimeError(f"VCFA region '{name}' entered status '{status}'")
            info(f"Region '{name}' status is '{status}'; checking again in {poll}s")
        else:
            info(f"Waiting for VCFA region '{name}' to appear")
        time.sleep(poll)
    raise RuntimeError(f"Timed out waiting for VCFA region '{name}' to become READY")


def create_or_update_region(client: RestClient, cfg: Dict[str, Any], timeout: int, poll: int):
    name = cfg.get("name")
    if not name:
        raise RuntimeError("vcfa.provider.regions[].name is required")

    existing = vcfa_region(client, name)
    nsx_all = client.paged("/cloudapi/v1/nsxManagers")
    supervisors_all = client.paged("/cloudapi/v1/supervisors")

    if "nsx_manager" in cfg:
        nsx = resolve_named_ref(nsx_all, cfg["nsx_manager"], "NSX Manager")
    elif existing:
        ref = object_ref(existing, "nsxManager")
        nsx = {"name": ref.get("name", ""), "id": ref.get("id", "")} if ref else None
    else:
        nsx = None
    if not nsx or not nsx.get("id"):
        raise RuntimeError(f"VCFA region '{name}' requires nsx_manager")

    if "supervisors" in cfg:
        supervisors = [resolve_named_ref(supervisors_all, x, "Supervisor") for x in cfg["supervisors"]]
    elif existing:
        supervisors = [
            {"name": s.get("name", ""), "id": s.get("id") or s.get("supervisorId")}
            for s in existing.get("supervisors", [])
            if s.get("id") or s.get("supervisorId")
        ]
    else:
        supervisors = []
    if not supervisors:
        raise RuntimeError(f"VCFA region '{name}' requires at least one supervisor")

    if "storage_policies" in cfg:
        storage_policies = sorted(set(str(x) for x in cfg["storage_policies"] if str(x).strip()))
    elif existing:
        storage_policies = existing.get("storagePolicies", [])
    else:
        storage_policies = []
    if not storage_policies:
        raise RuntimeError(f"VCFA region '{name}' requires at least one storage_policies entry")

    body = {
        "name": name,
        "nsxManager": nsx,
        "supervisors": supervisors,
        "storagePolicies": storage_policies,
    }
    if "description" in cfg:
        body["description"] = cfg.get("description", "")
    elif existing and existing.get("description"):
        body["description"] = existing["description"]

    if not existing:
        create(f"VCFA region '{name}'")
        client.post("/cloudapi/v1/regions", json=body)
        return wait_region_ready(client, name, timeout, poll)

    existing_nsx = object_ref(existing, "nsxManager") or {}
    changed = str(existing_nsx.get("id", "")) != str(nsx["id"])
    changed |= not string_set_equal(
        [x.get("id") or x.get("supervisorId") for x in existing.get("supervisors", [])],
        [x["id"] for x in supervisors],
    )
    changed |= not string_set_equal(existing.get("storagePolicies", []), storage_policies)
    if "description" in cfg:
        changed |= str(existing.get("description", "")) != str(cfg.get("description", ""))

    if not changed:
        skip(f"VCFA region '{name}' is already in desired state")
        return existing

    rid = object_id(existing)
    if not rid:
        raise RuntimeError(f"Unable to determine ID for VCFA region '{name}'")
    update(f"VCFA region '{name}'")
    client.put(f"/cloudapi/v1/regions/{urllib.parse.quote(rid, safe='')}", json=body)
    return wait_region_ready(client, name, timeout, poll)


# ============================================================
# VCFA infrastructure policies
# ============================================================

def wait_vcfa_compute_policy(
    client: RestClient,
    name: str,
    timeout: int,
    poll: int,
):
    """
    Wait for a vCenter compute policy to become visible in VCFA.

    The vCenterComputePolicies discovery endpoint can temporarily return
    HTTP 500/502/503/504 while provider inventory/discovery is refreshing.
    Those responses must not abort the overall synchronization wait.  They
    are treated as a failed poll and retried until the configured timeout.

    Non-transient HTTP errors still fail immediately.
    """
    deadline = time.time() + timeout
    attempt = 0
    last_error = None

    transient_markers = (
        "HTTP 500",
        "HTTP 502",
        "HTTP 503",
        "HTTP 504",
    )

    while time.time() < deadline:
        attempt += 1

        try:
            values = paged_values(
                client,
                "/cloudapi/v1/vCenterComputePolicies",
            )
            last_error = None

        except RuntimeError as exc:
            error_text = str(exc)

            if not any(
                marker in error_text
                for marker in transient_markers
            ):
                raise

            last_error = error_text
            remaining = max(0, int(deadline - time.time()))

            if remaining <= 0:
                break

            warn(
                f"VCFA compute-policy discovery endpoint is temporarily "
                f"unavailable while waiting for '{name}' "
                f"(check {attempt}, {remaining}s remaining); "
                f"checking again in {poll}s"
            )

            time.sleep(min(poll, remaining))
            continue

        for policy in values:
            if policy.get("name") == name:
                info(
                    f"VCFA discovered compute policy '{name}' "
                    f"after {attempt} check(s)"
                )
                return policy

        remaining = max(0, int(deadline - time.time()))

        if remaining <= 0:
            break

        info(
            f"VCFA has not discovered '{name}' yet; "
            f"checking again in {poll}s "
            f"({remaining}s remaining)"
        )

        time.sleep(min(poll, remaining))

    message = (
        f"Timed out after {timeout}s waiting for VCFA to discover "
        f"compute policy '{name}'"
    )

    if last_error:
        message += f". Last discovery endpoint error:\n{last_error}"

    raise RuntimeError(message)


def vcfa_infra_policies(client: RestClient):
    return client.paged("/cloudapi/v1/infraPolicies")


def normalize_infra_policy_rule(value: Any) -> Any:
    """
    Convert the JSON-friendly infrastructure policy rule schema to the VCFA
    Provider Infrastructure API schema.

    Supported JSON keys include:
      workload_policy_rule -> workloadPolicyRule
      guestos_family_rule  -> guestOsFamilyRule
      guest_os_family_rule -> guestOsFamilyRule
      guestos_rule         -> guestOsRule
      guest_os_rule        -> guestOsRule
      label_selector_rules -> labelSelectorRules
      rule_key             -> ruleKey

    Unknown keys are preserved so newer PolicyRule fields can pass through.
    """
    if isinstance(value, list):
        return [normalize_infra_policy_rule(item) for item in value]

    if not isinstance(value, dict):
        return value

    key_map = {
        "workload_policy_rule": "workloadPolicyRule",
        "guestos_family_rule": "guestOsFamilyRule",
        "guest_os_family_rule": "guestOsFamilyRule",
        "guestos_rule": "guestOsRule",
        "guest_os_rule": "guestOsRule",
        "label_selector_rules": "labelSelectorRules",
        "rule_key": "ruleKey",
    }

    result: Dict[str, Any] = {}

    for key, item in value.items():
        api_key = key_map.get(key, key)
        result[api_key] = normalize_infra_policy_rule(item)

    return result


def infra_policy_body(cfg: Dict[str, Any]) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "name": str(cfg.get("name", "") or "").strip(),
        "description": str(cfg.get("description", "") or ""),
        "vcComputePolicyName": cfg.get("vc_compute_policy_name"),
        "isMandatory": bool(cfg.get("is_mandatory", False)),
    }

    policy_rule = cfg.get("policy_rule")
    if policy_rule is not None:
        if not isinstance(policy_rule, dict):
            raise RuntimeError(
                f"Infrastructure policy '{body['name']}'.policy_rule "
                "must be an object"
            )

        body["policyRule"] = normalize_infra_policy_rule(policy_rule)

    return body


def infra_policy_managed_view(policy: Dict[str, Any]) -> Dict[str, Any]:
    """
    Return only fields managed by this JSON configuration so server-generated
    properties such as id, creationStatus, creationTime and sync state do not
    trigger false updates.
    """
    result: Dict[str, Any] = {
        "name": str(policy.get("name", "") or "").strip(),
        "description": str(policy.get("description", "") or ""),
        "vcComputePolicyName": policy.get("vcComputePolicyName"),
        "isMandatory": bool(policy.get("isMandatory", False)),
    }

    if "policyRule" in policy and policy.get("policyRule") is not None:
        result["policyRule"] = policy.get("policyRule")

    return result


def _canonical_policy_value(value: Any) -> Any:
    """
    Canonicalize JSON-like structures for stable comparison while preserving
    ordered rule arrays.
    """
    if isinstance(value, dict):
        return {
            key: _canonical_policy_value(value[key])
            for key in sorted(value)
        }

    if isinstance(value, list):
        return [_canonical_policy_value(item) for item in value]

    return value

def create_infra_policy_if_missing(
    client: RestClient,
    cfg: Dict[str, Any],
):
    name = str(cfg.get("name", "") or "").strip()
    if not name:
        raise RuntimeError(
            "Each vcfa.provider.infrastructure_policies entry requires 'name'"
        )

    desired = infra_policy_body(cfg)
    existing = None

    for policy in vcfa_infra_policies(client):
        if str(policy.get("name", "") or "").strip().casefold() == name.casefold():
            existing = policy
            break

    if existing is None:
        info(
            f"VCFA infrastructure policy create payload for '{name}':\n"
            f"{json.dumps(desired, indent=2)}"
        )
        create(f"VCFA infrastructure policy '{name}'")
        return client.post(
            "/cloudapi/v1/infraPolicies",
            json=desired,
        )

    current = infra_policy_managed_view(existing)

    # If policy_rule is omitted from JSON, leave an existing policyRule
    # unmanaged rather than deleting it. If policy_rule is explicitly present,
    # it becomes authoritative.
    if "policy_rule" not in cfg:
        current.pop("policyRule", None)
        desired.pop("policyRule", None)

    if _canonical_policy_value(current) == _canonical_policy_value(desired):
        skip(
            f"VCFA infrastructure policy '{name}' already matches configuration"
        )
        return existing

    changed_fields = []
    for field in (
        "description",
        "vcComputePolicyName",
        "isMandatory",
        "policyRule",
    ):
        if _canonical_policy_value(current.get(field)) != _canonical_policy_value(
            desired.get(field)
        ):
            changed_fields.append(field)

    policy_id = str(existing.get("id", "") or "").strip()
    if not policy_id:
        raise RuntimeError(
            f"VCFA infrastructure policy '{name}' differs from configuration "
            "but its id/URN was not returned by the API"
        )

    update_body = dict(desired)
    update_body["id"] = policy_id

    info(
        f"VCFA infrastructure policy '{name}' differs in: "
        f"{', '.join(changed_fields)}"
    )
    info(
        f"VCFA infrastructure policy update payload for '{name}':\n"
        f"{json.dumps(update_body, indent=2)}"
    )

    update(f"VCFA infrastructure policy '{name}'")

    encoded_id = urllib.parse.quote(policy_id, safe="")
    updated = client.put(
        f"/cloudapi/v1/infraPolicies/{encoded_id}",
        json=update_body,
    )

    info(
        f"VCFA infrastructure policy '{name}' updated"
    )

    return updated if isinstance(updated, dict) else existing


# ============================================================
# VCFA content libraries
# ============================================================

def system_client(client: RestClient) -> RestClient:
    new = RestClient(client.server, verify=client.verify, headers=dict(client.session.headers))
    new.session.headers["X-VMWARE-VCLOUD-AUTH-CONTEXT"] = "System"
    new.session.headers.pop("X-VMWARE-VCLOUD-TENANT-CONTEXT", None)
    return new


def content_libraries(client: RestClient):
    return paged_values(system_client(client), "/cloudapi/v1/contentLibraries")


def content_library(client: RestClient, name: str):
    for lib in content_libraries(client):
        lib_name = get_value(lib, "name", default="")
        if not lib_name:
            ref = object_ref(lib, "contentLibraryRef", "library")
            lib_name = get_value(ref, "name", default="") if ref else ""
        if lib_name == name:
            return lib
    return None


def wait_content_library_ready(
    client: RestClient,
    library_name: str,
    *,
    system_scope: bool,
    timeout: int = 600,
    poll: int = 5,
) -> Dict[str, Any]:
    """
    Wait until a content library is READY before any item create/upload work.

    Provider libraries are queried in System scope; organization libraries are
    queried through the already organization-scoped client.  NOT_READY and
    PARTIALLY_READY are transient. FAILED/ERROR are fatal.
    """
    deadline = time.time() + max(1, int(timeout))
    poll = max(1, int(poll))
    last_status = None
    last_seen = None

    while time.time() < deadline:
        library = (
            content_library(client, library_name)
            if system_scope
            else tenant_content_library(client, library_name)
        )

        remaining = max(0, int(deadline - time.time()))

        if not library:
            if last_seen is not False:
                info(
                    f"Waiting for content library '{library_name}' to become "
                    f"queryable before item upload ({remaining}s remaining)"
                )
                last_seen = False
            time.sleep(min(poll, max(1, remaining)))
            continue

        last_seen = True
        status = str(library.get("status", "") or "").upper()

        if status == "READY":
            info(
                f"Content library '{library_name}' is READY; item upload may proceed"
            )
            return library

        if status in ("FAILED", "ERROR"):
            raise RuntimeError(
                f"Content library '{library_name}' entered status '{status}' "
                "before content-library item upload"
            )

        if status != last_status:
            info(
                f"Content library '{library_name}' is not READY "
                f"(status='{status or 'UNKNOWN'}'); waiting before item upload "
                f"({remaining}s remaining)"
            )
            last_status = status

        time.sleep(min(poll, max(1, remaining)))

    raise RuntimeError(
        f"Timed out after {timeout}s waiting for content library "
        f"'{library_name}' to become READY before item upload. "
        f"Last status: '{last_status or 'UNKNOWN'}'"
    )


def storage_classes(client: RestClient):
    return paged_values(system_client(client), "/cloudapi/v1/storageClasses")


def resolve_library_storage_classes(client: RestClient, cfg: Dict[str, Any]):
    requested = cfg.get("storage_classes", [])
    if not requested:
        raise RuntimeError(
            f"Provider content library '{cfg.get('name','<unnamed>')}' requires storage_classes"
        )
    available = storage_classes(client)
    resolved = []
    for req in requested:
        if isinstance(req, str):
            req_name, req_region = req, ""
        else:
            req_name = str(req.get("name", ""))
            req_region = str(req.get("region", ""))
        matches = []
        for sc in available:
            sc_name = str(sc.get("name", ""))
            kube_name = str(sc.get("kubernetesCompliantName", ""))
            if req_name not in (sc_name, kube_name):
                continue
            region_ref = object_ref(sc, "region", "regionRef") or {}
            region_name = str(region_ref.get("name", ""))
            if req_region and region_name != req_region:
                continue
            matches.append(sc)
        if not matches:
            raise RuntimeError(f"Storage class '{req_name}' not found")
        if len(matches) > 1 and not req_region:
            raise RuntimeError(f"Storage class '{req_name}' exists in multiple regions; specify region")
        match = matches[0]
        sid = object_id(match)
        rref = object_ref(match, "region", "regionRef") or {}
        rid = object_id(rref)
        if not sid or not rid:
            raise RuntimeError(f"Storage class '{req_name}' is missing ID/region")
        resolved.append({
            "name": match.get("name", req_name),
            "id": sid,
            "region": {"name": rref.get("name", ""), "id": rid},
        })
        info(f"Content library storage class: '{resolved[-1]['name']}' in region '{rref.get('name','')}'")
    return resolved


def create_provider_library_if_missing(client: RestClient, cfg: Dict[str, Any]):
    name = cfg.get("name")
    if not name:
        raise RuntimeError("provider_content_libraries[].name is required")

    existing = content_library(client, name)
    if existing:
        skip(f"Provider content library '{name}' already exists")
        return existing

    requested_subscribed = bool(cfg.get("is_subscribed", False))
    subscription = cfg.get("subscription") if isinstance(cfg.get("subscription"), dict) else {}
    subscription_url = str(subscription.get("url", "") or "")

    if requested_subscribed and not subscription_url.strip():
        skip(f"Provider content library '{name}' has is_subscribed=true but no usable subscription; skipping")
        return None

    body = {
        "name": name,
        "description": cfg.get("description", ""),
        "libraryType": "PROVIDER",
        "storageClasses": resolve_library_storage_classes(client, cfg),
        "autoAttach": bool(cfg.get("auto_attach", False)),
        "isSubscribed": False,
    }

    if requested_subscribed:
        sub = {"subscriptionUrl": subscription_url}
        if "authenticated" in subscription:
            sub["authenticated"] = bool(subscription["authenticated"])
        if subscription.get("password") is not None:
            sub["password"] = subscription["password"]
        if "need_local_copy" in subscription:
            sub["needLocalCopy"] = bool(subscription["need_local_copy"])
        body["subscriptionConfig"] = sub
        body["isSubscribed"] = True

    create(f"Provider content library '{name}'")
    system_client(client).post("/cloudapi/v1/contentLibraries", json=body)

    deadline = time.time() + 120
    while time.time() < deadline:
        found = content_library(client, name)
        if found:
            status = str(found.get("status", "")).upper()
            if status == "FAILED":
                raise RuntimeError(f"Provider content library '{name}' entered FAILED state")
            if status in ("NOT_READY", "PARTIALLY_READY"):
                warn(f"Provider content library '{name}' created with status={status}")
            else:
                info(f"Provider content library '{name}' created")
            return found
        time.sleep(2)
    raise RuntimeError(f"Content library '{name}' did not appear within 120 seconds")


def content_library_items(
    client: RestClient,
    system_scope: bool = True,
):
    scoped_client = (
        system_client(client)
        if system_scope
        else client
    )

    return paged_values(
        scoped_client,
        "/cloudapi/v1/contentLibraryItems",
    )


def content_library_item_by_name(
    client: RestClient,
    library_id: str,
    name: str,
    system_scope: bool = True,
):
    for item in content_library_items(
        client,
        system_scope=system_scope,
    ):
        if item.get("name") != name:
            continue
        ref = object_ref(item, "contentLibrary", "contentLibraryRef") or {}
        if object_id(ref) == library_id:
            return item
    return None


def vcfa_content_library_item_type(value: Any) -> str:
    """
    Normalize friendly JSON values to VCFA 9.1 ContentLibraryItemType.

    VCFA distinguishes ISO uploads from template-based uploads.
    OVF/OVA are therefore sent as TEMPLATE.
    """
    normalized = str(value or "").strip().upper()

    mapping = {
        "ISO": "ISO",
        "OVF": "TEMPLATE",
        "OVA": "TEMPLATE",
        "TEMPLATE": "TEMPLATE",
    }

    if normalized not in mapping:
        raise RuntimeError(
            f"Unsupported content library item_type '{value}'. "
            "Use ISO, OVF, OVA, or TEMPLATE."
        )

    return mapping[normalized]


def wait_content_library_item_ready(
    client: RestClient,
    item_id: str,
    item_name: str,
    timeout: int = 1800,
    poll: int = 5,
) -> Dict[str, Any]:
    """
    Wait until VCFA finishes processing a content-library item after upload.

    VCFA 9.1 item states include:
      READY
      NOT_READY
      PARTIALLY_READY
      FAILED
    """
    encoded_item_id = urllib.parse.quote(
        item_id,
        safe="",
    )

    deadline = time.time() + timeout
    last_status = None

    while time.time() < deadline:
        item = client.get(
            f"/cloudapi/v1/contentLibraryItems/{encoded_item_id}"
        )

        if not isinstance(item, dict):
            raise RuntimeError(
                f"Unexpected response while waiting for content library "
                f"item '{item_name}'"
            )

        status = str(
            item.get("status", "") or ""
        ).upper()

        if status != last_status:
            if status:
                info(
                    f"Content library item '{item_name}' status: {status}"
                )
            else:
                info(
                    f"Waiting for content library item '{item_name}' "
                    "to report status"
                )
            last_status = status

        if status == "READY":
            size_bytes = item.get("sizeBytes")

            if size_bytes is not None:
                info(
                    f"Content library item '{item_name}' is READY "
                    f"({size_bytes} bytes)"
                )
            else:
                info(
                    f"Content library item '{item_name}' is READY"
                )

            return item

        if status == "FAILED":
            raise RuntimeError(
                f"Content library item '{item_name}' entered FAILED state"
            )

        # NOT_READY and PARTIALLY_READY are expected while VCFA is
        # ingesting/converting the uploaded files.
        time.sleep(poll)

    raise RuntimeError(
        f"Timed out after {timeout}s waiting for content library item "
        f"'{item_name}' to become READY"
    )


class UploadProgressFile:
    """
    File-like wrapper that reports upload progress while requests reads
    from the underlying file handle.
    """
    def __init__(
        self,
        file_path: Path,
        report_every_percent: int = 1,
    ):
        self.file_path = file_path
        self.file = file_path.open("rb")
        self.total = file_path.stat().st_size
        self.sent = 0
        self.report_every_percent = max(
            1,
            int(report_every_percent),
        )
        self.last_reported_percent = -1

    def __len__(self):
        return self.total

    def fileno(self):
        return self.file.fileno()

    def tell(self):
        return self.file.tell()

    def seek(self, offset, whence=0):
        result = self.file.seek(offset, whence)
        self.sent = self.file.tell()
        return result

    def read(self, size=-1):
        data = self.file.read(size)

        if data:
            self.sent += len(data)
            self._report_progress()

        return data

    def close(self):
        try:
            self._report_progress(force=True)
        finally:
            self.file.close()

    @property
    def closed(self):
        return self.file.closed

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False

    def _report_progress(
        self,
        force: bool = False,
    ) -> None:
        if self.total <= 0:
            percent = 100
        else:
            percent = min(
                100,
                int((self.sent * 100) / self.total),
            )

        should_report = (
            force
            or percent == 100
            or self.last_reported_percent < 0
            or (
                percent
                >= self.last_reported_percent
                + self.report_every_percent
            )
        )

        if not should_report:
            return

        self.last_reported_percent = percent

        sent_mb = self.sent / (1024 * 1024)
        total_mb = self.total / (1024 * 1024)

        print(
            f"\r[UPLOAD] {self.file_path.name}: "
            f"{percent:3d}% "
            f"({sent_mb:.1f}/{total_mb:.1f} MiB)",
            end="",
            flush=True,
        )

        if percent >= 100 or force:
            print()


def preflight_ovf_bundle(
    item_name: str,
    file_paths: List[Path],
) -> None:
    """Validate an OVF descriptor and its referenced local bundle files."""
    ovf_files = [p for p in file_paths if p.suffix.lower() == ".ovf"]
    if not ovf_files:
        return
    if len(ovf_files) != 1:
        raise RuntimeError(
            f"Content library item '{item_name}' must contain exactly one OVF "
            f"descriptor; found {len(ovf_files)}: "
            + ", ".join(str(p) for p in ovf_files)
        )

    ovf_path = ovf_files[0]
    raw = ovf_path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise RuntimeError(
            f"OVF preflight failed for '{item_name}': '{ovf_path}' starts "
            "with a UTF-8 BOM. vCenter/Content Library can reject descriptor.ovf "
            "during parsing. Save the OVF as UTF-8 without BOM."
        )

    stripped = raw.lstrip()
    if not stripped.startswith(b"<"):
        preview = raw[:32].hex(" ")
        raise RuntimeError(
            f"OVF preflight failed for '{item_name}': '{ovf_path}' does not "
            f"start with XML content. First bytes: {preview}"
        )

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise RuntimeError(
            f"OVF preflight failed for '{item_name}': XML parse error in "
            f"'{ovf_path}': {exc}"
        ) from None

    hrefs: List[str] = []
    for element in root.iter():
        if element.tag.rsplit('}', 1)[-1] != 'File':
            continue
        href = None
        for key, value in element.attrib.items():
            if key.rsplit('}', 1)[-1].lower() == 'href':
                href = str(value or '').strip()
                break
        if href:
            hrefs.append(urllib.parse.unquote(href))

    if not hrefs:
        raise RuntimeError(
            f"OVF preflight failed for '{item_name}': '{ovf_path}' contains "
            "no <File href=...> entries, so VCFA cannot discover VMDK upload slots."
        )

    configured_by_name = {p.name: p for p in file_paths}
    configured_lower = {p.name.lower(): p for p in file_paths}
    missing: List[str] = []
    resolved: List[str] = []
    for href in hrefs:
        href_name = Path(href).name
        match = configured_by_name.get(href_name) or configured_lower.get(href_name.lower())
        if match is None:
            candidate = ovf_path.parent / href
            if candidate.exists() and candidate.is_file():
                match = candidate
        if match is None:
            missing.append(href)
        else:
            resolved.append(f"{href} -> {match}")

    info(
        f"OVF preflight for '{item_name}': descriptor '{ovf_path.name}' "
        f"references {len(hrefs)} file(s)"
    )
    for entry in resolved:
        info(f"OVF reference verified: {entry}")

    if missing:
        raise RuntimeError(
            f"OVF preflight failed for '{item_name}': descriptor references "
            "file(s) that are not present in item_cfg.files or beside the OVF:\n  - "
            + "\n  - ".join(missing)
            + "\nConfigured files:\n  - "
            + "\n  - ".join(str(p) for p in file_paths)
        )

    # Warn about extra configured payload files not referenced by the OVF.
    referenced_names = {Path(h).name.lower() for h in hrefs}
    extras = [
        p for p in file_paths
        if p != ovf_path and p.name.lower() not in referenced_names
    ]
    for extra in extras:
        warn(
            f"OVF bundle for '{item_name}' includes '{extra.name}', but the "
            "descriptor does not reference it. VCFA may never create an upload "
            "slot for this file."
        )

def upload_content_library_item(
    client: RestClient,
    library: Dict[str, Any],
    item_cfg: Dict[str, Any],
    config_dir: Path,
    system_scope: bool = True,
):
    # Resolve the content-library identity strictly. Generic object_id() can
    # select an unrelated nested ID from a complex library response, which
    # then causes CONTENT_LIBRARY_ITEM_CREATE to fail as an invalid target.
    library_name = str(get_value(library, "name", default=""))
    if not library_name:
        library_ref = object_ref(library, "contentLibraryRef", "library") or {}
        library_name = str(get_value(library_ref, "name", default=""))

    library_id = str(library.get("id", "") or "").strip()
    if not library_id:
        library_ref = object_ref(library, "contentLibraryRef", "library") or {}
        library_id = str(
            library_ref.get("id", "")
            or library_ref.get("urn", "")
            or ""
        ).strip()

    item_name = item_cfg.get("name")

    if not library_id or not item_name:
        raise RuntimeError("Content library/item ID or name missing")

    if not library_id.startswith("urn:vcloud:contentLibrary:"):
        raise RuntimeError(
            f"Resolved content library '{library_name}' has unexpected ID "
            f"'{library_id}'. Expected urn:vcloud:contentLibrary:<uuid>.\n"
            f"Library object:\n{json.dumps(library, indent=2)}"
        )

    # CRITICAL GATE: never create or upload an item until the backing content
    # library itself has completed provisioning and reached READY.  Creating an
    # item while the library is NOT_READY can produce FAILED template items or
    # prevent VCFA from generating transfer URLs for descriptor/VMDK files.
    library_ready_timeout = int(
        item_cfg.get("library_ready_timeout_seconds", 600)
    )
    library_ready_poll = int(
        item_cfg.get("library_ready_poll_seconds", 5)
    )
    library = wait_content_library_ready(
        client,
        library_name,
        system_scope=system_scope,
        timeout=library_ready_timeout,
        poll=library_ready_poll,
    )

    refreshed_library_id = str(library.get("id", "") or "").strip()
    if refreshed_library_id and refreshed_library_id != library_id:
        raise RuntimeError(
            f"Content library '{library_name}' ID changed while waiting for READY: "
            f"was '{library_id}', now '{refreshed_library_id}'"
        )

    existing = content_library_item_by_name(
        client,
        library_id,
        item_name,
        system_scope=system_scope,
    )

    scoped_client = (
        system_client(client)
        if system_scope
        else client
    )

    raw_files = item_cfg.get("files")

    if raw_files is None:
        single_file = (
            item_cfg.get("file")
            or item_cfg.get("file_path")
            or ""
        )
        raw_files = [single_file] if single_file else []

    if isinstance(raw_files, str):
        raw_files = [raw_files]

    if not isinstance(raw_files, list) or not raw_files:
        raise RuntimeError(
            f"Content library item '{item_name}' requires at least one file"
        )

    # Preflight every configured source before creating anything in VCFA.
    # This prevents partially-created content-library items when a local
    # ISO/OVF/VMDK/NVRAM path is missing, invalid, unreadable, or empty.
    file_paths: List[Path] = []

    for raw_file in raw_files:
        file_path = resolve_path(
            str(raw_file or ""),
            config_dir,
        )

        if not file_path.exists():
            raise FileNotFoundError(
                f"Content library item '{item_name}' source file "
                f"does not exist: {file_path}"
            )

        if not file_path.is_file():
            raise RuntimeError(
                f"Content library item '{item_name}' source path "
                f"is not a regular file: {file_path}"
            )

        if not os.access(file_path, os.R_OK):
            raise RuntimeError(
                f"Content library item '{item_name}' source file "
                f"is not readable: {file_path}"
            )

        file_size = file_path.stat().st_size

        if file_size <= 0:
            raise RuntimeError(
                f"Content library item '{item_name}' source file "
                f"is empty: {file_path}"
            )

        info(
            f"Source file verified for '{item_name}': "
            f"'{file_path}' ({file_size / (1024 * 1024):.1f} MiB)"
        )

        file_paths.append(file_path)

    if not file_paths:
        raise RuntimeError(
            f"Content library item '{item_name}' has no source files configured"
        )

    configured_item_type = (
        item_cfg.get("item_type")
        or item_cfg.get("itemType")
        or ""
    )

    if not configured_item_type:
        raise RuntimeError(
            f"Content library item '{item_name}' requires item_type"
        )

    item_type = vcfa_content_library_item_type(
        configured_item_type
    )

    if str(configured_item_type).strip().upper() in ("OVF", "TEMPLATE"):
        preflight_ovf_bundle(item_name, file_paths)

    body = {
        "name": item_name,
        "contentLibrary": {
            "name": library_name,
            "id": library_id,
        },
        "itemType": item_type,
    }

    # VCFA 9.1 requires fileUploadSizeBytes for ISO uploads.
    # Template-based items (OVF/OVA) must not send this field.
    if item_type == "ISO":
        body["fileUploadSizeBytes"] = sum(
            file_path.stat().st_size
            for file_path in file_paths
        )

    if item_cfg.get("description"):
        body["description"] = item_cfg["description"]

    library_org = object_ref(library, "org", "orgRef") or {}
    library_org_id = object_id(library_org)
    if library_org_id:
        body["org"] = {
            "name": str(library_org.get("name", "") or ""),
            "id": str(library_org_id),
        }

    info(
        f"Resolved content library '{library_name}':\n"
        f"{json.dumps(library, indent=2)}"
    )
    info(
        f"Content library item create payload for '{item_name}':\n"
        f"{json.dumps(body, indent=2)}"
    )

    # Content-library item creation and file upload are separate phases in
    # VCFA. A previous run can therefore leave an item in NOT_READY,
    # PARTIALLY_READY, or FAILED state. Do not treat mere existence as
    # successful completion.
    if existing:
        existing_status = str(
            existing.get("status", "") or ""
        ).upper()
        existing_id = object_id(existing)

        if not existing_id:
            raise RuntimeError(
                f"Unable to determine ID for existing content library "
                f"item '{item_name}'"
            )

        if existing_status == "READY":
            skip(
                f"Content library item '{item_name}' already exists "
                f"in '{library_name}' and is READY"
            )
            return existing

        if existing_status in ("FAILED", "PARTIALLY_READY"):
            warn(
                f"Content library item '{item_name}' exists in "
                f"{existing_status} state; deleting it and restarting "
                f"the upload from scratch"
            )

            encoded_existing_id = urllib.parse.quote(
                existing_id,
                safe="",
            )

            delete_path = (
                f"/cloudapi/v1/contentLibraryItems/"
                f"{encoded_existing_id}"
            )

            # Tenant-scoped content-library items may reject force=true even
            # when a normal delete is permitted. Always try the standard
            # DELETE first. If that fails, surface the real VCFA error rather
            # than masking it behind a forbidden forced-delete attempt.
            try:
                scoped_client.delete(delete_path)
            except RuntimeError as delete_exc:
                warn(
                    f"Normal deletion of content library item '{item_name}' "
                    f"failed: {delete_exc}"
                )
                raise RuntimeError(
                    f"Unable to delete {existing_status} content library item "
                    f"'{item_name}' before restarting the upload. "
                    f"VCFA rejected the normal DELETE.\n{delete_exc}"
                ) from None

            delete_deadline = time.time() + 120
            while time.time() < delete_deadline:
                still_there = content_library_item_by_name(
                    client,
                    library_id,
                    item_name,
                    system_scope=system_scope,
                )
                if not still_there:
                    info(
                        f"Content library item '{item_name}' deleted; "
                        f"restarting upload"
                    )
                    break
                time.sleep(2)
            else:
                raise RuntimeError(
                    f"Timed out waiting for {existing_status} content "
                    f"library item '{item_name}' to be deleted"
                )

            existing = None
        else:
            info(
                f"Content library item '{item_name}' already exists "
                f"in '{library_name}' with status "
                f"'{existing_status or 'UNKNOWN'}'; resuming file upload"
            )

    if existing:
        item_id = object_id(existing)
    else:
        create(
            f"Content library item '{item_name}' in '{library_name}' "
            f"(configured type={str(configured_item_type).upper()}, "
            f"VCFA type={item_type})"
        )

        created = scoped_client.post(
            "/cloudapi/v1/contentLibraryItems",
            json=body,
        )

        item_id = object_id(created)

        if not item_id:
            raise RuntimeError(
                f"Unable to determine ID for content library item "
                f"'{item_name}'"
            )

    encoded = urllib.parse.quote(
        item_id,
        safe="",
    )

    uploaded_count = 0
    used_transfer_urls: set[str] = set()

    upload_url_timeout = int(
        item_cfg.get("upload_url_timeout_seconds", 600)
    )
    upload_connect_timeout = int(
        item_cfg.get("upload_connect_timeout_seconds", 30)
    )
    upload_read_timeout = int(
        item_cfg.get("upload_read_timeout_seconds", 3600)
    )
    # requests/urllib3 do not expose a distinct write timeout. During a
    # streamed PUT the socket can otherwise retain the short connect timeout
    # while the request body is being sent. Use a long scalar timeout for the
    # transfer socket so large VMDK/ISO uploads are not aborted after a short
    # write stall. This applies only to the raw transfer PUT, not normal API
    # requests.
    upload_socket_timeout = int(
        item_cfg.get(
            "upload_socket_timeout_seconds",
            max(upload_read_timeout, 3600),
        )
    )
    upload_retry_count = max(
        0, int(item_cfg.get("upload_retry_count", 0))
    )
    upload_retry_base_seconds = max(
        1, int(item_cfg.get("upload_retry_base_seconds", 2))
    )

    def transfer_record_name(record: Dict[str, Any]) -> str:
        name = str(
            record.get("name")
            or record.get("fileName")
            or ""
        ).strip()

        if name:
            return name

        transfer_url = str(
            record.get("transferUrl", "") or ""
        ).strip()

        if transfer_url:
            return Path(
                urllib.parse.urlparse(transfer_url).path
            ).name

        return ""

    def matching_transfer_record(
        local_file: Path,
        records: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        local_name = local_file.name
        local_name_lower = local_name.lower()
        suffix = local_file.suffix.lower()

        candidates = []

        for record in records:
            url = str(
                record.get("transferUrl", "") or ""
            ).strip()

            if not url or url in used_transfer_urls:
                continue

            record_name = transfer_record_name(record)
            record_name_lower = record_name.lower()

            # Exact filename is always preferred.
            if record_name_lower == local_name_lower:
                return record

            candidates.append(
                (record, record_name, record_name_lower)
            )

        # VCFA names the OVF upload target descriptor.ovf even when the
        # source OVF has a different filename. This alias is safe only for
        # a local .ovf file. Never use it for a VMDK/NVRAM.
        if suffix == ".ovf":
            ovf_candidates = [
                record
                for record, record_name, record_name_lower in candidates
                if record_name_lower == "descriptor.ovf"
                or record_name_lower.endswith(".ovf")
            ]

            if len(ovf_candidates) == 1:
                return ovf_candidates[0]

        # For non-OVF files, allow extension-only matching only when VCFA
        # exposes exactly one unused target of that type. This handles
        # server-generated target names without risking cross-type mapping.
        #
        # ISO targets are commonly renamed by VCFA from the local source
        # filename to <content-library-item-name>.iso, so .iso must be
        # included here as well.
        if suffix in (".iso", ".vmdk", ".nvram", ".mf", ".cert"):
            same_type = [
                record
                for record, record_name, record_name_lower in candidates
                if Path(record_name_lower).suffix == suffix
            ]

            if len(same_type) == 1:
                return same_type[0]

        return None

    for file_path in file_paths:
        deadline = time.time() + upload_url_timeout
        transfer_url = None
        transfer_name = None
        last_records_signature = None
        last_item_status = None
        next_status_check = 0.0

        while time.time() < deadline:
            files = paged_values(
                scoped_client,
                f"/cloudapi/v1/contentLibraryItems/{encoded}/files",
            )

            match = matching_transfer_record(
                file_path,
                files,
            )

            if match:
                transfer_url = str(
                    match.get("transferUrl", "") or ""
                ).strip()
                transfer_name = transfer_record_name(match)
                break

            records_signature = tuple(
                sorted(
                    (
                        transfer_record_name(record),
                        bool(record.get("transferUrl")),
                    )
                    for record in files
                )
            )

            if records_signature != last_records_signature:
                available = [
                    transfer_record_name(record) or "<unnamed>"
                    for record in files
                    if record.get("transferUrl")
                ]
                info(
                    f"Waiting for transfer URL for '{file_path.name}'. "
                    f"VCFA currently exposes: "
                    f"{', '.join(available) if available else '<none>'}"
                )
                last_records_signature = records_signature

            # After an OVF descriptor is uploaded VCFA may need time to parse
            # it and generate the VMDK/NVRAM upload slots. While waiting, also
            # monitor the item state so a genuine server-side failure is not
            # hidden behind an empty /files response until the URL timeout.
            now = time.time()
            if now >= next_status_check:
                current_item = content_library_item_by_name(
                    client,
                    library_id,
                    item_name,
                    system_scope=system_scope,
                )
                current_status = str(
                    (current_item or {}).get("status", "") or "UNKNOWN"
                ).upper()

                if current_status != last_item_status:
                    remaining = max(0, int(deadline - now))
                    info(
                        f"Content library item '{item_name}' status while "
                        f"waiting for '{file_path.name}': {current_status} "
                        f"({remaining}s remaining)"
                    )
                    last_item_status = current_status

                if current_status in ("FAILED", "ERROR"):
                    raise RuntimeError(
                        f"Content library item '{item_name}' entered "
                        f"status '{current_status}' while waiting for upload "
                        f"URL for '{file_path.name}'"
                    )

                if current_status == "READY":
                    raise RuntimeError(
                        f"Content library item '{item_name}' became READY "
                        f"before VCFA exposed an upload URL for configured "
                        f"file '{file_path.name}'. Check that the OVF actually "
                        f"references this file and that the configured files "
                        f"match the OVF descriptor."
                    )

                next_status_check = now + 10

            time.sleep(2)

        if not transfer_url:
            files = paged_values(
                scoped_client,
                f"/cloudapi/v1/contentLibraryItems/{encoded}/files",
            )
            debug_records = [
                {
                    "name": transfer_record_name(record),
                    "transferUrl": str(
                        record.get("transferUrl", "") or ""
                    ),
                }
                for record in files
            ]
            current_item = content_library_item_by_name(
                client,
                library_id,
                item_name,
                system_scope=system_scope,
            )
            final_status = str(
                (current_item or {}).get("status", "") or "UNKNOWN"
            ).upper()
            raise RuntimeError(
                f"Timed out after {upload_url_timeout}s waiting for the "
                f"correct upload URL for '{item_name}' / "
                f"'{file_path.name}'.\n"
                f"VCFA item status: {final_status}\n"
                f"VCFA file records:\n"
                f"{json.dumps(debug_records, indent=2)}"
            )

        transfer_path = urllib.parse.urlparse(
            transfer_url
        ).path

        info(
            f"Uploading '{file_path.name}' "
            f"({file_path.stat().st_size} bytes) -> "
            f"'{transfer_name or transfer_path}'"
        )

        progress_percent = int(
            item_cfg.get(
                "upload_progress_percent",
                1,
            )
        )

        response = None
        upload_error = None

        info(
            f"Transfer socket timeout for '{file_path.name}': "
            f"{upload_socket_timeout}s; retries: {upload_retry_count}"
        )

        for upload_attempt in range(
            1, upload_retry_count + 2
        ):
            try:
                with UploadProgressFile(
                    file_path,
                    report_every_percent=progress_percent,
                ) as fh:
                    response = requests.put(
                        transfer_url,
                        data=fh,
                        headers={
                            "Content-Type": "application/octet-stream",
                            "Content-Length": str(
                                file_path.stat().st_size
                            ),
                        },
                        verify=client.verify,
                        # A scalar timeout is intentional here. With a
                        # (connect, read) tuple, urllib3 can leave the short
                        # connect timeout active while streaming the request
                        # body, causing large uploads to fail with
                        # "The write operation timed out".
                        timeout=upload_socket_timeout,
                    )

                if response.ok:
                    upload_error = None
                    break

                upload_error = (
                    f"HTTP {response.status_code}\n"
                    f"{response.text}"
                )

            except requests.exceptions.RequestException as exc:
                upload_error = str(exc)

            if upload_attempt <= upload_retry_count:
                retry_seconds = min(
                    upload_retry_base_seconds
                    * (2 ** (upload_attempt - 1)),
                    30,
                )
                warn(
                    f"Upload of '{file_path.name}' failed on attempt "
                    f"{upload_attempt}/{upload_retry_count + 1}; "
                    f"retrying from the beginning in {retry_seconds}s: "
                    f"{upload_error}"
                )
                time.sleep(retry_seconds)

        if response is None or not response.ok:
            raise RuntimeError(
                f"Upload failed for '{file_path.name}' to "
                f"'{transfer_name or transfer_path}':\n"
                f"{upload_error or 'unknown error'}"
            )

        used_transfer_urls.add(transfer_url)
        uploaded_count += 1

    print(
        f"[UPLOADED] Content library item '{item_name}' "
        f"({uploaded_count} file(s))"
    )

    upload_timeout = int(
        item_cfg.get(
            "upload_timeout_seconds",
            1800,
        )
    )

    upload_poll = int(
        item_cfg.get(
            "upload_poll_seconds",
            5,
        )
    )

    info(
        f"Waiting for content library item '{item_name}' "
        "to finish processing"
    )

    return wait_content_library_item_ready(
        scoped_client,
        item_id,
        item_name,
        timeout=upload_timeout,
        poll=upload_poll,
    )


# ============================================================
# Provider organizations / quota / networking
# ============================================================

def bare_org_uuid(org_id: str) -> str:
    """
    Convert:
        urn:vcloud:org:6127c856-7315-46b8-b774-f2b8f1686c80
    to:
        6127c856-7315-46b8-b774-f2b8f1686c80
    """
    value = str(org_id or "").strip()

    if not value:
        raise RuntimeError("Organization ID is empty")

    if value.startswith("urn:vcloud:org:"):
        return value.rsplit(":", 1)[-1]

    return value


def vcfa_provider_local_session(
    server: str,
    username: str,
    password: str,
    verify: bool,
) -> RestClient:
    """
    Create a VCFA provider session using a VCFA-LOCAL provider account.

    Endpoint:
        POST /cloudapi/1.0.0/sessions/provider

    Authorization:
        Basic base64(<username>@system:<password>)
    """
    client = RestClient(server, verify=verify)

    basic_value = f"{username}@system:{password}"
    auth = base64.b64encode(basic_value.encode("utf-8")).decode("ascii")

    response = client.session.post(
        client.url("/cloudapi/1.0.0/sessions/provider"),
        headers={
            "Authorization": f"Basic {auth}",
            "Accept": "application/json;version=9.1.0",
        },
        timeout=120,
        verify=verify,
    )

    if not response.ok:
        try:
            detail = response.json()
            formatted = json.dumps(detail, indent=2)
        except Exception:
            formatted = response.text

        raise RuntimeError(
            "Failed to create VCFA local provider session: "
            f"HTTP {response.status_code}\\n{formatted}"
        )

    access_token = (
        response.headers.get("x-vmware-vcloud-access-token")
        or response.headers.get("X-VMWARE-VCLOUD-ACCESS-TOKEN")
    )

    if not access_token:
        raise RuntimeError(
            "VCFA local provider session succeeded but no "
            "x-vmware-vcloud-access-token header was returned."
        )

    return RestClient(
        server,
        verify=verify,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json;version=10.0.0.0-alpha",
        },
    )


def first_user_provider_client(
    default_provider_client: RestClient,
    vcfa_server: str,
    first_user_cfg: Dict[str, Any],
    config_dir: Path,
    verify: bool,
) -> RestClient:
    """
    Resolve the authentication source used specifically for first-user create.

    Supported config:

      "authentication": {
        "type": "api_token",
        "api_token_file": "..."
      }

    or:

      "authentication": {
        "type": "local",
        "username": "admin",
        "password_file": "..."
      }

    If authentication is omitted, the existing provider client is reused.
    """
    auth_cfg = first_user_cfg.get("authentication") or {}

    if not auth_cfg:
        return default_provider_client

    auth_type = str(auth_cfg.get("type", "") or "").strip().lower()

    if auth_type == "api_token":
        token_file_value = str(
            auth_cfg.get("api_token_file", "") or ""
        ).strip()

        if not token_file_value:
            raise RuntimeError(
                "first_user.authentication.api_token_file is required "
                "when authentication.type is 'api_token'."
            )

        token_file = resolve_path(token_file_value, config_dir)

        return vcfa_client(
            vcfa_server,
            token_file,
            verify,
        )

    if auth_type == "local":
        username = str(
            auth_cfg.get("username", "") or ""
        ).strip()

        password_file_value = str(
            auth_cfg.get("password_file", "") or ""
        ).strip()

        if not username:
            raise RuntimeError(
                "first_user.authentication.username is required "
                "when authentication.type is 'local'."
            )

        if not password_file_value:
            raise RuntimeError(
                "first_user.authentication.password_file is required "
                "when authentication.type is 'local'."
            )

        password = read_text_file(
            resolve_path(password_file_value, config_dir)
        )

        return vcfa_provider_local_session(
            vcfa_server,
            username,
            password,
            verify,
        )

    raise RuntimeError(
        "Unsupported first_user.authentication.type "
        f"'{auth_cfg.get('type')}'. Supported values are "
        "'api_token' and 'local'."
    )


def vcfa_tenant_local_session(
    server: str,
    organization: str,
    username: str,
    password: str,
    verify: bool,
) -> tuple[RestClient, str]:
    """
    Log in as a VCFA LOCAL tenant user.

    POST /cloudapi/1.0.0/sessions
    Authorization: Basic base64(username@organization:password)

    Returns:
      (authenticated RestClient, bearer access token)
    """
    client = RestClient(server, verify=verify)

    basic_value = f"{username}@{organization}:{password}"
    encoded_auth = base64.b64encode(
        basic_value.encode("utf-8")
    ).decode("ascii")

    response = client.session.post(
        client.url("/cloudapi/1.0.0/sessions"),
        headers={
            "Authorization": f"Basic {encoded_auth}",
            "Accept": "application/json;version=9.1.0",
        },
        timeout=120,
        verify=verify,
    )

    if not response.ok:
        try:
            detail = response.json()
            formatted = json.dumps(detail, indent=2)
        except Exception:
            formatted = response.text

        raise RuntimeError(
            f"Failed to authenticate local tenant user "
            f"'{username}@{organization}': HTTP {response.status_code}\n"
            f"{formatted}"
        )

    access_token = (
        response.headers.get("x-vmware-vcloud-access-token")
        or response.headers.get("X-VMWARE-VCLOUD-ACCESS-TOKEN")
    )

    if not access_token:
        raise RuntimeError(
            f"Tenant login for '{username}@{organization}' succeeded, "
            "but x-vmware-vcloud-access-token was not returned."
        )

    authenticated = RestClient(
        server,
        verify=verify,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json;version=9.1.0",
        },
    )

    return authenticated, access_token


def write_secret_file(path: Path, value: str) -> None:
    """
    Write a secret value with owner-only permissions where supported.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value.strip() + "\n", encoding="utf-8")

    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def vcfa_api_tokens(
    client: RestClient,
) -> List[Dict[str, Any]]:
    """
    Return API token registrations visible to the authenticated tenant user.
    """
    return paged_values(
        client,
        "/cloudapi/1.0.0/tokens",
    )


def vcfa_api_tokens_by_name(
    client: RestClient,
    token_name: str,
    username: Optional[str] = None,
) -> List[Dict[str, Any]]:
    matches: List[Dict[str, Any]] = []

    for token in vcfa_api_tokens(client):
        if str(token.get("name", "") or "") != token_name:
            continue

        token_username = str(
            token.get("username", "") or ""
        ).strip()

        if (
            username
            and token_username
            and token_username != username
        ):
            continue

        matches.append(token)

    return matches


def delete_vcfa_api_token(
    client: RestClient,
    token: Dict[str, Any],
) -> None:
    token_id = object_id(token)

    if not token_id:
        raise RuntimeError(
            f"Unable to determine ID for API token "
            f"'{token.get('name', '<unnamed>')}'"
        )

    encoded_token_id = urllib.parse.quote(
        token_id,
        safe="",
    )

    client.delete(
        f"/cloudapi/1.0.0/tokens/{encoded_token_id}"
    )


def create_first_user_api_token_if_requested(
    server: str,
    organization: str,
    first_user_cfg: Dict[str, Any],
    config_dir: Path,
    verify: bool,
) -> Optional[str]:
    """
    Create an API refresh token for the first LOCAL tenant user.

    JSON:
      "create_token": true,
      "replace_token": false,
      "api_token_file": "/path/to/token.txt"

    Behaviour:
      create_token=false
          Do not create or replace a token.
      create_token=true, replace_token=false
          Create a token only when api_token_file is missing or empty.
      create_token=true, replace_token=true
          Always create a new token and overwrite api_token_file.

    Uses the same username/password_file as first_user.

    Flow mirrors go-vcloud-director CreateToken/GetInitialApiToken:
      1. Local tenant login
      2. POST /oauth/tenant/{org}/register
      3. POST /oauth/tenant/{org}/token with jwt-bearer grant
      4. Save refresh_token
    """
    if not bool(first_user_cfg.get("create_token", False)):
        return None

    username = str(
        first_user_cfg.get("username", "") or ""
    ).strip()

    password_file_value = str(
        first_user_cfg.get("password_file", "") or ""
    ).strip()

    api_token_file_value = str(
        first_user_cfg.get("api_token_file", "") or ""
    ).strip()

    replace_token = bool(
        first_user_cfg.get("replace_token", False)
    )

    if not username:
        raise RuntimeError(
            f"first_user.username is required to create an API token "
            f"for organization '{organization}'."
        )

    if not password_file_value:
        raise RuntimeError(
            f"first_user.password_file is required to create an API token "
            f"for '{username}@{organization}'."
        )

    if not api_token_file_value:
        raise RuntimeError(
            f"first_user.api_token_file is required when create_token=true "
            f"for '{username}@{organization}'."
        )

    api_token_file = resolve_path(
        api_token_file_value,
        config_dir,
    )

    # Unless replace_token=true, preserve an existing non-empty token.
    # With replace_token=true a new VCFA API token is registered and the
    # token file is overwritten with the newly issued refresh token.
    if api_token_file.is_file():
        existing_value = api_token_file.read_text(
            encoding="utf-8"
        ).strip()

        if existing_value and not replace_token:
            skip(
                f"API token file already exists for "
                f"'{username}@{organization}' and replace_token=false: "
                f"{api_token_file}"
            )
            return existing_value

        if existing_value and replace_token:
            info(
                f"API token replacement requested for "
                f"'{username}@{organization}'"
            )

    password = read_text_file(
        resolve_path(password_file_value, config_dir)
    )

    info(
        f"Authenticating as local tenant user "
        f"'{username}@{organization}' for API token creation"
    )

    tenant_client, session_access_token = vcfa_tenant_local_session(
        server,
        organization,
        username,
        password,
        verify,
    )

    encoded_org = urllib.parse.quote(
        organization,
        safe="",
    )

    # Use the destination filename as the human-readable token name.
    token_name = api_token_file.stem

    if replace_token:
        existing_tokens = vcfa_api_tokens_by_name(
            tenant_client,
            token_name,
            username=username,
        )

        if existing_tokens:
            info(
                f"Found {len(existing_tokens)} existing VCFA API token "
                f"registration(s) named '{token_name}' for "
                f"'{username}@{organization}'"
            )

            for existing_token in existing_tokens:
                existing_token_id = object_id(existing_token) or "<unknown>"

                update(
                    f"Revoke API token '{token_name}' "
                    f"[{existing_token_id}]"
                )

                delete_vcfa_api_token(
                    tenant_client,
                    existing_token,
                )

            # Give VCFA a moment to release the unique token name before
            # attempting to register a replacement.
            time.sleep(1)
        else:
            info(
                f"No existing server-side API token registration named "
                f"'{token_name}' was found; creating a replacement"
            )

    if replace_token and api_token_file.is_file():
        update(
            f"API token '{token_name}' for "
            f"'{username}@{organization}'"
        )
    else:
        create(
            f"API token '{token_name}' for "
            f"'{username}@{organization}'"
        )

    registered = tenant_client.post(
        f"/oauth/tenant/{encoded_org}/register",
        json={
            "client_name": token_name,
        },
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )

    if not isinstance(registered, dict):
        raise RuntimeError(
            f"VCFA token registration for '{username}@{organization}' "
            "did not return a JSON object."
        )

    client_id = str(
        registered.get("client_id", "") or ""
    ).strip()

    if not client_id:
        raise RuntimeError(
            f"VCFA token registration for '{username}@{organization}' "
            "did not return client_id."
        )

    token_response = tenant_client.post(
        f"/oauth/tenant/{encoded_org}/token",
        data={
            "grant_type":
                "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": session_access_token,
            "client_id": client_id,
        },
        headers={
            "Accept": "application/json;version=9.1.0",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )

    if not isinstance(token_response, dict):
        raise RuntimeError(
            f"Initial API token request for "
            f"'{username}@{organization}' did not return JSON."
        )

    refresh_token = str(
        token_response.get("refresh_token", "") or ""
    ).strip()

    if not refresh_token:
        raise RuntimeError(
            f"Initial API token request for "
            f"'{username}@{organization}' did not return refresh_token."
        )

    write_secret_file(
        api_token_file,
        refresh_token,
    )

    action = "REPLACED" if replace_token else "CREATED"
    print(
        f"[{action}] API token for '{username}@{organization}' "
        f"saved to '{api_token_file}'"
    )

    return refresh_token


def vcfa_provider_local_session(
    server: str,
    username: str,
    password: str,
    verify: bool,
) -> RestClient:
    """
    Create a provider session using a VCFA LOCAL provider account.

    POST /cloudapi/1.0.0/sessions/provider
    Authorization: Basic base64(username@system:password)
    """
    client = RestClient(server, verify=verify)

    basic_value = f"{username}@system:{password}"
    encoded = base64.b64encode(
        basic_value.encode("utf-8")
    ).decode("ascii")

    response = client.session.post(
        client.url("/cloudapi/1.0.0/sessions/provider"),
        headers={
            "Authorization": f"Basic {encoded}",
            "Accept": "application/json;version=9.1.0",
        },
        timeout=120,
        verify=verify,
    )

    if not response.ok:
        try:
            detail = response.json()
            formatted = json.dumps(detail, indent=2)
        except Exception:
            formatted = response.text

        raise RuntimeError(
            "Failed to create VCFA LOCAL provider session: "
            f"HTTP {response.status_code}\n{formatted}"
        )

    access_token = (
        response.headers.get("x-vmware-vcloud-access-token")
        or response.headers.get("X-VMWARE-VCLOUD-ACCESS-TOKEN")
    )

    if not access_token:
        raise RuntimeError(
            "VCFA LOCAL provider login succeeded but "
            "x-vmware-vcloud-access-token was not returned."
        )

    return RestClient(
        server,
        verify=verify,
        headers={
            "Authorization": f"Bearer {access_token}",
            # Keep LOCAL provider/user access-control operations on the
            # VCFA 9.1 VcdUser schema. Using the 10.0 alpha schema here can
            # cause LOCAL-user PUT fields such as password/nameInSource to
            # be interpreted incorrectly by a VCFA 9.1 endpoint.
            "Accept": "application/json;version=9.1.0",
        },
    )


def first_user_write_client(
    default_provider_client: RestClient,
    server: str,
    provider_cfg: Dict[str, Any],
    config_dir: Path,
    verify: bool,
) -> RestClient:
    """
    Use provider.local_auth for operations that require full provider
    session authority, such as local-user creation and provider content
    library/item management.

    If local_auth is absent, fall back to the normal provider API-token client.
    """
    local_auth = provider_cfg.get("local_auth") or {}

    if not local_auth:
        return default_provider_client

    username = str(
        local_auth.get("username", "") or ""
    ).strip()

    password_file_value = str(
        local_auth.get("password_file", "") or ""
    ).strip()

    if not username:
        raise RuntimeError(
            "vcfa.provider.local_auth.username is required"
        )

    if not password_file_value:
        raise RuntimeError(
            "vcfa.provider.local_auth.password_file is required"
        )

    password = read_text_file(
        resolve_path(
            password_file_value,
            config_dir,
        )
    )

    info(
        f"Creating VCFA LOCAL provider session as '{username}@system' "
        "for privileged provider operation"
    )

    return vcfa_provider_local_session(
        server,
        username,
        password,
        verify,
    )


# ============================================================
# Tenant lookup / local user
#
# VCFA 9.1 behavior:
# - CloudAPI is used to query roles/users.
# - Local-user CREATE uses the legacy Admin XML API:
#     POST /api/admin/org/{org_uuid}/users
#   with API version 9.1.0.
# ============================================================

VCFA_LEGACY_XML_ACCEPT = "application/*+xml;version=9.1.0"
VCFA_LEGACY_USER_CONTENT_TYPE = "application/vnd.vmware.admin.user+xml"


def bare_org_uuid(org_id: str) -> str:
    value = str(org_id or "").strip()
    if not value:
        raise RuntimeError("Organization ID is empty")
    if value.startswith("urn:vcloud:org:"):
        return value.rsplit(":", 1)[-1]
    return value


def bare_role_uuid(role_id: str) -> str:
    value = str(role_id or "").strip()
    if not value:
        raise RuntimeError("Role ID is empty")
    if value.startswith("urn:vcloud:role:"):
        return value.rsplit(":", 1)[-1]
    return value


def tenant_lookup_client(
    provider_client: RestClient,
    org_id: str,
) -> RestClient:
    """
    Provider-authenticated CloudAPI client scoped to a tenant for reads.

    This matches the working VCFA 9.1 pattern:
        X-VMWARE-VCLOUD-TENANT-CONTEXT: urn:vcloud:org:...
    """
    headers = dict(provider_client.session.headers)

    headers.pop("X-VMWARE-VCLOUD-AUTH-CONTEXT", None)
    headers["X-VMWARE-VCLOUD-TENANT-CONTEXT"] = org_id

    return RestClient(
        provider_client.server,
        verify=provider_client.verify,
        headers=headers,
    )


def tenant_roles(
    provider_client: RestClient,
    org_id: str,
) -> List[Dict[str, Any]]:
    return paged_values(
        tenant_lookup_client(provider_client, org_id),
        "/cloudapi/1.0.0/roles",
    )


def configured_first_user_roles(
    first_user_cfg: Dict[str, Any],
    tenant_name: str,
) -> List[str]:
    """
    Normalize vcfa.provider.organizations.<org>.first_user.role.

    The JSON may use either:
        "role": "Organization Administrator"
    or:
        "role": ["Organization Owner", "Assembler Administrator", ...]
    """
    raw_roles = first_user_cfg.get("role")

    if raw_roles is None:
        raw_roles = ["Organization Administrator"]
    elif isinstance(raw_roles, str):
        raw_roles = [raw_roles]
    elif not isinstance(raw_roles, list):
        raise RuntimeError(
            f"first_user.role for organization '{tenant_name}' must be "
            "a string or an array of role names"
        )

    result: List[str] = []
    seen = set()

    for raw_role in raw_roles:
        role_name = str(raw_role or "").strip()
        if not role_name:
            raise RuntimeError(
                f"first_user.role for organization '{tenant_name}' "
                "contains an empty role name"
            )
        if role_name in seen:
            continue
        seen.add(role_name)
        result.append(role_name)

    if not result:
        raise RuntimeError(
            f"first_user.role for organization '{tenant_name}' must "
            "contain at least one role"
        )

    return result


def provider_org_is_classic_tenant(
    provider_cfg: Dict[str, Any],
    tenant_name: str,
    tenant: Optional[Dict[str, Any]] = None,
) -> bool:
    """
    Use the JSON value as the source of truth for organization type.

    Path:
      vcfa.provider.organizations.<org>.is_classic_tenant
    """
    provider_orgs = provider_cfg.get("organizations") or {}
    org_cfg = provider_orgs.get(tenant_name) or {}

    if "is_classic_tenant" in org_cfg:
        return bool(org_cfg.get("is_classic_tenant"))

    if isinstance(tenant, dict) and "isClassicTenant" in tenant:
        return bool(tenant.get("isClassicTenant"))

    return False


def resolve_first_user_roles(
    provider_client: RestClient,
    org_id: str,
    tenant_name: str,
    first_user_cfg: Dict[str, Any],
    is_classic_tenant: bool,
) -> List[Dict[str, Any]]:
    requested_names = configured_first_user_roles(
        first_user_cfg,
        tenant_name,
    )

    available = tenant_roles(provider_client, org_id)
    by_name = {
        str(role.get("name", "") or ""): role
        for role in available
        if role.get("name")
    }

    org_type = "VM Apps/classic" if is_classic_tenant else "All Apps/non-classic"
    info(
        f"Organization '{tenant_name}' is configured as {org_type}; "
        f"validating first_user.role against the returned role catalogue"
    )

    resolved: List[Dict[str, Any]] = []
    missing: List[str] = []

    for role_name in requested_names:
        role = by_name.get(role_name)
        if role is None:
            missing.append(role_name)
        else:
            resolved.append(role)

    if missing:
        available_names = sorted(by_name)
        raise RuntimeError(
            f"Role(s) {', '.join(repr(x) for x in missing)} were not found "
            f"for {org_type} organization '{tenant_name}'. Available roles: "
            f"{', '.join(available_names) if available_names else '<none>'}"
        )

    return resolved


def tenant_users(
    provider_client: RestClient,
    org_id: str,
) -> List[Dict[str, Any]]:
    return paged_values(
        tenant_lookup_client(provider_client, org_id),
        "/cloudapi/1.0.0/users",
    )


def tenant_user_by_name(
    provider_client: RestClient,
    org_id: str,
    username: str,
) -> Optional[Dict[str, Any]]:
    for user in tenant_users(provider_client, org_id):
        if str(user.get("username", "")) == username:
            return user
    return None


def create_local_user_legacy(
    provider_client: RestClient,
    tenant_id: str,
    username: str,
    password: str,
    role_id: str,
) -> None:
    """
    Create a local VCFA user through the legacy Admin XML API.

    VCFA 9.1 local-user creation is reliable on:
        POST /api/admin/org/{org_uuid}/users

    CloudAPI /cloudapi/1.0.0/users remains useful for reads but can
    reject local-user CREATE in this release.
    """
    org_uuid = bare_org_uuid(tenant_id)
    role_uuid = bare_role_uuid(role_id)

    username_attr = quoteattr(username)
    password_xml = escape(password)

    xml_body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<User xmlns="http://www.vmware.com/vcloud/v1.5" '
        f'name={username_attr}>\n'
        '  <IsEnabled>true</IsEnabled>\n'
        f'  <Role href="https://{provider_client.server}'
        f'/api/admin/role/{role_uuid}"/>\n'
        f'  <Password>{password_xml}</Password>\n'
        '</User>'
    )

    url = (
        f"https://{provider_client.server}"
        f"/api/admin/org/{org_uuid}/users"
    )

    headers = dict(provider_client.session.headers)
    headers.pop("X-VMWARE-VCLOUD-AUTH-CONTEXT", None)
    headers.pop("X-VMWARE-VCLOUD-TENANT-CONTEXT", None)
    headers["Accept"] = VCFA_LEGACY_XML_ACCEPT
    headers["Content-Type"] = VCFA_LEGACY_USER_CONTENT_TYPE

    response = provider_client.session.post(
        url,
        data=xml_body.encode("utf-8"),
        headers=headers,
        timeout=120,
        verify=provider_client.verify,
    )

    if response.status_code not in (200, 201, 202):
        body = response.text
        raise RuntimeError(
            f"POST {url} failed: HTTP {response.status_code}\n{body}"
        )


def create_first_user_if_missing(
    provider_client: RestClient,
    vcfa_server: str,
    provider_cfg: Dict[str, Any],
    tenant: Dict[str, Any],
    first_user_cfg: Dict[str, Any],
    config_dir: Path,
    verify: bool,
) -> Optional[Dict[str, Any]]:
    if not first_user_cfg:
        return None

    tenant_id = object_id(tenant)
    tenant_name = str(tenant.get("name", "") or "")

    if not tenant_id:
        raise RuntimeError(
            f"Unable to determine organization ID for first user in "
            f"'{tenant_name}'."
        )

    username = str(first_user_cfg.get("username", "") or "").strip()
    if not username:
        raise RuntimeError(
            f"first_user.username is required for organization "
            f"'{tenant_name}'."
        )

    provider_type = str(
        first_user_cfg.get("provider_type", "LOCAL") or "LOCAL"
    ).upper()
    if provider_type != "LOCAL":
        raise RuntimeError(
            f"first_user.provider_type for '{tenant_name}' must be LOCAL."
        )

    is_classic_tenant = provider_org_is_classic_tenant(
        provider_cfg,
        tenant_name,
        tenant,
    )

    user_provider_client = first_user_provider_client(
        provider_client,
        vcfa_server,
        first_user_cfg,
        config_dir,
        verify,
    )
    privileged_write_client = first_user_write_client(
        provider_client,
        vcfa_server,
        provider_cfg,
        config_dir,
        verify,
    )

    desired_roles = resolve_first_user_roles(
        user_provider_client,
        tenant_id,
        tenant_name,
        first_user_cfg,
        is_classic_tenant,
    )
    desired_role_ids = sorted(
        str(object_id(role) or "")
        for role in desired_roles
        if object_id(role)
    )
    desired_role_names = [
        str(role.get("name", "") or "")
        for role in desired_roles
    ]

    if len(desired_role_ids) != len(desired_roles):
        raise RuntimeError(
            f"Unable to determine ID for one or more first-user roles in "
            f"organization '{tenant_name}'"
        )

    desired_enabled = bool(first_user_cfg.get("enabled", True))

    def reconcile_existing_user(user: Dict[str, Any]) -> Dict[str, Any]:
        user_id = object_id(user)
        if not user_id:
            raise RuntimeError(
                f"Unable to determine ID for existing first user "
                f"'{username}' in organization '{tenant_name}'"
            )

        current_role_ids = user_role_ids(user)
        current_enabled = bool(user.get("enabled", True))
        differences: List[str] = []

        if current_role_ids != desired_role_ids:
            differences.append("roleEntityRefs")
        if current_enabled != desired_enabled:
            differences.append("enabled")

        role_text = ", ".join(desired_role_names)

        if not differences:
            skip(
                f"First user '{username}' already exists in organization "
                f"'{tenant_name}' with role(s): {role_text}"
            )
            return user

        info(
            f"First user '{username}' in organization '{tenant_name}' "
            f"differs from desired configuration: {', '.join(differences)}"
        )

        # Build a LOCAL-user update payload explicitly. Do not reuse the
        # LDAP access-control payload builder here because LOCAL user records
        # can omit orgEntityRef or return it without an ID. VCFA rejects such
        # a payload with "Id null is not a valid URN".
        update_body: Dict[str, Any] = {
            # VCFA 9.1 VM Apps user update validates the VcdUser.id in the
            # request body as well as the userUrn path parameter. Omitting it
            # can result in: VCD_50030 "Id null is not a valid URN".
            "id": str(user_id),
            "username": str(user.get("username") or username),
            "orgEntityRef": {
                "name": tenant_name,
                "id": tenant_id,
            },
            "providerType": "LOCAL",
            "enabled": desired_enabled,
            "inheritGroupRoles": False,
            "roleEntityRefs": [
                {
                    "name": str(role.get("name", "") or ""),
                    "id": str(object_id(role) or ""),
                }
                for role in desired_roles
            ],
        }

        # Preserve harmless user metadata when present, but never preserve
        # server-owned reference objects that may contain null IDs.
        for source, target in (
            ("fullName", "fullName"),
            ("description", "description"),
            ("email", "email"),
            ("phone", "phone"),
        ):
            if user.get(source) is not None:
                update_body[target] = user.get(source)

        # VCFA requires a password on every update of a LOCAL user, even when
        # the password itself is not being changed. Read the configured
        # first_user.password_file and send that value with the PUT.
        password_file_value = str(
            first_user_cfg.get("password_file", "") or ""
        ).strip()
        if not password_file_value:
            raise RuntimeError(
                f"first_user.password_file is required to update LOCAL first "
                f"user '{username}' in organization '{tenant_name}'"
            )

        password_file = resolve_path(password_file_value, config_dir)
        password = read_text_file(password_file)
        if not password:
            raise RuntimeError(
                f"first_user.password_file is empty for LOCAL first user "
                f"'{username}' in organization '{tenant_name}': "
                f"{password_file}"
            )

        update_body["password"] = password

        # VCFA treats nameInSource as immutable, but the PUT is effectively a
        # full VcdUser update for this field. Omitting an existing non-null
        # nameInSource can be interpreted as changing it to null, which VCFA
        # rejects with VCD_50102 "Cannot modify nameInSource of a user".
        # Preserve the exact server-returned value; never derive or change it.
        if "nameInSource" in user and user.get("nameInSource") is not None:
            update_body["nameInSource"] = user.get("nameInSource")

        # Defensive validation before sending the update.
        if not str(update_body.get("id") or "").startswith("urn:vcloud:user:"):
            raise RuntimeError(
                f"Invalid user reference while reconciling first user "
                f"'{username}' in '{tenant_name}': {update_body.get('id')!r}"
            )

        if not str(update_body["orgEntityRef"].get("id") or "").startswith("urn:vcloud:org:"):
            raise RuntimeError(
                f"Invalid organization reference while reconciling first user "
                f"'{username}' in '{tenant_name}': "
                f"{update_body['orgEntityRef']}"
            )

        invalid_role_refs = [
            ref for ref in update_body["roleEntityRefs"]
            if not str(ref.get("id") or "").startswith("urn:")
        ]
        if invalid_role_refs:
            raise RuntimeError(
                f"Invalid role reference(s) while reconciling first user "
                f"'{username}' in '{tenant_name}': "
                f"{json.dumps(invalid_role_refs, indent=2)}"
            )

        diagnostic_body = dict(update_body)
        if "password" in diagnostic_body:
            diagnostic_body["password"] = "<redacted>"

        info(
            f"First-user update payload for '{username}' in '{tenant_name}':\n"
            + json.dumps(diagnostic_body, indent=2)
        )

        encoded_user_id = urllib.parse.quote(str(user_id), safe="")
        write_client = tenant_lookup_client(
            privileged_write_client,
            tenant_id,
        )

        update(
            f"First user '{username}' in organization '{tenant_name}' "
            f"-> role(s): {role_text}"
        )

        updated = write_client.put(
            f"/cloudapi/1.0.0/users/{encoded_user_id}",
            json=update_body,
        )

        if isinstance(updated, dict):
            verified = updated
        else:
            verified = tenant_user_by_name(
                user_provider_client,
                tenant_id,
                username,
            )

        if not verified:
            raise RuntimeError(
                f"First user '{username}' in organization '{tenant_name}' "
                "was updated but could not be queried afterwards"
            )

        verified_role_ids = user_role_ids(verified)
        verified_enabled = bool(verified.get("enabled", True))

        if verified_role_ids != desired_role_ids:
            raise RuntimeError(
                f"First user '{username}' role reconciliation failed in "
                f"organization '{tenant_name}'. Desired roles: "
                f"{role_text}; API returned role IDs: "
                f"{verified_role_ids or '<none>'}"
            )

        if verified_enabled != desired_enabled:
            raise RuntimeError(
                f"First user '{username}' enabled-state reconciliation "
                f"failed in organization '{tenant_name}'"
            )

        info(
            f"First user '{username}' in organization '{tenant_name}' "
            f"verified with role(s): {role_text}"
        )
        return verified

    existing = tenant_user_by_name(
        user_provider_client,
        tenant_id,
        username,
    )

    if existing:
        user = reconcile_existing_user(existing)
    else:
        password_file_value = str(
            first_user_cfg.get("password_file", "") or ""
        ).strip()
        if not password_file_value:
            raise RuntimeError(
                f"first_user.password_file is required for organization "
                f"'{tenant_name}'."
            )

        password_file = resolve_path(password_file_value, config_dir)
        password = read_text_file(password_file)

        # The legacy VCD Admin XML create endpoint accepts one Role href.
        # Seed the LOCAL user with the first configured role, then reconcile
        # the complete role list through CloudAPI below.
        initial_role = desired_roles[0]
        initial_role_id = object_id(initial_role)
        initial_role_name = str(initial_role.get("name", "") or "")

        create(
            f"First user '{username}' in organization '{tenant_name}' "
            f"with initial role '{initial_role_name}'"
        )

        try:
            create_local_user_legacy(
                privileged_write_client,
                tenant_id,
                username,
                password,
                str(initial_role_id),
            )
        except RuntimeError as exc:
            raise RuntimeError(
                f"Failed to create first user '{username}' in organization "
                f"'{tenant_name}'.\n"
                f"Endpoint: /api/admin/org/{bare_org_uuid(tenant_id)}/users\n"
                f"API Accept: {VCFA_LEGACY_XML_ACCEPT}\n"
                f"Initial role: {initial_role_name} [{initial_role_id}]\n"
                f"{exc}"
            ) from None

        deadline = time.time() + 60
        user = None
        while time.time() < deadline:
            user = tenant_user_by_name(
                user_provider_client,
                tenant_id,
                username,
            )
            if user:
                print(
                    f"[CREATED] First user '{username}' in organization "
                    f"'{tenant_name}' [{object_id(user) or 'no-id'}]"
                )
                break
            time.sleep(2)

        if not user:
            raise RuntimeError(
                f"Local user '{username}' was created in organization "
                f"'{tenant_name}', but did not become queryable "
                "within 60 seconds."
            )

        # Apply all configured roles (and enabled state) after initial create.
        user = reconcile_existing_user(user)

    # Token creation is intentionally processed even when the local user
    # already existed. With replace_token=true this ensures the token reflects
    # the reconciled role set.
    create_first_user_api_token_if_requested(
        vcfa_server,
        tenant_name,
        first_user_cfg,
        config_dir,
        verify,
    )

    return user


# ============================================================
# Provider organizations / quota / networking
# ============================================================


def provider_organizations(client: RestClient):
    return client.paged("/cloudapi/1.0.0/orgs")


def provider_organization(client: RestClient, name: str):
    for org in provider_organizations(client):
        if org.get("name") == name:
            return org
    return None


def create_provider_organization_if_missing(
    client: RestClient,
    cfg: Dict[str, Any],
    timeout: int = 120,
    poll: int = 2,
):
    name = str(cfg["name"])

    existing = provider_organization(client, name)
    if existing:
        skip(f"VCFA organization '{name}' already exists")
        return existing

    body = {
        "name": name,
        "displayName": cfg.get("display_name", name),
        "description": cfg.get("description", ""),
        "isClassicTenant": bool(cfg.get("is_classic_tenant", False)),
        "isProviderConsumptionOrg": bool(
            cfg.get("is_provider_consumption_org", False)
        ),
        "isEnabled": bool(cfg.get("enabled", True)),
    }

    create(f"VCFA organization '{name}'")

    created = client.post(
        "/cloudapi/1.0.0/orgs",
        json=body,
    )

    # Some VCFA builds return the created Org body; others return
    # an empty/async response. Re-query by name before continuing.
    if isinstance(created, dict):
        created_id = object_id(created)
        created_name = str(created.get("name", "") or "")

        if created_id and created_name == name:
            return created

    deadline = time.time() + timeout

    while time.time() < deadline:
        current = provider_organization(client, name)

        if current:
            info(
                f"VCFA organization '{name}' is available "
                f"[{object_id(current) or 'no-id'}]"
            )
            return current

        time.sleep(poll)

    raise RuntimeError(
        f"VCFA organization '{name}' was created but did not become "
        f"queryable within {timeout} seconds"
    )


def vcfa_supervisors(client: RestClient) -> List[Dict[str, Any]]:
    return client.paged("/cloudapi/v1/supervisors")


def vcfa_supervisor_zones(client: RestClient) -> List[Dict[str, Any]]:
    return client.paged("/cloudapi/v1/supervisorZones")


def wait_vdc_ready(
    client: RestClient,
    name: str,
    timeout: int = 600,
    poll: int = 5,
) -> Dict[str, Any]:
    deadline = time.time() + timeout

    while time.time() < deadline:
        vdcs = client.paged("/cloudapi/v1/virtualDatacenters")
        current = next((v for v in vdcs if v.get("name") == name), None)

        if current:
            status = str(current.get("status", "") or "").upper()

            if status in ("", "READY"):
                return current

            if status == "FAILED":
                raise RuntimeError(
                    f"VCFA region quota '{name}' entered FAILED state"
                )

            info(
                f"Region quota '{name}' status is '{status}'; "
                f"checking again in {poll}s"
            )

        time.sleep(poll)

    raise RuntimeError(
        f"Timed out waiting for VCFA region quota '{name}' to become READY"
    )


def _normalized_ref_list(
    values: Any,
) -> List[tuple]:
    result: List[tuple] = []

    for value in values or []:
        if not isinstance(value, dict):
            continue

        result.append(
            (
                str(object_id(value) or ""),
                str(value.get("name", "") or ""),
            )
        )

    return sorted(result)


def _normalized_zone_allocations(
    values: Any,
) -> List[tuple]:
    result: List[tuple] = []

    for value in values or []:
        if not isinstance(value, dict):
            continue

        zone = object_ref(value, "zone") or {}
        allocation = (
            value.get("resourceAllocation") or {}
        )

        result.append(
            (
                str(object_id(zone) or ""),
                str(zone.get("name", "") or ""),
                int(allocation.get("memoryLimitMiB", 0) or 0),
                int(allocation.get("memoryReservationMiB", 0) or 0),
                int(allocation.get("cpuLimitMHz", 0) or 0),
                int(allocation.get("cpuReservationMHz", 0) or 0),
            )
        )

    return sorted(result)


def _region_quota_differences(
    existing: Dict[str, Any],
    desired: Dict[str, Any],
) -> List[str]:
    """
    Compare only fields managed by this configuration.

    id/status and other server-generated fields are deliberately ignored.
    """
    differences: List[str] = []

    if str(existing.get("name", "") or "") != str(
        desired.get("name", "") or ""
    ):
        differences.append("name")

    existing_full = bool(
        existing.get("isFullAllocation", False)
    )
    desired_full = bool(
        desired.get("isFullAllocation", False)
    )

    if existing_full != desired_full:
        differences.append("isFullAllocation")

    if _normalized_ref_list(
        existing.get("supervisors")
    ) != _normalized_ref_list(
        desired.get("supervisors")
    ):
        differences.append("supervisors")

    if _normalized_zone_allocations(
        existing.get("zoneResourceAllocation")
    ) != _normalized_zone_allocations(
        desired.get("zoneResourceAllocation")
    ):
        differences.append("zoneResourceAllocation")

    return differences


def _normalized_vm_class_refs(values: Any) -> List[tuple]:
    result: List[tuple] = []

    for value in values or []:
        if not isinstance(value, dict):
            continue

        result.append(
            (
                str(value.get("id", "") or ""),
                str(value.get("name", "") or ""),
            )
        )

    return sorted(result)


def region_quota_vm_classes(
    client: RestClient,
    regional_vdc_id: str,
) -> List[Dict[str, Any]]:
    encoded_vdc_id = urllib.parse.quote(
        regional_vdc_id,
        safe="",
    )

    return paged_values(
        client,
        f"/cloudapi/v1/virtualDatacenters/{encoded_vdc_id}/"
        "virtualMachineClasses?sortAsc=name&links=true",
    )


def available_vm_classes(
    client: RestClient,
) -> List[Dict[str, Any]]:
    """Return all provider-visible VM classes, including their region refs."""
    return paged_values(
        client,
        "/cloudapi/v1/virtualMachineClasses?sortAsc=name",
    )


def vm_classes_for_region(
    client: RestClient,
    regional_vdc: Dict[str, Any],
) -> List[Dict[str, str]]:
    """Return provider-visible VM classes belonging to this Regional VDC region."""
    vdc_region = object_ref(regional_vdc, "region", "regionRef") or {}
    region_id = str(object_id(vdc_region) or "")
    region_name = str(vdc_region.get("name", "") or "")

    if not region_id and not region_name:
        raise RuntimeError(
            f"Unable to determine region for Regional VDC "
            f"'{regional_vdc.get('name', '<unknown>')}'"
        )

    result: List[Dict[str, str]] = []

    for vm_class in available_vm_classes(client):
        vm_region = object_ref(vm_class, "region", "regionRef") or {}
        vm_region_id = str(object_id(vm_region) or "")
        vm_region_name = str(vm_region.get("name", "") or "")

        in_region = (
            (region_id and vm_region_id == region_id)
            or (
                not region_id
                and region_name
                and vm_region_name == region_name
            )
        )

        if not in_region:
            continue

        name = str(vm_class.get("name", "") or "").strip()
        vm_class_id = str(object_id(vm_class) or "").strip()

        if name and vm_class_id:
            result.append({
                "name": name,
                "id": vm_class_id,
            })

    return sorted(result, key=lambda item: item["name"])


def resolve_region_quota_vm_classes(
    client: RestClient,
    regional_vdc: Dict[str, Any],
    requested: List[Any],
) -> List[Dict[str, str]]:
    """Resolve configured VM-class names to IDs valid for the quota's region."""
    available = vm_classes_for_region(
        client,
        regional_vdc,
    )

    by_name: Dict[str, List[Dict[str, str]]] = {}
    for vm_class in available:
        by_name.setdefault(vm_class["name"], []).append(vm_class)

    resolved: List[Dict[str, str]] = []
    seen_names = set()

    for entry in requested:
        if isinstance(entry, str):
            requested_name = entry.strip()
        elif isinstance(entry, dict):
            requested_name = str(entry.get("name", "") or "").strip()
        else:
            raise RuntimeError(
                "Each region_quota.resources.vm_classes.classes entry must "
                "be a VM class name string or an object containing name"
            )

        if not requested_name:
            raise RuntimeError(
                "Each region_quota.resources.vm_classes.classes entry "
                "requires a name"
            )

        if requested_name in seen_names:
            continue
        seen_names.add(requested_name)

        matches = by_name.get(requested_name, [])

        if not matches:
            region_ref = object_ref(regional_vdc, "region", "regionRef") or {}
            region_display = str(
                region_ref.get("name")
                or object_id(region_ref)
                or "<unknown>"
            )
            available_names = ", ".join(
                sorted(item["name"] for item in available)
            )
            raise RuntimeError(
                f"VM class '{requested_name}' was not found in region "
                f"'{region_display}'. Available VM classes: "
                f"{available_names or '<none>'}"
            )

        if len(matches) > 1:
            raise RuntimeError(
                f"VM class '{requested_name}' matched multiple VM classes "
                "in the quota region"
            )

        resolved.append(matches[0])
        info(
            f"Resolved VM class '{requested_name}' -> "
            f"'{matches[0]['id']}'"
        )

    return resolved


def reconcile_region_quota_vm_classes(
    client: RestClient,
    regional_vdc: Dict[str, Any],
    cfg: Dict[str, Any],
) -> None:
    """
    Reconcile region_quota.resources.vm_classes.

    Supported JSON:

      "resources": {
        "vm_classes": {
          "all_classes": false,
          "classes": ["best-effort-small", "best-effort-medium"]
        }
      }

    all_classes=true assigns every provider-visible VM class in the Regional
    VDC's region. all_classes=false resolves each configured class name to its
    ID and assigns exactly that set.

    If resources.vm_classes is omitted, existing VM-class assignments are left
    unmanaged. An explicit all_classes=false with classes=[] removes all VM
    classes from the quota.
    """
    resources_cfg = cfg.get("resources")

    if resources_cfg is None:
        return

    if not isinstance(resources_cfg, dict):
        raise RuntimeError(
            "region_quota.resources must be an object"
        )

    if "vm_classes" not in resources_cfg:
        return

    vm_classes_cfg = resources_cfg.get("vm_classes")

    if vm_classes_cfg is None:
        vm_classes_cfg = {}

    if not isinstance(vm_classes_cfg, dict):
        raise RuntimeError(
            "region_quota.resources.vm_classes must be an object"
        )

    all_classes = bool(
        vm_classes_cfg.get("all_classes", False)
    )
    requested = vm_classes_cfg.get("classes", [])

    if requested is None:
        requested = []

    if not isinstance(requested, list):
        raise RuntimeError(
            "region_quota.resources.vm_classes.classes must be an array"
        )

    regional_vdc_id = object_id(regional_vdc)
    regional_vdc_name = str(
        regional_vdc.get("name", "")
        or regional_vdc_id
        or "<unknown>"
    )

    if not regional_vdc_id:
        raise RuntimeError(
            f"Unable to determine Regional VDC ID for region quota "
            f"'{regional_vdc_name}'"
        )

    if all_classes:
        if requested:
            warn(
                f"Region quota '{regional_vdc_name}' has all_classes=true; "
                "the configured classes list will be ignored"
            )

        desired = vm_classes_for_region(
            client,
            regional_vdc,
        )

        if not desired:
            raise RuntimeError(
                f"No VM classes were discovered for region quota "
                f"'{regional_vdc_name}' while all_classes=true"
            )

        info(
            f"Region quota '{regional_vdc_name}' will use all "
            f"{len(desired)} VM class(es) in its region"
        )
    else:
        desired = resolve_region_quota_vm_classes(
            client,
            regional_vdc,
            requested,
        )

    current = region_quota_vm_classes(
        client,
        regional_vdc_id,
    )

    if _normalized_vm_class_refs(current) == _normalized_vm_class_refs(desired):
        skip(
            f"VM classes for VCFA region quota '{regional_vdc_name}' "
            "are already in desired state"
        )
        return

    current_names = sorted(
        str(item.get("name", "") or item.get("id", "") or "")
        for item in current
    )
    desired_names = sorted(
        str(item.get("name", "") or item.get("id", "") or "")
        for item in desired
    )

    info(
        f"Region quota '{regional_vdc_name}' VM classes: "
        f"current={current_names}, desired={desired_names}"
    )

    encoded_vdc_id = urllib.parse.quote(
        regional_vdc_id,
        safe="",
    )

    update(
        f"VM classes for VCFA region quota '{regional_vdc_name}'"
    )

    body = {"values": desired}

    try:
        client.put(
            f"/cloudapi/v1/virtualDatacenters/{encoded_vdc_id}/"
            "virtualMachineClasses",
            json=body,
        )
    except RuntimeError as exc:
        raise RuntimeError(
            f"Failed to update VM classes for VCFA region quota "
            f"'{regional_vdc_name}'.\n"
            f"Payload:\n{json.dumps(body, indent=2)}\n"
            f"{exc}"
        ) from None

    verified = region_quota_vm_classes(
        client,
        regional_vdc_id,
    )

    if _normalized_vm_class_refs(verified) != _normalized_vm_class_refs(desired):
        raise RuntimeError(
            f"VM class update for VCFA region quota '{regional_vdc_name}' "
            "completed, but verification does not match the desired state.\n"
            f"Desired: {json.dumps(desired, indent=2)}\n"
            f"Actual: {json.dumps(verified, indent=2)}"
        )

    info(
        f"VM classes for VCFA region quota '{regional_vdc_name}' verified"
    )


def region_quota_storage_policies(
    client: RestClient,
    regional_vdc_id: str,
) -> List[Dict[str, Any]]:
    """Return storage policies currently assigned to a Regional VDC."""
    result: List[Dict[str, Any]] = []

    for policy in paged_values(
        client,
        "/cloudapi/v1/virtualDatacenterStoragePolicies",
    ):
        vdc_ref = object_ref(policy, "virtualDatacenter") or {}
        if object_id(vdc_ref) == regional_vdc_id:
            result.append(policy)

    return result


def available_region_storage_policies(
    client: RestClient,
) -> List[Dict[str, Any]]:
    return paged_values(
        client,
        "/cloudapi/v1/regionStoragePolicies?sortAsc=name",
    )


def resolve_region_quota_storage_classes(
    client: RestClient,
    regional_vdc: Dict[str, Any],
    requested: List[Any],
    current: List[Dict[str, Any]],
    is_full_allocation: bool,
) -> List[Dict[str, Any]]:
    """
    Resolve storage-class names to Region Storage Policy references.

    For full-allocation region quotas, storageLimitMiB is read-only in VCFA.
    The JSON therefore does not require a limit and the current/server value
    is retained (or 0 is supplied when the assignment is new).

    For partial/defined-resource region quotas, every configured storage class
    must provide a positive limit_gb value. It is converted to MiB for the
    VCFA API. Legacy storage_limit_GB / storage_limit_MiB keys remain accepted.
    """
    regional_vdc_id = object_id(regional_vdc)
    regional_vdc_name = str(regional_vdc.get("name", "") or regional_vdc_id or "<unknown>")
    region_ref = object_ref(regional_vdc, "region", "regionRef") or {}
    region_id = str(object_id(region_ref) or "")
    region_name = str(region_ref.get("name", "") or "")

    if not regional_vdc_id:
        raise RuntimeError(
            f"Unable to determine Regional VDC ID for region quota '{regional_vdc_name}'"
        )

    available = []
    for policy in available_region_storage_policies(client):
        policy_region = object_ref(policy, "region", "regionRef") or {}
        policy_region_id = str(object_id(policy_region) or "")
        policy_region_name = str(policy_region.get("name", "") or "")
        if (region_id and policy_region_id == region_id) or (region_name and policy_region_name == region_name):
            available.append(policy)

    current_by_region_policy_id: Dict[str, Dict[str, Any]] = {}
    for policy in current:
        region_policy_ref = object_ref(policy, "regionStoragePolicy") or {}
        region_policy_id = str(object_id(region_policy_ref) or "")
        if region_policy_id:
            current_by_region_policy_id[region_policy_id] = policy

    resolved: List[Dict[str, Any]] = []
    seen_ids = set()

    for entry in requested:
        if isinstance(entry, str):
            requested_name = entry.strip()
            requested_region = ""
            configured_limit_mib = None
        elif isinstance(entry, dict):
            requested_name = str(entry.get("name", "") or "").strip()
            requested_region = str(entry.get("region", "") or "").strip()
            configured_limit_mib = None

            # Canonical JSON field: storage_limit_mib maps directly to
            # VCFA storageLimitMiB without conversion.
            if "storage_limit_mib" in entry:
                value = entry.get("storage_limit_mib")
                if value is not None:
                    try:
                        configured_limit_mib = int(value)
                    except (TypeError, ValueError):
                        raise RuntimeError(
                            f"Storage class '{requested_name or '<unnamed>'}' has invalid "
                            f"storage_limit_mib={value!r}; expected an integer or null"
                        ) from None
            # Backwards-compatible legacy keys.
            elif "storage_limit_MiB" in entry:
                value = entry.get("storage_limit_MiB")
                if value is not None:
                    configured_limit_mib = int(value)
            elif "storage_limit_GB" in entry:
                value = entry.get("storage_limit_GB")
                if value is not None:
                    configured_limit_mib = int(round(float(value) * 1024))
            elif "limit_gb" in entry:
                value = entry.get("limit_gb")
                if value is not None:
                    configured_limit_mib = int(round(float(value) * 1024))
        else:
            raise RuntimeError(
                "Each region_quota.resources.storage_classes entry must be a name string or an object"
            )

        if not requested_name:
            raise RuntimeError(
                "Each region_quota.resources.storage_classes entry requires a name"
            )

        if requested_region and requested_region != region_name:
            raise RuntimeError(
                f"Storage class '{requested_name}' requests region '{requested_region}', "
                f"but region quota '{regional_vdc_name}' belongs to '{region_name}'"
            )

        matches = []
        for policy in available:
            names = {
                str(policy.get("name", "") or ""),
                str(policy.get("kubernetesCompliantName", "") or ""),
            }
            if requested_name in names:
                matches.append(policy)

        if not matches:
            available_names = sorted({
                str(policy.get("kubernetesCompliantName") or policy.get("name") or "")
                for policy in available
                if policy.get("kubernetesCompliantName") or policy.get("name")
            })
            raise RuntimeError(
                f"Storage class '{requested_name}' was not found in region '{region_name}'. "
                f"Available storage classes: {available_names}"
            )

        if len(matches) > 1:
            raise RuntimeError(
                f"Storage class name '{requested_name}' is ambiguous in region '{region_name}'"
            )

        match = matches[0]
        region_policy_id = object_id(match)
        if not region_policy_id:
            raise RuntimeError(
                f"Unable to determine Region Storage Policy ID for '{requested_name}'"
            )

        if region_policy_id in seen_ids:
            continue
        seen_ids.add(region_policy_id)

        existing = current_by_region_policy_id.get(str(region_policy_id))

        if is_full_allocation:
            # VCFA documents storageLimitMiB as read-only for full-allocation
            # region quotas. Do not require or enforce a limit from JSON.
            # The bulk API still requires the field, so retain the server value
            # for an existing assignment, or use 0 for a new assignment.
            if configured_limit_mib is not None:
                info(
                    f"Ignoring configured storage limit for '{requested_name}' on "
                    f"full-allocation region quota '{regional_vdc_name}'"
                )
            configured_limit_mib = int(
                (existing or {}).get("storageLimitMiB", 0) or 0
            )
        else:
            # A partial/defined-resource quota needs an explicit capacity for
            # every storage class. Zero/null would effectively leave it without
            # usable allocated storage.
            if configured_limit_mib is None:
                raise RuntimeError(
                    f"Storage class '{requested_name}' in partial region quota "
                    f"'{regional_vdc_name}' requires storage_limit_mib > 0"
                )
            if configured_limit_mib <= 0:
                raise RuntimeError(
                    f"Storage class '{requested_name}' in partial region quota "
                    f"'{regional_vdc_name}' has invalid storage_limit_mib; "
                    f"the value must be greater than 0"
                )

        resolved.append({
            "name": str(match.get("kubernetesCompliantName") or match.get("name") or requested_name),
            "virtualDatacenter": {
                "name": regional_vdc_name,
                "id": regional_vdc_id,
            },
            "regionStoragePolicy": {
                "name": str(match.get("name", "") or requested_name),
                "id": str(region_policy_id),
            },
            "storageLimitMiB": configured_limit_mib,
        })

    return resolved


def _normalized_region_quota_storage_policies(values: List[Dict[str, Any]]) -> List[tuple]:
    result = []
    for value in values or []:
        ref = object_ref(value, "regionStoragePolicy") or {}
        result.append((
            str(object_id(ref) or ""),
            int(value.get("storageLimitMiB", 0) or 0),
        ))
    return sorted(result)


def reconcile_region_quota_storage_classes(
    client: RestClient,
    regional_vdc: Dict[str, Any],
    cfg: Dict[str, Any],
) -> None:
    """Reconcile region_quota.resources.storage_classes using VCFA 9.1 bulk VDC storage-policy API."""
    resources_cfg = cfg.get("resources")
    if resources_cfg is None:
        return
    if not isinstance(resources_cfg, dict):
        raise RuntimeError("region_quota.resources must be an object")
    if "storage_classes" not in resources_cfg:
        return

    requested = resources_cfg.get("storage_classes")
    if requested is None:
        requested = []
    if not isinstance(requested, list):
        raise RuntimeError(
            "region_quota.resources.storage_classes must be an array"
        )

    regional_vdc_id = object_id(regional_vdc)
    regional_vdc_name = str(regional_vdc.get("name", "") or regional_vdc_id or "<unknown>")
    if not regional_vdc_id:
        raise RuntimeError(
            f"Unable to determine Regional VDC ID for region quota '{regional_vdc_name}'"
        )

    current = region_quota_storage_policies(client, regional_vdc_id)
    is_full_allocation = bool(cfg.get("is_full_allocation", False))

    desired = resolve_region_quota_storage_classes(
        client,
        regional_vdc,
        requested,
        current,
        is_full_allocation,
    )

    if _normalized_region_quota_storage_policies(current) == _normalized_region_quota_storage_policies(desired):
        skip(
            f"Storage classes for VCFA region quota '{regional_vdc_name}' are already in desired state"
        )
        return

    current_summary = [
        {
            "name": str((object_ref(x, "regionStoragePolicy") or {}).get("name", "")),
            "limit_gb": round(int(x.get("storageLimitMiB", 0) or 0) / 1024, 3),
        }
        for x in current
    ]
    desired_summary = [
        {
            "name": str((object_ref(x, "regionStoragePolicy") or {}).get("name", "")),
            "limit_gb": round(int(x.get("storageLimitMiB", 0) or 0) / 1024, 3),
        }
        for x in desired
    ]

    info(
        f"Region quota '{regional_vdc_name}' storage classes: "
        f"current={current_summary}, desired={desired_summary}"
    )

    encoded_vdc_id = urllib.parse.quote(regional_vdc_id, safe="")
    body = {"values": desired}

    update(
        f"Storage classes for VCFA region quota '{regional_vdc_name}'"
    )

    try:
        response = client.put(
            f"/cloudapi/v1/virtualDatacenters/{encoded_vdc_id}/"
            "virtualDatacenterStoragePolicies",
            json=body,
        )
    except RuntimeError as exc:
        raise RuntimeError(
            f"Failed to update storage classes for VCFA region quota '{regional_vdc_name}'.\n"
            f"Payload:\n{json.dumps(body, indent=2)}\n{exc}"
        ) from None

    verified = region_quota_storage_policies(client, regional_vdc_id)
    if _normalized_region_quota_storage_policies(verified) != _normalized_region_quota_storage_policies(desired):
        raise RuntimeError(
            f"Storage class update for VCFA region quota '{regional_vdc_name}' completed, "
            "but verification does not match the desired state.\n"
            f"Desired: {json.dumps(desired, indent=2)}\n"
            f"Actual: {json.dumps(verified, indent=2)}"
        )

    info(
        f"Storage classes for VCFA region quota '{regional_vdc_name}' verified"
    )

def region_quota_if_missing(
    client: RestClient,
    tenant: Dict[str, Any],
    cfg: Dict[str, Any],
):
    """
    Create OR reconcile a VCFA Region Quota.

    Existing quotas are located by organization ID + region ID, not by name.
    Managed fields are compared with the desired JSON and updated with
    PUT /cloudapi/v1/virtualDatacenters/{vdcUrn} when they differ.
    """
    if not cfg or not cfg.get("region"):
        skip(
            f"No region quota configured for "
            f"'{tenant.get('name','')}'"
        )
        return None

    tenant_name = str(
        tenant.get("name", "") or ""
    )
    tenant_id = object_id(tenant)

    if not tenant_id:
        raise RuntimeError(
            f"Unable to determine organization ID for '{tenant_name}'"
        )

    region_name = str(cfg["region"])
    region = vcfa_region(
        client,
        region_name,
    )

    if not region:
        raise RuntimeError(
            f"VCFA region '{region_name}' not found"
        )

    region_id = object_id(region)

    if not region_id:
        raise RuntimeError(
            f"Unable to determine ID for VCFA region '{region_name}'"
        )

    expected = f"{tenant_name}-{region_name}"

    # ------------------------------------------------------------
    # Build desired state first. This is used for both CREATE and UPDATE.
    # ------------------------------------------------------------

    is_full_allocation = bool(
        cfg.get("is_full_allocation", False)
    )

    body: Dict[str, Any] = {
        "name": expected,
        "org": {
            "name": tenant_name,
            "id": tenant_id,
        },
        "region": {
            "name": region_name,
            "id": region_id,
        },
        "isFullAllocation": is_full_allocation,
    }

    if is_full_allocation:
        # Explicit empty arrays make transitions from limited -> full
        # allocation deterministic during PUT.
        body["supervisors"] = []
        body["zoneResourceAllocation"] = []

    else:
        all_supervisors = vcfa_supervisors(
            client
        )

        region_supervisors: List[Dict[str, str]] = []

        for supervisor in all_supervisors:
            supervisor_region = (
                object_ref(supervisor, "region") or {}
            )
            supervisor_region_id = object_id(
                supervisor_region
            )
            supervisor_region_name = str(
                supervisor_region.get("name", "") or ""
            )

            if (
                supervisor_region_id == region_id
                or supervisor_region_name == region_name
            ):
                supervisor_id = (
                    supervisor.get("supervisorId")
                    or supervisor.get("id")
                )

                if supervisor_id:
                    region_supervisors.append(
                        {
                            "name": str(
                                supervisor.get("name", "") or ""
                            ),
                            "id": str(supervisor_id),
                        }
                    )

        if bool(
            cfg.get("all_supervisors", False)
        ):
            selected_supervisors = region_supervisors

        else:
            requested_supervisors = (
                cfg.get("supervisors", []) or []
            )
            selected_supervisors = []

            for requested in requested_supervisors:
                requested_name = (
                    str(requested)
                    if isinstance(requested, str)
                    else str(
                        requested.get("name", "") or ""
                    )
                )

                requested_id = (
                    ""
                    if isinstance(requested, str)
                    else str(
                        requested.get("id")
                        or requested.get("supervisorId")
                        or ""
                    )
                )

                match = next(
                    (
                        supervisor
                        for supervisor in region_supervisors
                        if (
                            (
                                requested_id
                                and supervisor["id"] == requested_id
                            )
                            or (
                                requested_name
                                and supervisor["name"] == requested_name
                            )
                        )
                    ),
                    None,
                )

                if not match:
                    raise RuntimeError(
                        f"Supervisor "
                        f"'{requested_name or requested_id}' "
                        f"was not found in region '{region_name}'"
                    )

                selected_supervisors.append(
                    match
                )

        if not selected_supervisors:
            raise RuntimeError(
                f"Region quota '{expected}' is not full-allocation "
                "and requires at least one supervisor"
            )

        body["supervisors"] = selected_supervisors

        capacity = cfg.get("capacity") or {}
        all_zones = bool(
            capacity.get("all_zones", False)
        )
        configured_zones = (
            capacity.get("zones", []) or []
        )

        zone_resource_allocation: List[
            Dict[str, Any]
        ] = []

        if not all_zones:
            available_zones = (
                vcfa_supervisor_zones(client)
            )

            for zone_cfg in configured_zones:
                zone_name = str(
                    zone_cfg.get("zone", "") or ""
                )

                if not zone_name:
                    raise RuntimeError(
                        f"A zone entry in region quota "
                        f"'{expected}' is missing 'zone'"
                    )

                zone_match = None

                for zone_record in available_zones:
                    zone_ref = (
                        object_ref(zone_record, "zone")
                        or {}
                    )
                    zone_region = (
                        object_ref(zone_record, "region")
                        or {}
                    )

                    candidate_name = str(
                        zone_ref.get("name")
                        or zone_record.get("name")
                        or ""
                    )

                    candidate_region_id = object_id(
                        zone_region
                    )
                    candidate_region_name = str(
                        zone_region.get("name", "") or ""
                    )

                    if (
                        candidate_name == zone_name
                        and (
                            candidate_region_id == region_id
                            or candidate_region_name
                            == region_name
                        )
                    ):
                        zone_match = zone_record
                        break

                if not zone_match:
                    raise RuntimeError(
                        f"Supervisor zone '{zone_name}' "
                        f"was not found in region '{region_name}'"
                    )

                zone_ref = (
                    object_ref(zone_match, "zone")
                    or {}
                )
                zone_id = (
                    object_id(zone_ref)
                    or object_id(zone_match)
                )

                if not zone_id:
                    raise RuntimeError(
                        f"Unable to determine ID for "
                        f"supervisor zone '{zone_name}'"
                    )

                resource_allocation = {
                    "memoryLimitMiB": int(
                        round(
                            float(
                                zone_cfg.get(
                                    "memory_limit_GB",
                                    0,
                                )
                            )
                            * 1024
                        )
                    ),
                    "memoryReservationMiB": int(
                        round(
                            float(
                                zone_cfg.get(
                                    "memory_reservation_GB",
                                    0,
                                )
                            )
                            * 1024
                        )
                    ),
                    "cpuLimitMHz": int(
                        round(
                            float(
                                zone_cfg.get(
                                    "cpu_limit_GHz",
                                    0,
                                )
                            )
                            * 1000
                        )
                    ),
                    "cpuReservationMHz": int(
                        round(
                            float(
                                zone_cfg.get(
                                    "cpu_reservation_GHz",
                                    0,
                                )
                            )
                            * 1000
                        )
                    ),
                }

                zone_resource_allocation.append(
                    {
                        "zone": {
                            "name": zone_name,
                            "id": str(zone_id),
                        },
                        "resourceAllocation":
                            resource_allocation,
                    }
                )

        # Empty means all zones for the selected supervisors.
        body["zoneResourceAllocation"] = (
            zone_resource_allocation
        )

    # ------------------------------------------------------------
    # Find the unique existing quota by tenant + region.
    # ------------------------------------------------------------

    vdcs = paged_values(
        client,
        "/cloudapi/v1/virtualDatacenters",
    )

    existing = None

    for vdc in vdcs:
        vdc_org = (
            object_ref(vdc, "org") or {}
        )
        vdc_region = (
            object_ref(vdc, "region") or {}
        )

        if (
            object_id(vdc_org) == tenant_id
            and object_id(vdc_region) == region_id
        ):
            existing = vdc
            break

    # ------------------------------------------------------------
    # UPDATE
    # ------------------------------------------------------------

    if existing:
        existing_name = str(
            existing.get("name", "") or expected
        )

        differences = _region_quota_differences(
            existing,
            body,
        )

        if not differences:
            skip(
                f"VCFA region quota '{existing_name}' "
                f"is already in desired state for organization "
                f"'{tenant_name}' and region '{region_name}'"
            )
            reconcile_region_quota_vm_classes(
                client,
                existing,
                cfg,
            )
            reconcile_region_quota_storage_classes(
                client,
                existing,
                cfg,
            )
            return existing

        existing_id = object_id(
            existing
        )

        if not existing_id:
            raise RuntimeError(
                f"Unable to determine ID for existing regional "
                f"quota '{existing_name}'"
            )

        info(
            f"Region quota differences for "
            f"'{existing_name}': {', '.join(differences)}"
        )

        update(
            f"VCFA region quota '{existing_name}'"
        )

        # Include the existing ID on update. Ignore status because it is
        # server-managed/read-only.
        update_body = dict(body)
        update_body["id"] = existing_id

        encoded_vdc_id = urllib.parse.quote(
            existing_id,
            safe="",
        )

        try:
            client.put(
                f"/cloudapi/v1/virtualDatacenters/"
                f"{encoded_vdc_id}",
                json=update_body,
            )
        except RuntimeError as exc:
            payload_text = json.dumps(
                update_body,
                indent=2,
            )
            raise RuntimeError(
                f"Failed to update VCFA region quota "
                f"'{existing_name}'.\n"
                f"Changed fields: {', '.join(differences)}\n"
                f"Payload:\n{payload_text}\n"
                f"{exc}"
            ) from None

        updated_vdc = wait_vdc_ready(
            client,
            expected,
        )

        reconcile_region_quota_vm_classes(
            client,
            updated_vdc,
            cfg,
        )
        reconcile_region_quota_storage_classes(
            client,
            updated_vdc,
            cfg,
        )

        return updated_vdc

    # ------------------------------------------------------------
    # CREATE
    # ------------------------------------------------------------

    create(
        f"VCFA region quota '{expected}'"
    )

    try:
        client.post(
            "/cloudapi/v1/virtualDatacenters",
            json=body,
        )
    except RuntimeError as exc:
        payload_text = json.dumps(
            body,
            indent=2,
        )
        raise RuntimeError(
            f"Failed to create VCFA region quota '{expected}'.\n"
            f"Payload:\n{payload_text}\n"
            f"{exc}"
        ) from None

    created_vdc = wait_vdc_ready(
        client,
        expected,
    )

    reconcile_region_quota_vm_classes(
        client,
        created_vdc,
        cfg,
    )
    reconcile_region_quota_storage_classes(
        client,
        created_vdc,
        cfg,
    )

    return created_vdc


def set_external_connection(client: RestClient, tenant: Dict[str, Any], region: Dict[str, Any], cfg: Dict[str, Any]):
    if not cfg:
        return
    connection_name = cfg.get("name")
    cluster_name = cfg.get("cluster")
    if not connection_name or not cluster_name:
        skip(f"External connection incomplete for '{tenant.get('name','')}'")
        return
    settings = client.paged("/cloudapi/v1/regionalNetworkingSettings")
    tenant_id = object_id(tenant)
    region_id = object_id(region)
    for s in settings:
        org_ref = object_ref(s, "orgRef", "org") or {}
        region_ref = object_ref(s, "regionRef", "region") or {}
        if object_id(org_ref) == tenant_id and object_id(region_ref) == region_id:
            skip(f"Regional networking setting already exists for '{tenant.get('name')}' / '{region.get('name')}'")
            return

    connections = client.paged("/cloudapi/v1/distributedVlanConnections")
    conn = next((x for x in connections if x.get("name") == connection_name), None)
    if not conn:
        raise RuntimeError(f"Distributed VLAN Connection '{connection_name}' not found")
    clusters = client.paged("/cloudapi/v1/virtualNetworkApplianceClusters")
    cluster = next((x for x in clusters if x.get("name") == cluster_name), None)
    if not cluster:
        raise RuntimeError(f"VNA Cluster '{cluster_name}' not found")
    body = {
        "name": f"{tenant.get('name')}-{region.get('name')}",
        "orgRef": {"name": tenant.get("name"), "id": tenant_id},
        "regionRef": {"name": region.get("name"), "id": region_id},
        "distributedVlanConnectionRef": {"name": connection_name, "id": object_id(conn)},
        "virtualNetworkApplianceClusterRef": {"name": cluster_name, "id": object_id(cluster)},
    }
    create(f"Regional networking setting '{body['name']}'")
    client.post("/cloudapi/v1/regionalNetworkingSettings", json=body)


# ============================================================
# Organization content libraries
# ============================================================


def organization_scoped_client(
    client: RestClient,
    org: Dict[str, Any],
) -> RestClient:
    org_id = object_id(org)
    org_name = str(org.get("name", "") or "")

    if not org_id or not org_name:
        raise RuntimeError(
            "Organization ID/name is required for organization-scoped API calls"
        )

    org_uuid = str(org_id).split(":")[-1]

    headers = dict(client.session.headers)
    headers["X-VMWARE-VCLOUD-AUTH-CONTEXT"] = org_name
    headers["X-VMWARE-VCLOUD-TENANT-CONTEXT"] = org_uuid

    return RestClient(
        client.server,
        verify=client.verify,
        headers=headers,
    )


def tenant_content_libraries(
    client: RestClient,
) -> List[Dict[str, Any]]:
    return paged_values(
        client,
        "/cloudapi/v1/contentLibraries",
    )


def tenant_content_library(
    client: RestClient,
    name: str,
) -> Optional[Dict[str, Any]]:
    for lib in tenant_content_libraries(client):
        if str(get_value(lib, "name", default="")) == name:
            return lib

    return None


def organization_regional_vdc(
    provider_client: RestClient,
    org: Dict[str, Any],
    region_name: str,
) -> Optional[Dict[str, Any]]:
    """Return the Regional VDC for an organization + region."""
    org_id = object_id(org)
    if not org_id:
        raise RuntimeError(
            f"Unable to determine organization ID for '{org.get('name', '<unknown>')}'"
        )

    for vdc in paged_values(
        provider_client,
        "/cloudapi/v1/virtualDatacenters",
    ):
        vdc_org = object_ref(vdc, "org") or {}
        vdc_region = object_ref(vdc, "region", "regionRef") or {}
        if object_id(vdc_org) != org_id:
            continue
        if str(vdc_region.get("name", "") or "") == region_name:
            return vdc

    return None


def validate_organization_library_storage_backing(
    provider_client: RestClient,
    org: Dict[str, Any],
    provider_org_cfg: Dict[str, Any],
    library_cfg: Dict[str, Any],
) -> None:
    """
    Validate that each configured organization content-library storage class is
    actually assigned to the organization's live Regional VDC.

    This catches cases where the library object itself can be created but a
    TEMPLATE/OVF item later fails immediately after descriptor.ovf because the
    tenant Regional VDC does not have compatible storage backing.
    """
    library_name = str(library_cfg.get("name", "<unnamed>") or "<unnamed>")
    requested = library_cfg.get("storage_classes") or []
    if not requested:
        return

    quota_cfg = provider_org_cfg.get("region_quota") or {}
    if not isinstance(quota_cfg, dict) or not quota_cfg.get("region"):
        warn(
            f"Cannot validate Regional VDC storage backing for content library "
            f"'{library_name}' because vcfa.provider.organizations."
            f"{org.get('name', '<unknown>')}.region_quota.region is not configured"
        )
        return

    region_name = str(quota_cfg.get("region", "") or "").strip()
    vdc = organization_regional_vdc(provider_client, org, region_name)
    if not vdc:
        raise RuntimeError(
            f"Content library '{library_name}' storage validation failed: no "
            f"Regional VDC was found for organization '{org.get('name', '')}' "
            f"in region '{region_name}'"
        )

    vdc_id = object_id(vdc)
    vdc_name = str(vdc.get("name", "") or vdc_id or "<unknown>")
    current_assignments = region_quota_storage_policies(provider_client, vdc_id)

    # Build a rich lookup for the region policies so both the display name and
    # kubernetesCompliantName can be compared with JSON storage-class names.
    region_policy_lookup: Dict[str, Dict[str, Any]] = {}
    for policy in available_region_storage_policies(provider_client):
        policy_region = object_ref(policy, "region", "regionRef") or {}
        if str(policy_region.get("name", "") or "") != region_name:
            continue
        pid = str(object_id(policy) or "")
        if pid:
            region_policy_lookup[pid] = policy

    assigned_names = set()
    assigned_details = []
    for assignment in current_assignments:
        ref = object_ref(assignment, "regionStoragePolicy") or {}
        pid = str(object_id(ref) or "")
        policy = region_policy_lookup.get(pid, {})
        display_name = str(policy.get("name") or ref.get("name") or "")
        kube_name = str(policy.get("kubernetesCompliantName") or "")
        if display_name:
            assigned_names.add(display_name)
        if kube_name:
            assigned_names.add(kube_name)
        assigned_details.append(
            {
                "name": display_name,
                "kubernetesCompliantName": kube_name,
                "id": pid,
            }
        )

    requested_names = []
    missing = []
    for entry in requested:
        if isinstance(entry, str):
            req_name = entry.strip()
            req_region = ""
        elif isinstance(entry, dict):
            req_name = str(entry.get("name", "") or "").strip()
            req_region = str(entry.get("region", "") or "").strip()
        else:
            raise RuntimeError(
                f"Content library '{library_name}' has an invalid storage_classes entry"
            )

        if not req_name:
            raise RuntimeError(
                f"Content library '{library_name}' has a storage_classes entry without a name"
            )
        if req_region and req_region != region_name:
            raise RuntimeError(
                f"Content library '{library_name}' requests storage class '{req_name}' "
                f"in region '{req_region}', but the organization Regional VDC is in "
                f"'{region_name}'"
            )

        requested_names.append(req_name)
        if req_name not in assigned_names:
            missing.append(req_name)

    info(
        f"Content library '{library_name}' storage backing check: Regional VDC "
        f"'{vdc_name}', requested={requested_names}, assigned="
        f"{sorted(name for name in assigned_names if name)}"
    )

    if missing:
        raise RuntimeError(
            f"Content library '{library_name}' requires storage class(es) not assigned "
            f"to Regional VDC '{vdc_name}': {', '.join(missing)}\n"
            f"Assigned Regional VDC storage policies:\n"
            f"{json.dumps(assigned_details, indent=2)}"
        )

    info(
        f"Content library '{library_name}' storage classes are available on "
        f"Regional VDC '{vdc_name}'"
    )

def tenant_storage_classes(
    client: RestClient,
) -> List[Dict[str, Any]]:
    return paged_values(
        client,
        "/cloudapi/v1/storageClasses",
    )


def resolve_tenant_library_storage_classes(
    client: RestClient,
    cfg: Dict[str, Any],
) -> List[Dict[str, Any]]:
    requested = cfg.get("storage_classes", [])

    if not requested:
        raise RuntimeError(
            f"Organization content library "
            f"'{cfg.get('name', '<unnamed>')}' requires storage_classes"
        )

    available = tenant_storage_classes(client)
    resolved: List[Dict[str, Any]] = []

    for req in requested:
        if isinstance(req, str):
            req_name = req
            req_region = ""
        else:
            req_name = str(req.get("name", "") or "")
            req_region = str(req.get("region", "") or "")

        matches = []

        for sc in available:
            sc_name = str(sc.get("name", "") or "")
            kube_name = str(sc.get("kubernetesCompliantName", "") or "")

            if req_name not in (sc_name, kube_name):
                continue

            region_ref = object_ref(sc, "region", "regionRef") or {}
            region_name = str(region_ref.get("name", "") or "")

            if req_region and region_name != req_region:
                continue

            matches.append(sc)

        if not matches:
            raise RuntimeError(
                f"Organization storage class '{req_name}' was not found"
            )

        if len(matches) > 1 and not req_region:
            raise RuntimeError(
                f"Organization storage class '{req_name}' exists in multiple "
                "regions; specify region"
            )

        sid = object_id(matches[0])

        if not sid:
            raise RuntimeError(
                f"Storage class '{req_name}' is missing an ID"
            )

        resolved.append({"id": sid})

    return resolved


def create_organization_library_if_missing(
    client: RestClient,
    org: Dict[str, Any],
    cfg: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    name = str(cfg.get("name", "") or "").strip()

    if not name:
        raise RuntimeError(
            "organization content library name is required"
        )

    scoped = organization_scoped_client(
        client,
        org,
    )

    existing = tenant_content_library(
        scoped,
        name,
    )

    if existing:
        skip(
            f"Organization content library '{name}' already exists"
        )
        return existing

    requested_subscribed = bool(
        cfg.get("is_subscribed", False)
    )

    subscription = (
        cfg.get("subscription")
        if isinstance(cfg.get("subscription"), dict)
        else {}
    )

    subscription_url = str(
        subscription.get("url", "") or ""
    )

    if requested_subscribed and not subscription_url.strip():
        skip(
            f"Organization content library '{name}' has "
            "is_subscribed=true but no usable subscription; skipping"
        )
        return None

    body = {
        "name": name,
        "description": cfg.get("description", ""),
        "isSubscribed": requested_subscribed,
        "autoAttach": bool(cfg.get("auto_attach", False)),
        "storageClasses": resolve_tenant_library_storage_classes(
            scoped,
            cfg,
        ),
    }

    if requested_subscribed:
        sub = {
            "subscriptionUrl": subscription_url,
        }

        if "authenticated" in subscription:
            sub["authenticated"] = bool(
                subscription["authenticated"]
            )

        if subscription.get("password") is not None:
            sub["password"] = subscription["password"]

        if "need_local_copy" in subscription:
            sub["needLocalCopy"] = bool(
                subscription["need_local_copy"]
            )

        body["subscriptionConfig"] = sub

    create(
        f"Organization content library '{name}'"
    )

    scoped.post(
        "/cloudapi/v1/contentLibraries",
        json=body,
        headers={
            "Accept": "application/json;version=10.0.0.0-alpha",
            "Content-Type": "application/json",
        },
    )

    deadline = time.time() + 180

    while time.time() < deadline:
        found = tenant_content_library(
            scoped,
            name,
        )

        if found:
            status = str(found.get("status", "") or "").upper()

            if status in ("FAILED", "ERROR"):
                raise RuntimeError(
                    f"Organization content library '{name}' entered "
                    f"status '{status}'"
                )

            info(
                f"Organization content library '{name}' created"
            )
            return found

        time.sleep(3)

    raise RuntimeError(
        f"Organization content library '{name}' did not appear within "
        "180 seconds"
    )


def configure_organization_content_libraries(
    client: RestClient,
    provider_client: RestClient,
    org: Dict[str, Any],
    provider_org_cfg: Dict[str, Any],
    libraries_cfg: Dict[str, Any],
    config_dir: Path,
) -> None:
    if not libraries_cfg:
        return

    if not isinstance(libraries_cfg, dict):
        raise RuntimeError(
            "organization content_libraries must be an object keyed by library name"
        )

    for library_name, lib_cfg in libraries_cfg.items():
        if not isinstance(lib_cfg, dict):
            raise RuntimeError(
                f"content_libraries['{library_name}'] must be an object"
            )

        effective_cfg = dict(lib_cfg)
        effective_cfg["name"] = library_name

        validate_organization_library_storage_backing(
            provider_client,
            org,
            provider_org_cfg,
            effective_cfg,
        )

        library = create_organization_library_if_missing(
            client,
            org,
            effective_cfg,
        )

        if not library:
            continue

        is_subscribed = bool(
            effective_cfg.get("is_subscribed", False)
        )

        subscription_url = str(
            (effective_cfg.get("subscription") or {}).get(
                "url",
                "",
            )
            or ""
        )

        items = effective_cfg.get("items", []) or []

        if is_subscribed and subscription_url.strip():
            if items:
                skip(
                    f"Ignoring {len(items)} local item(s) for subscribed "
                    f"organization content library '{library_name}'"
                )
            continue

        for item_cfg in items:
            upload_content_library_item(
                organization_scoped_client(client, org),
                library,
                item_cfg,
                config_dir,
                system_scope=False,
            )


# ============================================================
# Organization-scoped LDAP
# ============================================================

def organization_ldap_body(identity: Dict[str, Any], config_dir: Path):
    settings_source = str(identity.get("settings_source", "DEFINED")).upper()
    body = {"enabled": True, "settingsSource": settings_source}
    if settings_source != "DEFINED":
        return body

    cfg = identity.get("config") or {}
    conn = cfg.get("connection") or {}
    defined = {}

    mapping = {
        "host_name": "hostName",
        "port": "port",
        "ssl": "ssl",
        "search_base": "searchBase",
        "username": "userName",
    }
    for src, dst in mapping.items():
        if src in conn:
            defined[dst] = conn[src]

    if "group_search_base" in conn:
        defined["groupSearchBase"] = conn["group_search_base"]

    # VCFA 9.1 OpenAPI OrganizationLdapSettings does not expose
    # ldapType or authenticationMechanism in definedSettings.
    # Keep those values in the JSON if useful for documentation, but
    # do not send them in the OpenAPI request body.
    if conn.get("password_file"):
        defined["password"] = read_text_file(
            resolve_path(conn["password_file"], config_dir)
        )

    user_cfg = cfg.get("user_attributes") or {}
    user_map = {
        "object_class": "objectClass",
        "object_identifier": "objectIdentifier",
        "username": "userName",
        "email": "email",
        "display_name": "displayName",
        "given_name": "givenName",
        "surname": "surname",
        "telephone": "telephone",
        "group_membership_identifier": "groupMembershipIdentifier",
        "group_back_link": "groupBackLinkIdentifier",
    }
    if user_cfg:
        user_attributes = {}

        for src, dst in user_map.items():
            if src not in user_cfg:
                continue

            value = user_cfg[src]

            # VCFA rejects an empty/null group back-link identifier.
            if src == "group_back_link":
                if value is None or not str(value).strip():
                    continue

            user_attributes[dst] = value

        defined["userAttributes"] = user_attributes

    group_cfg = cfg.get("group_attributes") or {}
    group_map = {
        "object_class": "objectClass",
        "object_identifier": "objectIdentifier",
        "group_name": "groupName",
        "membership": "membership",
        "membership_identifier": "membershipIdentifier",
        "group_back_link_identifier": "backLinkIdentifier",
    }
    if group_cfg:
        group_attributes = {}

        for src, dst in group_map.items():
            if src not in group_cfg:
                continue

            value = group_cfg[src]

            # VCFA rejects an empty/null group back-link identifier.
            if src == "group_back_link_identifier":
                if value is None or not str(value).strip():
                    continue

            group_attributes[dst] = value

        defined["groupAttributes"] = group_attributes

    button = cfg.get("button") or {}
    if "label" in button:
        defined["customUiButtonLabel"] = button["label"]

    body["definedSettings"] = defined
    return body


def ldap_normalize_value(value: Any) -> Any:
    """
    Normalize values for comparison so harmless representation differences
    do not trigger an update.
    """
    if isinstance(value, dict):
        normalized = {}
        for key, item in value.items():
            if key == "password":
                continue

            normalized_item = ldap_normalize_value(item)

            # Ignore empty/null optional fields that VCFA may omit on read.
            if normalized_item in (None, "", {}, []):
                continue

            normalized[key] = normalized_item

        return normalized

    if isinstance(value, list):
        return [ldap_normalize_value(x) for x in value]

    if isinstance(value, str):
        return value.strip()

    return value


def ldap_compare_fields(
    desired: Any,
    current: Any,
    path: str = "",
) -> List[str]:
    """
    Return a list of field paths whose desired value differs from the current
    VCFA value. Only fields present in the desired configuration are checked.

    This prevents server-generated/default fields from causing unnecessary
    updates.
    """
    differences: List[str] = []

    desired = ldap_normalize_value(desired)
    current = ldap_normalize_value(current)

    if isinstance(desired, dict):
        current_dict = current if isinstance(current, dict) else {}

        for key, desired_value in desired.items():
            child_path = f"{path}.{key}" if path else key
            current_value = current_dict.get(key)

            differences.extend(
                ldap_compare_fields(
                    desired_value,
                    current_value,
                    child_path,
                )
            )

        return differences

    if isinstance(desired, list):
        current_list = current if isinstance(current, list) else []

        if desired != current_list:
            differences.append(path or "<root>")

        return differences

    if desired != current:
        differences.append(path or "<root>")

    return differences


def configure_organization_ldap(
    client: RestClient,
    org_name: str,
    identity: Dict[str, Any],
    config_dir: Path,
):
    desired = organization_ldap_body(identity, config_dir)
    current = client.get("/cloudapi/v1/orgSettings/ldap") or {}

    differences = ldap_compare_fields(
        desired,
        current,
    )

    if not differences:
        skip(
            f"LDAP identity provider for organization '{org_name}' "
            "is already in desired state"
        )
        return

    info(
        f"LDAP settings differing for organization '{org_name}': "
        + ", ".join(differences)
    )

    update(
        f"LDAP identity provider for organization '{org_name}'"
    )

    client.put(
        "/cloudapi/v1/orgSettings/ldap",
        json=desired,
    )

    print(
        f"[UPDATED] LDAP identity provider for organization '{org_name}'"
    )


def scope_provider_client_to_organization(
    provider_client: RestClient,
    org: Dict[str, Any],
) -> RestClient:
    """
    Return a copy of an authenticated provider client scoped into the target
    organization.

    GROUP_IMPORT needs provider privileges, but LDAP resolution must occur in
    the tenant context so VCFA uses that organization's LDAP settings.
    """
    org_id = object_id(org)

    if not org_id:
        raise RuntimeError(
            f"Unable to determine organization ID for "
            f"'{org.get('name', '<unknown>')}'"
        )

    headers = dict(provider_client.session.headers)
    headers.pop("X-VMWARE-VCLOUD-AUTH-CONTEXT", None)
    headers["X-VMWARE-VCLOUD-TENANT-CONTEXT"] = org_id

    return RestClient(
        provider_client.server,
        verify=provider_client.verify,
        headers=headers,
    )


# ============================================================
# Organization notifications / email server
# ============================================================

def normalize_email_connection_security(value: Any) -> str:
    normalized = str(value or "NONE").strip().upper()

    aliases = {
        "NONE": "NONE",
        "STARTTLS": "STARTTLS",
        "START_TLS": "STARTTLS",
        "START-TLS": "STARTTLS",
        "SSL": "SSL",
        "TLS": "SSL",
        "SSL/TLS": "SSL",
    }

    if normalized not in aliases:
        raise RuntimeError(
            f"Unsupported email connection_security '{value}'. "
            "Use none, starttls, or ssl."
        )

    return aliases[normalized]


def desired_notification_email_config(
    cfg: Dict[str, Any],
    config_dir: Path,
) -> Dict[str, Any]:
    """
    Map the JSON email_server object directly to the VCFA 9.1
    /notification/api/email-config payload.

    JSON:
      description
      host
      sender_name
      sender
      auth_required
      username
      password_file
      connection_security
      port
      starttls_upgrade_enabled
      trust_host
    """
    authentication_required = bool(
        cfg.get("auth_required", False)
    )

    username = str(
        cfg.get("username", "") or ""
    ).strip()

    password = ""

    if authentication_required:
        password_file = str(
            cfg.get("password_file", "") or ""
        ).strip()

        if not username:
            raise RuntimeError(
                "notifications.email_server.username is required "
                "when auth_required=true"
            )

        if not password_file:
            raise RuntimeError(
                "notifications.email_server.password_file is required "
                "when auth_required=true"
            )

        password = read_text_file(
            resolve_path(
                password_file,
                config_dir,
            )
        )

    host = str(
        cfg.get("host", "") or ""
    ).strip()

    if not host:
        raise RuntimeError(
            "notifications.email_server.host is required"
        )

    sender = str(
        cfg.get("sender", "") or ""
    ).strip()

    if not sender:
        raise RuntimeError(
            "notifications.email_server.sender is required"
        )

    return {
        "startTlsUpgradeEnabled": bool(
            cfg.get("starttls_upgrade_enabled", False)
        ),
        "authenticationRequired": authentication_required,
        "host": host,
        "trustHost": bool(
            cfg.get("trust_host", False)
        ),
        "name": str(
            cfg.get("description", "") or ""
        ),
        "port": int(
            cfg.get("port", 25)
        ),
        "sender": sender,
        "senderName": str(
            cfg.get("sender_name", "") or ""
        ),
        "userName": username,
        "password": password,
        "connectionSecurity": normalize_email_connection_security(
            cfg.get("connection_security", "none")
        ),
    }


def notification_email_config(
    client: RestClient,
) -> Optional[Dict[str, Any]]:
    try:
        result = client.get(
            "/notification/api/email-config",
            headers={"Accept": "application/json"},
        )
    except RuntimeError as exc:
        if "HTTP 404" in str(exc):
            return None
        raise

    return result if isinstance(result, dict) else None


def notification_email_comparable(
    value: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}

    return {
        "startTlsUpgradeEnabled": bool(
            value.get("startTlsUpgradeEnabled", False)
        ),
        "authenticationRequired": bool(
            value.get("authenticationRequired", False)
        ),
        "host": str(value.get("host", "") or ""),
        "trustHost": bool(value.get("trustHost", False)),
        "name": str(value.get("name", "") or ""),
        "port": int(value.get("port", 25) or 25),
        "sender": str(value.get("sender", "") or ""),
        "senderName": str(value.get("senderName", "") or ""),
        "userName": str(value.get("userName", "") or ""),
        "connectionSecurity": str(
            value.get("connectionSecurity", "NONE") or "NONE"
        ).upper(),
    }


def desired_notification_email_comparable(
    desired: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        key: desired[key]
        for key in (
            "startTlsUpgradeEnabled",
            "authenticationRequired",
            "host",
            "trustHost",
            "name",
            "port",
            "sender",
            "senderName",
            "userName",
            "connectionSecurity",
        )
    }


def configure_organization_email_server(
    client: RestClient,
    org_name: str,
    cfg: Dict[str, Any],
    config_dir: Path,
) -> None:
    desired = desired_notification_email_config(
        cfg,
        config_dir,
    )

    current = notification_email_config(client)
    current_compare = notification_email_comparable(current)
    desired_compare = desired_notification_email_comparable(desired)

    test_requested = bool(
        cfg.get("test", False)
    )

    if current_compare == desired_compare:
        skip(
            f"Email server for organization '{org_name}' "
            "is already in desired state"
        )

        if test_requested:
            info(
                f"Testing email server for organization '{org_name}'"
            )

            test_response = client.post(
                "/notification/api/email-config/test",
                json=desired,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "X-Requested-With": "XMLHttpRequest",
                },
            )

            if not isinstance(test_response, dict):
                raise RuntimeError(
                    f"Email server test for '{org_name}' did not return JSON"
                )

            status = str(
                test_response.get("status", "") or ""
            ).lower()

            message = str(
                test_response.get("message", "") or ""
            )

            if status != "success":
                raise RuntimeError(
                    f"Email server test failed for organization "
                    f"'{org_name}': {message or test_response}"
                )

            print(
                f"[PASS]   Email server test succeeded for "
                f"organization '{org_name}': {message}"
            )

        return

    differences = [
        key
        for key, desired_value in desired_compare.items()
        if current_compare.get(key) != desired_value
    ]

    info(
        f"Email server differences for '{org_name}': "
        + ", ".join(differences)
    )

    if current:
        update(f"Email server for organization '{org_name}'")
    else:
        create(f"Email server for organization '{org_name}'")

    response = client.post(
        "/notification/api/email-config",
        json=desired,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
        },
    )

    if not isinstance(response, dict):
        raise RuntimeError(
            f"Email server update for '{org_name}' did not return JSON"
        )

    actual = notification_email_comparable(response)

    mismatches = [
        key
        for key, desired_value in desired_compare.items()
        if actual.get(key) != desired_value
    ]

    if mismatches:
        warn(
            f"Email server for '{org_name}' was accepted but VCFA returned "
            f"different value(s): {', '.join(mismatches)}"
        )

        for key in mismatches:
            warn(
                f"Email server '{org_name}' {key}: "
                f"requested={desired_compare.get(key)!r}, "
                f"returned={actual.get(key)!r}"
            )
    else:
        print(
            f"[UPDATED] Email server for organization '{org_name}'"
        )

    if test_requested:
        info(
            f"Testing email server for organization '{org_name}'"
        )

        test_response = client.post(
            "/notification/api/email-config/test",
            json=desired,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            },
        )

        if not isinstance(test_response, dict):
            raise RuntimeError(
                f"Email server test for '{org_name}' did not return JSON"
            )

        status = str(
            test_response.get("status", "") or ""
        ).lower()

        message = str(
            test_response.get("message", "") or ""
        )

        if status != "success":
            raise RuntimeError(
                f"Email server test failed for organization "
                f"'{org_name}': {message or test_response}"
            )

        print(
            f"[PASS]   Email server test succeeded for "
            f"organization '{org_name}': {message}"
        )


# ============================================================
# Organization access control
# ============================================================

def organization_current(client: RestClient, org_name: str) -> Dict[str, Any]:
    orgs = client.paged("/cloudapi/1.0.0/orgs")

    for org in orgs:
        if str(org.get("name", "") or "") == org_name:
            return org

    raise RuntimeError(
        f"Organization '{org_name}' was not returned by the tenant API"
    )


def organization_roles(client: RestClient) -> List[Dict[str, Any]]:
    return client.paged("/cloudapi/1.0.0/roles")


def organization_role_by_name(
    client: RestClient,
    role_name: str,
) -> Dict[str, Any]:
    """Resolve an organization access-control role by exact name.

    Role aliases are deliberately not used here. The JSON must contain the
    actual role name exposed by the organization role catalogue. This keeps
    VM Apps/classic and All Apps/non-classic role models separate.
    """
    roles = organization_roles(client)
    requested = str(role_name or "").strip()

    for role in roles:
        if str(role.get("name", "") or "").strip() == requested:
            return role

    available = sorted(
        str(r.get("name", "") or "")
        for r in roles
        if r.get("name")
    )

    raise RuntimeError(
        f"Role '{role_name}' was not found. "
        f"Available roles: {', '.join(available)}"
    )


def organization_groups(client: RestClient) -> List[Dict[str, Any]]:
    return client.paged("/cloudapi/1.0.0/groups")


def organization_group_by_name(
    client: RestClient,
    group_name: str,
) -> Optional[Dict[str, Any]]:
    for group in organization_groups(client):
        candidate_names = {
            str(group.get("name", "") or ""),
            str(group.get("nameInSource", "") or ""),
        }

        if group_name in candidate_names:
            return group

    return None


def group_role_ids(group: Dict[str, Any]) -> List[str]:
    refs = group.get("roleEntityRefs")

    if isinstance(refs, list):
        return sorted(
            str(ref.get("id", "") or "")
            for ref in refs
            if isinstance(ref, dict) and ref.get("id")
        )

    ref = group.get("roleEntityRef")

    if isinstance(ref, dict) and ref.get("id"):
        return [str(ref["id"])]

    return []


def search_ldap_group(
    client: RestClient,
    group_name: str,
) -> Optional[Dict[str, Any]]:
    """
    Search the organization's configured LDAP identity provider for a group.

    VCFA endpoint:
        GET /cloudapi/1.0.0/ldap/search/group?q=<group_name>

    Returns the exact matching LDAP group when found, otherwise None.
    """
    encoded_query = urllib.parse.quote(
        group_name,
        safe="",
    )

    result = client.get(
        f"/cloudapi/1.0.0/ldap/search/group?q={encoded_query}"
    )

    if isinstance(result, dict):
        values = result.get("values") or result.get("items") or []
    elif isinstance(result, list):
        values = result
    else:
        values = []

    # Prefer exact source/name match.
    for item in values:
        if not isinstance(item, dict):
            continue

        candidates = {
            str(item.get("name", "") or ""),
            str(item.get("nameInSource", "") or ""),
        }

        if group_name in candidates:
            return item

    return None


def desired_group_payload(
    org: Dict[str, Any],
    group_name: str,
    role: Dict[str, Any],
    ldap_group: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Build a new-group import payload.

    VCFA must resolve nameInSource itself for a new imported LDAP group;
    explicitly sending nameInSource causes VCD_50131.
    """
    org_id = object_id(org)
    role_id = object_id(role)

    if not org_id:
        raise RuntimeError(
            f"Unable to determine organization ID for '{org.get('name','')}'"
        )

    if not role_id:
        raise RuntimeError(
            f"Unable to determine ID for role '{role.get('name','')}'"
        )

    body = {
        "name": group_name,
        "description": "",
        "orgEntityRef": {
            "name": str(org.get("name", "") or ""),
            "id": org_id,
        },
        "roleEntityRefs": [
            {
                "name": str(role.get("name", "") or ""),
                "id": role_id,
            }
        ],
        "providerType": "LDAP",
    }

    # Preserve a source reference returned by LDAP search when VCFA provides
    # one, but never send nameInSource on create.
    if isinstance(ldap_group, dict):
        source_ref = ldap_group.get("sourceEntityRef")
        if isinstance(source_ref, dict) and source_ref:
            body["sourceEntityRef"] = source_ref

    return body


def access_control_group_needs_update(
    current: Dict[str, Any],
    desired: Dict[str, Any],
) -> List[str]:
    """
    Compare only the access-control state managed by this script: role assignment.

    LDAP identity-source properties such as nameInSource, providerType and
    sourceEntityRef are owned by VCFA/LDAP and must not trigger GROUP_EDIT.
    """
    differences: List[str] = []

    desired_role_ids = sorted(
        str(ref.get("id", "") or "")
        for ref in desired.get("roleEntityRefs", [])
        if isinstance(ref, dict) and ref.get("id")
    )

    if group_role_ids(current) != desired_role_ids:
        differences.append("roleEntityRefs")

    return differences


def configure_access_control_group(
    read_client: RestClient,
    write_client: RestClient,
    org: Dict[str, Any],
    group_name: str,
    role: Dict[str, Any],
) -> Dict[str, Any]:
    existing = organization_group_by_name(
        read_client,
        group_name,
    )

    role_name = str(role.get("name", "") or "")

    if not existing:
        ldap_group = search_ldap_group(
            read_client,
            group_name,
        )

        if not ldap_group:
            warn(
                f"LDAP group '{group_name}' was not found in the "
                "configured identity provider; skipping access-control import"
            )
            return {}

        info(
            f"LDAP group '{group_name}' found in identity provider"
        )

        desired = desired_group_payload(
            org,
            group_name,
            role,
            ldap_group=ldap_group,
        )
        create(
            f"LDAP group '{group_name}' with role '{role_name}'"
        )

        created = write_client.post(
            "/cloudapi/1.0.0/groups",
            json=desired,
        )

        if isinstance(created, dict):
            return created

        found = organization_group_by_name(
            read_client,
            group_name,
        )

        if not found:
            raise RuntimeError(
                f"LDAP group '{group_name}' was created but "
                "could not be queried afterwards"
            )

        return found

    desired = desired_group_payload(
        org,
        group_name,
        role,
    )

    differences = access_control_group_needs_update(
        existing,
        desired,
    )

    if not differences:
        skip(
            f"LDAP group '{group_name}' already has role '{role_name}'"
        )
        return existing

    group_id = object_id(existing)

    if not group_id:
        raise RuntimeError(
            f"Unable to determine ID for LDAP group '{group_name}'"
        )

    info(
        f"Access control differences for group '{group_name}': "
        + ", ".join(differences)
    )

    update_body = {
        "name": str(existing.get("name") or group_name),
        "description": str(existing.get("description", "") or ""),
        "orgEntityRef": (
            existing.get("orgEntityRef")
            if isinstance(existing.get("orgEntityRef"), dict)
            else desired["orgEntityRef"]
        ),
        "roleEntityRefs": desired["roleEntityRefs"],
        "providerType": str(
            existing.get("providerType", "LDAP") or "LDAP"
        ),
    }

    if existing.get("nameInSource") is not None:
        update_body["nameInSource"] = existing.get("nameInSource")

    source_ref = existing.get("sourceEntityRef")
    if isinstance(source_ref, dict) and source_ref:
        update_body["sourceEntityRef"] = source_ref

    update(
        f"LDAP group '{group_name}' -> role '{role_name}'"
    )

    encoded_group_id = urllib.parse.quote(
        group_id,
        safe="",
    )

    updated = write_client.put(
        f"/cloudapi/1.0.0/groups/{encoded_group_id}",
        json=update_body,
    )

    return updated if isinstance(updated, dict) else existing


def organization_users(client: RestClient) -> List[Dict[str, Any]]:
    return paged_values(
        client,
        "/cloudapi/1.0.0/users",
    )


def organization_user_by_name(
    client: RestClient,
    username: str,
) -> Optional[Dict[str, Any]]:
    for user in organization_users(client):
        candidates = {
            str(user.get("username", "") or ""),
            str(user.get("nameInSource", "") or ""),
        }

        if username in candidates:
            return user

    return None


def search_ldap_user(
    client: RestClient,
    username: str,
) -> Optional[Dict[str, Any]]:
    """
    Search the configured organization LDAP source for an exact user match.

    VCFA endpoint:
        GET /cloudapi/1.0.0/ldap/search/user?q=<username>
    """
    encoded_query = urllib.parse.quote(
        username,
        safe="",
    )

    result = client.get(
        f"/cloudapi/1.0.0/ldap/search/user?q={encoded_query}"
    )

    if isinstance(result, dict):
        values = result.get("values") or result.get("items") or []
    elif isinstance(result, list):
        values = result
    else:
        values = []

    for item in values:
        if not isinstance(item, dict):
            continue

        candidates = {
            str(item.get("username", "") or ""),
            str(item.get("nameInSource", "") or ""),
        }

        if username in candidates:
            return item

    return None


def user_role_ids(user: Dict[str, Any]) -> List[str]:
    refs = user.get("roleEntityRefs")

    if not isinstance(refs, list):
        return []

    return sorted(
        str(ref.get("id", "") or "")
        for ref in refs
        if isinstance(ref, dict) and ref.get("id")
    )


def user_inherits_group_roles(user: Dict[str, Any]) -> bool:
    """
    Support both the VCFA 9.1 field and older OpenAPI field naming.
    """
    if "inheritGroupRoles" in user:
        return bool(user.get("inheritGroupRoles"))

    if "isGroupRole" in user:
        return bool(user.get("isGroupRole"))

    if "roleInherited" in user:
        return bool(user.get("roleInherited"))

    return False


def access_control_user_roles(
    client: RestClient,
    org_name: str,
    username: str,
    cfg: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    Resolve roles requested for a user.

    Preferred JSON:
        "roles": ["Organization User"]

    For convenience, a single "role": "Organization User" is also accepted.
    """
    raw_roles = cfg.get("roles")

    if raw_roles is None and "role" in cfg:
        raw_roles = cfg.get("role")

    if raw_roles is None:
        raw_roles = []

    if isinstance(raw_roles, str):
        raw_roles = [raw_roles]

    if not isinstance(raw_roles, list):
        raise RuntimeError(
            f"access_control.users['{username}'].roles for "
            f"'{org_name}' must be an array or string"
        )

    roles: List[Dict[str, Any]] = []
    seen = set()

    for raw_role in raw_roles:
        role_name = str(raw_role or "").strip()

        if not role_name or role_name in seen:
            continue

        seen.add(role_name)

        roles.append(
            organization_role_by_name(
                client,
                role_name,
            )
        )

    return roles


def desired_user_role_refs(
    roles: List[Dict[str, Any]],
) -> List[Dict[str, str]]:
    refs: List[Dict[str, str]] = []

    for role in roles:
        role_id = object_id(role)
        role_name = str(role.get("name", "") or "")

        if not role_id:
            raise RuntimeError(
                f"Unable to determine ID for role '{role_name}'"
            )

        refs.append(
            {
                "name": role_name,
                "id": role_id,
            }
        )

    return refs


def create_access_control_user_payload(
    org: Dict[str, Any],
    username: str,
    user_cfg: Dict[str, Any],
    roles: List[Dict[str, Any]],
    inherit_group_roles: bool,
) -> Dict[str, Any]:
    org_id = object_id(org)

    if not org_id:
        raise RuntimeError(
            f"Unable to determine organization ID for "
            f"'{org.get('name', '<unknown>')}'"
        )

    body: Dict[str, Any] = {
        "username": username,
        "orgEntityRef": {
            "name": str(org.get("name", "") or ""),
            "id": org_id,
        },
        "providerType": "LDAP",
        "enabled": bool(user_cfg.get("enabled", True)),
        "inheritGroupRoles": inherit_group_roles,
    }

    # VCFA documents roleEntityRefs as read-only inherited roles when
    # inheritGroupRoles=true, so never send direct roles in that mode.
    if not inherit_group_roles:
        body["roleEntityRefs"] = desired_user_role_refs(roles)

    return body


def existing_user_update_payload(
    existing: Dict[str, Any],
    org: Dict[str, Any],
    username: str,
    user_cfg: Dict[str, Any],
    roles: List[Dict[str, Any]],
    inherit_group_roles: bool,
) -> Dict[str, Any]:
    """
    Preserve server-owned user metadata while changing only access-control
    fields managed by the JSON.
    """
    org_ref = existing.get("orgEntityRef")

    if not isinstance(org_ref, dict):
        org_ref = {
            "name": str(org.get("name", "") or ""),
            "id": object_id(org),
        }

    body: Dict[str, Any] = {
        "username": str(existing.get("username") or username),
        "orgEntityRef": org_ref,
        "providerType": str(
            existing.get("providerType", "LDAP") or "LDAP"
        ),
        "enabled": bool(
            user_cfg.get(
                "enabled",
                existing.get("enabled", True),
            )
        ),
        "inheritGroupRoles": inherit_group_roles,
    }

    for source, target in (
        ("fullName", "fullName"),
        ("description", "description"),
        ("email", "email"),
        ("phone", "phone"),
        ("domain", "domain"),
    ):
        if source in existing and existing.get(source) is not None:
            body[target] = existing.get(source)

    # nameInSource is server/IdP-owned. Preserve it only on UPDATE.
    if existing.get("nameInSource") is not None:
        body["nameInSource"] = existing.get("nameInSource")

    if not inherit_group_roles:
        body["roleEntityRefs"] = desired_user_role_refs(roles)

    return body


def configure_access_control_user(
    read_client: RestClient,
    write_client: RestClient,
    org: Dict[str, Any],
    org_name: str,
    username: str,
    user_cfg: Dict[str, Any],
) -> Dict[str, Any]:
    inherit_group_roles = bool(
        user_cfg.get("inherit_role_from_group", False)
    )

    roles = access_control_user_roles(
        read_client,
        org_name,
        username,
        user_cfg,
    )

    if inherit_group_roles and roles:
        info(
            f"User '{username}' has inherit_role_from_group=true; "
            "configured direct role(s) will be ignored"
        )

    if not inherit_group_roles and not roles:
        raise RuntimeError(
            f"access_control user '{username}' in organization "
            f"'{org_name}' requires at least one role when "
            "inherit_role_from_group=false"
        )

    existing = organization_user_by_name(
        read_client,
        username,
    )

    if not existing:
        ldap_user = search_ldap_user(
            read_client,
            username,
        )

        if not ldap_user:
            warn(
                f"LDAP user '{username}' was not found in the configured "
                "identity provider; skipping access-control import"
            )
            return {}

        info(
            f"LDAP user '{username}' found in identity provider"
        )

        body = create_access_control_user_payload(
            org,
            username,
            user_cfg,
            roles,
            inherit_group_roles,
        )

        if inherit_group_roles:
            create(
                f"LDAP user '{username}' with roles inherited from group(s)"
            )
        else:
            role_names = ", ".join(
                str(role.get("name", "") or "")
                for role in roles
            )
            create(
                f"LDAP user '{username}' with role(s): {role_names}"
            )

        created = write_client.post(
            "/cloudapi/1.0.0/users",
            json=body,
        )

        if isinstance(created, dict):
            return created

        found = organization_user_by_name(
            read_client,
            username,
        )

        if not found:
            raise RuntimeError(
                f"LDAP user '{username}' was imported but could not "
                "be queried afterwards"
            )

        return found

    differences: List[str] = []

    current_inherit = user_inherits_group_roles(
        existing
    )

    if current_inherit != inherit_group_roles:
        differences.append("inheritGroupRoles")

    desired_enabled = bool(
        user_cfg.get(
            "enabled",
            existing.get("enabled", True),
        )
    )

    if bool(existing.get("enabled", True)) != desired_enabled:
        differences.append("enabled")

    # When inheritance is enabled, the effective roles are controlled by
    # group membership and roleEntityRefs are read-only inherited values.
    if not inherit_group_roles:
        desired_ids = sorted(
            ref["id"]
            for ref in desired_user_role_refs(roles)
        )

        if user_role_ids(existing) != desired_ids:
            differences.append("roleEntityRefs")

    if not differences:
        if inherit_group_roles:
            skip(
                f"LDAP user '{username}' already inherits role(s) "
                "from group membership"
            )
        else:
            role_names = ", ".join(
                str(role.get("name", "") or "")
                for role in roles
            )
            skip(
                f"LDAP user '{username}' already has role(s): {role_names}"
            )
        return existing

    user_id = object_id(existing)

    if not user_id:
        raise RuntimeError(
            f"Unable to determine ID for LDAP user '{username}'"
        )

    info(
        f"Access control differences for user '{username}': "
        + ", ".join(differences)
    )

    body = existing_user_update_payload(
        existing,
        org,
        username,
        user_cfg,
        roles,
        inherit_group_roles,
    )

    encoded_user_id = urllib.parse.quote(
        user_id,
        safe="",
    )

    if inherit_group_roles:
        update(
            f"LDAP user '{username}' -> inherit roles from group(s)"
        )
    else:
        role_names = ", ".join(
            str(role.get("name", "") or "")
            for role in roles
        )
        update(
            f"LDAP user '{username}' -> role(s): {role_names}"
        )

    updated = write_client.put(
        f"/cloudapi/1.0.0/users/{encoded_user_id}",
        json=body,
    )

    return updated if isinstance(updated, dict) else existing


def configure_organization_access_control(
    read_client: RestClient,
    provider_client: RestClient,
    provider_cfg: Dict[str, Any],
    vcfa_server: str,
    config_dir: Path,
    verify: bool,
    org_name: str,
    access_cfg: Dict[str, Any],
) -> None:
    if not access_cfg:
        return

    groups_cfg = access_cfg.get("groups") or {}

    if not isinstance(groups_cfg, dict):
        raise RuntimeError(
            f"access_control.groups for organization '{org_name}' "
            "must be an object keyed by VCFA role name"
        )

    org = organization_current(
        read_client,
        org_name,
    )

    # Use a full VCFA LOCAL provider session for privileged GROUP_IMPORT /
    # GROUP_EDIT operations when vcfa.provider.local_auth is configured.
    privileged_provider_client = first_user_write_client(
        provider_client,
        vcfa_server,
        provider_cfg,
        config_dir,
        verify,
    )

    write_client = scope_provider_client_to_organization(
        privileged_provider_client,
        org,
    )

    # Build the complete desired group -> role map from JSON.
    #
    # Expected JSON:
    #
    # "groups": {
    #   "Organization Administrator": [
    #     "SciTech-admins"
    #   ],
    #   "Organization User": [
    #     "SciTech-users",
    #     "SciTech-developers"
    #   ],
    #   "Organization Auditor": []
    # }
    desired_assignments: Dict[str, str] = {}

    # Cache role objects while validating every role named in JSON.
    role_cache: Dict[str, Dict[str, Any]] = {}

    for role_name, assignments in groups_cfg.items():
        if not isinstance(assignments, list):
            raise RuntimeError(
                f"access_control.groups['{role_name}'] for "
                f"'{org_name}' must be an array"
            )

        role_cache[role_name] = organization_role_by_name(
            read_client,
            role_name,
        )

        for raw_group_name in assignments:
            group_name = str(raw_group_name or "").strip()

            if not group_name:
                continue

            if (
                group_name in desired_assignments
                and desired_assignments[group_name] != role_name
            ):
                raise RuntimeError(
                    f"LDAP group '{group_name}' is assigned to both "
                    f"'{desired_assignments[group_name]}' and "
                    f"'{role_name}' in organization '{org_name}'"
                )

            desired_assignments[group_name] = role_name

    # Ensure every desired group exists and has exactly the configured role.
    for group_name, role_name in desired_assignments.items():
        configure_access_control_group(
            read_client,
            write_client,
            org,
            group_name,
            role_cache[role_name],
        )

    # Do not remove role assignments from groups that are not explicitly
    # named in the desired JSON. VCFA can return internal/imported group
    # records whose name/nameInSource is an opaque UUID; those must not be
    # treated as stale access-control entries.
    #
    # This reconciler is therefore authoritative only for groups explicitly
    # listed in access_control.groups. It creates missing listed groups and
    # fixes their role assignment, but leaves all other groups untouched.

    users_cfg = access_cfg.get("users") or {}

    if users_cfg:
        if not isinstance(users_cfg, dict):
            raise RuntimeError(
                f"access_control.users for organization '{org_name}' "
                "must be an object keyed by username"
            )

        for username, user_cfg in users_cfg.items():
            username = str(username or "").strip()

            if not username:
                raise RuntimeError(
                    f"access_control.users for '{org_name}' contains "
                    "an empty username"
                )

            if not isinstance(user_cfg, dict):
                raise RuntimeError(
                    f"access_control.users['{username}'] for "
                    f"'{org_name}' must be an object"
                )

            configure_access_control_user(
                read_client,
                write_client,
                org,
                org_name,
                username,
                user_cfg,
            )


def validate_notification_email_json(
    org_name: str,
    cfg: Dict[str, Any],
) -> None:
    if not cfg:
        return

    deprecated_keys = {
        "server_name": "host",
        "sender_address": "sender",
        "server_port": "port",
        "enable_insecure_starttls": "starttls_upgrade_enabled",
        "trust_certs": "trust_host",
        "authentication": "auth_required/username/password_file",
    }

    for old_key, new_key in deprecated_keys.items():
        if old_key in cfg:
            raise RuntimeError(
                f"Organization '{org_name}' email_server uses old key "
                f"'{old_key}'. Use '{new_key}' with the new JSON structure."
            )


def provider_first_user_config(
    provider_cfg: Dict[str, Any],
    org_name: str,
) -> Dict[str, Any]:
    provider_orgs = provider_cfg.get("organizations") or {}

    if not isinstance(provider_orgs, dict):
        raise RuntimeError(
            "vcfa.provider.organizations must be an object keyed by "
            "organization name"
        )

    provider_org_cfg = provider_orgs.get(org_name)

    if not isinstance(provider_org_cfg, dict):
        raise RuntimeError(
            f"vcfa.provider.organizations['{org_name}'] is required"
        )

    first_user_cfg = provider_org_cfg.get("first_user") or {}

    if not isinstance(first_user_cfg, dict):
        raise RuntimeError(
            f"vcfa.provider.organizations['{org_name}'].first_user "
            "must be an object"
        )

    return first_user_cfg


def provider_first_user_token_file(
    provider_cfg: Dict[str, Any],
    org_name: str,
    config_dir: Path,
) -> Path:
    first_user_cfg = provider_first_user_config(
        provider_cfg,
        org_name,
    )

    token_value = str(
        first_user_cfg.get("api_token_file", "") or ""
    ).strip()

    if not token_value:
        raise RuntimeError(
            f"vcfa.provider.organizations['{org_name}']."
            "first_user.api_token_file is required. A local first-user "
            "API token is required for every tenant."
        )

    return resolve_path(
        token_value,
        config_dir,
    )


def bootstrap_first_user_client(
    server: str,
    org_name: str,
    provider_cfg: Dict[str, Any],
    config_dir: Path,
    verify: bool,
) -> RestClient:
    first_user_cfg = provider_first_user_config(
        provider_cfg,
        org_name,
    )

    username = str(
        first_user_cfg.get("username", "") or ""
    ).strip()

    if not username:
        raise RuntimeError(
            f"vcfa.provider.organizations['{org_name}']."
            "first_user.username is required"
        )

    token_file = provider_first_user_token_file(
        provider_cfg,
        org_name,
        config_dir,
    )

    if not token_file.is_file() or not token_file.read_text(
        encoding="utf-8"
    ).strip():
        raise RuntimeError(
            f"Bootstrap API token for local first user "
            f"'{username}@{org_name}' is missing or empty: {token_file}"
        )

    info(
        f"Using local first user '{username}@{org_name}' "
        "for tenant bootstrap configuration"
    )

    return vcfa_client(
        server,
        token_file,
        verify,
        organization=org_name,
    )


def organization_admin_config(
    org_name: str,
    org_cfg: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Return normalized vcfa.organizations.<org>.org_admin configuration.

    Preferred form:
      "org_admin": {
        "username": "scitech.admin",
        "api_token_file": "/path/token.txt",
        "create_token": true,
        "replace_token": true
      }

    The older string form remains accepted for backward compatibility and
    takes api_token_file from the organization object.
    """
    raw = org_cfg.get("org_admin")

    if isinstance(raw, dict):
        cfg = dict(raw)

        username = str(
            cfg.get("username", "") or ""
        ).strip()

        if not username:
            raise RuntimeError(
                f"vcfa.organizations['{org_name}'].org_admin.username "
                "is required"
            )

        return cfg

    if isinstance(raw, str) and raw.strip():
        return {
            "username": raw.strip(),
            "api_token_file": str(
                org_cfg.get("api_token_file", "") or ""
            ).strip(),
            "create_token": False,
            "replace_token": False,
        }

    raise RuntimeError(
        f"vcfa.organizations['{org_name}'].org_admin must be an object "
        "containing username/api_token_file or a non-empty username string"
    )


def resolve_organization_admin_token_file(
    org_name: str,
    org_cfg: Dict[str, Any],
    config_dir: Path,
) -> Path:
    admin_cfg = organization_admin_config(
        org_name,
        org_cfg,
    )

    username = str(
        admin_cfg.get("username", "") or ""
    ).strip()

    token_value = str(
        admin_cfg.get("api_token_file", "") or ""
    ).strip()

    if not token_value:
        raise RuntimeError(
            f"vcfa.organizations['{org_name}'].org_admin.api_token_file "
            f"is required for org admin '{username}'"
        )

    return resolve_path(
        token_value,
        config_dir,
    )


def vcfa_tenant_password_session(
    server: str,
    org_name: str,
    username: str,
    password: str,
    verify: bool,
) -> tuple[RestClient, str]:
    """
    Authenticate a tenant user (LOCAL or LDAP) using the CloudAPI sessions
    endpoint and return both a RestClient and the session access token.

    Username format used by VCFA:
        <username>@<organization>

    Endpoint:
        POST /cloudapi/1.0.0/sessions
    """
    login_name = f"{username}@{org_name}"

    auth = base64.b64encode(
        f"{login_name}:{password}".encode("utf-8")
    ).decode("ascii")

    session = requests.Session()
    session.verify = verify

    response = session.post(
        f"https://{server}/cloudapi/1.0.0/sessions",
        headers={
            "Authorization": f"Basic {auth}",
            "Accept": "application/json;version=9.1.0",
        },
        timeout=120,
    )

    if not response.ok:
        try:
            detail = response.json()
            formatted = json.dumps(
                detail,
                indent=2,
            )
        except Exception:
            formatted = response.text

        raise RuntimeError(
            f"Failed to authenticate tenant user '{login_name}': "
            f"HTTP {response.status_code}\n{formatted}"
        )

    access_token = (
        response.headers.get(
            "X-VMWARE-VCLOUD-ACCESS-TOKEN"
        )
        or response.headers.get(
            "x-vmware-vcloud-access-token"
        )
        or ""
    ).strip()

    if not access_token:
        # Some builds may return a JSON token body instead.
        try:
            body = response.json()
        except Exception:
            body = {}

        if isinstance(body, dict):
            access_token = str(
                body.get("access_token")
                or body.get("token")
                or ""
            ).strip()

    if not access_token:
        raise RuntimeError(
            f"Tenant login for '{login_name}' succeeded but no "
            "X-VMWARE-VCLOUD-ACCESS-TOKEN was returned"
        )

    client = RestClient(
        server,
        verify=verify,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json;version=10.0.0.0-alpha",
        },
    )

    return client, access_token


def org_admin_token_name(
    token_file: Path,
    admin_cfg: Dict[str, Any],
) -> str:
    configured = str(
        admin_cfg.get("token_name", "") or ""
    ).strip()

    if configured:
        return configured

    return token_file.stem


def create_or_replace_org_admin_api_token(
    bootstrap_client: RestClient,
    server: str,
    org_name: str,
    org_cfg: Dict[str, Any],
    config_dir: Path,
    verify: bool,
) -> str:
    """
    Review/import the org_admin using the LOCAL first-user client, then
    authenticate directly as org_admin using username/password via
    /cloudapi/1.0.0/sessions.

    Once authenticated as org_admin, create or replace that user's own
    VCFA API token using:
        POST /oauth/tenant/<org>/register
        POST /oauth/tenant/<org>/token

    This allows create_token / replace_token to work for LDAP users because
    the token-management call is made from a password-authenticated session,
    not from an API-token-authenticated session.
    """
    admin_cfg = organization_admin_config(
        org_name,
        org_cfg,
    )

    username = str(
        admin_cfg.get("username", "") or ""
    ).strip()

    create_token = bool(
        admin_cfg.get("create_token", False)
    )

    replace_token = bool(
        admin_cfg.get("replace_token", False)
    )

    token_file = resolve_organization_admin_token_file(
        org_name,
        org_cfg,
        config_dir,
    )

    token_name = org_admin_token_name(
        token_file,
        admin_cfg,
    )

    # Confirm the external user has actually been imported after LDAP/access
    # control configuration.
    imported_user = organization_user_by_name(
        bootstrap_client,
        username,
    )

    if not imported_user:
        raise RuntimeError(
            f"Configured org_admin '{username}' was not found in "
            f"organization '{org_name}' after identity-provider/access-control "
            "configuration."
        )

    info(
        f"org_admin '{username}' exists in organization '{org_name}'"
    )

    existing_refresh_token = ""

    if token_file.is_file():
        existing_refresh_token = token_file.read_text(
            encoding="utf-8"
        ).strip()

    if replace_token and not create_token:
        warn(
            f"Organization '{org_name}' org_admin has "
            "replace_token=true but create_token=false; "
            "replace_token will be ignored"
        )

    if not create_token:
        if not existing_refresh_token:
            raise RuntimeError(
                f"Organization admin API token for "
                f"'{username}@{org_name}' is missing or empty: {token_file}"
            )

        info(
            f"Using existing API token '{token_name}' for "
            f"org_admin '{username}@{org_name}'"
        )

        # Validate it before switching.
        vcfa_access_token(
            server,
            existing_refresh_token,
            verify,
            organization=org_name,
        )

        return existing_refresh_token

    if existing_refresh_token and not replace_token:
        skip(
            f"API token '{token_name}' already exists for org_admin "
            f"'{username}@{org_name}' and replace_token=false"
        )

        vcfa_access_token(
            server,
            existing_refresh_token,
            verify,
            organization=org_name,
        )

        return existing_refresh_token

    password_file_value = str(
        admin_cfg.get("password_file", "") or ""
    ).strip()

    if not password_file_value:
        raise RuntimeError(
            f"vcfa.organizations['{org_name}'].org_admin.password_file "
            f"is required when create_token=true for '{username}'"
        )

    password = read_text_file(
        resolve_path(
            password_file_value,
            config_dir,
        )
    )

    info(
        f"Authenticating as org_admin '{username}@{org_name}' "
        "using tenant session API"
    )

    admin_session_client, session_access_token = vcfa_tenant_password_session(
        server,
        org_name,
        username,
        password,
        verify,
    )

    # If replace_token=true, revoke any existing registrations with the same
    # token name while authenticated as the target org_admin.
    if replace_token:
        try:
            existing_tokens = vcfa_api_tokens_by_name(
                admin_session_client,
                token_name,
                username=username,
            )
        except RuntimeError as exc:
            # Some builds may not support username filtering for self-owned
            # tokens. Retry without username.
            warn(
                f"Could not query org_admin API tokens with username filter: "
                f"{exc}"
            )
            existing_tokens = vcfa_api_tokens_by_name(
                admin_session_client,
                token_name,
            )

        for existing_token in existing_tokens:
            token_id = object_id(
                existing_token
            ) or "<unknown>"

            update(
                f"Revoke API token '{token_name}' for "
                f"org_admin '{username}@{org_name}' [{token_id}]"
            )

            delete_vcfa_api_token(
                admin_session_client,
                existing_token,
            )

        if existing_tokens:
            time.sleep(1)

    encoded_org = urllib.parse.quote(
        org_name,
        safe="",
    )

    action = "UPDATE" if replace_token else "CREATE"
    print(
        f"[{action}] API token '{token_name}' for "
        f"org admin '{username}@{org_name}'"
    )

    registered = admin_session_client.post(
        f"/oauth/tenant/{encoded_org}/register",
        json={
            "client_name": token_name,
        },
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )

    if not isinstance(registered, dict):
        raise RuntimeError(
            f"VCFA token registration for org_admin "
            f"'{username}@{org_name}' did not return JSON"
        )

    client_id = str(
        registered.get("client_id", "") or ""
    ).strip()

    if not client_id:
        raise RuntimeError(
            f"VCFA token registration for org_admin "
            f"'{username}@{org_name}' did not return client_id"
        )

    token_response = admin_session_client.post(
        f"/oauth/tenant/{encoded_org}/token",
        data={
            "grant_type":
                "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": session_access_token,
            "client_id": client_id,
        },
        headers={
            "Accept": "application/json;version=9.1.0",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )

    if not isinstance(token_response, dict):
        raise RuntimeError(
            f"API token request for org_admin "
            f"'{username}@{org_name}' did not return JSON"
        )

    refresh_token = str(
        token_response.get("refresh_token", "") or ""
    ).strip()

    if not refresh_token:
        raise RuntimeError(
            f"API token request for org_admin "
            f"'{username}@{org_name}' did not return refresh_token"
        )

    write_secret_file(
        token_file,
        refresh_token,
    )

    print(
        f"[SAVED]  API token for org_admin "
        f"'{username}@{org_name}' -> '{token_file}'"
    )

    return refresh_token


def organization_admin_client(
    bootstrap_client: RestClient,
    server: str,
    org_name: str,
    org_cfg: Dict[str, Any],
    config_dir: Path,
    verify: bool,
) -> RestClient:
    admin_cfg = organization_admin_config(
        org_name,
        org_cfg,
    )

    username = str(
        admin_cfg.get("username", "") or ""
    ).strip()

    create_or_replace_org_admin_api_token(
        bootstrap_client,
        server,
        org_name,
        org_cfg,
        config_dir,
        verify,
    )

    token_file = resolve_organization_admin_token_file(
        org_name,
        org_cfg,
        config_dir,
    )

    info(
        f"Switching organization '{org_name}' to org admin "
        f"'{username}' using '{token_file}'"
    )

    return vcfa_client(
        server,
        token_file,
        verify,
        organization=org_name,
    )


def validate_vcfa_configuration(
    config: Dict[str, Any],
    config_dir: Path,
) -> None:
    """
    Validate the keyed configuration format used by the current lab JSON.

    Supported keyed objects:
      vcfa.provider.regions.<region>
      vcfa.provider.provider_content_libraries.<library>
      vcfa.provider.organizations.<org>
      vcfa.organizations.<org>
      vcfa.organizations.<org>.content_libraries.<library>

    org_admin is a nested object:
      username
      password_file
      api_token_file
      create_token
      replace_token
    """
    vcfa_cfg = config.get("vcfa") or {}
    provider_cfg = vcfa_cfg.get("provider") or {}

    provider_regions = provider_cfg.get("regions") or {}
    provider_libraries = (
        provider_cfg.get("provider_content_libraries") or {}
    )
    provider_orgs = provider_cfg.get("organizations") or {}
    org_cfgs = vcfa_cfg.get("organizations") or {}

    if provider_regions and not isinstance(provider_regions, dict):
        raise RuntimeError(
            "vcfa.provider.regions must be an object keyed by region name"
        )

    for region_name, region_cfg in provider_regions.items():
        if not isinstance(region_cfg, dict):
            raise RuntimeError(
                f"vcfa.provider.regions['{region_name}'] must be an object"
            )

        if not str(
            region_cfg.get("nsx_manager", "") or ""
        ).strip():
            raise RuntimeError(
                f"vcfa.provider.regions['{region_name}'].nsx_manager is required"
            )

        supervisors = region_cfg.get("supervisors") or []
        if not isinstance(supervisors, list) or not supervisors:
            raise RuntimeError(
                f"vcfa.provider.regions['{region_name}'].supervisors "
                "must be a non-empty array"
            )

        storage_policies = region_cfg.get("storage_policies") or []
        if not isinstance(storage_policies, list) or not storage_policies:
            raise RuntimeError(
                f"vcfa.provider.regions['{region_name}'].storage_policies "
                "must be a non-empty array"
            )

    if provider_libraries and not isinstance(provider_libraries, dict):
        raise RuntimeError(
            "vcfa.provider.provider_content_libraries must be an object "
            "keyed by library name"
        )

    for library_name, library_cfg in provider_libraries.items():
        if not isinstance(library_cfg, dict):
            raise RuntimeError(
                f"vcfa.provider.provider_content_libraries['{library_name}'] "
                "must be an object"
            )

        storage_classes_cfg = library_cfg.get("storage_classes") or []
        if not isinstance(storage_classes_cfg, list) or not storage_classes_cfg:
            raise RuntimeError(
                f"Provider content library '{library_name}' requires "
                "a non-empty storage_classes array"
            )

        items = library_cfg.get("items") or []
        if not isinstance(items, list):
            raise RuntimeError(
                f"Provider content library '{library_name}'.items "
                "must be an array"
            )

        for item in items:
            if not isinstance(item, dict):
                raise RuntimeError(
                    f"Provider content library '{library_name}' contains "
                    "a non-object item"
                )

            if not str(item.get("name", "") or "").strip():
                raise RuntimeError(
                    f"Provider content library '{library_name}' contains "
                    "an item without a name"
                )

            if not str(
                item.get("item_type")
                or item.get("itemType")
                or ""
            ).strip():
                raise RuntimeError(
                    f"Provider content library item "
                    f"'{item.get('name', '<unnamed>')}' requires item_type"
                )

            files = item.get("files") or []
            if not library_cfg.get("is_subscribed", False):
                if not isinstance(files, list) or not files:
                    raise RuntimeError(
                        f"Provider content library item "
                        f"'{item.get('name', '<unnamed>')}' in "
                        f"'{library_name}' requires a non-empty files array"
                    )

    if provider_orgs and not isinstance(provider_orgs, dict):
        raise RuntimeError(
            "vcfa.provider.organizations must be an object keyed by "
            "organization name"
        )

    for org_name, provider_org_cfg in provider_orgs.items():
        if not isinstance(provider_org_cfg, dict):
            raise RuntimeError(
                f"vcfa.provider.organizations['{org_name}'] must be an object"
            )

        if "regional_quota" in provider_org_cfg:
            raise RuntimeError(
                f"vcfa.provider.organizations['{org_name}'].regional_quota "
                "is no longer supported; rename it to region_quota"
            )

        region_quota_cfg = provider_org_cfg.get("region_quota") or {}
        if region_quota_cfg and not isinstance(region_quota_cfg, dict):
            raise RuntimeError(
                f"vcfa.provider.organizations['{org_name}'].region_quota "
                "must be an object"
            )

        if isinstance(region_quota_cfg, dict):
            resources_cfg = region_quota_cfg.get("resources")

            if resources_cfg is not None and not isinstance(resources_cfg, dict):
                raise RuntimeError(
                    f"vcfa.provider.organizations['{org_name}']."
                    "region_quota.resources must be an object"
                )

            if isinstance(resources_cfg, dict) and "vm_classes" in resources_cfg:
                vm_classes_cfg = resources_cfg.get("vm_classes")

                if vm_classes_cfg is None:
                    vm_classes_cfg = {}

                if not isinstance(vm_classes_cfg, dict):
                    raise RuntimeError(
                        f"vcfa.provider.organizations['{org_name}']."
                        "region_quota.resources.vm_classes must be an object"
                    )

                classes_cfg = vm_classes_cfg.get("classes", [])
                if classes_cfg is None:
                    classes_cfg = []

                if not isinstance(classes_cfg, list):
                    raise RuntimeError(
                        f"vcfa.provider.organizations['{org_name}']."
                        "region_quota.resources.vm_classes.classes "
                        "must be an array"
                    )

                for vm_class in classes_cfg:
                    if isinstance(vm_class, str):
                        vm_class_name = vm_class.strip()
                    elif isinstance(vm_class, dict):
                        vm_class_name = str(
                            vm_class.get("name", "") or ""
                        ).strip()
                    else:
                        vm_class_name = ""

                    if not vm_class_name:
                        raise RuntimeError(
                            f"Each vcfa.provider.organizations['{org_name}']."
                            "region_quota.resources.vm_classes.classes entry "
                            "requires a name"
                        )

        resources_cfg = (provider_org_cfg.get("region_quota") or {}).get("resources")
        if isinstance(resources_cfg, dict) and "storage_classes" in resources_cfg:
            storage_classes_cfg = resources_cfg.get("storage_classes")
            if storage_classes_cfg is None:
                storage_classes_cfg = []
            if not isinstance(storage_classes_cfg, list):
                raise RuntimeError(
                    f"vcfa.provider.organizations['{org_name}']."
                    "region_quota.resources.storage_classes must be an array"
                )
            for storage_class in storage_classes_cfg:
                if isinstance(storage_class, str):
                    storage_class_name = storage_class.strip()
                elif isinstance(storage_class, dict):
                    storage_class_name = str(storage_class.get("name", "") or "").strip()
                else:
                    storage_class_name = ""
                if not storage_class_name:
                    raise RuntimeError(
                        f"Each vcfa.provider.organizations['{org_name}']."
                        "region_quota.resources.storage_classes entry requires a name"
                    )

        first_user = provider_org_cfg.get("first_user")

        if not isinstance(first_user, dict):
            raise RuntimeError(
                f"vcfa.provider.organizations['{org_name}'].first_user "
                "is required"
            )

        first_username = str(
            first_user.get("username", "") or ""
        ).strip()

        if not first_username:
            raise RuntimeError(
                f"vcfa.provider.organizations['{org_name}']."
                "first_user.username is required"
            )

        if not str(
            first_user.get("password_file", "") or ""
        ).strip():
            raise RuntimeError(
                f"vcfa.provider.organizations['{org_name}']."
                "first_user.password_file is required"
            )

        create_token = bool(
            first_user.get("create_token", False)
        )
        replace_token = bool(
            first_user.get("replace_token", False)
        )
        token_file = str(
            first_user.get("api_token_file", "") or ""
        ).strip()

        if create_token and not token_file:
            raise RuntimeError(
                f"vcfa.provider.organizations['{org_name}']."
                "first_user.api_token_file is required when "
                "create_token=true"
            )

        if replace_token and not create_token:
            warn(
                f"Organization '{org_name}' first_user has "
                "replace_token=true but create_token=false; "
                "replace_token will be ignored"
            )

    if org_cfgs and not isinstance(org_cfgs, dict):
        raise RuntimeError(
            "vcfa.organizations must be an object keyed by organization name"
        )

    for org_name, org_cfg in org_cfgs.items():
        if not isinstance(org_cfg, dict):
            raise RuntimeError(
                f"vcfa.organizations['{org_name}'] must be an object"
            )

        if org_name not in provider_orgs:
            raise RuntimeError(
                f"vcfa.organizations['{org_name}'] has no matching "
                f"vcfa.provider.organizations['{org_name}'] entry"
            )

        identity_cfg = org_cfg.get("identity_providers") or {}
        access_cfg = org_cfg.get("access_control") or {}

        if identity_cfg and not isinstance(identity_cfg, dict):
            raise RuntimeError(
                f"vcfa.organizations['{org_name}'].identity_providers "
                "must be an object keyed by provider type"
            )

        if isinstance(identity_cfg, dict):
            for provider_type, provider_cfg in identity_cfg.items():
                if provider_cfg in (None, {}):
                    continue

                if not isinstance(provider_cfg, dict):
                    raise RuntimeError(
                        f"vcfa.organizations['{org_name}']."
                        f"identity_providers['{provider_type}'] "
                        "must be an object"
                    )

                normalized_type = str(provider_type).strip().lower()

                if normalized_type not in ("ldap", "oidc", "saml"):
                    raise RuntimeError(
                        f"Unsupported identity provider type "
                        f"'{provider_type}' for organization '{org_name}'"
                    )

                if normalized_type == "ldap":
                    if not str(
                        provider_cfg.get("settings_source", "") or ""
                    ).strip():
                        raise RuntimeError(
                            f"vcfa.organizations['{org_name}']."
                            "identity_providers['ldap'].settings_source "
                            "is required"
                        )

                    ldap_config = provider_cfg.get("config") or {}
                    if not isinstance(ldap_config, dict):
                        raise RuntimeError(
                            f"vcfa.organizations['{org_name}']."
                            "identity_providers['ldap'].config "
                            "must be an object"
                        )

        if access_cfg and not isinstance(access_cfg, dict):
            raise RuntimeError(
                f"vcfa.organizations['{org_name}'].access_control "
                "must be an object"
            )

        configured_identity_providers = {
            provider_type: provider_cfg
            for provider_type, provider_cfg in (
                identity_cfg.items()
                if isinstance(identity_cfg, dict)
                else []
            )
            if isinstance(provider_cfg, dict) and provider_cfg
        }

        has_identity_and_access = bool(
            configured_identity_providers
            and access_cfg
        )

        if bool(configured_identity_providers) != bool(access_cfg):
            warn(
                f"Organization '{org_name}' supplies only one of "
                "configured identity_providers/access_control; "
                "the local first user will remain active for all "
                "organization tasks"
            )

        if has_identity_and_access:
            admin_cfg = organization_admin_config(
                org_name,
                org_cfg,
            )

            org_admin_username = str(
                admin_cfg.get("username", "") or ""
            ).strip()

            if not org_admin_username:
                raise RuntimeError(
                    f"vcfa.organizations['{org_name}']."
                    "org_admin.username is required"
                )

            org_token_file = str(
                admin_cfg.get("api_token_file", "") or ""
            ).strip()

            if not org_token_file:
                raise RuntimeError(
                    f"vcfa.organizations['{org_name}']."
                    "org_admin.api_token_file is required"
                )

            org_create_token = bool(
                admin_cfg.get("create_token", False)
            )
            org_replace_token = bool(
                admin_cfg.get("replace_token", False)
            )

            if org_create_token:
                if not str(
                    admin_cfg.get("password_file", "") or ""
                ).strip():
                    raise RuntimeError(
                        f"vcfa.organizations['{org_name}']."
                        "org_admin.password_file is required when "
                        "create_token=true"
                    )

            if org_replace_token and not org_create_token:
                warn(
                    f"Organization '{org_name}' org_admin has "
                    "replace_token=true but create_token=false; "
                    "replace_token will be ignored"
                )

            # org_admin should normally be one of the imported users when
            # access control explicitly defines users.
            access_users = access_cfg.get("users") or {}
            if isinstance(access_users, dict) and access_users:
                if org_admin_username not in access_users:
                    warn(
                        f"org_admin '{org_admin_username}' for '{org_name}' "
                        "is not listed under access_control.users"
                    )

        libraries = org_cfg.get("content_libraries") or {}

        if libraries and not isinstance(libraries, dict):
            raise RuntimeError(
                f"vcfa.organizations['{org_name}'].content_libraries "
                "must be an object keyed by library name"
            )

        for library_name, library_cfg in libraries.items():
            if not isinstance(library_cfg, dict):
                raise RuntimeError(
                    f"vcfa.organizations['{org_name}']."
                    f"content_libraries['{library_name}'] must be an object"
                )

            storage_classes_cfg = (
                library_cfg.get("storage_classes") or []
            )

            if (
                not isinstance(storage_classes_cfg, list)
                or not storage_classes_cfg
            ):
                raise RuntimeError(
                    f"Organization content library '{library_name}' "
                    "requires a non-empty storage_classes array"
                )

            items = library_cfg.get("items") or []

            if not isinstance(items, list):
                raise RuntimeError(
                    f"Organization content library '{library_name}'.items "
                    "must be an array"
                )

            for item in items:
                if not isinstance(item, dict):
                    raise RuntimeError(
                        f"Content library '{library_name}' contains "
                        "a non-object item"
                    )

                if not str(item.get("name", "") or "").strip():
                    raise RuntimeError(
                        f"Content library '{library_name}' contains "
                        "an item without a name"
                    )

                if not str(
                    item.get("item_type")
                    or item.get("itemType")
                    or ""
                ).strip():
                    raise RuntimeError(
                        f"Content library item "
                        f"'{item.get('name', '<unnamed>')}' in "
                        f"'{library_name}' requires item_type"
                    )

                files = item.get("files") or []

                if not isinstance(files, list) or not files:
                    raise RuntimeError(
                        f"Content library item "
                        f"'{item.get('name', '<unnamed>')}' in "
                        f"'{library_name}' requires a non-empty files array"
                    )

        notifications = org_cfg.get("notifications") or {}

        if notifications and not isinstance(notifications, dict):
            raise RuntimeError(
                f"vcfa.organizations['{org_name}'].notifications "
                "must be an object"
            )

        email_cfg = (
            notifications.get("email_server") or {}
            if isinstance(notifications, dict)
            else {}
        )

        if email_cfg:
            validate_notification_email_json(
                org_name,
                email_cfg,
            )


# ============================================================
# Organization projects
# ============================================================

PROJECT_SERVICE_API_VERSION = "2019-01-15"
DEFAULT_PROJECT_NAMES = {
    "default",
    "default-project",
    "default project",
}


def organization_projects(client: RestClient) -> List[Dict[str, Any]]:
    """Return projects visible in the current organization context."""
    page = 0
    size = 500
    result: List[Dict[str, Any]] = []

    while True:
        payload = client.get(
            "/project-service/api/projects"
            f"?apiVersion={PROJECT_SERVICE_API_VERSION}"
            f"&page={page}&size={size}"
        )

        if not isinstance(payload, dict):
            raise RuntimeError(
                "Project Service returned an unexpected response while "
                "querying organization projects"
            )

        values = payload.get("content") or []
        if not isinstance(values, list):
            raise RuntimeError(
                "Project Service response field 'content' is not an array"
            )

        result.extend(
            item for item in values if isinstance(item, dict)
        )

        if bool(payload.get("last", False)):
            break

        total_pages = payload.get("totalPages")
        if isinstance(total_pages, int) and page + 1 >= total_pages:
            break

        # Defensive stop for APIs that omit paging metadata.
        if len(values) < size:
            break

        page += 1

    return result


def _normalized_project_name(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def is_default_project(project: Dict[str, Any]) -> bool:
    """
    Identify VCFA's auto-created default project by its standard names.

    VCFA installations commonly expose it as 'default-project'. Some builds
    or workflows display it as 'default' / 'Default Project'.
    """
    name = _normalized_project_name(project.get("name"))
    return name in DEFAULT_PROJECT_NAMES


def delete_default_project_if_exists(
    client: RestClient,
    org_name: str,
    timeout: int = 120,
    poll: int = 2,
) -> None:
    """Delete the organization's default project when it exists."""
    projects = organization_projects(client)
    defaults = [project for project in projects if is_default_project(project)]

    if not defaults:
        skip(
            f"Default project does not exist in organization '{org_name}'"
        )
        return

    # There should only be one auto-created default project. If the API ever
    # returns more than one matching standard default name, delete each one
    # deterministically rather than leaving stale defaults behind.
    for project in defaults:
        project_id = str(project.get("id", "") or "").strip()
        project_name = str(project.get("name", "") or "<unnamed>")

        if not project_id:
            raise RuntimeError(
                f"Default project '{project_name}' in organization "
                f"'{org_name}' has no project ID"
            )

        info(
            f"Deleting default project '{project_name}' from organization '{org_name}'"
        )

        # Project Service DELETE expects the raw project UUID in the path and
        # does not require the apiVersion query parameter. Do not pass a URN
        # or append ?apiVersion=... here.
        project_uuid = str(project_id).strip()
        if project_uuid.startswith("urn:"):
            project_uuid = project_uuid.rsplit(":", 1)[-1]
        project_uuid = project_uuid.strip()
        if not project_uuid:
            raise RuntimeError(
                f"Unable to determine UUID for default project '{project_name}' "
                f"in organization '{org_name}'"
            )

        client.delete(
            f"/project-service/api/projects/{project_uuid}"
        )

        deadline = time.time() + timeout
        while time.time() < deadline:
            remaining = organization_projects(client)
            if not any(
                str(item.get("id", "") or "") == project_id
                for item in remaining
            ):
                info(
                    f"Default project '{project_name}' deleted from "
                    f"organization '{org_name}'"
                )
                break
            time.sleep(poll)
        else:
            raise RuntimeError(
                f"Timed out after {timeout}s waiting for default project "
                f"'{project_name}' to be deleted from organization "
                f"'{org_name}'"
            )


# ============================================================
# Organization namespace classes (CCI)
# ============================================================


def _namespace_class_payload(name: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "apiVersion": "infrastructure.cci.vmware.com/v1alpha2",
        "kind": "SupervisorNamespaceClass",
        "metadata": {"name": name},
        "spec": {
            "description": str(cfg.get("description", "") or ""),
        },
    }


def _namespace_class_config_payload(name: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    vm_classes = cfg.get("vm_classes") or []
    storage_classes = cfg.get("storage_classes") or []
    content_sources = cfg.get("content_source")
    if content_sources is None:
        content_sources = cfg.get("content_sources") or []
    zones = cfg.get("zones") or []

    if not isinstance(vm_classes, list):
        raise RuntimeError(f"namespaceClasses.{name}.vm_classes must be an array")
    if not isinstance(storage_classes, list):
        raise RuntimeError(f"namespaceClasses.{name}.storage_classes must be an array")
    if not isinstance(content_sources, list):
        raise RuntimeError(f"namespaceClasses.{name}.content_source must be an array")
    if not isinstance(zones, list):
        raise RuntimeError(f"namespaceClasses.{name}.zones must be an array")

    storage_payload = []
    for entry in storage_classes:
        if isinstance(entry, str):
            storage_payload.append({"name": entry})
            continue
        if not isinstance(entry, dict) or not str(entry.get("name", "") or "").strip():
            raise RuntimeError(
                f"Each namespaceClasses.{name}.storage_classes entry requires a name"
            )
        value = {"name": str(entry["name"])}
        if entry.get("limit") is not None:
            value["limit"] = str(entry["limit"])
        storage_payload.append(value)

    zone_payload = []
    for entry in zones:
        if not isinstance(entry, dict) or not str(entry.get("name", "") or "").strip():
            raise RuntimeError(f"Each namespaceClasses.{name}.zones entry requires a name")
        zone = {"name": str(entry["name"])}
        field_map = {
            "cpu_limit": "cpuLimit",
            "cpu_reservation": "cpuReservation",
            "memory_limit": "memoryLimit",
            "memory_reservation": "memoryReservation",
        }
        for source, target in field_map.items():
            if entry.get(source) is not None:
                zone[target] = str(entry[source])
        zone_payload.append(zone)

    return {
        "apiVersion": "infrastructure.cci.vmware.com/v1alpha2",
        "kind": "SupervisorNamespaceClassConfig",
        "metadata": {"name": name},
        "spec": {
            "vmClasses": [{"name": str(value)} for value in vm_classes],
            "storageClasses": storage_payload,
            "contentSources": [
                {"name": str(value), "type": "ContentLibrary"}
                for value in content_sources
            ],
            "zones": zone_payload,
        },
    }


def configure_organization_namespace_classes(
    client: RestClient,
    org_name: str,
    namespace_classes_cfg: Any,
) -> None:
    if namespace_classes_cfg in (None, [], {}):
        return

    if not isinstance(namespace_classes_cfg, dict):
        raise RuntimeError(
            f"namespaceClasses for organization '{org_name}' must be an object "
            "or an empty array"
        )

    cci = CciClient(client)

    for class_name, class_cfg in namespace_classes_cfg.items():
        if not isinstance(class_cfg, dict):
            raise RuntimeError(
                f"namespaceClasses.{class_name} must be an object"
            )

        class_name = str(class_name or "").strip()
        if not class_name:
            raise RuntimeError("Namespace class name cannot be empty")

        ensure_resource(
            name=class_name,
            resource_label="Namespace class",
            lookup=lambda class_name=class_name: cci.get_namespace_class(
                class_name
            ),
            create_func=lambda class_name=class_name, class_cfg=class_cfg: (
                cci.create_namespace_class(
                    _namespace_class_payload(class_name, class_cfg)
                )
            ),
        )

        ensure_resource(
            name=class_name,
            resource_label="Namespace class config",
            lookup=lambda class_name=class_name: (
                cci.get_namespace_class_config(class_name)
            ),
            create_func=lambda class_name=class_name, class_cfg=class_cfg: (
                cci.create_namespace_class_config(
                    _namespace_class_config_payload(class_name, class_cfg)
                )
            ),
        )


# ============================================================
# Organization project creation
# ============================================================

PROJECT_JSON_ROLE_FIELDS = {
    "administrators": "administrator",
    "users": "user",
    "advancedUsers": "advanced_user",
    "auditors": "auditor",
}


def _project_principal_type(value: str) -> str:
    """
    Infer Project Service principal type from the configured identifier.

    In this JSON model, group principals use the trailing '@' form returned/
    accepted by Project Service (for example 'SciTech-developers@').
    User principals do not end in '@'.
    """
    return "group" if str(value or "").strip().endswith("@") else "user"


def _project_by_name(
    client: RestClient,
    project_name: str,
) -> Optional[Dict[str, Any]]:
    wanted = str(project_name or "").strip().casefold()

    for project in organization_projects(client):
        actual = str(
            project.get("name", "") or ""
        ).strip().casefold()

        if actual == wanted:
            return project

    return None


def _project_specification(project_name: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build the Project Service create payload from the new project JSON schema:

      administrators: [...]
      users: [...]
      advancedUsers: [...]
      auditors: [...]

    The field itself defines the project role.
    """
    body: Dict[str, Any] = {
        "name": project_name,
        "description": str(cfg.get("description", "") or ""),
    }

    for json_field, role in PROJECT_JSON_ROLE_FIELDS.items():
        entries = cfg.get(json_field) or []
        if not isinstance(entries, list):
            raise RuntimeError(
                f"projects.{project_name}.{json_field} must be an array"
            )

        project_field = json_field
        principals: List[Dict[str, str]] = []

        seen = set()
        for raw_value in entries:
            value = str(raw_value or "").strip()
            if not value:
                raise RuntimeError(
                    f"projects.{project_name}.{json_field} contains an empty principal"
                )

            key = value.casefold()
            if key in seen:
                continue
            seen.add(key)

            principals.append({
                "email": value,
                "type": _project_principal_type(value),
            })

        if principals:
            body[project_field] = principals

    return body


def _project_principal_assignments(
    project_name: str,
    cfg: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    Convert the new project JSON schema into Project Service principal records.

    JSON is authoritative. Every entry in administrators/users/advancedUsers/
    auditors is included. Comparison later is case-insensitive.
    """
    principals: List[Dict[str, Any]] = []
    seen = set()

    for json_field, role in PROJECT_JSON_ROLE_FIELDS.items():
        entries = cfg.get(json_field) or []
        if not isinstance(entries, list):
            raise RuntimeError(
                f"projects.{project_name}.{json_field} must be an array"
            )

        for raw_value in entries:
            value = str(raw_value or "").strip()
            if not value:
                raise RuntimeError(
                    f"projects.{project_name}.{json_field} contains an empty principal"
                )

            principal_type = _project_principal_type(value)

            # Prevent duplicates irrespective of case, while still allowing the
            # same identifier to exist as a user and a group if VCFA ever returns that.
            key = (value.casefold(), principal_type.casefold())
            if key in seen:
                continue
            seen.add(key)

            principals.append({
                "email": value,
                "type": principal_type,
                "role": role,
            })

    return principals


def _project_get_by_id(
    client: RestClient,
    project_uuid: str,
) -> Dict[str, Any]:
    """Return the Project Service project object."""
    payload = client.get(
        f"/project-service/api/projects/{project_uuid}"
        f"?apiVersion={PROJECT_SERVICE_API_VERSION}"
    )
    return payload if isinstance(payload, dict) else {}


def _project_object_principals(
    project: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    Flatten Project Service role arrays into comparable principal records.

    Project Service returns principals grouped into fields such as:
      administrators, users, advancedUsers, auditors
    rather than a flat list containing a role property.
    """
    result: List[Dict[str, Any]] = []

    role_fields = {
        "administrators": "administrator",
        "users": "user",
        "advancedUsers": "advanced_user",
        "auditors": "auditor",
    }

    for field, role in role_fields.items():
        entries = project.get(field) or []
        if not isinstance(entries, list):
            continue

        for entry in entries:
            if not isinstance(entry, dict):
                continue

            email = str(entry.get("email", "") or "").strip()
            principal_type = str(entry.get("type", "") or "").strip()

            if not email:
                continue

            result.append({
                "id": str(entry.get("id", "") or "").strip(),
                "email": email,
                "type": principal_type,
                "role": role,
            })

    return result


def _project_principal_key(principal: Dict[str, Any]) -> tuple:
    """Normalize a Project Service principal for case-insensitive comparison."""
    email = str(principal.get("email", "") or "").strip().casefold()
    principal_type = str(principal.get("type", "") or "").strip().casefold()
    role = str(principal.get("role", "") or "").strip().casefold()
    return (email, principal_type, role)


def reconcile_project_principals(
    client: RestClient,
    org_name: str,
    project: Dict[str, Any],
    project_name: str,
    project_cfg: Dict[str, Any],
) -> None:
    """
    Make project membership exactly match projects.<project> role arrays.

    JSON is authoritative. No organization-wide access-control principals are
    implicitly added.
    """
    project_id = str(project.get("id", "") or "").strip()
    if not project_id:
        raise RuntimeError(
            f"Project '{project_name}' in organization '{org_name}' has no project ID"
        )

    project_uuid = urn_uuid(project_id)

    if not project_uuid:
        raise RuntimeError(
            f"Unable to determine UUID for project '{project_name}'"
        )

    desired = _project_principal_assignments(project_name, project_cfg)

    desired_by_identity = {
        (
            casefold_key(p.get("email")),
            casefold_key(p.get("type")),
        ): p
        for p in desired
    }

    live_project = _project_get_by_id(client, project_uuid)
    current = _project_object_principals(live_project)

    current_by_identity = {
        (
            casefold_key(p.get("email")),
            casefold_key(p.get("type")),
        ): p
        for p in current
    }

    modify: List[Dict[str, Any]] = []
    remove: List[Dict[str, Any]] = []

    for identity_key, desired_principal in desired_by_identity.items():
        existing = current_by_identity.get(identity_key)

        if existing is None:
            modify.append(desired_principal)
            continue

        current_role = casefold_key(existing.get("role"))
        desired_role = casefold_key(desired_principal.get("role"))

        if current_role != desired_role:
            modify.append(desired_principal)

    for identity_key, existing in current_by_identity.items():
        if identity_key in desired_by_identity:
            continue

        remove_entry = {
            "email": str(existing.get("email", "") or "").strip(),
            "type": str(existing.get("type", "") or "").strip(),
            "role": str(existing.get("role", "") or "").strip(),
        }

        existing_id = str(existing.get("id", "") or "").strip()
        if existing_id:
            remove_entry["id"] = existing_id

        remove.append(remove_entry)

    info(
        f"Project '{project_name}' desired principals from JSON: "
        f"{len(desired)} total"
    )

    for principal in desired:
        info(
            f"  configured project principal: {principal['type']} "
            f"'{principal['email']}' -> {principal['role']}"
        )

    if modify or remove:
        info(
            f"Reconciling project '{project_name}': "
            f"modify/add={len(modify)}, remove={len(remove)}"
        )

        body = {
            "modify": modify,
            "remove": remove,
        }

        client.request(
            "PATCH",
            f"/project-service/api/projects/{project_uuid}/principals"
            f"?apiVersion={PROJECT_SERVICE_API_VERSION}"
            f"&syncPrincipals=true",
            json=body,
        )
    else:
        skip(
            f"Project '{project_name}' principals already exactly match JSON"
        )

    # Verify using the Project object returned by GET /projects/{id}.
    verified_project = _project_get_by_id(client, project_uuid)
    verified = _project_object_principals(verified_project)

    verified_keys = {
        _project_principal_key(p)
        for p in verified
    }
    desired_keys = {
        _project_principal_key(p)
        for p in desired
    }

    missing = sorted(desired_keys - verified_keys)
    if missing:
        raise RuntimeError(
            f"Project '{project_name}' principal reconciliation completed, "
            f"but configured principals are still missing or have the wrong role: "
            f"{missing}"
        )

    verified_identities = {
        (key[0], key[1])
        for key in verified_keys
    }
    desired_identities = {
        (key[0], key[1])
        for key in desired_keys
    }

    extras = sorted(verified_identities - desired_identities)
    if extras:
        raise RuntimeError(
            f"Project '{project_name}' still contains principals that are not "
            f"defined in JSON: {extras}"
        )

    info(
        f"Project '{project_name}' principals verified: "
        f"{len(desired)} explicitly configured principal(s)"
    )


def _normalize_projects_config(
    projects_cfg: Any,
) -> tuple[bool, List[Dict[str, Any]]]:
    if projects_cfg in (None, [], {}):
        return False, []

    if isinstance(projects_cfg, list):
        result: List[Dict[str, Any]] = []
        seen = set()

        for entry in projects_cfg:
            if not isinstance(entry, dict):
                raise RuntimeError("Each projects entry must be an object")

            name = str(entry.get("name", "") or "").strip()
            if not name:
                raise RuntimeError("Each projects entry requires 'name'")

            key = name.casefold()
            if key in seen:
                raise RuntimeError(
                    f"Project '{name}' is configured more than once"
                )
            seen.add(key)

            result.append(entry)

        return False, result

    if isinstance(projects_cfg, dict):
        delete_default = bool(projects_cfg.get("delete_default", False))
        result: List[Dict[str, Any]] = []

        for project_name, project_cfg in projects_cfg.items():
            if project_name == "delete_default":
                continue

            if not isinstance(project_cfg, dict):
                raise RuntimeError(
                    f"projects.{project_name} must be an object"
                )

            entry = dict(project_cfg)
            entry.setdefault("name", project_name)
            result.append(entry)

        return delete_default, result

    raise RuntimeError("projects must be an array or an object")


def configure_organization_projects(
    client: RestClient,
    org_name: str,
    projects_cfg: Any,
    delete_default_project: bool = False,
) -> None:
    legacy_delete_default, projects = _normalize_projects_config(projects_cfg)

    if delete_default_project or legacy_delete_default:
        delete_default_project_if_exists(client, org_name)

    for project_cfg in projects:
        project_name = str(project_cfg.get("name", "") or "").strip()
        if not project_name:
            raise RuntimeError("Each projects entry requires 'name'")

        existing = _project_by_name(client, project_name)

        if existing:
            skip(
                f"Project '{project_name}' already exists in organization "
                f"'{org_name}'; reconciling users/groups"
            )
            reconcile_project_principals(
                client,
                org_name,
                existing,
                project_name,
                project_cfg,
            )
            continue

        body = _project_specification(project_name, project_cfg)

        create(
            f"Project '{project_name}' in organization '{org_name}'"
        )

        created = client.post(
            f"/project-service/api/projects"
            f"?apiVersion={PROJECT_SERVICE_API_VERSION}",
            json=body,
        )

        if not isinstance(created, dict):
            created = _project_by_name(client, project_name)

        if not created:
            raise RuntimeError(
                f"Project '{project_name}' was created but could not "
                "be queried afterwards"
            )

        info(
            f"Project '{project_name}' created in organization '{org_name}'"
        )

        reconcile_project_principals(
            client,
            org_name,
            created,
            project_name,
            project_cfg,
        )

# ============================================================
# Organization Supervisor Namespaces (CCI)
# ============================================================

CCI_SUPERVISOR_NAMESPACE_API_PREFIX = (
    "/cci/kubernetes/apis/infrastructure.cci.vmware.com/v1alpha2/"
    "namespaces"
)


def _configured_namespace_marker(value: str) -> str:
    """
    Produce a Kubernetes-label-safe, stable marker for the configured namespace.
    """
    value = str(value or "").strip().casefold()
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    value = value.strip("._-")
    if not value:
        raise RuntimeError("Unable to derive configured namespace marker")
    return value[:63]


def _namespace_class_zone_defaults(
    class_config: Dict[str, Any],
    requested_zone: str,
) -> Dict[str, str]:
    """
    Resolve required zone CPU/memory values from the NamespaceClassConfig.

    A class config can define a specific zone or wildcard '*'.  Namespace
    creation requires all four values in spec.initialClassConfigOverrides.zones.
    """
    spec = class_config.get("spec") or {}
    zones = spec.get("zones") or []
    if not isinstance(zones, list):
        zones = []

    wanted = str(requested_zone or "").strip().casefold()
    wildcard = None
    exact = None

    for entry in zones:
        if not isinstance(entry, dict):
            continue
        entry_name = str(entry.get("name", "") or "").strip().casefold()
        if entry_name == wanted:
            exact = entry
            break
        if entry_name == "*":
            wildcard = entry

    source = exact or wildcard
    if not isinstance(source, dict):
        raise RuntimeError(
            f"Namespace class config has no zone defaults matching "
            f"'{requested_zone}' and no wildcard '*' zone"
        )

    required = (
        "cpuLimit",
        "cpuReservation",
        "memoryLimit",
        "memoryReservation",
    )

    result: Dict[str, str] = {}
    missing = []

    for field in required:
        value = source.get(field)
        if value is None or not str(value).strip():
            missing.append(field)
        else:
            result[field] = str(value)

    if missing:
        raise RuntimeError(
            f"Namespace class config zone '{source.get('name')}' is missing "
            f"required value(s): {', '.join(missing)}"
        )

    return result


def _supervisor_namespace_payload(
    namespace_cfg: Dict[str, Any],
    class_config: Dict[str, Any],
) -> Dict[str, Any]:
    name = str(namespace_cfg.get("name", "") or "").strip()
    project_name = str(namespace_cfg.get("project_name", "") or "").strip()
    class_name = str(namespace_cfg.get("class_name", "") or "").strip()
    region_name = str(namespace_cfg.get("region_name", "") or "").strip()

    if not project_name:
        raise RuntimeError(f"Namespace '{name}' requires 'project_name'")
    if not class_name:
        raise RuntimeError(f"Namespace '{name}' requires 'class_name'")
    if not region_name:
        raise RuntimeError(f"Namespace '{name}' requires 'region_name'")

    zones = namespace_cfg.get("zones") or []
    if not isinstance(zones, list) or not zones:
        raise RuntimeError(
            f"Namespace '{name}' requires a non-empty 'zones' array"
        )

    zone_payload: List[Dict[str, Any]] = []
    seen_zones = set()

    for zone_entry in zones:
        if isinstance(zone_entry, str):
            zone_name = zone_entry.strip()
            zone_cfg: Dict[str, Any] = {}
        elif isinstance(zone_entry, dict):
            zone_name = str(zone_entry.get("name", "") or "").strip()
            zone_cfg = zone_entry
        else:
            raise RuntimeError(
                f"Namespace '{name}' zones entries must be strings or objects"
            )

        if not zone_name:
            raise RuntimeError(
                f"Namespace '{name}' contains an empty zone name"
            )

        zone_key = zone_name.casefold()
        if zone_key in seen_zones:
            continue
        seen_zones.add(zone_key)

        # The API requires all four limit/reservation fields, even when the
        # namespace JSON only selects a zone.  Start from the selected
        # NamespaceClassConfig zone (or wildcard '*') and then allow explicit
        # namespace-level overrides.
        defaults = _namespace_class_zone_defaults(
            class_config,
            zone_name,
        )

        zone: Dict[str, Any] = {
            "name": zone_name,
            **defaults,
        }

        override_map = {
            "cpu_limit": "cpuLimit",
            "cpu_reservation": "cpuReservation",
            "memory_limit": "memoryLimit",
            "memory_reservation": "memoryReservation",
        }
        for source_key, target_key in override_map.items():
            if zone_cfg.get(source_key) is not None:
                zone[target_key] = str(zone_cfg[source_key])

        zone_payload.append(zone)

    # Storage classes:
    # - If the NamespaceClassConfig already defines storageClasses, inherit them.
    # - Otherwise use namespaces[].storage_classes as namespace-level overrides.
    class_spec = class_config.get("spec") or {}
    if not isinstance(class_spec, dict):
        class_spec = {}

    class_storage_classes = class_spec.get("storageClasses") or []
    if not isinstance(class_storage_classes, list):
        class_storage_classes = []

    namespace_storage_classes = namespace_cfg.get("storage_classes") or []
    if not isinstance(namespace_storage_classes, list):
        raise RuntimeError(
            f"Namespace '{name or '<unnamed>'}'.storage_classes must be an array"
        )

    storage_override: List[Dict[str, Any]] = []

    if not class_storage_classes:
        seen_storage = set()

        for storage_cfg in namespace_storage_classes:
            if not isinstance(storage_cfg, dict):
                raise RuntimeError(
                    f"Namespace '{name or '<unnamed>'}' storage_classes entries "
                    "must be objects"
                )

            storage_name = str(storage_cfg.get("name", "") or "").strip()
            if not storage_name:
                raise RuntimeError(
                    f"Namespace '{name or '<unnamed>'}' contains a storage class "
                    "without a name"
                )

            key = storage_name.casefold()
            if key in seen_storage:
                continue
            seen_storage.add(key)

            entry: Dict[str, Any] = {"name": storage_name}

            limit = storage_cfg.get("limit")
            if limit is not None and str(limit).strip():
                entry["limit"] = str(limit).strip()

            storage_override.append(entry)

        if not storage_override:
            raise RuntimeError(
                f"Cannot create namespace '{name or '<unnamed>'}': "
                f"SupervisorNamespaceClassConfig '{class_name}' has no "
                "storageClasses and namespaces[].storage_classes is empty"
            )

    spec: Dict[str, Any] = {
        "description": str(namespace_cfg.get("description", "") or ""),
        "regionName": region_name,
        "className": class_name,
        "initialClassConfigOverrides": {
            "zones": zone_payload,
        },
    }

    if storage_override:
        spec["initialClassConfigOverrides"]["storageClasses"] = storage_override

    vpc_name = str(namespace_cfg.get("vpcName", "") or "").strip()
    if vpc_name:
        spec["vpcName"] = vpc_name

    if "sharedVpc" in namespace_cfg and namespace_cfg.get("sharedVpc") is not None:
        spec["sharedVpc"] = bool(namespace_cfg.get("sharedVpc"))

    connectivity_profile = namespace_cfg.get("connectivity_profile")
    if connectivity_profile is not None and str(connectivity_profile).strip():
        spec["connectivityProfile"] = str(connectivity_profile).strip()

    private_subnet = namespace_cfg.get("private_subnet")
    if private_subnet is not None and str(private_subnet).strip():
        spec["privateSubnet"] = str(private_subnet).strip()

    shared_subnet_names = namespace_cfg.get("shared_subnet_names")
    if shared_subnet_names is not None:
        if not isinstance(shared_subnet_names, list):
            raise RuntimeError(
                f"Namespace '{name}'.shared_subnet_names must be an array"
            )

        cleaned_shared_subnets = []
        seen_subnets = set()

        for subnet in shared_subnet_names:
            subnet_name = str(subnet or "").strip()
            if not subnet_name:
                continue

            subnet_key = subnet_name.casefold()
            if subnet_key in seen_subnets:
                continue

            seen_subnets.add(subnet_key)
            cleaned_shared_subnets.append(subnet_name)

        if cleaned_shared_subnets:
            spec["sharedSubnetNames"] = cleaned_shared_subnets

    generate_name = str(
        namespace_cfg.get("generate_name", "") or ""
    ).strip()

    if not generate_name:
        raise RuntimeError(
            f"Namespace '{name}' cannot be created because 'generate_name' is empty"
        )

    return {
        "apiVersion": "infrastructure.cci.vmware.com/v1alpha2",
        "kind": "SupervisorNamespace",
        "metadata": {
            "generateName": generate_name,
            "namespace": project_name,
        },
        "spec": spec,
    }


def compare_supervisor_namespace(
    namespace_name: str,
    namespace_cfg: Dict[str, Any],
    existing: Dict[str, Any],
) -> List[str]:
    """
    Compare an existing SupervisorNamespace with the desired JSON configuration.

    This function is read-only. It reports differences but never reconciles,
    patches, updates, deletes, or recreates the namespace.
    """
    differences: List[str] = []

    spec = existing.get("spec") or {}
    metadata = existing.get("metadata") or {}

    if not isinstance(spec, dict):
        spec = {}
    if not isinstance(metadata, dict):
        metadata = {}

    desired_project = str(
        namespace_cfg.get("project_name", "") or ""
    ).strip()
    existing_project = str(
        metadata.get("namespace", "") or ""
    ).strip()

    desired_class = str(
        namespace_cfg.get("class_name", "") or ""
    ).strip()
    existing_class = str(
        spec.get("className", "") or ""
    ).strip()

    desired_region = str(
        namespace_cfg.get("region_name", "") or ""
    ).strip()
    existing_region = str(
        spec.get("regionName", "") or ""
    ).strip()

    desired_vpc = str(
        namespace_cfg.get("vpcName", "") or ""
    ).strip()
    existing_vpc = str(
        spec.get("vpcName", "") or ""
    ).strip()

    desired_shared_vpc = namespace_cfg.get("sharedVpc")
    existing_shared_vpc = spec.get("sharedVpc")

    desired_description = str(
        namespace_cfg.get("description", "") or ""
    )
    existing_description = str(
        spec.get("description", "") or ""
    )

    desired_zones = [
        str(z.get("name", "") if isinstance(z, dict) else z).strip()
        for z in (namespace_cfg.get("zones") or [])
        if str(z.get("name", "") if isinstance(z, dict) else z).strip()
    ]

    existing_overrides = (
        spec.get("initialClassConfigOverrides")
        or spec.get("classConfigOverrides")
        or {}
    )
    if not isinstance(existing_overrides, dict):
        existing_overrides = {}

    existing_zone_entries = existing_overrides.get("zones") or []
    existing_zones = []
    if isinstance(existing_zone_entries, list):
        for zone in existing_zone_entries:
            if isinstance(zone, dict):
                zone_name = str(zone.get("name", "") or "").strip()
                if zone_name:
                    existing_zones.append(zone_name)
            elif zone:
                existing_zones.append(str(zone).strip())

    desired_connectivity_profile = namespace_cfg.get("connectivity_profile")
    existing_connectivity_profile = spec.get("connectivityProfile")

    desired_private_subnet = namespace_cfg.get("private_subnet")
    existing_private_subnet = spec.get("privateSubnet")

    desired_shared_subnets = [
        str(x).strip()
        for x in (namespace_cfg.get("shared_subnet_names") or [])
        if str(x).strip()
    ]
    existing_shared_subnets = spec.get("sharedSubnetNames") or []
    if not isinstance(existing_shared_subnets, list):
        existing_shared_subnets = []

    def compare_scalar(
        label: str,
        desired: Any,
        actual: Any,
        *,
        case_insensitive: bool = False,
    ) -> None:
        if case_insensitive:
            d = str(desired or "").strip().casefold()
            a = str(actual or "").strip().casefold()
        else:
            d = desired
            a = actual

        if d != a:
            differences.append(
                f"{label}: configured={desired!r}, existing={actual!r}"
            )

    compare_scalar(
        "project_name",
        desired_project,
        existing_project,
        case_insensitive=True,
    )
    compare_scalar(
        "class_name",
        desired_class,
        existing_class,
        case_insensitive=True,
    )
    compare_scalar(
        "region_name",
        desired_region,
        existing_region,
        case_insensitive=True,
    )
    compare_scalar(
        "description",
        desired_description,
        existing_description,
    )
    compare_scalar(
        "vpcName",
        desired_vpc,
        existing_vpc,
        case_insensitive=True,
    )

    if desired_shared_vpc is not None:
        compare_scalar(
            "sharedVpc",
            bool(desired_shared_vpc),
            bool(existing_shared_vpc),
        )

    compare_scalar(
        "connectivity_profile",
        "" if desired_connectivity_profile is None else str(desired_connectivity_profile).strip(),
        "" if existing_connectivity_profile is None else str(existing_connectivity_profile).strip(),
        case_insensitive=True,
    )

    compare_scalar(
        "private_subnet",
        "" if desired_private_subnet is None else str(desired_private_subnet).strip(),
        "" if existing_private_subnet is None else str(existing_private_subnet).strip(),
        case_insensitive=True,
    )

    if sorted(x.casefold() for x in desired_zones) != sorted(
        x.casefold() for x in existing_zones
    ):
        differences.append(
            f"zones: configured={desired_zones!r}, existing={existing_zones!r}"
        )

    if sorted(x.casefold() for x in desired_shared_subnets) != sorted(
        str(x).strip().casefold() for x in existing_shared_subnets
    ):
        differences.append(
            "shared_subnet_names: "
            f"configured={desired_shared_subnets!r}, "
            f"existing={existing_shared_subnets!r}"
        )

    info(
        f"Supervisor namespace '{namespace_name}' exists; "
        "comparing configuration"
    )

    if differences:
        for difference in differences:
            warn(
                f"Supervisor namespace '{namespace_name}' differs: "
                f"{difference}"
            )
        warn(
            f"Supervisor namespace '{namespace_name}' differs from JSON; "
            "no reconciliation performed"
        )
    else:
        info(
            f"Supervisor namespace '{namespace_name}' matches JSON; "
            "no reconciliation required"
        )

    return differences

def configure_organization_namespaces(
    client: RestClient,
    org_name: str,
    namespaces_cfg: Any,
) -> None:
    if not namespaces_cfg:
        return

    if not isinstance(namespaces_cfg, list):
        raise RuntimeError(
            f"namespaces for organization '{org_name}' must be an array"
        )

    cci = CciClient(client)

    for namespace_cfg in namespaces_cfg:
        if not isinstance(namespace_cfg, dict):
            raise RuntimeError(
                f"Each namespaces entry for organization '{org_name}' "
                "must be an object"
            )

        namespace_name = str(
            namespace_cfg.get("name", "") or ""
        ).strip()
        generate_name = str(
            namespace_cfg.get("generate_name", "") or ""
        ).strip()
        project_name = str(
            namespace_cfg.get("project_name", "") or ""
        ).strip()

        # `name` is optional. If present, use it only for exact lookup of an
        # existing namespace. If null/empty, skip lookup and let generate_name
        # control whether a new namespace is created.
        if not project_name:
            raise RuntimeError(
                f"Namespace '{namespace_name}' requires 'project_name'"
            )

        # `name` is ONLY for finding an existing namespace.
        # Check the configured project first, then every other project in the
        # organization by exact, case-insensitive metadata.name. This detects
        # namespaces that exist but are attached to a different project than
        # the JSON specifies.
        existing = None

        if namespace_name:
            existing = cci.find_supervisor_namespace(
                project_name,
                namespace_name,
            )

        if namespace_name and not existing:
            for candidate_project in organization_projects(client):
                candidate_name = str(
                    candidate_project.get("name", "") or ""
                ).strip()

                if not candidate_name:
                    continue

                if candidate_name.casefold() == project_name.casefold():
                    continue

                candidate = cci.find_supervisor_namespace(
                    candidate_name,
                    namespace_name,
                )

                if candidate:
                    existing = candidate
                    info(
                        f"Supervisor namespace '{namespace_name}' was found "
                        f"in project '{candidate_name}' rather than configured "
                        f"project '{project_name}'"
                    )
                    break

        if existing:
            compare_supervisor_namespace(
                namespace_name,
                namespace_cfg,
                existing,
            )
            continue

        # If the configured namespace does not exist, only create it when
        # generate_name has explicitly been supplied.
        if not generate_name:
            display_name = namespace_name or "<unnamed>"
            skip(
                f"Supervisor namespace '{display_name}' was not found in "
                f"project '{project_name}' and 'generate_name' is empty; "
                "creation skipped"
            )
            continue

        class_name = str(
            namespace_cfg.get("class_name", "") or ""
        ).strip()

        if not class_name:
            raise RuntimeError(
                f"Namespace '{namespace_name}' requires 'class_name' "
                "when 'generate_name' is configured"
            )

        # Creation-only dependencies are validated after the existing namespace
        # lookup, so pre-existing namespaces do not depend on current class
        # configuration being present in this JSON run.
        project = _project_by_name(client, project_name)
        if not project:
            raise RuntimeError(
                f"Cannot create namespace '{namespace_name}' because "
                f"project '{project_name}' does not exist in organization "
                f"'{org_name}'"
            )

        if cci.get_namespace_class(class_name) is None:
            raise RuntimeError(
                f"Cannot create namespace '{namespace_name}': "
                f"SupervisorNamespaceClass '{class_name}' was not found"
            )

        class_config = cci.get_namespace_class_config(class_name)
        if not class_config:
            raise RuntimeError(
                f"Cannot create namespace '{namespace_name}': "
                f"SupervisorNamespaceClassConfig '{class_name}' was not found"
            )

        payload = _supervisor_namespace_payload(
            namespace_cfg,
            class_config,
        )

        info(
            f"Supervisor namespace create payload for "
            f"'{namespace_name}':\n"
            f"{json.dumps(payload, indent=2)}"
        )

        create(
            f"Supervisor namespace using generateName '{generate_name}' "
            f"in project '{project_name}'"
        )

        created = cci.create_supervisor_namespace(
            project_name,
            payload,
        )

        actual_name = ""
        if isinstance(created, dict):
            actual_metadata = created.get("metadata") or {}
            if isinstance(actual_metadata, dict):
                actual_name = str(
                    actual_metadata.get("name", "") or ""
                ).strip()

        if actual_name:
            info(
                f"Supervisor namespace creation accepted; generated "
                f"Kubernetes name is '{actual_name}'"
            )

            # Once created, wait using the actual metadata.name returned by CCI.
            cci.wait_supervisor_namespace(
                project_name,
                actual_name,
            )

            info(
                f"Supervisor namespace '{actual_name}' verified in "
                f"project '{project_name}'"
            )
        else:
            info(
                f"Supervisor namespace creation accepted for generateName "
                f"'{generate_name}' in project '{project_name}'"
            )


# ============================================================
# Main orchestration
# Shared REST/CCI reconciliation primitives are defined above.
# ============================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("config", help="Path to JSON configuration")
    args = parser.parse_args()

    config_path = Path(args.config).expanduser().resolve()
    config_dir = config_path.parent
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Invalid JSON in '{config_path}' at line {exc.lineno}, "
            f"column {exc.colno}: {exc.msg}"
        ) from None

    validate_vcfa_configuration(config, config_dir)

    if (
        config.get("vcenter", {}).get("ignore_certificate_errors", False)
        or config.get("vcfa", {}).get("ignore_certificate_errors", False)
    ):
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    vc_cfg = config.get("vcenter") or {}
    vcfa_cfg = config.get("vcfa") or {}
    provider_cfg = vcfa_cfg.get("provider") or {}

    vc_si = None
    vc_rest = None
    if vc_cfg.get("server"):
        password = read_text_file(resolve_path(vc_cfg["password_file"], config_dir))
        ignore = bool(vc_cfg.get("ignore_certificate_errors", False))
        info(f"Connecting to vCenter {vc_cfg['server']}")
        vc_si = connect_vcenter(vc_cfg["server"], vc_cfg["username"], password, ignore)
        vc_rest = vcenter_rest_session(vc_cfg["server"], vc_cfg["username"], password, not ignore)

        vc_instance_uuid = str(getattr(vc_si.content.about, "instanceUuid", "") or "")
        if not vc_instance_uuid:
            raise RuntimeError("Unable to determine vCenter instance UUID required for VAPI tag-association DynamicIDs.")
        info(f"vCenter instance UUID: {vc_instance_uuid}")

        print("\n========================================")
        print(" vCenter Categories / Tags")
        print("========================================")

        tag_cfg_root = vc_cfg.get("tags") or {}

        for cat_cfg in tag_cfg_root.get("categories", []):
            vc_create_category_if_missing(vc_rest, cat_cfg)
            for tag_cfg in cat_cfg.get("tags", []):
                vc_create_tag_if_missing(vc_rest, cat_cfg["name"], tag_cfg)

        for assignment in tag_cfg_root.get("assignments", []):
            assignment_type = str(
                assignment.get("type", "") or ""
            ).strip()

            entity = find_vcenter_object(
                vc_si,
                assignment_type,
                assignment["name"],
            )

            # Normal VirtualMachine objects can be tagged through the vCenter
            # tagging API. Skip only Supervisor/VM Service managed VMs, which
            # expose the vmware-system-vm-uuid annotation in extraConfig and
            # must be managed through the Supervisor/VM Service path.
            if assignment_type.casefold() == "virtualmachine":
                is_supervisor_vm = False

                try:
                    for option in (entity.config.extraConfig or []):
                        key = str(getattr(option, "key", "") or "").casefold()
                        if key == "vmware-system-vm-uuid":
                            is_supervisor_vm = True
                            break
                except Exception:
                    pass

                if is_supervisor_vm:
                    skip(
                        f"'{assignment.get('name', '')}' is a Supervisor-based "
                        "VM; vCenter tag assignment is intentionally skipped"
                    )
                    continue
            moid = str(entity._moId)
            dynamic_type = assignment_type

            info(
                f"Resolved vCenter tagging target "
                f"'{assignment['name']}' -> "
                f"{vc_vapi_object_type(dynamic_type)}:{moid}"
            )

            for tag_cfg in assignment.get("tags", []):
                tag = vc_get_tag(
                    vc_rest,
                    tag_cfg["category"],
                    tag_cfg["tag"],
                )

                if not tag:
                    raise RuntimeError(
                        f"Tag '{tag_cfg['category']}/{tag_cfg['tag']}' "
                        "does not exist"
                    )

                tag_id = tag.get("tag") or tag.get("id")

                attached = vc_list_attached_tags(
                    vc_rest,
                    dynamic_type,
                    moid,
                    vc_instance_uuid,
                )

                if tag_id in attached:
                    skip(
                        f"'{assignment['name']}' already has "
                        f"'{tag_cfg['category']}/{tag_cfg['tag']}'"
                    )
                    continue

                create(
                    f"Assign '{tag_cfg['category']}/{tag_cfg['tag']}' "
                    f"to '{assignment['name']}'"
                )

                vc_attach_tag(
                    vc_rest,
                    tag_id,
                    dynamic_type,
                    moid,
                    vc_instance_uuid,
                )

                verify_deadline = time.time() + 30
                verified: List[str] = []

                while time.time() < verify_deadline:
                    verified = vc_list_attached_tags(
                        vc_rest,
                        dynamic_type,
                        moid,
                        vc_instance_uuid,
                    )

                    if str(tag_id) in {
                        str(value)
                        for value in verified
                    }:
                        break

                    time.sleep(2)

                if str(tag_id) not in {
                    str(value)
                    for value in verified
                }:
                    raise RuntimeError(
                        f"Tag assignment verification failed for "
                        f"'{assignment['name']}' -> "
                        f"'{tag_cfg['category']}/{tag_cfg['tag']}'. "
                        f"vCenter returned attached tag IDs: {verified}"
                    )

                info(
                    f"Assigned '{tag_cfg['category']}/{tag_cfg['tag']}' "
                    f"to '{assignment['name']}'"
                )

        print("\n========================================")
        print(" vCenter Compute Policies")
        print("========================================")
        for policy in vc_cfg.get("policies", []):
            vc_create_compute_policy_if_missing(vc_rest, policy)

    provider_client = None
    if vcfa_cfg.get("server") and provider_cfg:
        ignore = bool(vcfa_cfg.get("ignore_certificate_errors", False))
        token_file = resolve_path(provider_cfg["api_token_file"], config_dir)
        provider_client = vcfa_client(vcfa_cfg["server"], token_file, not ignore)

        regions_cfg = provider_cfg.get("regions") or {}
        if regions_cfg:
            if not isinstance(regions_cfg, dict):
                raise RuntimeError(
                    "vcfa.provider.regions must be an object keyed by region name"
                )

            print("\n========================================")
            print(" VCFA Regions")
            print("========================================")

            timeout = int(
                provider_cfg.get("region_sync_timeout_seconds", 600)
            )
            poll = int(
                provider_cfg.get("region_sync_poll_seconds", 5)
            )

            for region_name, region_cfg in regions_cfg.items():
                if not isinstance(region_cfg, dict):
                    raise RuntimeError(
                        f"vcfa.provider.regions['{region_name}'] must be an object"
                    )

                effective_region_cfg = dict(region_cfg)
                effective_region_cfg["name"] = region_name

                create_or_update_region(
                    provider_client,
                    effective_region_cfg,
                    timeout,
                    poll,
                )

        infra_cfg = provider_cfg.get("infrastructure_policies", [])
        if infra_cfg:
            print("\n========================================")
            print(" VCFA Infrastructure Policies")
            print("========================================")
            timeout = int(provider_cfg.get("compute_policy_sync_timeout_seconds", 600))
            poll = int(provider_cfg.get("compute_policy_sync_poll_seconds", 10))
            for policy in infra_cfg:
                if policy.get("vc_compute_policy_name"):
                    wait_vcfa_compute_policy(provider_client, policy["vc_compute_policy_name"], timeout, poll)
                create_infra_policy_if_missing(provider_client, policy)

        libraries_cfg = provider_cfg.get("provider_content_libraries") or {}
        if libraries_cfg:
            if not isinstance(libraries_cfg, dict):
                raise RuntimeError(
                    "vcfa.provider.provider_content_libraries must be an object "
                    "keyed by library name"
                )

            print("\n========================================")
            print(" VCFA Provider Content Libraries")
            print("========================================")

            # Provider content-library item creation requires
            # CONTENT_LIBRARY_ITEM_MANAGE. In VCFA 9.1 the provider API-token
            # session can be able to manage the library itself yet still be
            # denied when creating items. Use provider.local_auth when it is
            # configured so the complete provider content-library workflow runs
            # under the LOCAL provider/system session.
            provider_content_client = first_user_write_client(
                provider_client,
                vcfa_cfg["server"],
                provider_cfg,
                config_dir,
                not ignore,
            )

            if provider_content_client is provider_client:
                warn(
                    "vcfa.provider.local_auth is not configured; provider "
                    "content-library operations will use the provider API-token "
                    "session and may require CONTENT_LIBRARY_ITEM_MANAGE"
                )
            else:
                info(
                    "Provider content-library operations will use the LOCAL "
                    "provider/system session"
                )

            for library_name, lib_cfg in libraries_cfg.items():
                if not isinstance(lib_cfg, dict):
                    raise RuntimeError(
                        f"vcfa.provider.provider_content_libraries['{library_name}'] "
                        "must be an object"
                    )

                lib_cfg = dict(lib_cfg)
                lib_cfg["name"] = library_name
                is_subscribed = bool(lib_cfg.get("is_subscribed", False))
                subscription_url = str((lib_cfg.get("subscription") or {}).get("url", "") or "")
                if is_subscribed and not subscription_url.strip():
                    skip(f"Provider content library '{lib_cfg.get('name','')}' has is_subscribed=true but no usable subscription; skipping")
                    continue

                library = create_provider_library_if_missing(
                    provider_content_client,
                    lib_cfg,
                )
                if not library:
                    continue

                items = lib_cfg.get("items", []) or []
                if is_subscribed and subscription_url.strip():
                    if items:
                        skip(f"Ignoring {len(items)} local item(s) for subscribed content library '{lib_cfg.get('name','')}'")
                    continue

                for item_cfg in items:
                    upload_content_library_item(
                        provider_content_client,
                        library,
                        item_cfg,
                        config_dir,
                    )

        orgs_cfg = provider_cfg.get("organizations") or {}
        if orgs_cfg:
            if not isinstance(orgs_cfg, dict):
                raise RuntimeError(
                    "vcfa.provider.organizations must be an object keyed by organization name"
                )

            print("\n========================================")
            print(" VCFA Provider Organizations")
            print("========================================")

            for org_name, org_cfg in orgs_cfg.items():
                if not isinstance(org_cfg, dict):
                    raise RuntimeError(
                        f"vcfa.provider.organizations['{org_name}'] must be an object"
                    )

                effective_org_cfg = dict(org_cfg)
                effective_org_cfg["name"] = org_name

                tenant = create_provider_organization_if_missing(
                    provider_client,
                    effective_org_cfg,
                )

                if effective_org_cfg.get("first_user"):
                    effective_first_user_cfg = dict(
                        effective_org_cfg["first_user"]
                    )

                    create_token = bool(
                        effective_first_user_cfg.get(
                            "create_token",
                            False,
                        )
                    )

                    replace_token = bool(
                        effective_first_user_cfg.get(
                            "replace_token",
                            False,
                        )
                    )

                    # replace_token only makes sense when token creation is
                    # enabled for this run.
                    if replace_token and not create_token:
                        warn(
                            f"Organization '{org_name}' first_user has "
                            "replace_token=true but create_token=false; "
                            "replace_token will be ignored"
                        )

                    if create_token:
                        if not str(
                            effective_first_user_cfg.get(
                                "api_token_file",
                                "",
                            )
                            or ""
                        ).strip():
                            raise RuntimeError(
                                f"vcfa.provider.organizations['{org_name}']."
                                "first_user.api_token_file is required when "
                                "create_token=true"
                            )

                    create_first_user_if_missing(
                        provider_client,
                        vcfa_cfg["server"],
                        provider_cfg,
                        tenant,
                        effective_first_user_cfg,
                        config_dir,
                        not ignore,
                    )
                else:
                    raise RuntimeError(
                        f"vcfa.provider.organizations['{org_name}']."
                        "first_user is required for every tenant"
                    )

                region_quota = region_quota_if_missing(
                    provider_client,
                    tenant,
                    effective_org_cfg.get("region_quota") or {},
                )

                ext = effective_org_cfg.get("external_connection") or {}
                region_quota_cfg = effective_org_cfg.get("region_quota") or {}

                if ext and region_quota_cfg.get("region"):
                    region = vcfa_region(
                        provider_client,
                        region_quota_cfg["region"],
                    )

                    if region:
                        set_external_connection(
                            provider_client,
                            tenant,
                            region,
                            ext,
                        )

    org_configs = vcfa_cfg.get("organizations") or {}
    if org_configs:
        if not isinstance(org_configs, dict):
            raise RuntimeError(
                "vcfa.organizations must be an object keyed by organization name"
            )

        print("\n========================================")
        print(" Organization Configuration")
        print("========================================")

        if not vcfa_cfg.get("server"):
            raise RuntimeError(
                "vcfa.server is required for vcfa.organizations"
            )

        ignore = bool(
            vcfa_cfg.get("ignore_certificate_errors", False)
        )

        for name, org_cfg in org_configs.items():
            if not isinstance(org_cfg, dict):
                raise RuntimeError(
                    f"vcfa.organizations['{name}'] must be an object"
                )

            info(f"Configuring organization '{name}'")

            identity_cfg = (
                org_cfg.get("identity_providers") or {}
            )
            access_cfg = (
                org_cfg.get("access_control") or {}
            )

            configured_identity_providers = {
                provider_type: provider_cfg
                for provider_type, provider_cfg in (
                    identity_cfg.items()
                    if isinstance(identity_cfg, dict)
                    else []
                )
                if isinstance(provider_cfg, dict) and provider_cfg
            }

            has_identity_and_access = bool(
                configured_identity_providers
                and access_cfg
            )

            if has_identity_and_access:
                admin_cfg = organization_admin_config(
                    name,
                    org_cfg,
                )
                info(
                    f"Tenant '{name}' will bootstrap with its LOCAL first "
                    f"user, then switch to org_admin "
                    f"'{admin_cfg.get('username')}' after identity/access "
                    "configuration"
                )
            else:
                info(
                    f"Tenant '{name}' will use its LOCAL first user for "
                    "all organization configuration"
                )

            # Always begin tenant processing using the LOCAL first user and
            # its configured API token. This client is authoritative
            # for reviewing/configuring LDAP and access-control bootstrap.
            bootstrap_org_client = bootstrap_first_user_client(
                vcfa_cfg["server"],
                name,
                provider_cfg,
                config_dir,
                not ignore,
            )

            # Keep the LOCAL first-user client available for privileged
            # tenant bootstrap operations (notably content-library item
            # management). The general organization client may later switch
            # to the external org_admin after identity/access configuration.
            org_client = bootstrap_org_client

            for provider_type, provider_settings in (
                configured_identity_providers.items()
            ):
                normalized_type = str(
                    provider_type
                ).strip().lower()

                if normalized_type == "ldap":
                    identity = dict(provider_settings)
                    identity["type"] = "ldap"

                    configure_organization_ldap(
                        org_client,
                        name,
                        identity,
                        config_dir,
                    )

                elif normalized_type == "oidc":
                    warn(
                        f"OIDC identity provider configuration is present "
                        f"for '{name}', but OIDC processing is not "
                        "implemented yet"
                    )

                elif normalized_type == "saml":
                    warn(
                        f"SAML identity provider configuration is present "
                        f"for '{name}', but SAML processing is not "
                        "implemented yet"
                    )

                else:
                    raise RuntimeError(
                        f"Unsupported identity provider type "
                        f"'{provider_type}' for '{name}'"
                    )

            if access_cfg:
                print(
                    f"\n--- Access Control: {name} ---"
                )

                configure_organization_access_control(
                    org_client,
                    provider_client,
                    provider_cfg,
                    vcfa_cfg["server"],
                    config_dir,
                    not ignore,
                    name,
                    access_cfg,
                )

            # Once BOTH identity provider and access control have been
            # successfully reconciled, switch to the configured external
            # organization administrator for every remaining tenant task.
            #
            # If either section is absent, deliberately remain authenticated
            # as the local first user for the rest of the tenant.
            if has_identity_and_access:
                org_client = organization_admin_client(
                    org_client,
                    vcfa_cfg["server"],
                    name,
                    org_cfg,
                    config_dir,
                    not ignore,
                )

                info(
                    f"Identity provider and access control for '{name}' are "
                    "configured; all remaining tasks will run as "
                    f"org_admin "
                    f"'{organization_admin_config(name, org_cfg).get('username')}'"
                )
            else:
                info(
                    f"Organization '{name}' has no complete identity-provider "
                    "and access-control configuration; all remaining tasks "
                    "will continue as the local first user"
                )

            notifications_cfg = (
                org_cfg.get("notifications") or {}
            )

            email_server_cfg = (
                notifications_cfg.get("email_server") or {}
                if isinstance(notifications_cfg, dict)
                else {}
            )

            if email_server_cfg:
                validate_notification_email_json(
                    name,
                    email_server_cfg,
                )

                print(
                    f"\n--- Email Server: {name} ---"
                )

                configure_organization_email_server(
                    org_client,
                    name,
                    email_server_cfg,
                    config_dir,
                )

            organization_libraries_cfg = (
                org_cfg.get("content_libraries") or {}
            )

            if organization_libraries_cfg:
                print(
                    f"\n--- Content Libraries: {name} ---"
                )

                org_record = provider_organization(
                    provider_client,
                    name,
                )

                if not org_record:
                    raise RuntimeError(
                        f"Unable to resolve provider organization '{name}' "
                        "for organization content-library configuration"
                    )

                info(
                    f"Organization content-library operations for '{name}' "
                    "will use the LOCAL first-user client"
                )

                provider_org_cfg = (
                    (provider_cfg.get("organizations") or {}).get(name) or {}
                )

                configure_organization_content_libraries(
                    bootstrap_org_client,
                    provider_client,
                    org_record,
                    provider_org_cfg,
                    organization_libraries_cfg,
                    config_dir,
                )

            namespace_classes_cfg = org_cfg.get("namespaceClasses")
            if namespace_classes_cfg:
                print(f"\n--- Namespace Classes: {name} ---")

                # SupervisorNamespaceClass and SupervisorNamespaceClassConfig
                # are cluster-scoped CCI infrastructure resources. Tenant
                # organization administrators do not necessarily have RBAC to
                # get/create/update them. Always use the privileged VCFA LOCAL
                # provider session when provider.local_auth is configured.
                namespace_class_client = first_user_write_client(
                    provider_client,
                    vcfa_cfg["server"],
                    provider_cfg,
                    config_dir,
                    not ignore,
                )
                info(
                    f"Namespace class operations for '{name}' will use the "
                    "tenant org_admin client"
                )

                configure_organization_namespace_classes(
                    org_client,
                    name,
                    namespace_classes_cfg,
                )

            projects_cfg = org_cfg.get("projects") or []
            delete_default_project = bool(
                org_cfg.get("delete_default_project", False)
            )
            if projects_cfg or delete_default_project:
                print(f"\n--- Projects: {name} ---")
                configure_organization_projects(
                    org_client,
                    name,
                    projects_cfg,
                    delete_default_project=delete_default_project,
                )

            namespaces_cfg = org_cfg.get("namespaces") or []
            if namespaces_cfg:
                print(f"\n--- Namespaces: {name} ---")
                configure_organization_namespaces(
                    org_client,
                    name,
                    namespaces_cfg,
                )

            if org_cfg.get("blueprints"):
                warn(
                    f"blueprints configured for '{name}' but blueprint "
                    "processing is not implemented yet"
                )

            if org_cfg.get("catalog_items"):
                warn(
                    f"catalog_items configured for '{name}' but catalog "
                    "item processing is not implemented yet"
                )

            if org_cfg.get("policies"):
                warn(
                    f"policies configured for '{name}' but policy "
                    "processing is not implemented yet"
                )

            if org_cfg.get("deployments"):
                warn(
                    f"deployments configured for '{name}' but deployment "
                    "processing is not implemented yet"
                )

    if vc_rest:
        try:
            vc_rest.delete("/api/session")
        except Exception:
            pass

    if vc_si and Disconnect:
        Disconnect(vc_si)

    print("\nConfiguration completed successfully.")


if __name__ == "__main__":
    script_start_time = time.monotonic()
    print(f"[TIMER]  Script started at {time.strftime('%Y-%m-%d %H:%M:%S')}")
    try:
        main()
    finally:
        elapsed_seconds = int(time.monotonic() - script_start_time)
        hours, remainder = divmod(elapsed_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        print(
            f"\n[TIMER]  Total script runtime: "
            f"{hours:02d}:{minutes:02d}:{seconds:02d} "
            f"({elapsed_seconds} seconds)"
        )
