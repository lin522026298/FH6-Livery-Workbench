class WorkbenchError(RuntimeError):
    """A user-actionable validation or workflow error."""


class SafetyError(WorkbenchError):
    """A safety gate rejected a potentially destructive operation."""

