"""领域错误。HTTP 层据此映射状态码。"""


class DomainError(Exception):
    status_code = 400

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.message = message
        if status_code is not None:
            self.status_code = status_code


class NotFound(DomainError):
    status_code = 404


class BadRequest(DomainError):
    status_code = 400


class Conflict(DomainError):
    status_code = 409


class InvalidState(DomainError):
    status_code = 409


class NotAuthorized(DomainError):
    status_code = 403
