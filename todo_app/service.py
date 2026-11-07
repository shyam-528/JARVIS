"""
Service layer for the todo application.
"""

from todo_app.database import Database
from todo_app.models import Task, TaskPriority, TaskStatus


class TodoService:
    """Business logic service for todo operations."""

    def __init__(self, database: Database):
        """Initialize with a database instance."""
        self.db = database

    def add_task(self, title: str, description: str = "", priority: str = "medium") -> Task:
        """Add a new task."""
        # Validate priority
        try:
            TaskPriority(priority)
        except ValueError:
            raise ValueError(
                f"Invalid priority: {priority}. Must be low, medium, or high."
            ) from None

        # Validate title
        if not title.strip():
            raise ValueError("Task title cannot be empty.")

        return self.db.add_task(title.strip(), description.strip(), priority)

    def get_task(self, task_id: int) -> Task | None:
        """Get a task by ID."""
        return self.db.get_task(task_id)

    def list_tasks(
        self, status_filter: TaskStatus | None = None, sort_by: str = "created"
    ) -> list[Task]:
        """List tasks with optional filtering and sorting."""
        return self.db.list_tasks(status_filter, sort_by)

    def complete_task(self, task_id: int) -> bool:
        """Mark task as completed."""
        return self.db.complete_task(task_id)

    def uncomplete_task(self, task_id: int) -> bool:
        """Mark task as pending."""
        return self.db.uncomplete_task(task_id)

    def delete_task(self, task_id: int) -> bool:
        """Delete a task."""
        return self.db.delete_task(task_id)

    def get_stats(self) -> dict:
        """Get task statistics."""
        return self.db.get_stats()
