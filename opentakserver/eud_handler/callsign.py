"""Who owns a callsign right now.

A callsign has to be unique server-wide. euds.callsign carries a UNIQUE
constraint, and EudHandler names each EUD's RabbitMQ queue after it, so two
devices sharing a callsign also share the queue their direct messages arrive
on.

Kept apart from EudHandler so it can be tested against a database without
standing up a socket server.
"""

from sqlalchemy import select

from opentakserver.models.EUD import EUD


def callsign_owner(session, callsign, uid):
    """Return the EUD already holding `callsign`, or None.

    A device reconnecting under its own callsign is not a collision, so an
    EUD whose uid equals `uid` never counts as the owner. An empty callsign
    never collides either: the column is nullable and CoT from a relay may
    carry no <contact> tag at all.
    """
    if not callsign:
        return None

    row = session.execute(
        select(EUD).filter(EUD.callsign == callsign, EUD.uid != uid)
    ).first()
    return row[0] if row else None
