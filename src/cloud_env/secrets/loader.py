from __future__ import annotations

from cloud_env.lib.log import log
from cloud_env.secrets.broker import fetch_broker_env
from cloud_env.secrets.onepassword import load_1password

# Kept out of the secrets exported to the session: git identity variables override the per-owner
# identity in git config, and gh and git get their GitHub tokens from this script
SESSION_ENV_EXCLUDED = {
    "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL",
    "GITHUB_TOKEN", "GH_TOKEN", "GITHUB_APP_TOKENS", "BOTICORN_TOKENS",
}  # fmt: skip


def load_secrets() -> tuple[dict[str, str], bool]:
    """Returns the session's secrets, and whether both the broker and 1Password delivered."""
    broker = fetch_broker_env()
    if broker is None:
        log("broker: continuing without secrets")
        return {}, False
    token = broker.pop("OP_SERVICE_ACCOUNT_TOKEN", None)
    environment_id = broker.pop("OP_ENVIRONMENT_ID", None)
    log(f"broker: received {len(broker)} variables")
    if not (token and environment_id):
        log("1password: broker sent no token or environment ID, skipping")
        return broker, False
    onepassword = load_1password(token, environment_id)
    if onepassword is None:
        return broker, False
    # Broker values win, so a broker-minted token replaces a long-lived one from 1Password
    return onepassword | broker, True
