from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, DeclarativeBase

from app.config import settings

_IS_SQLITE = settings.DATABASE_URL.startswith("sqlite")

connect_args = {}
if _IS_SQLITE:
    # timeout: sqlite3 的 busy 等待上限（秒），与下面 PRAGMA busy_timeout 一致
    connect_args = {"check_same_thread": False, "timeout": 15}

engine = create_engine(
    settings.DATABASE_URL,
    echo=False,
    connect_args=connect_args,
    pool_pre_ping=True,
    pool_recycle=1800,
    # 连接池容量：SQLite 默认只有 pool_size=5 / overflow=10，而前端一个页面首屏会
    # 并发打十几个接口（监控页还会轮询 7 个），很容易把 5 条连接占满，
    # 后续请求要排队到 pool_timeout(30s) 才拿到连接 —— 表现为“接口莫名卡几十秒”。
    # SQLite 是本地文件 + WAL，多开连接几乎没有代价。
    pool_size=20,
    max_overflow=20,
    pool_timeout=10,
)

if _IS_SQLITE:
    @event.listens_for(engine, "connect")
    def _sqlite_on_connect(dbapi_conn, _record):  # type: ignore[no-untyped-def]
        """给每个新连接开 WAL。

        为什么必须做：SQLite 默认的 rollback journal 模式下，写事务会阻塞所有读。
        本体页面首屏会并发打十来个接口（浏览器还会轮询 dashboard/monitor），
        在 journal 模式下表现为「database is locked」或几秒到几十秒的莫名等待 ——
        看起来就像“页面很慢/登录失败”。

        WAL 允许「多读 + 单写」并发；synchronous=NORMAL 在 WAL 下依然安全，
        但省掉每次提交的 fsync；busy_timeout 把锁冲突退化成短暂重试。
        """
        cur = dbapi_conn.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA busy_timeout=15000")
            # SQLite 默认把排序/大事务的溢出数据写到临时文件；本机环境里打开
            # 临时文件会失败，报 "unable to open database file"（哪怕主库完全正常）。
            # 实测：object_index 90k 行做 ORDER BY 必然触发。
            # 强制用内存做临时表即可根治。
            cur.execute("PRAGMA temp_store=MEMORY")
            # 页缓存从默认 2MB 提到 64MB，减少大表读写时的换入换出
            cur.execute("PRAGMA cache_size=-64000")
        finally:
            cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
