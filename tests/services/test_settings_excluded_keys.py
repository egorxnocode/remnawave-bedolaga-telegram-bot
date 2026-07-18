"""Security: identity / auth secrets must not be editable via the settings API.

Privilege-escalation guard. PUT /cabinet/admin/settings/{key} only requires the
`settings:edit` permission, and the settings service exposes every model field
except EXCLUDED_KEYS. ADMIN_EMAILS grants admin (settings.is_admin), and the JWT
/ web-api / webhook secrets let an attacker forge sessions and requests — none of
these may be writable by a delegated admin, or they self-promote to superadmin.
"""

from __future__ import annotations

import pytest

from app.services.system_settings_service import BotConfigurationService


EXCLUDED_AUTH_KEYS = [
    'ADMIN_EMAILS',
    'CABINET_JWT_SECRET',
    'WEB_API_DEFAULT_TOKEN',
    'WEB_API_TOKEN_HMAC_SECRET',
    'WEBHOOK_SECRET_TOKEN',
]

EXCLUDED_SAFETY_KEYS = [
    'AI_SUPPORT_MODE',
    'AI_SUPPORT_WORKER_ENABLED',
    'AI_SUPPORT_WORKER_POLL_SECONDS',
    'AI_SUPPORT_WORKER_SHUTDOWN_SECONDS',
    'AI_SUPPORT_PROMPT_VERSION',
    'AI_SUPPORT_KB_VERSION',
    'AI_SUPPORT_ANTHROPIC_API_KEY',
    'AI_SUPPORT_MODEL_ID',
    'AI_SUPPORT_PROVIDER_TIMEOUT_SECONDS',
    'AI_SUPPORT_PROVIDER_MAX_CONCURRENCY',
    'AI_SUPPORT_PROVIDER_MAX_TOKENS',
    'AI_SUPPORT_PROVIDER_MAX_RETRIES',
    'AI_SUPPORT_PROVIDER_CIRCUIT_FAILURES',
    'AI_SUPPORT_PROVIDER_CIRCUIT_RESET_SECONDS',
]


def test_identity_and_auth_secrets_are_excluded() -> None:
    for key in EXCLUDED_AUTH_KEYS:
        assert key in BotConfigurationService.EXCLUDED_KEYS, f'{key} must be in EXCLUDED_KEYS'


def test_excluded_keys_have_no_editable_definition() -> None:
    # No definition -> admin_settings update_setting's get_definition() raises
    # KeyError -> 404, so a settings:edit admin cannot write these keys.
    for key in EXCLUDED_AUTH_KEYS:
        with pytest.raises(KeyError):
            BotConfigurationService.get_definition(key)


def test_ai_support_mode_is_environment_only() -> None:
    for key in EXCLUDED_SAFETY_KEYS:
        assert key in BotConfigurationService.EXCLUDED_KEYS
        with pytest.raises(KeyError):
            BotConfigurationService.get_definition(key)
