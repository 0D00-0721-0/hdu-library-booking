"""Shared cancellation, uncertain-outcome and transport exceptions."""




class TaskCancelled(RuntimeError):
    """No further operation was sent after the cancellation request."""


class ResultUncertain(RuntimeError):
    """An operation was sent but its outcome could not be confirmed."""


class RequestFailure(RuntimeError):
    def __init__(self, message, retryable=False, outcome_unknown=None):
        super().__init__(message)
        self.retryable = bool(retryable)
        self.outcome_unknown = self.retryable if outcome_unknown is None else bool(outcome_unknown)
