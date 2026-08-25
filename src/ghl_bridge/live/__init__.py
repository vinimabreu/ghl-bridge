"""The live side: the same port, pointed at the real platform.

Import :class:`LiveHighLevel` and hand it a transport. The default
:class:`~ghl_bridge.live.transport.RequestsTransport` needs the optional
extra (``pip install "ghl-bridge[live]"``); everything else in this
subpackage runs on the core install.
"""

from .adapter import LiveHighLevel, Transport
from .mapping import BASE_URL, VERSION_CONTACTS, VERSION_CONVERSATIONS, HttpResponse, PlannedRequest
from .transport import RequestsTransport

__all__ = [
    "BASE_URL",
    "VERSION_CONTACTS",
    "VERSION_CONVERSATIONS",
    "HttpResponse",
    "LiveHighLevel",
    "PlannedRequest",
    "RequestsTransport",
    "Transport",
]
