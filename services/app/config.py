import os
from dataclasses import dataclass, field


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    database_url: str = _env("DATABASE_URL", "postgresql://app:app@localhost:5432/platform")
    jwt_secret: str = _env("JWT_SECRET", "dev-secret")
    master_key: str = _env("MASTER_KEY", "")
    ollama_base_url: str = _env("OLLAMA_BASE_URL", "http://localhost:11434")
    sandbox_url: str = _env("SANDBOX_URL", "http://localhost:8001")
    renderer_url: str = _env("RENDERER_URL", "http://localhost:8002")
    artifact_dir: str = _env("ARTIFACT_DIR", "/data/artifacts")
    models_file: str = _env("MODELS_FILE", "/app/config/models.yaml")
    agents_dir: str = _env("AGENTS_DIR", "/app/config/agents")
    skills_dir: str = _env("SKILLS_DIR", "/app/config/skills")
    infra_dir: str = _env("INFRA_DIR", "/app/config/infra")
    elevenlabs_api_key: str = _env("ELEVENLABS_API_KEY")
    jwt_ttl_hours: int = int(_env("JWT_TTL_HOURS", "12"))
    # Guards enforced in code (design doc defaults)
    max_steps_per_task: int = int(_env("MAX_STEPS_PER_TASK", "30"))
    step_wall_time_s: int = int(_env("STEP_WALL_TIME_S", "900"))
    identical_tool_call_limit: int = int(_env("IDENTICAL_TOOL_CALL_LIMIT", "3"))
    stall_after_s: int = int(_env("STALL_AFTER_S", "600"))
    lease_s: int = int(_env("LEASE_S", "30"))
    max_step_retries: int = 2
    max_reassignments: int = 1
    router_threshold: float = float(_env("ROUTER_THRESHOLD", "0.6"))
    seed_users: list = field(default_factory=lambda: [
        ("admin", _env("SEED_ADMIN_EMAIL", "admin@memy.local"), _env("SEED_ADMIN_PASSWORD")),
        ("alice", _env("SEED_ALICE_EMAIL", "alice@memy.local"), _env("SEED_ALICE_PASSWORD")),
        ("bob", _env("SEED_BOB_EMAIL", "bob@memy.local"), _env("SEED_BOB_PASSWORD")),
    ])


settings = Settings()
