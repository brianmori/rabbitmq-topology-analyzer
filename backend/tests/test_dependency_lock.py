import re
import shutil
import subprocess
import tomllib
from importlib.metadata import distribution
from pathlib import Path

import rabbitmq_management_sdk

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def test_sdk_and_third_party_dependencies_use_hashed_registry_packages() -> None:
    project = tomllib.loads((BACKEND_ROOT / "pyproject.toml").read_text())
    lock = tomllib.loads((BACKEND_ROOT / "uv.lock").read_text())

    assert "sources" not in project.get("tool", {}).get("uv", {})
    assert "rabbitmq-management-sdk==0.5.0" in project["project"]["dependencies"]
    assert "httpx2>=2.16.0,<3" in project["project"]["dependencies"]
    assert not {"httpx", "httpcore"} & {package["name"] for package in lock["package"]}
    assert "hatchling==1.32.4" in project["dependency-groups"]["build"]
    for package in lock["package"]:
        if package["name"] == project["project"]["name"]:
            continue
        assert package["source"] == {"registry": "https://pypi.org/simple"}
        artifacts = [*package.get("wheels", [])]
        if "sdist" in package:
            artifacts.append(package["sdist"])
        assert artifacts
        assert all(re.fullmatch(r"sha256:[0-9a-f]{64}", item["hash"]) for item in artifacts)
        if package["name"] == "rabbitmq-management-sdk":
            assert package["version"] == "0.5.0"
            assert package["sdist"]["hash"] == (
                "sha256:f34b795f7a815ac235f597d6f7da06ffbb6bc9c9313108030b99b1bbdb82ad35"
            )
            assert [wheel["hash"] for wheel in package["wheels"]] == [
                "sha256:2afd5ba1f4773fddc35064fac0e0450848c70fe4392065a60a9b7405b68ab015",
            ]


def test_installed_sdk_matches_the_registry_pin_without_a_local_override() -> None:
    installed = distribution("rabbitmq-management-sdk")
    assert installed.version == "0.5.0"
    assert installed.read_text("direct_url.json") is None
    installed_package = Path(installed.locate_file("rabbitmq_management_sdk")).resolve()
    assert "site-packages" in installed_package.parts
    assert Path(rabbitmq_management_sdk.__file__).resolve().is_relative_to(installed_package)


def copy_dependency_files(tmp_path: Path) -> Path:
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copyfile(BACKEND_ROOT / name, tmp_path / name)
    return tmp_path


def test_shared_lock_is_current_without_a_sibling_checkout(tmp_path: Path) -> None:
    root = copy_dependency_files(tmp_path)
    original = (root / "uv.lock").read_bytes()

    subprocess.run(["uv", "lock", "--check", "--offline", "--project", str(root)], check=True)

    assert (root / "uv.lock").read_bytes() == original


def test_native_lock_check_rejects_requirement_drift_without_rewriting(tmp_path: Path) -> None:
    root = copy_dependency_files(tmp_path)
    project_path = root / "pyproject.toml"
    project_path.write_text(project_path.read_text().replace("httpx2>=2.16.0,<3", "httpx2==2.13.0"))
    original = (root / "uv.lock").read_bytes()

    result = subprocess.run(
        ["uv", "lock", "--check", "--offline", "--project", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert (root / "uv.lock").read_bytes() == original
