#!/bin/sh
# Vault password script for the generator playbooks.
# Prints $KAYOBE_VAULT_PASSWORD like kayobe-vault-password-helper,
# without needing the kayobe venv on PATH.
# ansible-vault runs an executable password file and reads its stdout.
if [ -z "${KAYOBE_VAULT_PASSWORD:-}" ]; then
    echo "KAYOBE_VAULT_PASSWORD is not set" >&2
    exit 1
fi
printf '%s\n' "$KAYOBE_VAULT_PASSWORD"
