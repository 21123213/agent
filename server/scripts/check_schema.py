"""快速检查云库是否具备 v2 表结构。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from app.infra.db import fetch_all, fetch_one  # noqa: E402


def main() -> None:
    tables = fetch_all("SHOW TABLES")
    names = sorted(list(t.values())[0] for t in tables)
    print("tables:", ", ".join(names))
    required = [
        "knowledge_bases",
        "file_metadata",
        "file_content",
        "document_chunks",
        "document_elements",
        "s_unit",
        "s_role",
        "users",
    ]
    for name in required:
        ok = fetch_one("SHOW TABLES LIKE %s", (name,))
        print(f"  {name}: {'OK' if ok else 'MISSING'}")

    if fetch_one("SHOW TABLES LIKE %s", ("file_metadata",)):
        cols = fetch_all("SHOW COLUMNS FROM file_metadata")
        col_names = {c["Field"] for c in cols}
        for need in ("unit_id", "knowledge_base_id", "storage_backend"):
            print(f"  file_metadata.{need}: {'OK' if need in col_names else 'MISSING'}")

    if fetch_one("SHOW TABLES LIKE %s", ("knowledge_bases",)):
        kbs = fetch_all(
            "SELECT id, name, unit_id, owner_user_id, status FROM knowledge_bases LIMIT 10"
        )
        print("knowledge_bases rows:", kbs)

    if fetch_one("SHOW TABLES LIKE %s", ("users",)):
        users = fetch_all(
            "SELECT id, username, unit_id, role_id FROM users LIMIT 5"
        )
        print("users sample:", users)


if __name__ == "__main__":
    main()
