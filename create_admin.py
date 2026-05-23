#!/usr/bin/env python3
"""Create the first admin user. Run once before starting the server.

Usage:
    python3 create_admin.py <username> <password>
"""
import sys
from werkzeug.security import generate_password_hash
from db import init_db, get_db


def main():
    if len(sys.argv) != 3:
        print("Usage: python3 create_admin.py <username> <password>")
        sys.exit(1)

    username, password = sys.argv[1], sys.argv[2]
    display_name = username.replace("_", " ").title()

    init_db()
    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM users WHERE username = ?", (username,)
        ).fetchone()
        if existing:
            print(f"User '{username}' already exists.")
            sys.exit(1)
        conn.execute(
            "INSERT INTO users (username, display_name, password_hash, is_admin)"
            " VALUES (?, ?, ?, 1)",
            (username, display_name, generate_password_hash(password)),
        )
        conn.commit()

    print(f"Admin '{username}' created successfully.")


if __name__ == "__main__":
    main()
