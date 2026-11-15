"""Pure initial three-service lab recipe, not a launcher or admission decision.

Returns relative-path -> bytes inputs only. No directories, volumes, containers,
credentials or journals are read or written. A future admitted controller must
materialize private files/log directories and install the synthetic seed into a
fresh named volume before starting anything. That volume belongs to separately
observed guest Docker storage; it is not a generation-contained bind directory.

The fixed token and cookie values below are public synthetic test values. Gateway
responses cover admin, non-admin and default-deny cases, not live authentication.
Browser images can attempt provider traffic at startup; these inputs establish no
isolation, startup compatibility, Nginx acceptance or browser/provider acceptance.
Frozen-reference/probe fixtures, retained-resource admission and the full recovery
harness remain separate work. No candidate, approval or execution receipt is made.
"""

import copy

import network_lab_capacity as capacity
import network_policy as policy
import yaml

BRIDGE_TOKEN = "codex-lab-synthetic-bridge-token-not-a-secret"
COOKIE_NAME = "codex_lab_session"
ADMIN_COOKIE = "synthetic-admin-not-a-secret"
USER_COOKIE = "synthetic-user-not-a-secret"
COMPOSE = "configuration/docker-compose.yml"
NGINX = "configuration/nginx/nginx.conf"
GATEWAY = "configuration/gateway/nginx.conf"
FRAGMENT = "configuration/nginx/projects-locations.d/grok-bridge.conf"
SEED = "seed/fixture-generation.txt"
_MARKER = "# ── from projects-locations.d/grok-bridge.conf ──"
_NEXT_MARKER = "# ── from projects-locations.d/fixture-health.conf ──"
_HEADER = "# Synthetic lab input only; no execution or admission approval.\n"
_NGINX_PREFIX = (
    "worker_processes 1;\n"
    "pid /tmp/nginx.pid;\n"
    "error_log /dev/stderr warn;\n"
    "events { worker_connections 64; }\n"
    "http {\n"
    "    access_log off;\n"
    "    client_body_temp_path /tmp/client_temp;\n"
    "    proxy_temp_path /tmp/proxy_temp;\n"
)


class LabFixtureError(ValueError):
    """Sanitized refusal without caller-supplied identity or configuration values."""


def _bind(context, relative, target, *, read_only):
    return {
        "type": "bind",
        "source": str(context.generation_root / relative),
        "target": target,
        "read_only": read_only,
        "bind": {"create_host_path": False},
    }


def _service(context, role, image, memory):
    return {
        "image": image,
        "container_name": context.resource_map()[role],
        "pull_policy": "never",
        "restart": "no",
        "mem_limit": memory,
        "networks": {"default": {"aliases": [role]}},
        "logging": {"driver": "json-file", "options": {"max-size": "1m", "max-file": "1"}},
    }


def _compose(context):
    pins = dict(context.images)
    bridge = _service(context, policy.BRIDGE, pins["original"], "2g")
    bridge.update(
        cpus=2,
        shm_size="512m",
        environment={"BRIDGE_TOKEN": BRIDGE_TOKEN, "BRIDGE_PUBLIC_PATH": "/grok-bridge"},
        volumes=[{"type": "volume", "source": "original-data", "target": "/data"}],
        healthcheck={
            "test": [
                "CMD",
                "python",
                "-c",
                "import urllib.request; "
                "assert urllib.request.urlopen('http://127.0.0.1:8443/healthz',timeout=3).status==200",
            ],
            "interval": "5s",
            "timeout": "5s",
            "retries": 36,
            "start_period": "30s",
        },
    )
    services = {policy.BRIDGE: bridge}
    for role, configuration in (("nginx", NGINX), ("llm-proxy2", GATEWAY)):
        service = _service(context, role, pins["nginx"], "128m")
        service["volumes"] = [
            _bind(context, configuration, "/etc/nginx/nginx.conf", read_only=True),
            _bind(context, f"runtime/{role}-logs", "/var/log/nginx", read_only=False),
        ]
        services[role] = service
    return {
        "name": context.project,
        "services": services,
        "volumes": {
            "original-data": {"name": context.project + "-original-data", "driver": "local"}
        },
        "networks": {
            "default": {
                "name": context.resource_map()[policy.DEFAULT_NAME],
                "driver": "bridge",
                "internal": False,
            }
        },
    }


def _gateway():
    return (
        _HEADER + _NGINX_PREFIX + f"    map $cookie_{COOKIE_NAME} $fixture_role {{\n"
        "        default denied;\n"
        f'        "{ADMIN_COOKIE}" admin;\n'
        f'        "{USER_COOKIE}" user;\n'
        "    }\n"
        "    server {\n"
        "        listen 3000;\n"
        "        server_name grok-lab.invalid;\n"
        "        default_type application/json;\n"
        "        location = /api/auth/me {\n"
        "            if ($fixture_role = denied) { return 401; }\n"
        '            return 200 \'{"role":"$fixture_role","fixture":"synthetic"}\';\n'
        "        }\n"
        "        location = /fixture-health { return 200 'synthetic-gateway'; }\n"
        "        location / { return 404; }\n"
        "    }\n"
        "}\n"
    ).encode()


