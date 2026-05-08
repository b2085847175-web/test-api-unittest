import os
from typing import Any, Dict

import yaml
from config.project_env import get_explicit_api_base_url, reload_project_env, resolve_effective_env


reload_project_env()


class Settings:
    """只服务 AI 登录与对话接口的最小配置加载器。"""

    def __init__(self) -> None:
        self._config: Dict[str, Any] = {}
        self.load_config()

    def load_config(self) -> None:
        reload_project_env()
        env = resolve_effective_env(os.getenv("ENV", "dev"))
        config_path = os.path.join(os.path.dirname(__file__), "env.yaml")

        with open(config_path, "r", encoding="utf-8") as file:
            all_configs = yaml.safe_load(file) or {}

        env_config = all_configs.get(env, {})
        common_config = all_configs.get("common", {})
        merged = {**common_config, **env_config}
        self._config = self._resolve_placeholders(merged)
        explicit_api_base_url = get_explicit_api_base_url(env)
        if explicit_api_base_url:
            self._config["api_base_url"] = explicit_api_base_url
        env_key = env.upper()
        chat_defaults = self._config.get("chat_defaults", {})
        self._config["login_account"] = self._first_nonempty(
            self._config.get("login_account"),
            os.getenv(f"LOGIN_ACCOUNT_{env_key}"),
            os.getenv(f"CONTEXT_{env_key}_LOGIN_ACCOUNT"),
            os.getenv("LOGIN_ACCOUNT"),
        )
        self._config["login_password"] = self._first_nonempty(
            self._config.get("login_password"),
            os.getenv(f"LOGIN_PASSWORD_{env_key}"),
            os.getenv(f"CONTEXT_{env_key}_LOGIN_PASSWORD"),
            os.getenv("LOGIN_PASSWORD"),
        )
        self._config["chat_account"] = self._first_nonempty(
            self._config.get("chat_account"),
            os.getenv(f"CHAT_ACCOUNT_{env_key}"),
            os.getenv(f"CONTEXT_{env_key}_CHAT_ACCOUNT"),
            os.getenv("CHAT_ACCOUNT"),
            chat_defaults.get("account"),
            "测试专用1",
        )
        shop_id = self._first_nonempty(
            os.getenv(f"CHAT_SHOP_ID_{env_key}"),
            os.getenv(f"CONTEXT_{env_key}_SHOP_ID"),
        )
        self._config["shop_id"] = shop_id
        self._config["shop_name"] = self._first_nonempty(
            os.getenv(f"CHAT_SHOP_NAME_{env_key}"),
            os.getenv(f"CONTEXT_{env_key}_SHOP_NAME"),
            f"shop_{shop_id}" if shop_id else "",
        )
        self._config["access_token"] = self._first_nonempty(
            self._config.get("access_token"),
            os.getenv(f"ACCESS_TOKEN_{env_key}"),
            os.getenv("CONTEXT_CONSOLE_ACCESS_TOKEN") if env == "console" else "",
            os.getenv("CONTEXT_PROD_ACCESS_TOKEN") if env == "console" else "",
        )
        self._config["env"] = env

    def _resolve_placeholders(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {key: self._resolve_placeholders(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._resolve_placeholders(item) for item in value]
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

    def get(self, key: str, default: Any = None) -> Any:
        return self._config.get(key, default)

    def get_api_base_url(self) -> str:
        return str(self._config.get("api_base_url", "")).rstrip("/")

    def get_headers(self) -> Dict[str, str]:
        return dict(self._config.get("headers", {}))

    def get_timeout(self) -> int:
        return int(self._config.get("timeout", 30))

    def get_retry_count(self) -> int:
        return int(self._config.get("retry_count", 3))

    def get_retry_interval(self) -> int:
        return int(self._config.get("retry_interval", 1))

    def get_current_env(self) -> str:
        return str(self._config.get("env", "dev"))

    def get_login_account(self) -> str:
        return str(self._config.get("login_account", ""))

    def get_login_password(self) -> str:
        return str(self._config.get("login_password", ""))

    def get_chat_account(self) -> str:
        return str(self._config.get("chat_account", "测试专用1"))

    def get_shop_id(self) -> str:
        return str(self._config.get("shop_id", ""))

    def get_chat_platform(self) -> str:
        chat_defaults = self._config.get("chat_defaults", {})
        return str(chat_defaults.get("platform", "tmall"))

    def get_chat_is_test(self) -> bool:
        chat_defaults = self._config.get("chat_defaults", {})
        if self.get_current_env() == "console":
            return False
        return self._to_bool(chat_defaults.get("is_test"), True)

    def get_shop_name(self) -> str:
        configured_name = str(self._config.get("shop_name", "")).strip()
        if configured_name:
            return configured_name
        normalized_shop_id = self.get_shop_id().strip()
        if normalized_shop_id:
            return f"shop_{normalized_shop_id}"
        return ""

    @staticmethod
    def _first_nonempty(*values: Any) -> str:
        for value in values:
            normalized = str(value or "").strip()
            if normalized:
                return normalized
        return ""

    @staticmethod
    def _to_bool(value: Any, default: bool) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "y", "on"}
        return bool(value)


settings = Settings()
