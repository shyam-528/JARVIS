#!/usr/bin/env python3
"""
CLI Todo Application with SQLite storage.
A simple, production-quality task manager.
"""

import argparse
import sys
from pathlib import Path

from todo_app.database import Database
from todo_app.models import Task, TaskStatus
from todo_app.service import TodoService


def create_parser() -> argparse.ArgumentParser:
    """Create the argument parser with all subcommands."""
    parser = argparse.ArgumentParser(
        prog="todo",
        description="A simple CLI todo application with SQLite storage",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  todo add "Buy groceries"
  todo add "Finish report" --priority high
  todo list
  todo list --status pending
  todo complete 1
  todo delete 2
  todo show 1
        """,
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=Path.home() / ".todo.db",
        help="Path to SQLite database file (default: ~/.todo.db)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose output")

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Add command
    add_parser = subparsers.add_parser("add", help="Add a new task")
    add_parser.add_argument("title", help="Task title")
    add_parser.add_argument("-d", "--description", default="", help="Task description")
    add_parser.add_argument(
        "-p",
        "--priority",
        choices=["low", "medium", "high"],
        default="medium",
        help="Task priority (default: medium)",
    )

    # List command
    list_parser = subparsers.add_parser("list", help="List tasks")
    list_parser.add_argument(
        "-s",
        "--status",
        choices=["pending", "completed", "all"],
        default="pending",
        help="Filter by status (default: pending)",
    )
    list_parser.add_argument(
        "--sort",
        choices=["created", "priority", "title"],
        default="created",
        help="Sort by field (default: created)",
    )

    # Complete command
    complete_parser = subparsers.add_parser("complete", help="Mark task as completed")
    complete_parser.add_argument("id", type=int, help="Task ID")

    # Uncomplete command
    uncomplete_parser = subparsers.add_parser("uncomplete", help="Mark task as pending")
    uncomplete_parser.add_argument("id", type=int, help="Task ID")

    # Delete command
    delete_parser = subparsers.add_parser("delete", help="Delete a task")
    delete_parser.add_argument("id", type=int, help="Task ID")

    # Show command
    show_parser = subparsers.add_parser("show", help="Show task details")
    show_parser.add_argument("id", type=int, help="Task ID")

    # Stats command
    subparsers.add_parser("stats", help="Show statistics")

    return parser


def format_task(task: Task, verbose: bool = False) -> str:
    """Format a task for display."""
    status_icon = "✓" if task.status == TaskStatus.COMPLETED else "○"
    priority_icon = {"low": "▼", "medium": "●", "high": "▲"}[task.priority]

    lines = [
        f"  {status_icon} [{task.id}] {priority_icon} {task.title}",
    ]

    if verbose or task.description:
        lines.append(f"      Description: {task.description or '(none)'}")

    if verbose:
        try:
            created_str = (
                task.created_at.strftime("%Y-%m-%d %H:%M")
                if hasattr(task.created_at, "strftime")
                else str(task.created_at or "(unknown)")
            )
        except Exception:
            created_str = str(task.created_at)
        lines.append(f"      Created: {created_str}")
        if task.completed_at:
            try:
                comp_str = (
                    task.completed_at.strftime("%Y-%m-%d %H:%M")
                    if hasattr(task.completed_at, "strftime")
                    else str(task.completed_at)
                )
            except Exception:
                comp_str = str(task.completed_at)
            lines.append(f"      Completed: {comp_str}")

    return "\n".join(lines)


def cmd_add(service: TodoService, args: argparse.Namespace) -> int:
    """Add a new task."""
    task = service.add_task(args.title, args.description, args.priority)
    print(f"Added task #{task.id}: {task.title}")
    return 0


def cmd_list(service: TodoService, args: argparse.Namespace) -> int:
    """List tasks."""
    status_filter = None if args.status == "all" else TaskStatus(args.status)
    tasks = service.list_tasks(status_filter, args.sort)

    if not tasks:
        print("No tasks found.")
        return 0

    print(f"{'ID':>4}  {'Status':>10}  {'Priority':>8}  Title")
    print("-" * 60)
    for task in tasks:
        status_str = task.status.value
        priority_str = task.priority
        title = task.title[:40] + "..." if len(task.title) > 40 else task.title
        print(f"{task.id:>4}  {status_str:>10}  {priority_str:>8}  {title}")

    return 0


def cmd_complete(service: TodoService, args: argparse.Namespace) -> int:
    """Mark task as completed."""
    if service.complete_task(args.id):
        print(f"Task #{args.id} marked as completed.")
        return 0
    else:
        print(f"Task #{args.id} not found.", file=sys.stderr)
        return 1


def cmd_uncomplete(service: TodoService, args: argparse.Namespace) -> int:
    """Mark task as pending."""
    if service.uncomplete_task(args.id):
        print(f"Task #{args.id} marked as pending.")
        return 0
    else:
        print(f"Task #{args.id} not found.", file=sys.stderr)
        return 1


def cmd_delete(service: TodoService, args: argparse.Namespace) -> int:
    """Delete a task."""
    if service.delete_task(args.id):
        print(f"Task #{args.id} deleted.")
        return 0
    else:
        print(f"Task #{args.id} not found.", file=sys.stderr)
        return 1


def cmd_show(service: TodoService, args: argparse.Namespace) -> int:
    """Show task details."""
    task = service.get_task(args.id)
    if task:
        print(format_task(task, verbose=True))
        return 0
    else:
        print(f"Task #{args.id} not found.", file=sys.stderr)
        return 1


def cmd_stats(service: TodoService, args: argparse.Namespace) -> int:
    """Show statistics."""
    stats = service.get_stats()
    print("Todo Statistics")
    print("=" * 30)
    print(f"Total tasks:     {stats['total']}")
    print(f"Completed:       {stats['completed']}")
    print(f"Pending:         {stats['pending']}")
    print(f"High priority:   {stats['high_priority']}")
    print(f"Medium priority: {stats['medium_priority']}")
    print(f"Low priority:    {stats['low_priority']}")
    return 0


COMMANDS = {
    "add": cmd_add,
    "list": cmd_list,
    "complete": cmd_complete,
    "uncomplete": cmd_uncomplete,
    "delete": cmd_delete,
    "show": cmd_show,
    "stats": cmd_stats,
}


def main() -> int:
    """Main entry point."""
    parser = create_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 0

    # Initialize database and service
    db = Database(args.db)
    service = TodoService(db)

    try:
        handler = COMMANDS[args.command]
        return handler(service, args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except Exception as e:
        if args.verbose:
            raise
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
