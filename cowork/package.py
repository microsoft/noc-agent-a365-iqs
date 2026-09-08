"""Build the Cowork sideload package without mutating the manifest template."""

from __future__ import annotations

import argparse
import json
import os
import uuid
import zipfile
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
PLACEHOLDERS = {
    "${MCP_URL}": "mcp_url",
    "${AUTH_CONFIG_REFERENCE_ID}": "auth_config_reference_id",
    "${MANIFEST_ID}": "manifest_id",
}


def _value(argument: str | None, env_name: str) -> str:
    value = (argument or os.getenv(env_name, "")).strip()
    if not value:
        raise ValueError(f"Provide --{env_name.lower().replace('_', '-')} or set {env_name}.")
    return value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mcp-url")
    parser.add_argument("--auth-config-reference-id")
    parser.add_argument("--manifest-id")
    parser.add_argument("--output", type=Path, default=ROOT / "build" / "noc-cowork.zip")
    args = parser.parse_args()

    values = {
        "mcp_url": _value(args.mcp_url, "COWORK_MCP_URL").rstrip("/"),
        "auth_config_reference_id": _value(
            args.auth_config_reference_id,
            "COWORK_AUTH_CONFIG_REFERENCE_ID",
        ),
        "manifest_id": _value(args.manifest_id, "COWORK_MANIFEST_ID"),
    }
    parsed_url = urlparse(values["mcp_url"])
    if parsed_url.scheme != "https" or not parsed_url.netloc or parsed_url.path != "/mcp":
        raise ValueError("COWORK_MCP_URL must be an HTTPS canonical URL ending in /mcp.")
    uuid.UUID(values["manifest_id"])
    if len(values["auth_config_reference_id"]) > 128:
        raise ValueError("COWORK_AUTH_CONFIG_REFERENCE_ID exceeds the manifest's 128-character limit.")

    manifest_text = (ROOT / "manifest.json").read_text(encoding="utf-8")
    for placeholder, key in PLACEHOLDERS.items():
        manifest_text = manifest_text.replace(placeholder, values[key])
    if "${" in manifest_text:
        raise ValueError("The resolved manifest still contains a placeholder.")
    manifest = json.loads(manifest_text)

    skill_files = [
        ROOT / item["folder"].removeprefix("./") / "SKILL.md"
        for item in manifest.get("agentSkills", [])
    ]
    required_files = [
        ROOT / "color.png",
        ROOT / "outline.png",
        ROOT / "tools" / "noc-mcp-tools.json",
        *skill_files,
    ]
    missing = [str(path.relative_to(ROOT)) for path in required_files if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"Missing package files: {', '.join(missing)}")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.output, "w", compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr("manifest.json", json.dumps(manifest, indent=2) + "\n")
        for path in required_files:
            archive_name = path.name if path.parent == ROOT / "tools" else path.relative_to(ROOT).as_posix()
            package.write(path, archive_name)
    print(args.output.resolve())


if __name__ == "__main__":
    main()
