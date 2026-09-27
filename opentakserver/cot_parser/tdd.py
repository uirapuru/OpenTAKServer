"""Helpers for t-x-d-d (EUD disconnect / map object delete) events.

ATAK reuses the same CoT type, ``t-x-d-d``, for two unrelated situations:
the user logging out (their own EUD disconnecting) and the user deleting a
map object they placed (a marker, route, etc). Both arrive as a bare
``t-x-d-d`` event whose only distinguishing feature is the first ``<link>``
inside ``<detail>``: on a self logout it points at the sender's own uid, on
an object delete it points at the deleted object's uid instead.

This module is intentionally dependency-light (bs4 only) so it can be unit
tested without booting the full OpenTAKServer stack (Flask app, database,
RabbitMQ, ...).
"""


def is_self_delete(event, sender_uid):
    """Return True only when a t-x-d-d event is the sender disconnecting itself.

    ``event`` is the BeautifulSoup ``<event>`` tag already parsed by the
    caller. Anything that isn't unambiguously a self logout -- no
    ``<detail>``, no ``<link>`` inside it, or a ``<link>`` without a
    ``uid`` -- returns False rather than assuming it is a self delete.
    """
    detail = event.find("detail")
    if detail is None:
        return False

    link = detail.find("link")
    if link is None:
        return False

    return link.attrs.get("uid") == sender_uid
