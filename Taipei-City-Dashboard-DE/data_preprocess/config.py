import os
from pathlib import Path


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())


_load_env(Path(__file__).parent.parent.parent / "docker" / ".env")

# ── PostgreSQL (dashboard) ─────────────────────────────────────────────────────
PG_DASHBOARD = {
    "host":     "localhost",
    "port":     5433,
    "dbname":   os.getenv("DB_DASHBOARD_DBNAME", "dashboard"),
    "user":     os.getenv("DB_DASHBOARD_USER", "postgres"),
    "password": os.getenv("DB_DASHBOARD_PASSWORD", ""),
}

# ── PostgreSQL (manager) ───────────────────────────────────────────────────────
PG_MANAGER = {
    "host":     "localhost",
    "port":     5432,
    "dbname":   os.getenv("DB_MANAGER_DBNAME", "dashboardmanager"),
    "user":     os.getenv("DB_MANAGER_USER", "postgres"),
    "password": os.getenv("DB_MANAGER_PASSWORD", ""),
}

# ── 中央氣象署 Open Data ───────────────────────────────────────────────────────
CWA_API_KEY = os.getenv("CWA_API_KEY", "")

# ── 環境部 Open Data ───────────────────────────────────────────────────────────
EPA_API_KEY = os.getenv("EPA_API_KEY", "")
