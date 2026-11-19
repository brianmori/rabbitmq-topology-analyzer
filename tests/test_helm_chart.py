"""Exercise Helm's real renderer for supported and rejected configurations."""

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "charts/rabbitmq-topology-analyzer"
RUNTIME_UID = 10001
SECRET_FILE_MODE = 0o440
ALTERNATIVE_SERVICE_PORT = 8080


def render(values: dict[str, Any], release: str = "test") -> subprocess.CompletedProcess[str]:
    """Render values through stdin without writing credential-shaped artifacts."""
    return subprocess.run(
        ["helm", "template", release, str(CHART), "--namespace", "topology", "--values", "-"],
        input=json.dumps(values),
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )


def resources(values: dict[str, Any], release: str = "test") -> dict[str, Any]:
    """Index the chart's single resource of each kind after a successful render."""
    result = render(values, release)
    assert result.returncode == 0, result.stderr
    documents = list(yaml.safe_load_all(result.stdout))
    assert len({item["kind"] for item in documents}) == len(documents)
    return {item["kind"]: item for item in documents}


def test_default_deployment_preserves_process_ownership_and_capacity() -> None:
    """Verify the deployable default's state, security, storage, and probe wiring."""
    manifests = resources({})
    assert set(manifests) == {"Deployment", "ConfigMap", "Service"}
    deployment = manifests["Deployment"]
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"] == {"type": "Recreate"}
    template = deployment["spec"]["template"]
    pod = template["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["fsGroup"] == RUNTIME_UID
    [container] = pod["containers"]
    assert container["command"] == ["python", "-m", "uvicorn", "main:app"]
    assert container["args"] == ["--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
    assert container["image"] == "ghcr.io/brianmori/rabbitmq-topology-analyzer:0.1.0"
    assert container["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }
    config = manifests["ConfigMap"]
    assert container["envFrom"] == [{"configMapRef": {"name": config["metadata"]["name"]}}]
    settings = config["data"]
    assert all(isinstance(value, str) for value in settings.values())
    assert settings["RABBITMQ_DEPLOYMENT_MODE"] == "production"
    assert settings["RABBITMQ_AUTH_MODE"] == "none"
    assert settings["RABBITMQ_ANALYSIS_WORKERS"] == "2"
    assert "RABBITMQ_FULL_EXPORT_DEFINITIONS_DIR" not in settings
    assert not any("BASIC_" in key or "OIDC_" in key for key in settings)
    mounts = {mount["name"]: mount for mount in container["volumeMounts"]}
    assert mounts["analysis-results"]["mountPath"] == settings["RABBITMQ_ANALYSIS_RESULTS_DIR"]
    assert mounts["runtime-tmp"]["mountPath"] == "/tmp"
    volumes = {volume["name"]: volume for volume in pod["volumes"]}
    result_volume = volumes["analysis-results"]["emptyDir"]
    temporary_volume = volumes["runtime-tmp"]["emptyDir"]
    assert "medium" not in result_volume and "medium" not in temporary_volume
    assert int(settings["RABBITMQ_ANALYSIS_RESULTS_MAX_BYTES"]) < int(result_volume["sizeLimit"])
    assert int(result_volume["sizeLimit"]) + int(temporary_volume["sizeLimit"]) < int(
        container["resources"]["limits"]["ephemeral-storage"]
    )
    for name, path in [
        ("startupProbe", "ready"),
        ("readinessProbe", "ready"),
        ("livenessProbe", "live"),
    ]:
        assert container[name]["httpGet"] == {"path": f"/api/health/{path}", "port": "http"}
    selector = deployment["spec"]["selector"]["matchLabels"]
    assert manifests["Service"]["spec"]["selector"] == selector == template["metadata"]["labels"]


@pytest.mark.parametrize("mode", ["basic", "oidc"])
def test_authentication_projects_only_selected_external_secret_keys(mode: str) -> None:
    """Keep credential material out of Helm releases and provider namespaces exclusive."""
    manifests = resources(
        {
            "auth": {
                "mode": mode,
                "publicOrigin": "https://TOPOLOGY.example.invalid:443",
                "existingSecret": "external-auth",
                "basic": {"usersKey": "custom-users"},
                "oidc": {
                    "issuer": "https://issuer.example.invalid",
                    "clientId": "topology",
                    "roleClaim": "roles",
                    "clientSecretKey": "custom-client",
                    "roleMappingKey": "custom-roles",
                },
            }
        }
    )
    assert "Secret" not in manifests
    settings = manifests["ConfigMap"]["data"]
    assert settings["RABBITMQ_PUBLIC_ORIGIN"] == "https://topology.example.invalid"
    pod = manifests["Deployment"]["spec"]["template"]["spec"]
    [secret] = [volume["secret"] for volume in pod["volumes"] if "secret" in volume]
    assert secret["secretName"] == "external-auth"
    assert secret["defaultMode"] == SECRET_FILE_MODE
    [mount] = [
        item
        for item in pod["containers"][0]["volumeMounts"]
        if item["name"] == "authentication-files"
    ]
    assert mount["readOnly"] is True
    if mode == "basic":
        assert secret["items"] == [{"key": "custom-users", "path": "users.json"}]
        assert settings["RABBITMQ_BASIC_USERS_FILE"] == f"{mount['mountPath']}/users.json"
        assert not any("OIDC_" in key for key in settings)
    else:
        assert secret["items"] == [
            {"key": "custom-client", "path": "client-secret"},
            {"key": "custom-roles", "path": "role-mapping.json"},
        ]
        assert settings["RABBITMQ_OIDC_CLIENT_SECRET_FILE"] == f"{mount['mountPath']}/client-secret"
        assert (
            settings["RABBITMQ_OIDC_ROLE_MAPPING_FILE"] == f"{mount['mountPath']}/role-mapping.json"
        )
        assert (
            settings["RABBITMQ_OIDC_REDIRECT_URI"]
            == "https://topology.example.invalid/api/auth/oidc/callback"
        )
        assert not any("BASIC_" in key for key in settings)


@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        ("https://TOPOLOGY.example.invalid:0443", "https://topology.example.invalid"),
        ("https://xn--bndel-kva.example:08443", "https://xn--bndel-kva.example:8443"),
        ("https://[2001:db8::1]:0443", "https://[2001:db8::1]"),
        ("https://[2001:db8::1234]", "https://[2001:db8::1234]"),
    ],
)
def test_oidc_callback_uses_the_same_origin_as_the_backend(origin: str, expected: str) -> None:
    """Keep the generated callback and canonical public origin in agreement."""
    settings = resources(_oidc_values(origin))["ConfigMap"]["data"]
    assert settings["RABBITMQ_PUBLIC_ORIGIN"] == expected
    assert settings["RABBITMQ_OIDC_REDIRECT_URI"] == f"{expected}/api/auth/oidc/callback"


@pytest.mark.parametrize(
    ("origin", "message"),
    [
        ("https://bündel.example", "use Punycode for internationalized names"),
        ("https://topology.example.invalid:65536", "port must be between 1 and 65535"),
    ],
)
def test_invalid_oidc_origin_fails_with_actionable_render_error(origin: str, message: str) -> None:
    """Reject inputs that cannot produce a usable callback URI."""
    result = render(_oidc_values(origin))
    assert result.returncode != 0
    assert message in result.stderr


def _oidc_values(origin: str) -> dict[str, Any]:
    return {
        "auth": {
            "mode": "oidc",
            "publicOrigin": origin,
            "existingSecret": "external-auth",
            "oidc": {
                "issuer": "https://issuer.example.invalid",
                "clientId": "topology",
                "roleClaim": "roles",
            },
        }
    }


def test_static_capture_ingress_digest_and_release_isolation() -> None:
    """Render operator overrides and verify mounted inputs and traffic select this release."""
    digest = "sha256:" + "a" * 64
    manifests = resources(
        {
            "captures": {"existingClaim": "external-captures"},
            "image": {"digest": digest, "tag": "ignored"},
            "imagePullSecrets": [{"name": "registry-auth"}],
            "service": {"port": 8080},
            "ingress": {
                "enabled": True,
                "className": "example",
                "host": "topology.example.invalid",
                "tlsSecretName": "external-tls",
                "annotations": {"example.invalid/body-limit": "55m"},
            },
        },
        "another",
    )
    pod = manifests["Deployment"]["spec"]["template"]["spec"]
    [capture] = [v for v in pod["volumes"] if "persistentVolumeClaim" in v]
    assert capture["persistentVolumeClaim"] == {"claimName": "external-captures", "readOnly": True}
    [mount] = [m for m in pod["containers"][0]["volumeMounts"] if m["name"] == capture["name"]]
    assert mount["readOnly"] is True
    assert (
        mount["mountPath"] == manifests["ConfigMap"]["data"]["RABBITMQ_FULL_EXPORT_DEFINITIONS_DIR"]
    )
    assert pod["imagePullSecrets"] == [{"name": "registry-auth"}]
    assert pod["containers"][0]["image"].endswith("@" + digest)
    service = manifests["Service"]
    assert service["spec"]["ports"][0]["port"] == ALTERNATIVE_SERVICE_PORT
    ingress = manifests["Ingress"]["spec"]
    assert ingress["ingressClassName"] == "example"
    assert ingress["tls"] == [{"hosts": ["topology.example.invalid"], "secretName": "external-tls"}]
    assert ingress["rules"][0]["http"]["paths"][0]["backend"]["service"] == {
        "name": service["metadata"]["name"],
        "port": {"name": "http"},
    }
    other = resources({})["Service"]
    assert service["metadata"]["name"] != other["metadata"]["name"]
    assert service["spec"]["selector"] != other["spec"]["selector"]


def test_configuration_change_rolls_the_single_state_owner() -> None:
    """Require a new pod template when environment settings change."""
    before = resources({})["Deployment"]["spec"]["template"]["metadata"]["annotations"]
    after = resources({"analysis": {"workers": 3}})["Deployment"]["spec"]["template"]["metadata"][
        "annotations"
    ]
    assert before["checksum/config"] != after["checksum/config"]


@pytest.mark.parametrize(
    "values",
    [
        {"replicaCount": 2},
        {"autoscaling": {"enabled": True}},
        {"extraEnv": []},
        {"analysis": {"workers": 0}},
        {"analysis": {"resultTtlSeconds": 14401}},
        {"auth": {"mode": "legacy"}},
        {"auth": {"mode": "basic"}},
        {
            "auth": {
                "mode": "basic",
                "publicOrigin": "http://example.invalid",
                "existingSecret": "auth",
            }
        },
        {
            "auth": {
                "mode": "oidc",
                "publicOrigin": "https://example.invalid",
                "existingSecret": "auth",
            }
        },
        {"ingress": {"enabled": True}},
        {"storage": {"resultsMaxBytes": 0}},
        {"storage": {"resultsMaxBytes": 4294967296}},
        {"storage": {"ephemeralLimitBytes": 4294967296}},
        {"storage": {"ephemeralRequestBytes": 9000000000}},
    ],
)
def test_invalid_or_unsupported_values_fail_before_deployment(values: dict[str, Any]) -> None:
    """Exercise schema and cross-field capacity validation through Helm itself."""
    result = render(values)
    assert result.returncode != 0
    assert result.stderr
