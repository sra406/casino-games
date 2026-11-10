"""MySQL persistence for accounts, credits, and future role permissions."""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager

import mysql.connector
from mysql.connector import pooling
from werkzeug.security import check_password_hash, generate_password_hash


_pool = None
_pool_lock = threading.Lock()


def _connection_pool():
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                required = ("MYSQL_DATABASE", "MYSQL_USER", "MYSQL_PASSWORD")
                missing = [key for key in required if not os.environ.get(key)]
                if missing:
                    raise RuntimeError("Set MySQL settings before starting: " + ", ".join(missing))
                _pool = pooling.MySQLConnectionPool(
                    pool_name="blackjack_pool",
                    pool_size=10,
                    host="localhost",
                    port=int(os.environ.get("MYSQL_PORT", "3306")),
                    user=os.environ["MYSQL_USER"],
                    password=os.environ["MYSQL_PASSWORD"],
                    database=os.environ["MYSQL_DATABASE"],
                    autocommit=False,
                    connection_timeout=8,
                )
    return _pool


@contextmanager
def _transaction(dictionary: bool = False):
    """Yield a cursor; commit if the block succeeds, roll back if it raises."""
    conn = _connection_pool().get_connection()
    try:
        yield conn.cursor(dictionary=dictionary)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


ACCOUNT_COLUMNS = "id, username, credits, preferred_bet, account_type"


