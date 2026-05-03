#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PS_SCRIPT_WIN="$(wslpath -w "$SCRIPT_DIR/run-local.ps1")"

usage() {
    cat <<'EOF'
Usage:
  bash tests/infra/run-local.sh --vm-name NAME --checkpoint NAME [options]

Required:
  --vm-name NAME
  --checkpoint NAME

Options:
  --scenario scaffold|installer   Default: installer
  --run-playwright                Run Playwright browser checks after smoke-test
  --installer-path PATH           Optional; defaults to latest dist/windows-native installer
  --guest-username NAME           If provided, run-local.ps1 prompts for the password
  --guest-password VALUE          Test-only plaintext password; avoids the prompt
  --skip-checkpoint-restore
  --keep-vm-running
  --help

Example:
  bash tests/infra/run-local.sh \
    --vm-name "Windows 11 dev environment" \
    --checkpoint "clean-slate-2" \
    --scenario scaffold \
    --guest-username user
EOF
}

VM_NAME=""
CHECKPOINT_NAME=""
SCENARIO="installer"
INSTALLER_PATH=""
GUEST_USERNAME=""
GUEST_PASSWORD=""
RUN_PLAYWRIGHT=0
SKIP_RESTORE=0
KEEP_VM_RUNNING=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --vm-name)
            VM_NAME="${2:-}"
            shift 2
            ;;
        --checkpoint)
            CHECKPOINT_NAME="${2:-}"
            shift 2
            ;;
        --scenario)
            SCENARIO="${2:-}"
            shift 2
            ;;
        --installer-path)
            INSTALLER_PATH="${2:-}"
            shift 2
            ;;
        --guest-username)
            GUEST_USERNAME="${2:-}"
            shift 2
            ;;
        --guest-password)
            GUEST_PASSWORD="${2:-}"
            shift 2
            ;;
        --run-playwright)
            RUN_PLAYWRIGHT=1
            shift
            ;;
        --skip-checkpoint-restore)
            SKIP_RESTORE=1
            shift
            ;;
        --keep-vm-running)
            KEEP_VM_RUNNING=1
            shift
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            echo "Unknown argument: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

if [[ -z "$VM_NAME" || -z "$CHECKPOINT_NAME" ]]; then
    usage >&2
    exit 1
fi

case "$SCENARIO" in
    scaffold|installer) ;;
    *)
        echo "Invalid scenario: $SCENARIO" >&2
        exit 1
        ;;
esac

ARGS=(
    -NoProfile
    -ExecutionPolicy Bypass
    -File "$PS_SCRIPT_WIN"
    -VmName "$VM_NAME"
    -CheckpointName "$CHECKPOINT_NAME"
    -Scenario "$SCENARIO"
)

if [[ -n "$INSTALLER_PATH" ]]; then
    ARGS+=(-InstallerPath "$(wslpath -w "$INSTALLER_PATH")")
fi

if [[ -n "$GUEST_USERNAME" ]]; then
    ARGS+=(-GuestUsername "$GUEST_USERNAME")
fi

if [[ -n "$GUEST_PASSWORD" ]]; then
    export MSA_E2E_GUEST_PASSWORD="$GUEST_PASSWORD"
    if [[ -n "${WSLENV:-}" ]]; then
        case ":$WSLENV:" in
            *:MSA_E2E_GUEST_PASSWORD:*) ;;
            *) export WSLENV="MSA_E2E_GUEST_PASSWORD:$WSLENV" ;;
        esac
    else
        export WSLENV="MSA_E2E_GUEST_PASSWORD"
    fi
fi

if [[ "$RUN_PLAYWRIGHT" -eq 1 ]]; then
    ARGS+=(-RunPlaywright)
fi

if [[ "$SKIP_RESTORE" -eq 1 ]]; then
    ARGS+=(-SkipCheckpointRestore)
fi

if [[ "$KEEP_VM_RUNNING" -eq 1 ]]; then
    ARGS+=(-KeepVmRunning)
fi

exec powershell.exe "${ARGS[@]}"
