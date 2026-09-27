"""Fixed advisory boundary and Node MCP host launcher.

The base package remains dependency-free on Python 3.9+. The separately
installed official Node SDK host reaches it only through a private bridge.
"""

import hashlib
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any
from pathlib import Path

from .review import (DEFAULT_MAX_BYTES, DEFAULT_MAX_ITEMS, build_review_batch,
                     import_review_payload, _json_bytes, _trim_bundle)
from .chatgpt_policy import cache_key, cache_status

TOOL_NAMES = ("get_review_batch", "submit_review_batch")
HOST_MANIFEST_FILE = "docs/mcp-advisory-host.json"
NODE_HOST_FILE = "mcp_host/megaprog-mcp-host.mjs"
NODE_VERIFY_FILE = "mcp_host/verify.mjs"
TAILSCALE_BIN = "tailscale"

GET_REVIEW_BATCH_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "max_items": {"type": "integer", "minimum": 1},
        "max_bytes": {"type": "integer", "minimum": 256},
    },
}

SUBMIT_REVIEW_BATCH_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["contract_version", "task_id", "task_hash", "bundle_id",
                 "repo_state_hash", "advisory"],
    "properties": {
        "contract_version": {"type": "string", "minLength": 1},
        "task_id": {"type": "string", "minLength": 1},
        "task_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "bundle_id": {"type": "string", "pattern": "^[0-9a-f]{24}$"},
        "repo_state_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "advisory": {},
    },
}

TOOL_DEFINITIONS = (
    {"name": "get_review_batch", "description":
     "Return a bounded, read-only MegaProg advisory review batch.",
     "input_schema": GET_REVIEW_BATCH_SCHEMA},
    {"name": "submit_review_batch", "description":
     "Store one validated untrusted advisory response for an issued review batch.",
     "input_schema": SUBMIT_REVIEW_BATCH_SCHEMA},
)


def _canonical_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def host_manifest():
    """Return the fixed, SDK-neutral contract a future official host must meet."""
    tools = []
    for definition in TOOL_DEFINITIONS:
        tools.append({"name": definition["name"],
                      "description": definition["description"],
                      # MCP uses inputSchema; the dependency-free adapter keeps
                      # input_schema because that is its explicit host bridge.
                      "inputSchema": definition["input_schema"]})
    unsigned = {
        "manifest_version": 1,
        "name": "megaprog-advisory",
        "transport": {"required": "streamable-http", "path": "/mcp"},
        "official_sdk": {"node": "@modelcontextprotocol/sdk>=1,<2"},
        "tools": tools,
        "forbidden_capabilities": ["shell", "git", "filesystem", "source",
                                   "model", "browser", "network", "write"],
        "persistence": "Only validated untrusted advisory artifacts may be written.",
    }
    return dict(unsigned, contract_sha256=hashlib.sha256(_canonical_bytes(unsigned)).hexdigest())


def verify_host_manifest(path):
    """Fail closed unless the checked-in host contract is exactly canonical."""
    candidate = Path(path)
    if candidate.is_symlink() or not candidate.is_file():
        raise ValueError("MCP advisory host manifest is missing or is not a regular file")
    try:
        actual = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("MCP advisory host manifest is not valid UTF-8 JSON: %s" % exc)
    if actual != host_manifest():
        raise ValueError("MCP advisory host manifest does not match the fixed safe contract")
    return {"path": str(candidate), "sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
            "tools": list(TOOL_NAMES)}


def _runtime_probe():
    """Probe the explicit Node dependency only; never install or use a network."""
    probe = {"python_version": ".".join(str(value) for value in sys.version_info[:3]),
             "node": False, "node_sdk": False, "sdk": "@modelcontextprotocol/sdk>=1,<2"}
    try:
        version = subprocess.run(["node", "--version"], text=True, capture_output=True,
                                 timeout=5, check=True).stdout.strip()
        probe["node"] = version
        loaded = subprocess.run(["node", "--input-type=module", "-e",
                                 "import('@modelcontextprotocol/sdk/server/mcp.js')"],
                                cwd=str(Path(__file__).parents[1]), text=True,
                                capture_output=True, timeout=10)
        probe["node_sdk"] = loaded.returncode == 0
    except (OSError, subprocess.SubprocessError):
        pass
    return probe


