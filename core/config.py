"""프로젝트 공통 설정. 값은 환경변수 → .env 순으로 읽는다(이미 있는 환경변수가 우선).

Windows 테스트: 프로젝트 루트의 .env
Ubuntu 배포  : systemd EnvironmentFile=/etc/asanbis.env (.env 파일 없이)
"""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]          # 프로젝트 루트 (core/의 상위)
DATA_DIR = ROOT / "data"
SNAPSHOT_DIR = DATA_DIR / "snapshots"

load_dotenv(ROOT / ".env")


def db_params() -> dict:
    """pymysql.connect(**db_params()) 형태로 쓴다."""
    params = dict(
        host=os.getenv("DB_HOST", "127.0.0.1"),
        port=int(os.getenv("DB_PORT", "3306")),
        user=os.environ["DB_USER"],
        password=os.getenv("DB_PASSWORD", ""),
        database=os.getenv("DB_NAME", "asan_bis"),
        charset="utf8mb4",
        autocommit=False,
    )
    if os.getenv("DB_SOCKET"):                      # 리눅스 소켓 접속 시에만
        params["unix_socket"] = os.environ["DB_SOCKET"]
    return params
