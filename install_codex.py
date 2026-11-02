"""Install the pinned, integrity-checked official Codex Linux runtime."""
import base64
import hashlib
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
from urllib.request import urlopen


VERSION = "0.114.0"
PACKAGES = {
    "amd64": ("x64", "MF6RhxfBoccccd5CYII2C7TL2TvYzombBQhanDZfQAajr6n7IuoazCmoHDlrFHS4AcSw9bYA4MRlrwEjbEjgsw=="),
    "arm64": ("arm64", "oUFZGZgMiEqmo85C+dfhckpVApH25alLWfwBgfKr2IRgdx/tovy8vN101f6kj01E6nDDe4LfJdNSZGtZht4fRA=="),
}


def install(architecture, destination):
    platform, expected = PACKAGES[architecture]
    url = f"https://registry.npmjs.org/@openai/codex/-/codex-{VERSION}-linux-{platform}.tgz"
    with tempfile.TemporaryFile() as package:
        digest = hashlib.sha512()
        size = 0
        with urlopen(url, timeout=60) as response:
            while block := response.read(1024 * 1024):
                size += len(block)
                if size > 200_000_000:
                    raise ValueError("Codex package exceeds download limit")
                digest.update(block)
                package.write(block)
        if base64.b64encode(digest.digest()).decode() != expected:
            raise ValueError("Codex package integrity verification failed")
        package.seek(0)
        with tarfile.open(fileobj=package, mode="r:gz") as archive:
            root = Path(destination)
            root.mkdir(parents=True, exist_ok=True)
            installed = set()
            for member in archive.getmembers():
                parts = Path(member.name).parts
                if not member.isfile() or len(parts) < 5 or parts[:2] != ("package", "vendor"):
                    continue
                relative = Path(*parts[3:])
                if ".." in relative.parts or str(relative) not in ("codex/codex", "path/rg"):
                    continue
                if str(relative) == "codex/codex":
                    relative = Path("bin/codex")
                elif str(relative) == "path/rg":
                    relative = Path("bin/rg")
                if relative.parts[0] != "bin":
                    continue
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source, path.open("wb") as target:
                    shutil.copyfileobj(source, target)
                path.chmod(0o755)
                installed.add(str(relative))
            if "bin/codex" not in installed:
                raise ValueError("Unexpected Codex package contents")



if __name__ == "__main__":
    install(sys.argv[1], sys.argv[2])
