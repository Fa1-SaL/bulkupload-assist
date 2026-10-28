"""Issue owner-run Apps Script upload codes in private local files."""
import argparse
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets


def _write_private(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as file:
        file.write(payload)
        file.write('\n')


def bridge_secret(output: Path) -> None:
    _write_private(output, secrets.token_urlsafe(48))
    print(f'Bridge secret saved privately to {output}.')


def derive_access_code(user: str, secret: str) -> str:
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', user):
        raise ValueError('Use a short user label with letters, numbers, _ or -.')
    secret = secret.rstrip('\r\n')
    if len(secret) < 32:
        raise ValueError('The owner secret is missing or too short.')
    key = hmac.new(secret.encode('utf-8'),
                   ('bulk-upload-code-v1:' + user).encode('utf-8'),
                   hashlib.sha256).hexdigest()
    return user + ':' + key


def issue_code(user: str, secret_file: Path, output: Path) -> None:
    code = derive_access_code(user, secret_file.read_text(encoding='utf-8'))
    _write_private(output, code)
    print(f'Private access code for {user} saved to {output}. Share only with that person.')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    bridge = sub.add_parser('bridge-secret', help='Create the owner-only Apps Script secret')
    bridge.add_argument('--output', type=Path, default=Path('bridge_secret.txt'))
    code = sub.add_parser('issue-code', help='Create one private, revocable user code')
    code.add_argument('user')
    code.add_argument('--secret', type=Path, default=Path('bridge_secret.txt'))
    code.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.action == 'bridge-secret':
        bridge_secret(args.output)
    else:
        issue_code(args.user, args.secret, args.output)


if __name__ == '__main__':
    main()