def _original_routes():
    """Original transport paths only; positive responses must come from the bridge."""
    return (
        "location = /grok-bridge { return 302 /grok-bridge/login; }\n"
        "location = /grok-bridge/vnc {\n"
        "    return 302 /grok-bridge/vnc/vnc.html?path=grok-bridge/vnc/websockify;\n"
        "}\n"
        "location = /grok-bridge/vnc/websockify {\n"
        f"    set $fixture_grok_host {policy.BRIDGE};\n"
        "    rewrite ^/grok-bridge/vnc/websockify$ /websockify break;\n"
        "    proxy_pass http://$fixture_grok_host:6080;\n"
        "    proxy_http_version 1.1;\n"
        "    proxy_set_header Host $host;\n"
        "    proxy_set_header Cookie $http_cookie;\n"
        "    proxy_set_header Origin $http_origin;\n"
        "    proxy_set_header Upgrade $http_upgrade;\n"
        '    proxy_set_header Connection "upgrade";\n'
        "    proxy_connect_timeout 3s;\n"
        "    proxy_read_timeout 30s;\n"
        "    proxy_buffering off;\n"
        "}\n"
        "location /grok-bridge/vnc/ {\n"
        f"    set $fixture_grok_host {policy.BRIDGE};\n"
        "    rewrite ^/grok-bridge/vnc/(.*)$ /$1 break;\n"
        "    proxy_pass http://$fixture_grok_host:6080;\n"
        "    proxy_set_header Host $host;\n"
        "    proxy_set_header Cookie $http_cookie;\n"
        "    proxy_connect_timeout 3s;\n"
        "    proxy_read_timeout 30s;\n"
        "}\n"
        "location /grok-bridge/ {\n"
        f"    set $fixture_grok_host {policy.BRIDGE};\n"
        "    rewrite ^/grok-bridge(/.*)$ $1 break;\n"
        "    proxy_pass http://$fixture_grok_host:8443;\n"
        "    proxy_http_version 1.1;\n"
        "    proxy_set_header Host $host;\n"
        "    proxy_set_header Cookie $http_cookie;\n"
        "    proxy_set_header Origin $http_origin;\n"
        "    proxy_set_header X-Bridge-Token $http_x_bridge_token;\n"
        "    proxy_set_header X-Forwarded-Proto $scheme;\n"
        "    proxy_connect_timeout 3s;\n"
        "    proxy_read_timeout 30s;\n"
        "}\n"
    )


def original_fixture_inputs(context, *, lab_binding):
    """Produce detached deterministic initial inputs; supplied binding is consistency only.

    Keys are relative to the exact generation. Config binds and future candidate
    data must be generation-contained. The seed is an input for later installation
    into the original named volume, not an automatically populated Compose mount.
    No source checkout, image availability, filesystem ownership or authority is
    observed here. The maintained candidate fragment is deliberately not copied.
    """
    try:
        ctx = capacity._context(context)
        ctx.verify_journal_binding(lab_binding)
        binding = copy.deepcopy(lab_binding)
        original = _original_routes()
        fragment = _HEADER + original
        compiled_fragment = "".join("            " + line + "\n" for line in fragment.splitlines())
        nginx = (
            _HEADER + _NGINX_PREFIX + "    resolver 127.0.0.11 ipv6=off;\n"
            "    server {\n"
            "        listen 8080;\n"
            "        server_name grok-lab.invalid;\n"
            f"        {_MARKER}\n"
            f"{compiled_fragment}\n"
            f"        {_NEXT_MARKER}\n"
            "        location = /fixture-health { return 200 'synthetic-nginx'; }\n"
            "    }\n"
            "}\n"
        )
        result = {
            COMPOSE: (_HEADER + yaml.safe_dump(_compose(ctx), sort_keys=False)).encode(),
            NGINX: nginx.encode(),
            GATEWAY: _gateway(),
            FRAGMENT: fragment.encode(),
            SEED: ("synthetic-original-generation\ncontext_sha256=" + ctx.digest() + "\n").encode(),
        }
        ctx.verify_journal_binding(lab_binding)
        if capacity._context(context) != ctx or not capacity._exact(binding, lab_binding):
            raise LabFixtureError("Fixture context or retained binding changed")
        return result
    except LabFixtureError:
        raise
    except Exception:  # noqa: BLE001 -- malformed input diagnostics may contain private identities.
        raise LabFixtureError(
            "Synthetic fixture inputs require an exact lab context and binding"
        ) from None
