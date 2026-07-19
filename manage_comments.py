import argparse
import os
from pathlib import Path

from comments import delete_comment, moderation_rows, set_comment_status
from scrape_chp_traffic import connect_database


def database_args(parser):
    parser.add_argument("--database", default=os.environ.get("DATABASE", "chp_traffic.sqlite"))
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))


def connect_from_args(args):
    return connect_database(Path(args.database), args.database_url)


def cmd_list(args):
    conn = connect_from_args(args)
    try:
        rows = moderation_rows(conn, args.status, args.limit)
    finally:
        conn.close()
    if not rows:
        print(f"No {args.status} comments.")
        return
    for row in rows:
        name = row.get("display_name") or "Anonymous"
        contact = f" contact={row['contact']}" if row.get("contact") else ""
        print(f"#{row['id']} {row['created_at']} {row['status']} {row['event_key']} {name}{contact}")
        print(f"  {row['body']}")


def cmd_approve(args):
    update_status(args, "approved")


def cmd_reject(args):
    update_status(args, "rejected")


def update_status(args, status):
    conn = connect_from_args(args)
    try:
        set_comment_status(conn, args.id, status)
        conn.commit()
    finally:
        conn.close()
    print(f"Comment #{args.id} marked {status}.")


def cmd_delete(args):
    conn = connect_from_args(args)
    try:
        delete_comment(conn, args.id)
        conn.commit()
    finally:
        conn.close()
    print(f"Comment #{args.id} deleted.")


def build_parser():
    parser = argparse.ArgumentParser(description="Moderate Crestmap incident comments.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List comments by status.")
    database_args(list_parser)
    list_parser.add_argument("--status", default="pending", choices=["pending", "approved", "rejected"])
    list_parser.add_argument("--limit", default=50, type=int)
    list_parser.set_defaults(func=cmd_list)

    approve_parser = subparsers.add_parser("approve", help="Approve a pending comment.")
    database_args(approve_parser)
    approve_parser.add_argument("id", type=int)
    approve_parser.set_defaults(func=cmd_approve)

    reject_parser = subparsers.add_parser("reject", help="Reject a pending comment.")
    database_args(reject_parser)
    reject_parser.add_argument("id", type=int)
    reject_parser.set_defaults(func=cmd_reject)

    delete_parser = subparsers.add_parser("delete", help="Delete a comment.")
    database_args(delete_parser)
    delete_parser.add_argument("id", type=int)
    delete_parser.set_defaults(func=cmd_delete)

    return parser


def main():
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