class MCPRuntimeUnavailable(RuntimeError):
    """Raised instead of guessing at an MCP runtime or adding a dependency."""


def runtime_status(root=None):
    """Report facts only; never install, discover through the network, or start SDKs."""
    probe = _runtime_probe()
    available = bool(probe["node_sdk"])
    status = {"available": available, "tools": list(TOOL_NAMES), "probe": probe,
              "api": False, "browser_automation": False, "manifest": None,
              "transport": {"kind": "streamable-http", "path": "/mcp"},
              "bind": {"host": "127.0.0.1", "port": 8000}}
    if root is not None:
        manifest = Path(root) / HOST_MANIFEST_FILE
        try:
            status["manifest"] = verify_host_manifest(manifest)
        except ValueError as exc:
            status["manifest"] = {"path": str(manifest), "valid": False, "error": str(exc)}
    if available:
        status["reason"] = "The official Node MCP SDK is available for the Streamable HTTP advisory host."
    elif not probe["node"]:
        status["reason"] = "BLOCKED: Node.js 20+ and the explicit optional MCP host dependency are required."
    else:
        status["reason"] = ("BLOCKED: Node is present but the explicit optional @modelcontextprotocol/sdk "
                            "dependency is not installed. Run npm install; no API fallback, browser "
                            "automation, or network fallback was started.")
    return status


def discover_mcp_tools(root):
    """Run the official Node SDK's real loopback discovery probe, fail closed."""
    status = runtime_status(root)
    if not status["available"]:
        raise MCPRuntimeUnavailable(status["reason"])
    command = ["node", str(Path(root) / NODE_VERIFY_FILE), "--root", str(Path(root).resolve())]
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=30, check=True)
        discovered = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise MCPRuntimeUnavailable("local Node MCP discovery failed: %s" % exc) from exc
    if discovered != {"tools": list(TOOL_NAMES), "resources": [], "prompts": []}:
        raise ValueError("MCP discovery did not expose exactly the fixed advisory contract")
    discovered["forbidden_capabilities"] = host_manifest()["forbidden_capabilities"]
    return discovered


def run_mcp_host(root, host="127.0.0.1", port=8000):
    """Block while serving the documented Streamable HTTP endpoint at ``/mcp``."""
    if not isinstance(host, str) or not host:
        raise ValueError("MCP bind host must be a non-empty string")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("MCP bind port must be an integer from 1 through 65535")
    status = runtime_status(root)
    if not status["available"]:
        raise MCPRuntimeUnavailable(status["reason"])
    command = ["node", str(Path(root) / NODE_HOST_FILE), "--root", str(Path(root).resolve()),
               "--host", host, "--port", str(port)]
    subprocess.run(command, check=True)


def _command_json(command, timeout=8):
    """Run a local diagnostic command and return JSON without retaining stderr."""
    result = subprocess.run(command, text=True, capture_output=True,
                            timeout=timeout, check=False)
    try:
        value = json.loads(result.stdout)
    except (ValueError, TypeError):
        value = None
    return result.returncode, value


def _tailscale_help(binary):
    result = subprocess.run([binary, "funnel", "--help"], text=True,
                            capture_output=True, timeout=8, check=False)
    return result.returncode == 0, " ".join((result.stdout or "").split())


