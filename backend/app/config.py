from pathlib import Path

from pydantic_settings import BaseSettings

_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    DATABASE_URL: str = "sqlite:///./ontology.db"
    REDIS_URL: str = "redis://localhost:6379/0"
    NEO4J_URI: str = "bolt://localhost:7687"
    NEO4J_USER: str = "neo4j"
    NEO4J_PASSWORD: str = "ontology123"
    SECRET_KEY: str = "change-me-in-production"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 480
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    LLM_BASE_URL: str = "https://coding.dashscope.aliyuncs.com/v1"
    LLM_API_KEY: str = ""
    LLM_MODEL: str = "qwen3.5-plus"
    HOST: str = "0.0.0.0"
    PORT: int = 8001
    DEBUG: bool = True

    # ── 动作写回 ──
    # 数据面默认把 ALLOWED_WRITE_PURPOSES 设为空集合，任何 DML 都被拒。
    # 打开这个开关后，才允许"动作"通过 ExecuteService 写回源库
    # （仍需 Connection.writable=true 才真正生效）。
    ACTION_WRITEBACK_ENABLED: bool = False

    # ── 动作脚本（custom_script 执行器）──
    # 默认关闭。打开后脚本会在**子进程**里执行（AST 校验 + 受限 builtins +
    # 硬超时），而不是在服务进程里 exec。即使打开，也不是不可逃逸的沙箱。
    ACTION_SCRIPT_ENABLED: bool = False
    ACTION_SCRIPT_TIMEOUT_SECONDS: int = 3

    # ── 指标层（Cube）──
    # 元枢自己不实现指标口径，指标由 Cube 提供；这里只配连接信息。
    # 留空则 /api/v1/metrics/* 返回 not_configured，不影响其他功能。
    CUBE_API_URL: str = "http://127.0.0.1:4000"
    CUBE_API_SECRET: str = "unified-cube-local-dev-secret-change-me"
    CUBE_TIMEOUT_SECONDS: float = 25.0
    # 是否在启动时种入电信行业演示数据（4 个智能体 + 2 个技能）。
    # 默认关闭：这两个 seed 是"表空就补"，开着的话删掉之后一重启又回来。
    # 想恢复演示数据时设 SEED_DEMO_DATA=true。
    SEED_DEMO_DATA: bool = False
    # 对象索引的存储。留空则退回 SQLite（ontology.db 里的 object_index 表）。
    # 配成 Postgres 后：JSONB+GIN 索引、SQL 侧分页、增量 upsert。
    OBJECT_STORE_URL: str = ""
    # 单次查询最多等多久（Cube 异步，需要重发轮询）
    CUBE_QUERY_WAIT_SECONDS: float = 60.0
    # Cube 的模型目录（yml）。对象过滤条件自动同步要用它，留空则同步功能不可用。
    CUBE_MODEL_DIR: str = str(
        Path(__file__).resolve().parents[2] / "cube-unified" / "cube" / "model" / "cubes"
    )

    # ── 临时分析引擎（Auto-Ontology）──
    # 方案 B 的兜底路径：Cube 答不了的问题转给 Auto-Ontology 编译任意 SQL。
    # 留空则 /api/v1/ask 会明确报告"临时分析不可用"，不会伪造答案。
    ADHOC_ENGINE_URL: str = "http://127.0.0.1:3001"
    ADHOC_ENGINE_TIMEOUT_SECONDS: float = 120.0
    # Auto-Ontology 里注册了多个数据库时必须指定目标库，否则它的检索/编译管道
    # 会因为无法定位而报 "Input validation failed for candidate_preparation"。
    ADHOC_TARGET_DB: str = "dwh_dwd"

    # ── 对象索引定时同步 ──
    # 索引是对象实例的物化快照，源表变化后需要重建；这里控制后台自动重建。
    OBJECT_INDEX_SYNC_ENABLED: bool = True
    OBJECT_INDEX_SYNC_INTERVAL_SECONDS: int = 600       # 默认 10 分钟
    OBJECT_INDEX_SYNC_INITIAL_DELAY_SECONDS: int = 30   # 启动后先等一会儿再跑

    # 数仓直连（可选）。原默认值里写死了生产 root 密码，已清空；
    # 需要时在 .env 里配置 DWD_DATABASE_URL。
    DWD_DATABASE_URL: str = ""
    # 对象存储地址在 .env 里配置（例如 http://minio:9000）
    MINIO_ENDPOINT: str = ""
    MINIO_ACCESS_KEY: str = ""
    # 原默认值是真实密钥，已清空；在 .env 里配置 MINIO_SECRET_KEY。
    MINIO_SECRET_KEY: str = ""
    MINIO_BUCKET: str = "milvus"

    model_config = {"env_file": str(_ENV_FILE), "extra": "ignore"}


settings = Settings()
