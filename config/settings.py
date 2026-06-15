import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required env var: {name}. Check your .env file.")
    return value


def _optional(name: str, default: str) -> str:
    return os.getenv(name, default).strip() or default


@dataclass
class Settings:
    FEISHU_APP_ID: str = field(default_factory=lambda: _require("FEISHU_APP_ID"))
    FEISHU_APP_SECRET: str = field(default_factory=lambda: _require("FEISHU_APP_SECRET"))
    FEISHU_ENCRYPT_KEY: str = field(default_factory=lambda: os.getenv("FEISHU_ENCRYPT_KEY", ""))
    FEISHU_VERIFY_TOKEN: str = field(default_factory=lambda: os.getenv("FEISHU_VERIFY_TOKEN", ""))

    MINIMAX_API_KEY: str = field(default_factory=lambda: _require("MINIMAX_API_KEY"))
    MINIMAX_BASE_URL: str = field(default_factory=lambda: _optional("MINIMAX_BASE_URL", "https://api.minimax.chat/v1"))
    MINIMAX_MODEL: str = field(default_factory=lambda: _optional("MINIMAX_MODEL", "MiniMax-Text-01"))

    AGENT_MAX_ITERATIONS: int = field(default_factory=lambda: int(os.getenv("AGENT_MAX_ITERATIONS", "30")))
    AGENT_TIMEOUT_SECONDS: int = field(default_factory=lambda: int(os.getenv("AGENT_TIMEOUT_SECONDS", "120")))
    AGENT_POOL_MAX_SIZE: int = field(default_factory=lambda: int(os.getenv("AGENT_POOL_MAX_SIZE", "100")))

    BENCH_API_BASE_URL: str = field(default_factory=lambda: _optional("BENCH_API_BASE_URL", "http://localhost:9013"))
    VLM_API_BASE_URL: str = field(default_factory=lambda: _optional("VLM_API_BASE_URL", "http://localhost:9014"))

    HTTP_PORT: int = field(default_factory=lambda: int(os.getenv("HTTP_PORT", "8088")))
    LOG_LEVEL: str = field(default_factory=lambda: _optional("LOG_LEVEL", "INFO"))

    # OCL — Output Control Layer (Phase 3)
    OCL_ADMIN_USER_IDS: str = field(default_factory=lambda: _optional("OCL_ADMIN_USER_IDS", ""))
    OCL_MAX_OUTPUT_CHARS: int = field(default_factory=lambda: int(os.getenv("OCL_MAX_OUTPUT_CHARS", "4000")))
    OCL_WARN_OUTPUT_CHARS: int = field(default_factory=lambda: int(os.getenv("OCL_WARN_OUTPUT_CHARS", "2000")))
    OCL_CONTENT_BLOCK_MESSAGE: str = field(default_factory=lambda: _optional(
        "OCL_CONTENT_BLOCK_MESSAGE", "抱歉，该内容不在我的服务范围内，请换一个问题。"
    ))

    def __repr__(self) -> str:
        return (
            f"Settings(FEISHU_APP_ID={self.FEISHU_APP_ID!r}, "
            f"MINIMAX_API_KEY=***, MINIMAX_MODEL={self.MINIMAX_MODEL!r})"
        )


settings = Settings()