def diagnose_deployment(root):
    """Read-only local preflight for a stable HTTPS Tailscale deployment."""
    status = {
        "state": "BLOCKED", "client": {"available": False},
        "login": {"state": "unknown"}, "hostname": None,
        "https": {"capable": False}, "funnel": {"capable": False},
        "endpoint_template": None, "next_step": "Install Tailscale and log in; no public listener was changed.",
        "mutations": [],
    }
    binary = shutil.which(TAILSCALE_BIN)
    if not binary:
        status["next_step"] = "Install the official Tailscale client, run `tailscale up`, then rerun mcp-advisory deploy-diagnose."
        return status
    status["client"] = {"available": True, "path": binary}
    try:
        code, version = _command_json([binary, "version"], timeout=5)
        status["client"]["version"] = version if code == 0 and isinstance(version, str) else "installed"
    except (OSError, subprocess.SubprocessError):
        status["client"]["version"] = "installed"
    try:
        code, data = _command_json([binary, "status", "--json"])
    except (OSError, subprocess.SubprocessError) as exc:
        status["login"] = {"state": "unavailable", "reason": type(exc).__name__}
        status["next_step"] = "Start the local Tailscale service and log in, then rerun mcp-advisory deploy-diagnose."
        return status
    if code != 0 or not isinstance(data, dict):
        status["login"] = {"state": "not-logged-in"}
        status["next_step"] = "Run `tailscale up` interactively, then rerun mcp-advisory deploy-diagnose."
        return status
    self_info = data.get("Self") if isinstance(data.get("Self"), dict) else {}
    backend = data.get("BackendState")
    online = self_info.get("Online")
    logged_in = backend in (None, "Running") and online is not False
    status["login"] = {"state": "ready" if logged_in else "not-logged-in"}
    dns_name = self_info.get("DNSName") or self_info.get("HostName")
    if isinstance(dns_name, str) and dns_name:
        dns_name = dns_name.rstrip(".")
        status["hostname"] = dns_name
        status["https"] = {"capable": True, "reason": "Tailscale DNS name is available"}
        status["endpoint_template"] = "https://%s/mcp" % dns_name
    funnel_help = False
    try:
        funnel_help, _ = _tailscale_help(binary)
        funnel_code, funnel_data = _command_json([binary, "funnel", "status", "--json"])
        funnel_present = funnel_code == 0 and isinstance(funnel_data, (dict, list))
    except (OSError, subprocess.SubprocessError):
        funnel_present = False
    status["funnel"] = {"capable": bool(funnel_help and logged_in and status["hostname"]),
                         "configured": bool(funnel_present)}
    if not logged_in:
        status["next_step"] = "Run `tailscale up` interactively, then rerun mcp-advisory deploy-diagnose."
    elif not status["hostname"]:
        status["next_step"] = "Enable a Tailscale DNS name in the admin console, then rerun mcp-advisory deploy-diagnose."
    elif not funnel_help:
        status["next_step"] = "Update the official Tailscale client with Funnel support, then rerun this diagnostic."
    else:
        status["state"] = "READY"
        status["next_step"] = "Review the public-risk prompt, then explicitly run `mcp-advisory funnel-start --confirm-public`."
    return status


def _healthcheck(port, timeout=1):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=timeout) as response:
            payload = json.loads(response.read(4096).decode("utf-8"))
            return response.status == 200 and payload.get("ok") is True and payload.get("endpoint") == "/mcp"
    except (OSError, ValueError, urllib.error.URLError):
        return False


def start_funnel(root, port=8000, confirm_public=False):
    """Explicitly run a loopback host behind Tailscale Funnel until stopped."""
    if not confirm_public:
        raise ValueError("Funnel is public; repeat with --confirm-public to continue")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("MCP bind port must be an integer from 1 through 65535")
    deployment = diagnose_deployment(root)
    if deployment["state"] != "READY":
        return dict(deployment, next_step=deployment["next_step"], state="BLOCKED")
    binary = shutil.which(TAILSCALE_BIN)
    host_command = ["node", str(Path(root) / NODE_HOST_FILE), "--root", str(Path(root).resolve()),
                    "--host", "127.0.0.1", "--port", str(port)]
    host_process = None
    funnel_process = None
    try:
        host_process = subprocess.Popen(host_command, cwd=str(root),
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        text=True)
        for _ in range(40):
            if host_process.poll() is not None:
                raise MCPRuntimeUnavailable("local MCP host exited before health check")
            if _healthcheck(port):
                break
            time.sleep(0.1)
        else:
            raise MCPRuntimeUnavailable("local MCP host did not pass /health before Funnel start")
        funnel_process = subprocess.Popen([binary, "funnel", "--https=443",
                                           "http://127.0.0.1:%d" % port],
                                          cwd=str(root), stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL, text=True)
        print(json.dumps({"state": "PUBLIC", "url": deployment["endpoint_template"],
                          "health": "ok", "stop": "Ctrl-C stops Funnel and the local host."}, ensure_ascii=False))
        funnel_process.wait()
        return {"state": "STOPPED", "url": deployment["endpoint_template"]}
    finally:
        if funnel_process is not None:
            if funnel_process.poll() is None:
                funnel_process.terminate()
                try:
                    funnel_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    funnel_process.kill()
                    funnel_process.wait(timeout=5)
            subprocess.run([binary, "funnel", "off"], cwd=str(root),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=8, check=False)
        if host_process is not None and host_process.poll() is None:
            host_process.terminate()
            try:
                host_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                host_process.kill()
                host_process.wait(timeout=5)


