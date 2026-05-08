import os
from pathlib import Path
from typing import Any, Dict

import yaml
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


def _first_nonempty(*values: Any) -> str:
    for value in values:
        normalized = str(value or "").strip()
        if normalized:
            return normalized
    return ""


def _load_context_env_config(target_env: str) -> Dict[str, Any]:
    config_path = os.path.join(os.path.dirname(__file__), "env.yaml")
    all_configs = _read_yaml_file(config_path)
    common_config = all_configs.get("common", {})
    env_config = all_configs.get(target_env, {})
    merged = {**common_config, **env_config}
    return _resolve_placeholders(merged)


def _resolve_placeholders(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _resolve_placeholders(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_placeholders(item) for item in value]
    if isinstance(value, str):
        import re

        pattern = r"\$\{([^}:]+)(?::([^}]*))?\}"

        def replace(match):
            env_var = match.group(1)
            default_value = match.group(2)
            env_value = os.getenv(env_var)
            if env_value is not None:
                return env_value
            if default_value is not None:
                return default_value
            return match.group(0)

        return re.sub(pattern, replace, value)
    return value


def _read_yaml_file(file_path: str) -> Dict[str, Any]:
    if not os.path.exists(file_path):
        return {}
    with open(file_path, "r", encoding="utf-8") as file:
        return yaml.safe_load(file) or {}


def _to_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def load_context_runtime(target_env: str) -> Dict[str, Any]:
    reload_project_env()
    normalized_env = resolve_effective_env(str(target_env or "").strip().lower(), default="dev")
    if normalized_env not in {"dev", "console"}:
        raise ValueError(f"context target_env only supports dev/console, got: {target_env}")

    env_config = _load_context_env_config(normalized_env)
    chat_defaults = env_config.get("chat_defaults", {})
    env_key = normalized_env.upper()

    chat_account = _first_nonempty(
        env_config.get("chat_account"),
        os.getenv(f"CHAT_ACCOUNT_{env_key}"),
        os.getenv(f"CONTEXT_{env_key}_CHAT_ACCOUNT"),
        os.getenv("CHAT_ACCOUNT"),
        chat_defaults.get("account"),
        "测试专用1",
    )
    platform = str(chat_defaults.get("platform", "tmall")).strip() or "tmall"
    api_base_url = get_explicit_api_base_url(normalized_env) or str(env_config.get("api_base_url", "")).rstrip("/")
    shop_id = _first_nonempty(
        os.getenv(f"CHAT_SHOP_ID_{env_key}"),
        os.getenv(f"CONTEXT_{env_key}_SHOP_ID"),
    )
    shop_name = _first_nonempty(
        os.getenv(f"CHAT_SHOP_NAME_{env_key}"),
        os.getenv(f"CONTEXT_{env_key}_SHOP_NAME"),
        f"shop_{shop_id}" if shop_id else "",
    )
    access_token = _first_nonempty(
        env_config.get("access_token"),
        os.getenv(f"ACCESS_TOKEN_{env_key}"),
        os.getenv("CONTEXT_CONSOLE_ACCESS_TOKEN") if normalized_env == "console" else "",
        os.getenv("CONTEXT_PROD_ACCESS_TOKEN") if normalized_env == "console" else "",
    )
    login_account = _first_nonempty(
        env_config.get("login_account"),
        os.getenv(f"LOGIN_ACCOUNT_{env_key}"),
        os.getenv(f"CONTEXT_{env_key}_LOGIN_ACCOUNT"),
        os.getenv("LOGIN_ACCOUNT"),
    )
    login_password = _first_nonempty(
        env_config.get("login_password"),
        os.getenv(f"LOGIN_PASSWORD_{env_key}"),
        os.getenv(f"CONTEXT_{env_key}_LOGIN_PASSWORD"),
        os.getenv("LOGIN_PASSWORD"),
    )
    auth_mode = "token" if normalized_env == "console" and access_token else "login"

    runtime = {
        "target_env": normalized_env,
        "auth_mode": auth_mode,
        "api_base_url": api_base_url,
        "headers": dict(env_config.get("headers", {})),
        "chat_account": chat_account,
        "platform": platform,
        "is_test": False if normalized_env == "console" else _to_bool(chat_defaults.get("is_test"), True),
        "shop_id": shop_id,
        "shop_name": shop_name,
        "access_token": access_token,
        "console_token": access_token if normalized_env == "console" else "",
        "login_account": login_account,
        "login_password": login_password,
    }

    if not runtime["api_base_url"]:
        raise ValueError(f"context runtime missing api_base_url for env: {normalized_env}")
    if not runtime["shop_id"]:
        raise ValueError(f"context runtime missing shop_id for env: {normalized_env}")
    if auth_mode == "login" and (not runtime["login_account"] or not runtime["login_password"]):
        raise ValueError(
            f"{normalized_env} context runtime requires "
            f"LOGIN_ACCOUNT_{env_key}/LOGIN_PASSWORD_{env_key}"
            " (or the legacy generic LOGIN_ACCOUNT/LOGIN_PASSWORD fallback)"
        )
    return runtime