def init_db() -> None:
    """Create the application schema and seed built-in roles/permissions."""
    statements = [
        """CREATE TABLE IF NOT EXISTS accounts (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            username VARCHAR(20) NOT NULL,
            password_hash VARCHAR(255) NULL,
            account_type ENUM('permanent', 'temporary') NOT NULL DEFAULT 'permanent',
            credits BIGINT NOT NULL DEFAULT 500,
            preferred_bet INT NOT NULL DEFAULT 10,
            profile_image MEDIUMBLOB NULL,
            profile_image_mime VARCHAR(32) NULL,
            profile_image_version BIGINT UNSIGNED NOT NULL DEFAULT 0,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uq_accounts_username (username)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
        """CREATE TABLE IF NOT EXISTS roles (
            id SMALLINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            role_key VARCHAR(40) NOT NULL UNIQUE,
            description VARCHAR(160) NOT NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
        """CREATE TABLE IF NOT EXISTS permissions (
            id SMALLINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            permission_key VARCHAR(80) NOT NULL UNIQUE,
            description VARCHAR(160) NOT NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
        """CREATE TABLE IF NOT EXISTS account_roles (
            account_id BIGINT UNSIGNED NOT NULL,
            role_id SMALLINT UNSIGNED NOT NULL,
            granted_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (account_id, role_id),
            CONSTRAINT fk_account_roles_account FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE,
            CONSTRAINT fk_account_roles_role FOREIGN KEY (role_id) REFERENCES roles(id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
        """CREATE TABLE IF NOT EXISTS role_permissions (
            role_id SMALLINT UNSIGNED NOT NULL,
            permission_id SMALLINT UNSIGNED NOT NULL,
            PRIMARY KEY (role_id, permission_id),
            CONSTRAINT fk_role_permissions_role FOREIGN KEY (role_id) REFERENCES roles(id) ON DELETE CASCADE,
            CONSTRAINT fk_role_permissions_permission FOREIGN KEY (permission_id) REFERENCES permissions(id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
        """CREATE TABLE IF NOT EXISTS active_round_wagers (
            round_id CHAR(32) NOT NULL,
            account_id BIGINT UNSIGNED NOT NULL,
            bet INT NOT NULL,
            PRIMARY KEY (round_id, account_id),
            CONSTRAINT fk_active_wagers_account FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
    ]
    with _transaction() as cur:
        for statement in statements:
            cur.execute(statement)
        cur.execute(
            "SELECT COLUMN_NAME, IS_NULLABLE FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'accounts' "
            "AND COLUMN_NAME IN ('password_hash', 'account_type', 'profile_image', "
            "'profile_image_mime', 'profile_image_version')"
        )
        columns = {row[0]: row[1] for row in cur.fetchall()}
        if "account_type" not in columns:
            cur.execute(
                "ALTER TABLE accounts ADD COLUMN account_type "
                "ENUM('permanent', 'temporary') NOT NULL DEFAULT 'permanent' AFTER password_hash"
            )
        if columns.get("password_hash") == "NO":
            cur.execute("ALTER TABLE accounts MODIFY password_hash VARCHAR(255) NULL")
        if "profile_image" not in columns:
            cur.execute("ALTER TABLE accounts ADD COLUMN profile_image MEDIUMBLOB NULL")
        if "profile_image_mime" not in columns:
            cur.execute("ALTER TABLE accounts ADD COLUMN profile_image_mime VARCHAR(32) NULL AFTER profile_image")
        if "profile_image_version" not in columns:
            cur.execute(
                "ALTER TABLE accounts ADD COLUMN profile_image_version "
                "BIGINT UNSIGNED NOT NULL DEFAULT 0 AFTER profile_image_mime"
            )
        for key, description in (
            ("player", "Standard blackjack player"),
            ("admin", "Future administrative access"),
            ("debug", "Future diagnostic access"),
        ):
            cur.execute(
                "INSERT INTO roles (role_key, description) VALUES (%s, %s) "
                "ON DUPLICATE KEY UPDATE description = VALUES(description)",
                (key, description),
            )
        for key, description in (
            ("admin.access", "Access future administrative tools"),
            ("debug.access", "Access future diagnostic tools"),
        ):
            cur.execute(
                "INSERT INTO permissions (permission_key, description) VALUES (%s, %s) "
                "ON DUPLICATE KEY UPDATE description = VALUES(description)",
                (key, description),
            )
        cur.execute(
            "INSERT IGNORE INTO role_permissions (role_id, permission_id) "
            "SELECT r.id, p.id FROM roles r JOIN permissions p "
            "WHERE (r.role_key = 'admin' AND p.permission_key IN ('admin.access', 'debug.access')) "
            "OR (r.role_key = 'debug' AND p.permission_key = 'debug.access')"
        )


def _insert_account(username: str, password_hash: str | None, account_type: str) -> dict | None:
    """Insert an account with the standard player role; None if the username is taken."""
    try:
        with _transaction(dictionary=True) as cur:
            cur.execute(
                "INSERT INTO accounts (username, password_hash, account_type) VALUES (%s, %s, %s)",
                (username, password_hash, account_type),
            )
            account_id = cur.lastrowid
            cur.execute(
                "INSERT INTO account_roles (account_id, role_id) "
                "SELECT %s, id FROM roles WHERE role_key = 'player'",
                (account_id,),
            )
    except mysql.connector.IntegrityError:
        return None
    return get_account_by_id(account_id)


def create_account(username: str, password: str) -> dict | None:
    """Create a permanent account with the standard player role and return its row."""
    return _insert_account(username, generate_password_hash(password), "permanent")


def create_temporary_account(username: str) -> dict | None:
    """Create a username-only account that is removed when the session ends."""
    return _insert_account(username, None, "temporary")


def authenticate(username: str, password: str) -> dict | None:
    with _transaction(dictionary=True) as cur:
        cur.execute(
            f"SELECT {ACCOUNT_COLUMNS}, password_hash "
            "FROM accounts WHERE username = %s AND account_type = 'permanent'",
            (username,),
        )
        row = cur.fetchone()
    if not row or not check_password_hash(row.pop("password_hash"), password):
        return None
    return row


def get_account_by_id(account_id: int) -> dict | None:
    with _transaction(dictionary=True) as cur:
        cur.execute(f"SELECT {ACCOUNT_COLUMNS} FROM accounts WHERE id = %s", (account_id,))
        return cur.fetchone()


def get_account_by_username(username: str) -> dict | None:
    with _transaction(dictionary=True) as cur:
        cur.execute(f"SELECT {ACCOUNT_COLUMNS} FROM accounts WHERE username = %s", (username,))
        return cur.fetchone()


def get_profile_image_version(account_id: int) -> int:
    with _transaction() as cur:
        cur.execute("SELECT profile_image_version FROM accounts WHERE id = %s", (account_id,))
        row = cur.fetchone()
    return int(row[0]) if row else 0


def get_profile_image(account_id: int) -> tuple[bytes, str] | None:
    with _transaction() as cur:
        cur.execute(
            "SELECT profile_image, profile_image_mime FROM accounts WHERE id = %s",
            (account_id,),
        )
        row = cur.fetchone()
    if not row or row[0] is None or row[1] is None:
        return None
    return bytes(row[0]), str(row[1])


def save_profile_image(account_id: int, image: bytes, mime_type: str) -> int:
    with _transaction() as cur:
        cur.execute(
            "UPDATE accounts SET profile_image = %s, profile_image_mime = %s, "
            "profile_image_version = profile_image_version + 1 WHERE id = %s",
            (image, mime_type, account_id),
        )
        if cur.rowcount != 1:
            raise ValueError("Account not found")
        cur.execute("SELECT profile_image_version FROM accounts WHERE id = %s", (account_id,))
        return int(cur.fetchone()[0])


def bootstrap_admin_account(username: str, password: str) -> dict:
    """Create the configured admin if needed, then grant its admin role."""
    account = get_account_by_username(username) or create_account(username, password) or get_account_by_username(username)
    if account is None:
        raise RuntimeError("Could not create or find the configured admin account")
    with _transaction() as cur:
        cur.execute(
            "UPDATE accounts SET password_hash = %s WHERE id = %s",
            (generate_password_hash(password), account["id"]),
        )
        cur.execute(
            "INSERT IGNORE INTO account_roles (account_id, role_id) "
            "SELECT %s, id FROM roles WHERE role_key = 'admin'",
            (account["id"],),
        )
    return account


def delete_temporary_account(account_id: int) -> None:
    with _transaction() as cur:
        cur.execute("DELETE FROM accounts WHERE id = %s AND account_type = 'temporary'", (account_id,))


def cleanup_temporary_accounts() -> int:
    """Delete every temporary account; related roles and pending wagers cascade."""
    with _transaction() as cur:
        cur.execute("DELETE FROM accounts WHERE account_type = 'temporary'")
        return cur.rowcount


def update_bet(account_id: int, bet: int) -> None:
    with _transaction() as cur:
        cur.execute("UPDATE accounts SET preferred_bet = %s WHERE id = %s", (bet, account_id))


def _debit(cur, account_id: int, amount: int, error: str) -> None:
    cur.execute(
        "UPDATE accounts SET credits = credits - %s WHERE id = %s AND credits >= %s",
        (amount, account_id, amount),
    )
    if cur.rowcount != 1:
        raise ValueError(error)


def _balance(cur, account_id: int) -> int:
    cur.execute("SELECT credits FROM accounts WHERE id = %s", (account_id,))
    return cur.fetchone()["credits"]


def debit_round_bets(round_id: str, account_bets: list[tuple[int, int]]) -> dict[int, int]:
    """Debit all participating wagers in one transaction or debit none."""
    balances = {}
    with _transaction(dictionary=True) as cur:
        for account_id, bet in account_bets:
            _debit(cur, account_id, bet, "A player has insufficient credits")
            balances[account_id] = _balance(cur, account_id)
            # A table whose bets carry between plays (craps) reuses its round id.
            cur.execute(
                "INSERT INTO active_round_wagers (round_id, account_id, bet) VALUES (%s, %s, %s) "
                "ON DUPLICATE KEY UPDATE bet = bet + VALUES(bet)",
                (round_id, account_id, bet),
            )
    return balances


def add_round_wager(round_id: str, account_id: int, amount: int) -> int:
    """Debit a split/double wager and add it to the round's restart-refund record."""
    with _transaction(dictionary=True) as cur:
        _debit(cur, account_id, amount, "Insufficient credits")
        cur.execute(
            "INSERT INTO active_round_wagers (round_id, account_id, bet) VALUES (%s, %s, %s) "
            "ON DUPLICATE KEY UPDATE bet = bet + VALUES(bet)",
            (round_id, account_id, amount),
        )
        return _balance(cur, account_id)


def settle_round_payouts(
    round_id: str, account_payouts: list[tuple[int, int]], carried: list[tuple[int, int]] = (),
) -> dict[int, int]:
    """Credit round winnings atomically and return each resulting balance.

    `carried` lists wagers still on the table afterwards (craps bets that stay up);
    they stay recorded for a restart refund under the same round id.
    """
    balances = {}
    with _transaction(dictionary=True) as cur:
        for account_id, payout in account_payouts:
            if payout <= 0:
                continue
            cur.execute("UPDATE accounts SET credits = credits + %s WHERE id = %s", (payout, account_id))
            if cur.rowcount != 1:
                raise ValueError("Account is missing during round settlement")
            balances[account_id] = _balance(cur, account_id)
        cur.execute("DELETE FROM active_round_wagers WHERE round_id = %s", (round_id,))
        for account_id, amount in carried:
            if amount > 0:
                cur.execute(
                    "INSERT INTO active_round_wagers (round_id, account_id, bet) VALUES (%s, %s, %s)",
                    (round_id, account_id, amount),
                )
    return balances


def return_round_wager(round_id: str, account_id: int, refund: int, forfeit: int = 0) -> int:
    """Take wagers off the table: credit `refund` back, drop `forfeit` as lost; returns the balance."""
    with _transaction(dictionary=True) as cur:
        if refund > 0:
            cur.execute("UPDATE accounts SET credits = credits + %s WHERE id = %s", (refund, account_id))
        cur.execute(
            "UPDATE active_round_wagers SET bet = bet - %s WHERE round_id = %s AND account_id = %s",
            (refund + forfeit, round_id, account_id),
        )
        cur.execute(
            "DELETE FROM active_round_wagers WHERE round_id = %s AND account_id = %s AND bet <= 0",
            (round_id, account_id),
        )
        return _balance(cur, account_id)


def set_credits(username: str, credits: int) -> dict | None:
    """Set an account's balance to an exact amount and return its current row."""
    with _transaction() as cur:
        cur.execute("UPDATE accounts SET credits = %s WHERE username = %s", (credits, username))
    return get_account_by_username(username)


def refund_incomplete_rounds() -> int:
    """Refund wagers left pending by a server exit before the round settled."""
    with _transaction() as cur:
        cur.execute(
            "UPDATE accounts a JOIN ("
            "SELECT account_id, SUM(bet) AS refund FROM active_round_wagers GROUP BY account_id"
            ") w ON w.account_id = a.id SET a.credits = a.credits + w.refund"
        )
        refunded_accounts = cur.rowcount
        cur.execute("DELETE FROM active_round_wagers")
        return refunded_accounts


def has_permission(account_id: int, permission_key: str) -> bool:
    """Check whether any of the account's roles grants permission_key."""
    with _transaction() as cur:
        cur.execute(
            "SELECT 1 FROM account_roles ar "
            "JOIN role_permissions rp ON rp.role_id = ar.role_id "
            "JOIN permissions p ON p.id = rp.permission_id "
            "WHERE ar.account_id = %s AND p.permission_key = %s LIMIT 1",
            (account_id, permission_key),
        )
        return cur.fetchone() is not None
