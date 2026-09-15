class DomainError(Exception):
    def __init__(self, code: str, status: int = 400, *, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.status = status
        self.retryable = retryable
