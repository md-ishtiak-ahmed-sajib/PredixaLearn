"""Regression checks for deployment-only lockfile entries."""

from pathlib import Path


def test_uvicorn_standard_linux_dependency_is_pinned_with_hashes() -> None:
    """Keep the Linux-only uvloop dependency valid for hashed production installs."""
    lockfile = Path(__file__).resolve().parents[1] / "requirements.lock"
    contents = lockfile.read_text(encoding="utf-8")

    assert "uvicorn[standard]==0.51.0" in contents
    assert (
        'uvloop==0.22.1 ; sys_platform != "win32" and sys_platform != "cygwin" '
        'and platform_python_implementation != "PyPy"'
    ) in contents
    assert "--hash=sha256:6c84bae345b9147082b17371e3dd5d42775bddce91f885499017f4607fdaf39f" in contents
    assert "--hash=sha256:7b5b1ac819a3f946d3b2ee07f09149578ae76066d70b44df3fa990add49a82e4" in contents
