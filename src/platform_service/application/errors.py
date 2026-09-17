"""Expected application-layer failures shared by transport adapters."""


class NotFound(Exception):
    pass


class Forbidden(Exception):
    pass


class Conflict(Exception):
    pass
