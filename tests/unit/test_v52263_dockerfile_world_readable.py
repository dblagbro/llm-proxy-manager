"""v5.22.63: the image must make all copied source readable by the non-root user.

app/resources/ arrived from the NFS tree as mode 0774, so appuser could not import
it (ModuleNotFoundError at runtime). The Dockerfile now normalises permissions.
"""

from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parents[2] / "Dockerfile"


def test_dockerfile_normalises_source_permissions():
    text = DOCKERFILE.read_text()
    assert "chmod -R a+rX /app/app" in text
    assert text.index("chmod -R a+rX /app/app") < text.index("USER appuser")
