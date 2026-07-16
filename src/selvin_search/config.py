"""selvin-search-mcp runtime configuration.

Env-var prefix: SELVIN_*  (e.g. SELVIN_API_KEY, SELVIN_MODEL).

Provider modes (SELVIN_PROVIDER):
  - zhipu (default): base_url=https://open.bigmodel.cn/api/paas/v4
  - custom:          user supplies SELVIN_API_URL & SELVIN_MODEL themselves

Search modes (SELVIN_SEARCH_MODE):
  - parallel (default):     call provider search API and online-capable model in parallel
  - api:                    call provider search API first, then summarize
  - model_online:           ask an online-capable chat model to search directly
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional


_PROVIDER_DEFAULTS: dict[str, dict[str, str]] = {
    "zhipu": {
        "api_url": "https://open.bigmodel.cn/api/paas/v4",
    },
    "custom": {
        "api_url": "",
    },
}

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DOTENV_PATHS = (_PROJECT_ROOT / ".env", Path.cwd() / ".env")
_DOTENV_CACHE: Optional[dict[str, str]] = None


def _load_dotenv() -> dict[str, str]:
    global _DOTENV_CACHE
    if _DOTENV_CACHE is not None:
        return _DOTENV_CACHE

    values: dict[str, str] = {}
    for path in _DOTENV_PATHS:
        if not path.exists():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                values[key] = value
    _DOTENV_CACHE = values
    return values


def _env(*names: str, default: Optional[str] = None) -> Optional[str]:
    dotenv = _load_dotenv()
    for n in names:
        v = os.getenv(n)
        if v not in (None, ""):
            return v
        v = dotenv.get(n)
        if v not in (None, ""):
            return v
    return default


def _bool(*names: str, default: bool = False) -> bool:
    v = _env(*names)
    if v is None:
        return default
    return v.lower() in ("true", "1", "yes", "on")


class Config:
    _instance = None

    _SETUP_HINT = (
        "请复制 .env.example 为 .env，并设置 SELVIN_API_KEY 或 ZHIPU_API_KEY，"
        "同时设置 SELVIN_MODEL。"
    )

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._config_file = None
            cls._instance._cached_model = None
        return cls._instance

    # ── persistent config file ─────────────────────────────────────────

    @property
    def config_file(self) -> Path:
        if self._config_file is None:
            config_dir = Path.home() / ".config" / "selvin-search"
            try:
                config_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                config_dir = Path.cwd() / ".selvin-search"
                config_dir.mkdir(parents=True, exist_ok=True)
            self._config_file = config_dir / "config.json"
        return self._config_file

    def _load_config_file(self) -> dict:
        if not self.config_file.exists():
            return {}
        try:
            with open(self.config_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}

    def _save_config_file(self, config_data: dict) -> None:
        try:
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(config_data, f, ensure_ascii=False, indent=2)
        except IOError as e:
            raise ValueError(f"无法保存配置文件: {str(e)}")

    # ── provider selection ─────────────────────────────────────────────

    @property
    def provider(self) -> str:
        v = (_env("SELVIN_PROVIDER") or "").strip().lower()
        if v in _PROVIDER_DEFAULTS:
            return v
        return "zhipu"

    @property
    def search_mode(self) -> str:
        v = (_env("SELVIN_SEARCH_MODE", default="parallel") or "parallel").strip().lower()
        if v in {"parallel", "api", "model_online"}:
            return v
        return "parallel"

    @property
    def api_url(self) -> str:
        url = _env("SELVIN_API_URL")
        if not url:
            url = _PROVIDER_DEFAULTS[self.provider]["api_url"]
        if not url:
            raise ValueError(f"API URL 未配置。\n{self._SETUP_HINT}")
        return url

    @property
    def api_key(self) -> str:
        key = _env("SELVIN_API_KEY", "ZHIPU_API_KEY")
        if not key:
            raise ValueError(f"API Key 未配置。\n{self._SETUP_HINT}")
        return key

    @property
    def online_api_url(self) -> str:
        return _env("SELVIN_ONLINE_API_URL") or self.api_url

    @property
    def online_api_key(self) -> str:
        key = _env("SELVIN_ONLINE_API_KEY")
        if key:
            return key
        return self.api_key

    def provider_extra_headers(self) -> dict[str, str]:
        return {}

    # ── debug / retry knobs ────────────────────────────────────────────

    @property
    def debug_enabled(self) -> bool:
        return _bool("SELVIN_DEBUG")

    @property
    def retry_max_attempts(self) -> int:
        return int(_env("SELVIN_RETRY_MAX_ATTEMPTS", default="3"))

    @property
    def retry_multiplier(self) -> float:
        return float(_env("SELVIN_RETRY_MULTIPLIER", default="1"))

    @property
    def retry_max_wait(self) -> int:
        return int(_env("SELVIN_RETRY_MAX_WAIT", default="10"))

    # ── logging ────────────────────────────────────────────────────────

    @property
    def log_level(self) -> str:
        return (_env("SELVIN_LOG_LEVEL", default="INFO") or "INFO").upper()

    @property
    def log_dir(self) -> Path:
        log_dir_str = _env("SELVIN_LOG_DIR", default="logs") or "logs"
        log_dir = Path(log_dir_str)
        if log_dir.is_absolute():
            return log_dir

        home_log_dir = Path.home() / ".config" / "selvin-search" / log_dir_str
        try:
            home_log_dir.mkdir(parents=True, exist_ok=True)
            return home_log_dir
        except OSError:
            pass

        cwd_log_dir = Path.cwd() / log_dir_str
        try:
            cwd_log_dir.mkdir(parents=True, exist_ok=True)
            return cwd_log_dir
        except OSError:
            pass

        tmp_log_dir = Path("/tmp") / "selvin-search" / log_dir_str
        tmp_log_dir.mkdir(parents=True, exist_ok=True)
        return tmp_log_dir

    # ── model ──────────────────────────────────────────────────────────

    def _apply_model_suffix(self, model: str) -> str:
        return model

    @property
    def zhipu_search_engine(self) -> str:
        return _env("ZHIPU_SEARCH_ENGINE", "SELVIN_SEARCH_ENGINE", default="search_pro") or "search_pro"

    @property
    def zhipu_search_count(self) -> int:
        return int(_env("ZHIPU_SEARCH_COUNT", "SELVIN_SEARCH_COUNT", default="5") or "5")

    @property
    def zhipu_content_size(self) -> str:
        return _env("ZHIPU_CONTENT_SIZE", "SELVIN_CONTENT_SIZE", default="high") or "high"

    @property
    def zhipu_recency_filter(self) -> str:
        return _env("ZHIPU_SEARCH_RECENCY_FILTER", "SELVIN_SEARCH_RECENCY_FILTER", default="noLimit") or "noLimit"

    def effective_model(self, model: str) -> str:
        """Return the model ID that should be sent to the upstream provider."""
        return self._apply_model_suffix(model)

    @property
    def model(self) -> str:
        if self._cached_model is not None:
            return self._cached_model

        model = (
            _env("SELVIN_MODEL")
            or self._load_config_file().get("model")
        )
        if not model:
            raise ValueError(f"模型未配置。\n{self._SETUP_HINT}")
        self._cached_model = self._apply_model_suffix(model)
        return self._cached_model

    def set_model(self, model: str) -> None:
        config_data = self._load_config_file()
        config_data["model"] = model
        self._save_config_file(config_data)
        self._cached_model = self._apply_model_suffix(model)

    @property
    def online_model(self) -> str:
        model = _env("SELVIN_ONLINE_MODEL")
        if model:
            return self._apply_model_suffix(model)
        return self.model

    @property
    def max_tokens(self) -> int:
        return int(_env("SELVIN_MAX_TOKENS", default="1800") or "1800")

    @property
    def rank_max_tokens(self) -> int:
        return int(_env("SELVIN_RANK_MAX_TOKENS", default="128") or "128")

    @property
    def online_source_fetch_enabled(self) -> bool:
        return _bool("SELVIN_FETCH_ONLINE_SOURCES", default=True)

    @property
    def online_source_fetch_count(self) -> int:
        return int(_env("SELVIN_FETCH_ONLINE_SOURCE_COUNT", default="5") or "5")

    @property
    def online_source_fetch_chars(self) -> int:
        return int(_env("SELVIN_FETCH_ONLINE_SOURCE_CHARS", default="3000") or "3000")

    @property
    def api_cancel_grace_seconds(self) -> float:
        return float(_env("SELVIN_API_CANCEL_GRACE_SECONDS", default="0.5") or "0.5")

    # ── reporting ──────────────────────────────────────────────────────

    @staticmethod
    def _mask_api_key(key: Optional[str]) -> str:
        if not key:
            return "未配置"
        if len(key) <= 8:
            return "***"
        return f"{key[:4]}{'*' * (len(key) - 8)}{key[-4:]}"

    def get_config_info(self) -> dict:
        try:
            api_url = self.api_url
            api_key_masked = self._mask_api_key(self.api_key)
            model = self.model
            config_status = "✅ 配置完整"
        except ValueError as e:
            api_url = "未配置"
            api_key_masked = "未配置"
            model = "未配置"
            config_status = f"❌ 配置错误: {str(e)}"

        return {
            "SELVIN_PROVIDER": self.provider,
            "SELVIN_SEARCH_MODE": self.search_mode,
            "SELVIN_API_URL": api_url,
            "SELVIN_API_KEY": api_key_masked,
            "SELVIN_MODEL": model,
            "SELVIN_ONLINE_API_URL": _env("SELVIN_ONLINE_API_URL") or "未单独配置，默认复用 SELVIN_API_URL",
            "SELVIN_ONLINE_API_KEY": self._mask_api_key(_env("SELVIN_ONLINE_API_KEY")),
            "SELVIN_ONLINE_MODEL": _env("SELVIN_ONLINE_MODEL") or "未单独配置，默认复用 SELVIN_MODEL",
            "SELVIN_MAX_TOKENS": self.max_tokens,
            "SELVIN_RANK_MAX_TOKENS": self.rank_max_tokens,
            "SELVIN_FETCH_ONLINE_SOURCES": self.online_source_fetch_enabled,
            "SELVIN_FETCH_ONLINE_SOURCE_COUNT": self.online_source_fetch_count,
            "SELVIN_FETCH_ONLINE_SOURCE_CHARS": self.online_source_fetch_chars,
            "SELVIN_API_CANCEL_GRACE_SECONDS": self.api_cancel_grace_seconds,
            "ZHIPU_SEARCH_ENGINE": self.zhipu_search_engine,
            "ZHIPU_SEARCH_COUNT": self.zhipu_search_count,
            "ZHIPU_CONTENT_SIZE": self.zhipu_content_size,
            "ZHIPU_SEARCH_RECENCY_FILTER": self.zhipu_recency_filter,
            "SELVIN_DEBUG": self.debug_enabled,
            "SELVIN_LOG_LEVEL": self.log_level,
            "SELVIN_LOG_DIR": str(self.log_dir),
            "config_status": config_status,
        }


config = Config()
