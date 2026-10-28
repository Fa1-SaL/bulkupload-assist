"""Owner-only local setup. Never commit the output files or share their contents."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets

def _write_private(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        file.write(payload)
        file.write("\n")


def bridge_secret(output: Path) -> None:
    _write_private(output, secrets.token_urlsafe(48))
    print(f"Bridge secret saved to {output} with owner-only file permissions. Keep it private.")


def new_code(username: str, current: Path, output: Path) -> None:
    if not username or any(c in username for c in "\n\r"):
        raise ValueError("Provide a short user name")
    codes = json.loads(current.read_text(encoding="utf-8")) if current.exists() else {}
    if not isinstance(codes, dict) or username in codes:
        raise ValueError("User already exists or code file is invalid")
    code = secrets.token_urlsafe(32)
    codes[username] = hashlib.sha256(code.encode("utf-8")).hexdigest()
    _write_private(output, json.dumps(codes, indent=2, sort_keys=True))
    print(f"Access code for {username}: {code}")
    print(f"Hash list saved to {output}. Share only this user's code with them privately.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    bridge = sub.add_parser("bridge-secret", help="Create a private Apps Script bridge secret")
    bridge.add_argument("--output", type=Path, default=Path("bridge_secret.txt"))
    code = sub.add_parser("new-code", help="Create one high-entropy user access code")
    code.add_argument("username")
    code.add_argument("--current", type=Path, default=Path("upload_codes.json"))
    code.add_argument("--output", type=Path, required=True,
                      help="A new private JSON file; replace the old file after reviewing it")
    args = parser.parse_args()
    if args.action == "bridge-secret":
        bridge_secret(args.output)
    else:
        new_code(args.username, args.current, args.output)


if __name__ == "__main__":
    main()
