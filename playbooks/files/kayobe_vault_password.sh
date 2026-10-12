#!/bin/sh
# Vault password script for the generator playbooks: prints $KAYOBE_VAULT_PASSWORD like
# kayobe-vault-password-helper, without needing the kayobe venv on PATH.
if [ -z "${KAYOBE_VAULT_PASSWORD:-}" ]; then
    echo "KAYOBE_VAULT_PASSWORD is not set" >&2
    exit 1
fi
printf '%s\n' "$KAYOBE_VAULT_PASSWORD"
