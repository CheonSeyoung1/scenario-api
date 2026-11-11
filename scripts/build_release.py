"""실행 파일과 문서만 포함한 재현 가능한 ZIP 릴리스를 만든다."""
import hashlib
from pathlib import Path
import re
import sys
import zipfile


ROOT = Path(__file__).resolve().parents[1]
FILES = ("README.md", "Tools/SCENARIO_REST.md", "Tools/scenario_api.py", "Tools/scenario_transfer.py")


def main():
    version = sys.argv[1] if len(sys.argv) == 2 else ""
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", version):
        raise SystemExit("Usage: python3 scripts/build_release.py vMAJOR.MINOR.PATCH")
    output = ROOT / "dist"
    output.mkdir(exist_ok=True)
    name = "scenario-cli-" + version
    archive = output / (name + ".zip")
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for filename in FILES:
            entry = zipfile.ZipInfo(name + "/" + filename)
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            bundle.writestr(entry, (ROOT / filename).read_bytes())
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (output / "SHA256SUMS").write_text(digest + "  " + archive.name + "\n", encoding="utf-8")
    print(archive)


if __name__ == "__main__":
    main()
