# issue_type: security
# severity: high
# description: SQL query built with string concatenation — use parameterized queries
# validate: '+ username' not in fixed_code and ('?' in fixed_code or '%s' in fixed_code or 'params' in fixed_code)

import sqlite3


def get_user_by_name(conn: sqlite3.Connection, username: str) -> dict:
    query = "SELECT * FROM users WHERE username = '" + username + "'"
    cursor = conn.execute(query)
    row = cursor.fetchone()
    return dict(row) if row else {}


def delete_user(conn: sqlite3.Connection, user_id: str) -> None:
    query = "DELETE FROM users WHERE id = " + user_id
    conn.execute(query)
    conn.commit()


def search_products(conn: sqlite3.Connection, keyword: str) -> list:
    query = "SELECT * FROM products WHERE name LIKE '%" + keyword + "%'"
    return conn.execute(query).fetchall()
