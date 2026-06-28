from pydantic import BaseModel


class AccessTicket(BaseModel):
    """A short-lived signed ticket for a media stream or the WebSocket (#30).

    ``ticket`` is passed back as the ``?ticket=`` query param; ``expires_in`` is
    the lifetime in seconds, after which the client must mint a fresh one.
    """

    ticket: str
    expires_in: int