def _options(arguments):
    if arguments is None:
        return DEFAULT_MAX_ITEMS, DEFAULT_MAX_BYTES
    if not isinstance(arguments, dict) or set(arguments) - {"max_items", "max_bytes"}:
        raise ValueError("get_review_batch accepts only max_items and max_bytes")
    max_items = arguments.get("max_items", DEFAULT_MAX_ITEMS)
    max_bytes = arguments.get("max_bytes", DEFAULT_MAX_BYTES)
    if type(max_items) is not int or type(max_bytes) is not int:
        raise ValueError("get_review_batch limits must be integers")
    return max_items, max_bytes


class AdvisoryGateway:
    """Small in-memory capability boundary for exactly two advisory operations."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self._issued = {}
        # Session-local only: a restart intentionally drops advisory content.
        # Cache data is never promoted to an instruction or a Codex result.
        self._cache = {}

    def get_review_batch(self, arguments=None):
        max_items, max_bytes = _options(arguments)
        batch = build_review_batch(self.root, max_items, max_bytes)
        original_items = len(batch["items"])
        uncached = []
        for item in batch["items"]:
            status = cache_status(self._cache, item["task_hash"], batch["repo_state_hash"])
            if status in ("exact_hit", "negative_hit"):
                continue
            item["cache_status"] = status
            uncached.append(item)
        batch["items"] = uncached
        batch["limits"]["selected_items"] = len(uncached)
        batch["limits"]["cached_items"] = original_items - len(uncached)
        batch["bundle_id"] = ""
        batch["bundle_id"] = hashlib.sha256(_json_bytes(batch)).hexdigest()[:24]
        batch = _trim_bundle(batch, batch["limits"]["max_bytes"])
        # Only hashes and IDs are retained.  A gateway restart deliberately
        # invalidates outstanding submissions instead of persisting session data.
        self._issued[batch["bundle_id"]] = {
            "bundle_id": batch["bundle_id"], "items": [
                {"task_id": item["task_id"], "task_hash": item["task_hash"]}
                for item in batch["items"]
            ],
        }
        return batch

    def submit_review_batch(self, response):
        if not isinstance(response, dict):
            raise ValueError("submit_review_batch requires a JSON object")
        issued = self._issued.get(response.get("bundle_id"))
        if issued is None:
            raise ValueError("review response does not match an issued batch")
        artifact = import_review_payload(self.root, response, issued)
        self._cache[cache_key(response["task_hash"], response["repo_state_hash"])] = {
            "task_hash": response["task_hash"], "repo_state_hash": response["repo_state_hash"],
            "negative": isinstance(response.get("advisory"), dict) and
                        response["advisory"].get("negative") is True,
        }
        return artifact


def register_advisory_tools(root, register_tool):
    """Register the only permitted tools through an official host's bridge.

    ``register_tool`` is supplied by the host rather than imported or guessed
    here. It must accept ``name``, ``description``, ``input_schema`` and
    ``handler`` keyword arguments. The returned gateway must stay alive while
    the host serves requests because issued batch IDs are held in memory.
    """
    if not callable(register_tool):
        raise TypeError("register_tool must be the documented MCP/Apps SDK host bridge")
    gateway = AdvisoryGateway(root)
    handlers = (gateway.get_review_batch, gateway.submit_review_batch)
    for definition, handler in zip(TOOL_DEFINITIONS, handlers):
        register_tool(name=definition["name"], description=definition["description"],
                      input_schema=definition["input_schema"], handler=handler)
    return gateway
