#!/usr/bin/env python3
import argparse
import subprocess
import sys

PASSWORD_URI = "op://jxs6qrivegu7ekpzkt27seurvy/gwarbmwzdfyk2t5iabrvz2b64q/password"


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Get an Ansible vault password from 1password CLI"
    )
    parser.add_argument(
        "--vault-id",
        action="store",
        default=None,
        dest="vault_id",
        help="name of the vault secret to get from keyring",
    )
    return parser


def get_password(path: str) -> str:
    return subprocess.run(
        ["op", "read", path],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout.strip()


def main() -> None:
    args = build_arg_parser().parse_args()

    assert args.vault_id is None

    print(get_password(PASSWORD_URI))


if __name__ == "__main__":
    main()
