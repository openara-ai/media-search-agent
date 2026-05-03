import argparse
import os
import sys

from . import cmd_api, cmd_index, cmd_status


def main():
    ap = argparse.ArgumentParser(
        prog="msa",
        description="Media Search Agent — index media, manage the API, and more.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Commands:
  status      Show install, service, and index status
  index       Index media files, export to Qdrant, create backups
  api         Start, stop, restart, and check the status of the API server
  uninstall   Remove Media Search Agent (installed builds only)

Run 'msa <command> --help' for command-specific help.
        """,
    )
    sp = ap.add_subparsers(dest="group", metavar="COMMAND")

    cmd_status.register(sp)
    cmd_index.register(sp)
    cmd_api.register(sp)
    sp.add_parser("uninstall", help="Remove Media Search Agent (installed builds only)")

    args = ap.parse_args()

    if not args.group:
        ap.print_help()
        sys.exit(0)

    if args.group == "status":
        cmd_status.handle(args)
    elif args.group == "index":
        cmd_index.handle(args)
    elif args.group == "api":
        cmd_api.handle(args)
    elif args.group == "uninstall":
        _cmd_uninstall()


def _cmd_uninstall() -> None:
    msa_root = os.environ.get("MSA_ROOT")
    if not msa_root:
        print(
            "Error: MSA_ROOT is not set.\n"
            "Run 'msa uninstall' via the installed launcher (~/.local/bin/msa), "
            "not directly from the venv.",
            file=sys.stderr,
        )
        sys.exit(1)
    uninstall_sh = os.path.join(msa_root, "uninstall.sh")
    if not os.path.isfile(uninstall_sh):
        print(f"Error: uninstall.sh not found at {uninstall_sh}", file=sys.stderr)
        sys.exit(1)
    os.execv("/bin/bash", ["/bin/bash", uninstall_sh])
