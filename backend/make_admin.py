"""Promotes an account to the institution (admin) role.

    python3 make_admin.py someone@university.edu
    python3 make_admin.py someone@university.edu --demote

Deliberately a server-side CLI rather than an HTTP endpoint. Any route
that can grant admin is a route worth attacking, and a project of this
size has no need for one: whoever can run this already has shell access to
the machine and the database, so it grants nothing they didn't have.

Every promotion is written to the audit log.
"""
import argparse
import sys

from _env import load_dotenv

load_dotenv()

import accounts
import db


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("email")
    parser.add_argument("--demote", action="store_true", help="set the account back to student")
    args = parser.parse_args()

    role = accounts.DEFAULT_ROLE if args.demote else "admin"

    db.open_pool()
    db.init_db()
    try:
        with db.get_conn() as conn:
            user = accounts.get_user_by_email(conn, args.email)
            if user is None:
                print(f"No account found for {args.email}. Register it first.", file=sys.stderr)
                return 1
            if user["role"] == role:
                print(f"{user['email']} is already '{role}' — nothing to do.")
                return 0

            conn.execute(db.q("UPDATE users SET role = ? WHERE user_id = ?"), (role, user["user_id"]))
            accounts.audit(conn, "role.changed", actor_id=user["user_id"],
                           detail=f"{user['role']} -> {role} (cli)")

            # A role change must not wait for the old token to expire.
            revoked = accounts.revoke_all_sessions(conn, user["user_id"])

        print(f"{args.email}: {user['role']} -> {role}")
        if revoked:
            print(f"  revoked {revoked} active session(s) — they'll need to sign in again")
        return 0
    finally:
        db.close_pool()


if __name__ == "__main__":
    raise SystemExit(main())
