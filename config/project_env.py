import os
from pathlib import Path
from typing import Dict

from dotenv import dotenv_values


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DOTENV_PATH = _PROJECT_ROOT / ".env"
_PREVIOUS_DOTENV_VALUES: Dict[str, str] = {}
_ALIASED_ENV_VALUES: Dict[str, str] = {}
_ENV_NAME_ALIASES = {
    "prod": "console",
}
_LEGACY_ENV_ALIASES = {
    "AI_BASE_URL_CONSOLE": "AI_BASE_URL_PROD",
}


def _read_dotenv_values() -> Dict[str, str]:
    if not _DOTENV_PATH.exists():
        return {}
    # Accept UTF-8 with BOM so an editor-added BOM does not break keys like `ENV`.
    parsed_values = dotenv_values(_DOTENV_PATH, encoding="utf-8-sig")
    return {
        str(key): str(value)
        for key, value in parsed_values.items()
        if key and value is not None
    }


def normalize_env_name(value: str, default: str = "dev") -> str:
    normalized = str(value or "").strip().lower()
    if not normalized:
        normalized = default
    return _ENV_NAME_ALIASES.get(normalized, normalized)


def resolve_effective_env(value: str, default: str = "dev") -> str:
    return normalize_env_name(value, default=default)


def get_explicit_api_base_url(target_env: str) -> str:
    generic_base_url = str(os.getenv("AI_BASE_URL", "")).strip()
    if generic_base_url:
        return generic_base_url.rstrip("/")

    normalized_env = normalize_env_name(target_env, default="dev")
    if normalized_env == "console":
        return str(os.getenv("AI_BASE_URL_CONSOLE", os.getenv("AI_BASE_URL_PROD", ""))).strip().rstrip("/")
    return str(os.getenv("AI_BASE_URL_DEV", "")).strip().rstrip("/")


def _sync_legacy_env_aliases(current_values: Dict[str, str]) -> None:
    global _ALIASED_ENV_VALUES

    previous_aliased = _ALIASED_ENV_VALUES
    new_aliased: Dict[str, str] = {}

    for target_key, source_key in _LEGACY_ENV_ALIASES.items():
        if target_key in current_values:
            continue

        source_value = os.environ.get(source_key)
        current_target_value = os.environ.get(target_key)
        previous_alias_value = previous_aliased.get(target_key)

        if source_value:
            if current_target_value is None or (
                previous_alias_value is not None and current_target_value == previous_alias_value
            ):
                os.environ[target_key] = source_value
                new_aliased[target_key] = source_value
            continue

        if previous_alias_value is not None and current_target_value == previous_alias_value:
            os.environ.pop(target_key, None)

    _ALIASED_ENV_VALUES = new_aliased


def reload_project_env() -> Dict[str, str]:
    global _PREVIOUS_DOTENV_VALUES

    current_values = _read_dotenv_values()
    previous_values = _PREVIOUS_DOTENV_VALUES

    for key, old_value in previous_values.items():
        if key in current_values:
            continue
        if os.environ.get(key) == old_value:
            os.environ.pop(key, None)

    for key, new_value in current_values.items():
        current_env_value = os.environ.get(key)
        previous_value = previous_values.get(key)

        if current_env_value is None:
            os.environ[key] = new_value
            continue
        if previous_value is not None and current_env_value == previous_value:
            os.environ[key] = new_value

    _sync_legacy_env_aliases(current_values)
    _PREVIOUS_DOTENV_VALUES = current_values
    return dict(current_values)
